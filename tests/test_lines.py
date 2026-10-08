"""Version 2: several production lines sharing PLC channels and dispatchers."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.hardware.plc import factory as plc_factory
from app.services.counting_service import counting_service
from app.services.line_config import PRIMARY_LINE_ID, SCHEMA_VERSION, normalize_line, plc_address_clashes, upgrade_state
from app.services.line_service import LineManager, SyncPairer
from app.services.plc_dispatcher_service import PLCDispatcherService, _eval_condition
from app.services.plc_failsafe_service import PLCFailsafeService
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService, backup_v1_files


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file, dispatcher and line manager."""
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(SettingsPersistenceService, "_state", state)
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    monkeypatch.setattr(PLCDispatcherService, "_cards", [])
    monkeypatch.setattr(PLCDispatcherService, "_states", {})
    monkeypatch.setattr(PLCDispatcherService, "_line_tasks", {})
    monkeypatch.setattr(PLCDispatcherService, "_endpoint_locks", {})
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    saved_config = counting_service.config
    counting_service.reset_counts()
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)
    counting_service.reset_counts()


class _CrossingTracker:
    """Stands in for WirelineTracker: every update reports one crossing of `class_name`."""

    def __init__(self, class_name="bottle", is_defect=False):
        self.class_name = class_name
        self.is_defect = is_defect
        self.objects = {}
        self._recently_counted = set()
        self.next_id = 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, self.class_name, self.is_defect, 0.9)]


def _feed(counter, camera_id, class_name="bottle", is_defect=False, frames=1):
    counter._trackers[camera_id] = _CrossingTracker(class_name, is_defect)
    for _ in range(frames):
        counter.process_frame([], 640, 480, camera_rotation=90, camera_id=camera_id)


def _products(monkeypatch, codes, list_id="list-1"):
    """The product list the readers of a test are set to: {code: product name}."""
    from app.services.product_service import product_catalog
    monkeypatch.setattr(product_catalog, "_lists", {list_id: {c: {"code": c, "name": n} for c, n in codes.items()}})
    monkeypatch.setattr(product_catalog, "_names", {list_id: "List 1"})


def _add_line(name, cameras=(), **extra):
    # A vision camera is not saved without a model; tests that care which one name it.
    cameras = [cam if cam.get("role", "vision") != "vision" else {"model_id": "model-test", **cam} for cam in cameras]
    line = SettingsPersistenceService.save_line({"name": name, "cameras": cameras, **extra})
    line_module.line_manager.apply_state()
    return line


# ── Settings upgrade ──────────────────────────────────────────────────────────

V1_STATE = {
    "version": 41,
    "active_camera_id": "cam-usb-0",
    "active_model_id": "model-A",
    "camera_auto_connect": True,
    "action_trigger": {"line1_position": 0.2, "line2_position": 0.8, "expected_classes": ["can"], "send_mqtt": True},
    "plc_actions": [{"id": "reject-gate", "name": "Reject gate", "plc_endpoint_id": "ep1", "target_address": "40001"}],
    "communication_endpoints": [],
}


def test_v1_settings_upgrade_into_line1_and_stay_readable_by_v1(monkeypatch, tmp_path):
    state_file = tmp_path / "system_state.json"
    state_file.write_text(json.dumps(V1_STATE), encoding="utf-8")
    db_file = tmp_path / "factory_data.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(state_file))
    monkeypatch.setattr(SettingsPersistenceService, "_state", {})
    monkeypatch.setattr(line_module, "line_manager", LineManager())
    saved_config = counting_service.config

    written = backup_v1_files(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        SettingsPersistenceService.load()
    finally:
        counting_service.update_config(saved_config)

    # Both backups exist and hold the version 1 data.
    # Compared as paths: the database path comes from a URL, with forward slashes on Windows too.
    assert sorted(Path(p) for p in written) == sorted([tmp_path / "system_state.v1-backup.json", tmp_path / "factory_data.v1-backup.db"])
    assert json.loads((tmp_path / "system_state.v1-backup.json").read_text())["plc_actions"] == V1_STATE["plc_actions"]
    with sqlite3.connect(tmp_path / "factory_data.v1-backup.db") as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == (7,)

    on_disk = json.loads(state_file.read_text())
    assert on_disk["schema_version"] == SCHEMA_VERSION
    line1 = on_disk["lines"][0]
    assert line1["id"] == PRIMARY_LINE_ID and line1["name"] == "Line 1"
    # Version 1's one camera ran the active model and counted the classes typed for
    # the server: the camera holds both now, and nothing of them is left elsewhere.
    assert line1["cameras"] == [{
        "camera_id": "cam-usb-0", "role": "vision", "counting": True, "qr_hold_ms": 1500,
        "model_id": "model-A", "expected_classes": ["can"], "defect_classes": ["defect", "scratch", "broken"],
        "name_based_defects": True,
    }]
    assert "model_id" not in line1 and "active_model_id" not in on_disk
    # Version 1's "connect the camera on startup" is now the one switch for every line.
    assert "auto_connect" not in line1 and on_disk["camera_auto_connect"] is True
    # Version 1 reads these keys: they are unchanged, and Line 1 is what they describe.
    for key in ("active_camera_id", "plc_actions"):
        assert on_disk[key] == V1_STATE[key]
    # The count lines stay where they were; "send results" became send cards
    # (none here: version 1 had its MQTT switch on, but no broker to send to).
    assert on_disk["action_trigger"] == {"line1_position": 0.2, "line2_position": 0.8}
    assert on_disk["send_actions"] == []
    assert "action_trigger" not in line1 and "plc_actions" not in line1 and "send_actions" not in line1
    assert SettingsPersistenceService.get_line(PRIMARY_LINE_ID)["plc_actions"] == V1_STATE["plc_actions"]

    # A second start takes no new backup and changes nothing.
    assert backup_v1_files(f"sqlite:///{db_file.as_posix()}") == []


def test_fresh_install_takes_no_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    assert backup_v1_files(f"sqlite:///{(tmp_path / 'x.db').as_posix()}") == []


def test_line_validation():
    with pytest.raises(ValueError, match="at most 2 cameras"):
        normalize_line({"name": "A", "cameras": [{"camera_id": c} for c in "abc"]})
    with pytest.raises(ValueError, match="Sync needs"):
        normalize_line({"name": "A", "cameras": [{"camera_id": "a"}], "sync": {"enabled": True}})
    with pytest.raises(ValueError, match="role"):
        normalize_line({"name": "A", "cameras": [{"camera_id": "a", "role": "laser"}]})
    two = normalize_line({"name": "A", "cameras": [{"camera_id": "a"}, {"camera_id": "b"}]})
    # The first vision camera becomes the counting camera when none is marked.
    assert [c["counting"] for c in two["cameras"]] == [True, False]
    # The yield target is a percentage; 0 (the default) means no target.
    assert two["yield_target"] == 0.0
    assert normalize_line({"name": "A", "yield_target": "97.5"})["yield_target"] == 97.5
    with pytest.raises(ValueError, match="Yield target"):
        normalize_line({"name": "A", "yield_target": 101})


def test_a_camera_belongs_to_one_line(lines):
    _add_line("Packing 1", [{"camera_id": "cam-A"}])
    with pytest.raises(ValueError, match="already belongs"):
        SettingsPersistenceService.save_line({"name": "Packing 2", "cameras": [{"camera_id": "cam-A", "model_id": "model-test"}]})
    with pytest.raises(ValueError, match="Line 1 cannot be deleted"):
        SettingsPersistenceService.delete_line(PRIMARY_LINE_ID)


# ── Counting ──────────────────────────────────────────────────────────────────

def test_two_lines_counting_the_same_class_keep_separate_counts(lines):
    _add_line("Line 1", [{"camera_id": "cam-1"}], id=PRIMARY_LINE_ID)
    line2 = _add_line("Packing 2", [{"camera_id": "cam-2"}])

    _feed(lines.counter_for_camera("cam-1"), "cam-1", frames=3)
    _feed(lines.counter_for_camera("cam-2"), "cam-2", frames=5)

    assert lines.get(PRIMARY_LINE_ID).counter is counting_service
    assert counting_service.total_inspected == 3
    assert counting_service.counts_by_class == {"bottle": 3}
    other = lines.get(line2["id"]).counter
    assert other is not counting_service
    assert other.total_inspected == 5 and other.counts_by_class == {"bottle": 5}

    lines.get(line2["id"]).reset()
    assert other.total_inspected == 0
    assert counting_service.total_inspected == 3


def test_unclaimed_camera_feeds_line1_only_while_line1_has_no_cameras(lines):
    lines.apply_state()
    routed = lines.route("cam-X")
    assert routed is not None and routed[0].id == PRIMARY_LINE_ID and routed[1] == "vision"
    _add_line("Line 1", [{"camera_id": "cam-1"}], id=PRIMARY_LINE_ID)
    assert lines.route("cam-X") is None
    assert lines.counter_for_camera("cam-X") is counting_service


def test_second_vision_camera_has_its_own_counter_and_wirelines(lines):
    line = _add_line("Dual", [
        {"camera_id": "top", "role": "vision", "counting": True},
        {"camera_id": "side", "role": "vision", "line1_position": 0.1, "line2_position": 0.9},
    ])
    runtime = lines.get(line["id"])
    side = lines.counter_for_camera("side")
    assert side is not runtime.counter and side.camera_id == "side"
    assert (side.config.line1_position, side.config.line2_position) == (0.1, 0.9)
    _feed(side, "side", frames=2)
    _feed(runtime.counter, "top", frames=1)
    # The line totals come from the counting camera only.
    assert runtime.counter.total_inspected == 1 and side.total_inspected == 2


def test_crossing_events_carry_their_line(lines, monkeypatch):
    line = _add_line("Packing 2", [{"camera_id": "cam-2"}])
    counter = lines.counter_for_camera("cam-2")
    sent = []
    monkeypatch.setattr(counter, "dispatch_event", lambda payload, plc_event, is_defect: sent.append((payload, plc_event)))
    _feed(counter, "cam-2")
    payload, plc_event = sent[0]
    assert payload["line_id"] == line["id"] and payload["line_name"] == "Packing 2" and payload["camera_id"] == "cam-2"
    assert plc_event["line_id"] == line["id"] and plc_event["event_type"] == "crossing"
    # Existing payload fields are still there.
    for key in ("event", "track_id", "class_name", "result", "total_inspected", "metrics"):
        assert key in payload


# ── PLC cards ─────────────────────────────────────────────────────────────────

def _plc_card(cid, **extra):
    return {"id": cid, "name": cid, "enabled": True, "trigger_type": "line_cross", "trigger_condition": "any",
            "rearm_lockout_ms": 0, "execution_policy": "every_frame", "plc_endpoint_id": "ep1",
            "operation": "SET", "target_address": "40001", **extra}


def test_an_event_on_line1_never_fires_a_line2_card(lines, monkeypatch):
    fired = []

    async def fake_dispatch(card, state, event):
        fired.append((card["id"], event.get("line_id", PRIMARY_LINE_ID)))

    monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(lambda cls, c, s, e: fake_dispatch(c, s, e)))
    PLCDispatcherService.set_cards([_plc_card("gate-1")], line_id=PRIMARY_LINE_ID)
    PLCDispatcherService.set_cards([_plc_card("gate-2")], line_id="line-2")

    async def run():
        await PLCDispatcherService.evaluate({"event_id": "a", "line_id": PRIMARY_LINE_ID, "result": "good"})
        await PLCDispatcherService.evaluate({"event_id": "b", "line_id": "line-2", "result": "good"})
        # An event with no line (version 1 callers) belongs to Line 1.
        await PLCDispatcherService.evaluate({"event_id": "c", "result": "good"})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert sorted(fired) == [("gate-1", PRIMARY_LINE_ID), ("gate-1", PRIMARY_LINE_ID), ("gate-2", "line-2")]
    # Replacing one line's cards leaves the other line's alone.
    PLCDispatcherService.set_cards([], line_id="line-2")
    assert [c["id"] for c in PLCDispatcherService._cards] == ["gate-1"]


def test_second_vision_camera_only_fires_cards_that_name_it(lines):
    plain = {"trigger_type": "line_cross", "trigger_condition": "any"}
    named = {**plain, "camera_id": "side"}
    from app.services.plc_dispatcher_service import _camera_matches
    main_event = {"camera_id": "top", "counting_camera": True}
    side_event = {"camera_id": "side", "counting_camera": False}
    assert _camera_matches(plain, main_event) and not _camera_matches(plain, side_event)
    assert _camera_matches(named, side_event) and not _camera_matches(named, main_event)


class _FakePLC:
    def __init__(self, endpoint):
        self._ep = endpoint
        self.is_connected = True
        self.last_error = ""
        self.active = 0
        self.max_active = 0
        self.ops = []

    async def connect(self):
        self.is_connected = True
        return True

    async def disconnect(self):
        self.is_connected = False

    async def execute_operation(self, operation, address, write_value, pulse_duration_ms):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.ops.append((operation, address))
        self.active -= 1
        return True, "ok"


def test_two_lines_on_one_plc_share_one_connection_and_queue_their_writes(lines, monkeypatch):
    endpoint = {"id": "ep1", "name": "Shared PLC", "protocol": "plc", "enabled": True, "timeout": 1}
    monkeypatch.setattr(SettingsPersistenceService, "get_endpoints", classmethod(lambda cls, protocol=None: [endpoint]))
    driver = _FakePLC(endpoint)
    created = []

    def get_driver(ep):
        created.append(ep["id"])
        return driver

    monkeypatch.setattr(plc_factory.PLCDriverFactory, "get_driver", staticmethod(get_driver))
    monkeypatch.setattr(PLCFailsafeService, "needs_safe_state", classmethod(lambda cls, ep: False))
    PLCDispatcherService.set_cards([_plc_card("l1-light", target_address="1")], line_id=PRIMARY_LINE_ID)
    PLCDispatcherService.set_cards([_plc_card("l2-light", target_address="2")], line_id="line-2")

    async def run():
        await PLCDispatcherService.evaluate({"event_id": "a", "line_id": PRIMARY_LINE_ID, "result": "good"})
        await PLCDispatcherService.evaluate({"event_id": "b", "line_id": "line-2", "result": "good"})
        await asyncio.gather(*PLCDispatcherService._dispatch_tasks)

    asyncio.run(run())
    assert sorted(driver.ops) == [("SET", "1"), ("SET", "2")]
    assert driver.max_active == 1  # the per-PLC queue never lets two writes overlap
    assert set(created) == {"ep1"}


def test_per_line_dispatch_limit_does_not_block_other_lines(lines, monkeypatch):
    monkeypatch.setattr(PLCDispatcherService, "_max_dispatch_tasks", 1)
    started = []

    async def slow_dispatch(card, state, event):
        started.append(event["line_id"])
        await asyncio.sleep(0.05)

    monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(lambda cls, c, s, e: slow_dispatch(c, s, e)))
    monkeypatch.setattr(PLCDispatcherService, "_report_alarm", staticmethod(lambda *a, **k: None))
    PLCDispatcherService.set_cards([_plc_card("a")], line_id=PRIMARY_LINE_ID)
    PLCDispatcherService.set_cards([_plc_card("b")], line_id="line-2")

    async def run():
        await PLCDispatcherService.evaluate({"event_id": "1", "line_id": PRIMARY_LINE_ID})
        await PLCDispatcherService.evaluate({"event_id": "2", "line_id": PRIMARY_LINE_ID})  # over Line 1's limit
        await PLCDispatcherService.evaluate({"event_id": "3", "line_id": "line-2"})  # Line 2 still has room
        await asyncio.gather(*PLCDispatcherService._dispatch_tasks)

    asyncio.run(run())
    assert started == [PRIMARY_LINE_ID, "line-2"]


def test_plc_address_clash_is_reported():
    clashes = plc_address_clashes({
        "line-1": [{"name": "Tower", "plc_endpoint_id": "ep1", "target_address": "Q0.1"}],
        "line-2": [{"name": "Tower 2", "plc_endpoint_id": "ep1", "target_address": "q0.1"},
                   {"name": "Other", "plc_endpoint_id": "ep1", "target_address": "Q0.2"}],
    })
    assert clashes == [{"plc_endpoint_id": "ep1", "address": "q0.1", "lines": ["line-1", "line-2"],
                        "cards": {"line-1": ["Tower"], "line-2": ["Tower 2"]}}]


def test_known_and_unknown_codes_fire_their_own_cards():
    known = {"event_type": "qr_read", "qr_code": "A1", "qr_status": "known"}
    unknown = {"event_type": "qr_read", "qr_code": "ZZ", "qr_status": "unknown"}
    no_read = {"event_type": "crossing", "result": "good", "qr_code": None, "qr_status": "no_read"}
    cards = {t: {"trigger_type": t, "trigger_condition": "any"} for t in ("qr_read", "qr_known", "qr_unknown", "qr_no_read", "line_cross")}
    assert [t for t, c in cards.items() if _eval_condition(c, known)] == ["qr_read", "qr_known"]
    assert [t for t, c in cards.items() if _eval_condition(c, unknown)] == ["qr_read", "qr_unknown"]
    # A synced crossing with no code fires the no-read card and the crossing cards.
    assert [t for t, c in cards.items() if _eval_condition(c, no_read)] == ["qr_no_read", "line_cross"]


# ── Sync ──────────────────────────────────────────────────────────────────────

def test_sync_pairer():
    p = SyncPairer(0.5)
    # A code read shortly before the crossing pairs with it.
    assert p.add_read(10.0, "code-1") == []
    assert p.add_crossing(10.3, "box-1") == [("paired", "box-1", "code-1")]
    # A crossing waits for a code that arrives inside the window.
    assert p.add_crossing(20.0, "box-2") == []
    assert p.next_deadline() == 20.5
    assert p.add_read(20.4, "code-2") == [("paired", "box-2", "code-2")]
    # A crossing without a code goes out as a no-read, a code without a crossing as unpaired.
    assert p.add_crossing(30.0, "box-3") == []
    assert p.add_read(40.0, "code-3") == [("no_read", "box-3", None)]
    assert p.expire(40.6) == [("unpaired", None, "code-3")]
    assert p.waiting == 0


def test_sync_on_a_line_sends_one_event_per_product(lines, monkeypatch):
    _products(monkeypatch, {"SKU-1": "Widget"})

    line = _add_line("Packing 3", [{"camera_id": "vis"}, {"camera_id": "qr", "role": "qr", "product_list_id": "list-1"}],
                     sync={"enabled": True, "window_ms": 80}, enabled=True)
    runtime = lines.get(line["id"])
    counter = runtime.counter
    assert runtime.sync_enabled and counter.event_sink is not None
    crossings, qr_events = [], []
    monkeypatch.setattr(counter, "dispatch_event", lambda payload, plc_event, is_defect: crossings.append((payload, plc_event)))
    monkeypatch.setattr(runtime, "_emit_qr", lambda read, paired: qr_events.append((read["code"], paired)))

    async def run():
        from app.services.counting_service import CountingService
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            runtime.on_qr_read("qr", "SKU-1", "QR_CODE")
            await asyncio.sleep(0.01)
            _feed(counter, "vis")                       # crossing inside the window -> paired
            await asyncio.sleep(0.01)
            _feed(counter, "vis")                       # crossing with no code -> no read
            await asyncio.sleep(0.2)
            runtime.on_qr_read("qr", "STRAY", "QR_CODE")  # code with no crossing -> unpaired
            await asyncio.sleep(0.2)
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(run())
    assert len(crossings) == 2
    first, second = crossings
    assert first[0]["qr_code"] == "SKU-1" and first[0]["qr_status"] == "known" and first[0]["product_name"] == "Widget"
    assert first[1]["qr_status"] == "known"
    assert second[0]["qr_status"] == "no_read" and second[0]["qr_code"] is None
    assert qr_events == [("STRAY", False)]
    assert runtime.qr_stats["no_reads"] == 1 and runtime.qr_stats["unpaired"] == 1
    statuses = [r["status"] for r in runtime.recent_reads()]
    assert "no_read" in statuses and "known" in statuses


def test_qr_reads_without_sync_go_out_on_their_own(lines, monkeypatch):
    _products(monkeypatch, {})
    line = _add_line("QR only", [{"camera_id": "qr", "role": "qr", "product_list_id": "list-1"}])
    runtime = lines.get(line["id"])
    sent = []
    monkeypatch.setattr(runtime.counter, "send", lambda event, payload: sent.append(payload))

    async def run():
        from app.services.counting_service import CountingService
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            runtime.on_qr_read("qr", "UNLISTED", "EAN_13")
            await asyncio.sleep(0.02)
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(run())
    assert sent[0]["event"] == "QR_CODE_READ" and sent[0]["qr_status"] == "unknown"
    assert sent[0]["line_id"] == line["id"] and sent[0]["paired"] is None


def test_qr_reader_counts_a_code_once_until_it_leaves_view(lines, monkeypatch):
    from app.schemas.qr import BarcodeDecodeResponse, BarcodeItem
    from app.services.qr_service import _QRReaderWorker

    _add_line("QR only", [{"camera_id": "qr", "role": "qr", "qr_hold_ms": 1000}])
    seen = []
    runtime = lines.route("qr")[0]
    monkeypatch.setattr(runtime, "on_qr_read", lambda cam, code, fmt, t=None: seen.append((code, t)))
    worker = _QRReaderWorker("qr")
    try:
        frames = {"on": BarcodeDecodeResponse(total_found=1, codes=[BarcodeItem(code_type="QR_CODE", data="SKU-1")],
                                              decode_time_ms=1.0, image_width=10, image_height=10),
                  "off": BarcodeDecodeResponse(total_found=0, codes=[], decode_time_ms=1.0, image_width=10, image_height=10)}
        current = {"frame": "on"}
        monkeypatch.setattr(worker._engine, "decode", lambda mat, **_: frames[current["frame"]])
        worker.process(None, now=0.0)
        worker.process(None, now=0.5)   # same code, still in view
        worker.process(None, now=1.2)   # still in view: not a new read
        current["frame"] = "off"
        worker.process(None, now=2.0)
        worker.process(None, now=3.0)   # out of view longer than the hold time
        current["frame"] = "on"
        worker.process(None, now=3.1)
    finally:
        worker.stop()
    assert [code for code, _ in seen] == ["SKU-1", "SKU-1"]
    assert [t for _, t in seen] == [0.0, 3.1]


# ── Flows and metrics ─────────────────────────────────────────────────────────

def test_flow_limited_to_a_line_ignores_other_lines():
    from app.engines.flow_engine import _node_matches
    node = {"type": "event_qr", "config": {"production_line": "line-2"}}
    assert _node_matches(node, "qr_code", {"line_id": "line-2"})
    assert not _node_matches(node, "qr_code", {"line_id": PRIMARY_LINE_ID})
    assert _node_matches({"type": "event_qr", "config": {}}, "qr_code", {"line_id": PRIMARY_LINE_ID})


def test_prometheus_metrics_have_a_line_label():
    from app.services.health_service import to_prometheus
    text = to_prometheus({"uptime_seconds": 1.0, "lines": [{"line": "line-2", "name": 'Pack "B"', "good_total": 4}]})
    assert 'vision_server_line_good_total{line="line-2",line_name="Pack \\"B\\""} 4' in text


# ── Frames reach the camera's own line ────────────────────────────────────────

def test_inference_worker_feeds_the_cameras_line_and_uses_its_model(lines, monkeypatch):
    import time
    import numpy as np
    from app.services.vision_service import _CameraInferenceWorker
    from tests.conftest import FakeInferenceEngine

    line = _add_line("Packing 2", [{"camera_id": "cam-2", "model_id": "model-B"}])
    runtime = lines.get(line["id"])
    line_engine = FakeInferenceEngine(class_name="carton")
    other_engine = FakeInferenceEngine()
    lines._engines["model-B"] = {"engine": line_engine, "name": "Model B"}
    lines._engines["model-other"] = {"engine": other_engine, "name": "Another camera's model"}
    runtime.counter._trackers["cam-2"] = _CrossingTracker("carton")

    worker = _CameraInferenceWorker("cam-2")
    try:
        assert worker.submit_frame_if_idle(np.zeros((48, 64, 3), dtype=np.uint8), 1)
        deadline = time.time() + 3
        while runtime.counter.total_inspected == 0 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert line_engine.calls == 1 and other_engine.calls == 0
    assert runtime.counter.counts_by_class == {"carton": 1}
    assert counting_service.total_inspected == 0
    assert lines.summary(runtime)["model_name"] == "Model B"
    # A camera no model was picked for runs none: there is no server-wide model to fall back to.
    engine, name, model_id = lines.engine_for_camera("unassigned-cam")
    assert engine.is_loaded is False and model_id is None


# ── A vision camera that also reads codes ─────────────────────────────────────

def test_a_vision_camera_can_read_codes_too():
    line = normalize_line({"name": "A", "cameras": [
        {"camera_id": "a", "read_codes": True, "qr_trigger": "line2", "qr_trigger_delay_ms": 50},
        {"camera_id": "b", "qr_trigger": "line1"},
    ], "sync": {"enabled": True}})
    a, b = line["cameras"]
    assert a["read_codes"] is True and a["qr_trigger"] == "line2" and a["qr_trigger_delay_ms"] == 50
    # Without Read codes a vision camera keeps no code settings.
    assert "read_codes" not in b and "qr_trigger" not in b
    assert line["sync"]["enabled"] is True  # one camera that counts and reads codes is enough
    with pytest.raises(ValueError, match="Sync needs"):
        normalize_line({"name": "A", "cameras": [{"camera_id": "a"}], "sync": {"enabled": True}})
    with pytest.raises(ValueError, match="capture"):
        normalize_line({"name": "A", "cameras": [{"camera_id": "a", "read_codes": True, "qr_trigger": "line9"}]})


def test_one_camera_counts_and_pairs_the_codes_it_reads(lines, monkeypatch):
    _products(monkeypatch, {"SKU-1": "Widget"})

    line = _add_line("Single camera", [{"camera_id": "vis", "read_codes": True, "product_list_id": "list-1"}],
                     sync={"enabled": True, "window_ms": 80}, enabled=True)
    runtime = lines.get(line["id"])
    assert runtime.reads_codes("vis") and runtime.has_code_reader and runtime.sync_enabled
    assert runtime.capture_triggers == {}  # reads continuously
    row = lines.summary(runtime)
    assert row["has_qr"] is True and row["cameras"][0]["read_codes"] is True
    assert row["cameras"][0]["qr_trigger"] == "continuous"

    crossings = []
    monkeypatch.setattr(runtime.counter, "dispatch_event", lambda payload, plc_event, is_defect: crossings.append(payload))

    async def run():
        from app.services.counting_service import CountingService
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            runtime.on_qr_read("vis", "SKU-1", "QR_CODE")
            await asyncio.sleep(0.01)
            _feed(runtime.counter, "vis")
            await asyncio.sleep(0.2)
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(run())
    assert [(c["qr_code"], c["qr_status"]) for c in crossings] == [("SKU-1", "known")]


def test_a_vision_camera_can_take_one_picture_per_product(lines):
    line = _add_line("Per product", [{"camera_id": "vis", "read_codes": True, "qr_trigger": "line2"}], enabled=True)
    runtime = lines.get(line["id"])
    assert runtime.capture_triggers == {"vis": (2, 0.0)} and runtime.qr_triggered("vis")
    assert runtime.counter.line_crossing_sink is not None


def test_codes_from_a_vision_camera_reach_its_line_but_not_its_frame_rate(lines, monkeypatch):
    import numpy as np

    from app.schemas.qr import BarcodeDecodeResponse, BarcodeItem
    from app.services.qr_service import _QRReaderWorker

    line = _add_line("Codes", [{"camera_id": "vis", "read_codes": True}], enabled=True)
    runtime = lines.get(line["id"])
    reads, frames, captures = [], [], []
    monkeypatch.setattr(runtime, "on_qr_read", lambda cam, code, fmt, t=None, **kw: reads.append((cam, code)))
    monkeypatch.setattr(runtime, "record_frame", lambda cam, now=None: frames.append(cam))
    monkeypatch.setattr(runtime, "on_qr_capture", lambda cam, record, jpeg: captures.append(record["status"]))
    worker = _QRReaderWorker("vis")
    try:
        found = BarcodeDecodeResponse(total_found=1, codes=[BarcodeItem(code_type="QR_CODE", data="SKU-9")],
                                      decode_time_ms=1.0, image_width=10, image_height=10)
        monkeypatch.setattr(worker._engine, "decode", lambda mat, **_: found)
        worker.process(np.zeros((10, 10, 3), np.uint8))
        monkeypatch.setattr(worker, "capture", lambda request: ({"status": "read"}, None))
        worker._run_capture({})
    finally:
        worker.stop()
    assert reads == [("vis", "SKU-9")] and captures == ["read"]
    assert frames == []  # the model's thread counts this camera's frames


def test_the_runner_feeds_a_code_reading_vision_camera_to_both_workers(lines, monkeypatch):
    import threading
    import time as _time

    import numpy as np

    from app.services import qr_service, vision_service
    from app.state.application_state import app_state

    class Camera:
        is_connected = True

        def __init__(self):
            self.fid = 0

        def get_latest_frame_id(self):
            self.fid += 1
            return self.fid

        def get_latest_raw_mat(self, copy=True):
            return True, np.zeros((8, 8, 3), np.uint8), self.fid

    class Worker:
        is_busy = False

        def __init__(self):
            self.frames = 0

        def submit_frame_if_idle(self, mat, fid, copy=True):
            self.frames += 1
            return True

    _add_line("Both", [{"camera_id": "vis", "read_codes": True}], enabled=True)
    yolo, codes = Worker(), Worker()
    monkeypatch.setattr(app_state, "cameras", {"vis": Camera()})
    monkeypatch.setattr(vision_service.CameraStreamPipeline, "get_worker", classmethod(lambda cls, cid: yolo))
    monkeypatch.setattr(qr_service.QRReaderPipeline, "get_worker", classmethod(lambda cls, cid: codes))
    stop = threading.Event()
    monkeypatch.setattr(vision_service.ContinuousVisionRunner, "_stop_event", stop)
    runner = threading.Thread(target=vision_service.ContinuousVisionRunner._loop, daemon=True)
    runner.start()
    _time.sleep(0.2)
    stop.set()
    runner.join(timeout=2)
    assert yolo.frames > 3 and codes.frames > 3
