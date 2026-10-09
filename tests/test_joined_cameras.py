"""Several vision cameras on one product: a camera that joins the counting camera's result.

A joined camera looks at the same products as the counting camera, before or
after it on the belt. Each of its crossings is matched to one of the counting
camera's products by travel time, and the product gets one result, is counted
once and sends one event.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services.card_triggers import card_fires
from app.services.counting_service import CountingService, counting_service
from app.services.line_config import (
    CAMERA_KEPT_KEYS,
    PRIMARY_LINE_ID,
    REASON_NO_CODE,
    REASON_NOT_LISTED,
    REASON_STATION_NO_RESULT,
    REASON_VISION,
    REJECT_REASON_CODES,
    REJECT_REASONS,
    SCHEMA_VERSION,
    TEMPLATE_FIELDS,
    line_warnings,
    normalize_line,
    product_result,
    product_verdict,
    upgrade_state,
)
from app.services.line_service import JoinedStation, LineManager, ProductAssembly
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.send_dispatcher_service import MESSAGE_FIELDS, build_message, render_template
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

LIST = "list-1"
SYNC_MS = 80


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file, dispatcher and line manager; the product list holds LISTED."""
    from app.services.product_service import product_catalog

    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(SettingsPersistenceService, "_state", state)
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    monkeypatch.setattr(PLCDispatcherService, "_cards", [])
    monkeypatch.setattr(PLCDispatcherService, "_states", {})
    monkeypatch.setattr(product_catalog, "_lists", {LIST: {"LISTED": {"code": "LISTED", "name": "Widget"}}})
    monkeypatch.setattr(product_catalog, "_names", {LIST: "List 1"})
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    saved_config = counting_service.config
    counting_service.reset_counts()
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)
    counting_service.reset_counts()


class _CrossingTracker:
    """Stands in for WirelineTracker: every update reports one crossing."""

    def __init__(self, is_defect=False, class_name=None):
        self.is_defect = is_defect
        self.class_name = class_name or ("scratch" if is_defect else "bottle")
        self.objects, self._recently_counted, self.next_id = {}, set(), 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, self.class_name, self.is_defect, 0.87)]


def _cross(counter, camera_id, is_defect=False):
    counter._trackers[camera_id] = _CrossingTracker(is_defect)
    counter.process_frame([], 640, 480, camera_rotation=90, camera_id=camera_id)


def _joined(camera_id, offset_ms, window_ms, missing="ignore", **extra):
    return {"camera_id": camera_id, "station": "join", "join_offset_ms": offset_ms, "join_window_ms": window_ms,
            "join_missing": missing, **extra}


def _line(manager, cameras, name="Joined", sync=None):
    # A vision camera is not saved without a model.
    cameras = [cam if cam.get("role", "vision") != "vision" else {"model_id": "model-test", **cam} for cam in cameras]
    line = SettingsPersistenceService.save_line({"name": name, "cameras": cameras, "enabled": True,
                                                 "sync": sync or {"window_ms": SYNC_MS}})
    manager.apply_state()
    runtime = manager.get(line["id"])
    # Every event a counter of the line sends: the line's products and an own station's.
    runtime.sent = []
    for counter in [runtime.counter, *runtime.aux_counters.values()]:
        counter.dispatch_event = lambda payload, plc_event, is_defect: runtime.sent.append((payload, plc_event))
    return runtime


def _run(steps):
    """Run ``steps`` (an async function) with the line runtimes' event loop set, as in the server."""
    async def main():
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            await steps()
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(main())


def _totals(counter):
    return counter.total_inspected, counter.good_count, counter.rejected_count


def _station(payload, camera_id):
    return next(row for row in payload["stations"] if row["camera_id"] == camera_id)


# ── The rule ──────────────────────────────────────────────────────────────────

GOOD = {"is_defect": False, "class_name": "bottle", "confidence": 0.9}
BAD = {"is_defect": True, "class_name": "scratch", "confidence": 0.8}
CHECK = {"qr": {"action": "accept_listed", "no_read": "reject"}}
OTHER_CODE = {"camera_id": "qr", "known": False}
LISTED_CODE = {"camera_id": "qr", "known": True}


@pytest.mark.parametrize("vision_reject, stations, missing, read, checks, expected", [
    # every camera good -> good
    (False, {"side": GOOD}, {"side": "reject"}, None, {}, (False, None, None)),
    # the counting camera rejects first, whatever the others say
    (True, {"side": BAD}, {}, OTHER_CODE, CHECK, (True, REASON_VISION, "top")),
    # then the joined cameras, in card order
    (False, {"side": GOOD, "under": BAD, "end": BAD}, {}, None, {}, (True, REASON_VISION, "under")),
    # then the code...
    (False, {"side": BAD}, {}, OTHER_CODE, CHECK, (True, REASON_VISION, "side")),
    (False, {"side": GOOD}, {}, OTHER_CODE, CHECK, (True, REASON_NOT_LISTED, "qr")),
    (False, {"side": None}, {"side": "reject"}, None, CHECK, (True, REASON_NO_CODE, "qr")),
    # ...then a joined camera that saw nothing
    (False, {"side": None}, {"side": "reject"}, LISTED_CODE, CHECK, (True, REASON_STATION_NO_RESULT, "side")),
    (False, {"side": None, "end": None}, {"side": "ignore", "end": "reject"}, None, {}, (True, REASON_STATION_NO_RESULT, "end")),
    (False, {"side": None}, {"side": "ignore"}, None, {}, (False, None, None)),
])
def test_one_result_from_every_camera(vision_reject, stations, missing, read, checks, expected):
    assert product_result(vision_reject, read, checks, stations, missing, "top") == expected
    assert product_verdict(vision_reject, read, checks, stations, missing) == expected[:2]


def test_the_new_reason_and_placeholder_are_known():
    assert REASON_STATION_NO_RESULT == "station_no_result" and REASON_STATION_NO_RESULT in REJECT_REASONS
    assert REJECT_REASON_CODES[REASON_STATION_NO_RESULT] == 5
    assert "reject_camera" in TEMPLATE_FIELDS and "stations" in MESSAGE_FIELDS and "reject_camera_id" in MESSAGE_FIELDS


# ── Settings ──────────────────────────────────────────────────────────────────

def test_join_settings_and_their_defaults():
    line = normalize_line({"name": "A", "cameras": [
        {"camera_id": "top", "counting": True, "station": "join", "join_offset_ms": 100},
        {"camera_id": "side", "station": "join"},
        _joined("end", -300, 250, "reject"),
        {"camera_id": "own", "station": "own", "join_offset_ms": 100},
        {"camera_id": "qr", "role": "qr", "station": "join"},
    ]})
    top, side, end, own, reader = line["cameras"]
    # The counting camera makes the product result: it joins nothing.
    assert "station" not in top and "join_offset_ms" not in top
    assert (side["join_offset_ms"], side["join_window_ms"], side["join_missing"]) == (0, 500, "ignore")
    assert (end["join_offset_ms"], end["join_window_ms"], end["join_missing"]) == (-300, 250, "reject")
    assert "join_offset_ms" not in own and "station" not in reader
    assert set(("join_offset_ms", "join_window_ms", "join_missing")) <= set(CAMERA_KEPT_KEYS)


@pytest.mark.parametrize("camera, message", [
    (_joined("side", 60001, 500), "travel time from the counting camera must be between -60000 and 60000"),
    (_joined("side", 0, 40), "match window must be between 50 and 10000"),
    (_joined("side", 0, 20000), "match window must be between 50 and 10000"),
    (_joined("side", 0, 500, "maybe"), "'reject' or 'ignore'"),
])
def test_join_settings_validation(camera, message):
    with pytest.raises(ValueError, match=message):
        normalize_line({"name": "A", "cameras": [{"camera_id": "top", "counting": True}, camera]})


def test_a_joined_camera_needs_a_counting_camera():
    with pytest.raises(ValueError, match="Camera 1 joins the product result of the counting camera, but the line has no counting camera"):
        normalize_line({"name": "A", "cameras": [_joined("a", 0, 500), _joined("b", 100, 500)]})
    # Without a camera marked Counting, the first camera that does not join counts.
    line = normalize_line({"name": "A", "cameras": [_joined("a", 0, 500), {"camera_id": "b"}]})
    assert [(c["camera_id"], c["counting"], c.get("station")) for c in line["cameras"]] == [("a", False, "join"), ("b", True, None)]


def test_a_client_that_does_not_send_the_join_settings_keeps_them(lines):
    line = SettingsPersistenceService.save_line({"name": "Kept", "cameras": [
        {"camera_id": "top", "counting": True, "model_id": "model-test"},
        {"model_id": "model-test", **_joined("side", 400, 300, "reject")},
    ]})
    again = SettingsPersistenceService.save_line({"id": line["id"], "cameras": [
        {"camera_id": "top", "counting": True}, {"camera_id": "side"},
    ]})
    side = again["cameras"][1]
    assert (side["station"], side["join_offset_ms"], side["join_window_ms"], side["join_missing"]) == ("join", 400, 300, "reject")
    # Back to an own station: the join settings go.
    own = SettingsPersistenceService.save_line({"id": line["id"], "cameras": [
        {"camera_id": "top", "counting": True}, {"camera_id": "side", "station": "own"},
    ]})
    assert own["cameras"][1]["station"] == "own" and "join_offset_ms" not in own["cameras"][1]


def test_version_8_settings_load_without_the_new_keys(lines):
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    assert state["schema_version"] == SCHEMA_VERSION == 8
    # A file saved before the join settings existed: a camera set to "join" with none of them.
    state["lines"].append({"id": "line-old", "name": "Old", "enabled": True, "sync": {"enabled": False, "window_ms": 500},
                           "cameras": [
                               {"camera_id": "top", "role": "vision", "counting": True, "model_id": "model-test"},
                               {"camera_id": "side", "role": "vision", "counting": False, "station": "join", "model_id": "model-test"},
                               {"camera_id": "own", "role": "vision", "counting": False, "model_id": "model-test"},
                           ]})
    assert upgrade_state(state) is False
    SettingsPersistenceService._state = state
    lines.apply_state()
    runtime = lines.get("line-old")
    assert runtime.assembly.stations == (JoinedStation("side", 0.0, 0.5, "ignore"),)
    assert runtime.counter.event_sink is not None and runtime.aux_counters["own"].event_sink is None
    saved = normalize_line(SettingsPersistenceService.get_line("line-old"))
    assert [(c.get("join_offset_ms"), c.get("join_window_ms"), c.get("join_missing")) for c in saved["cameras"]] == [
        (None, None, None), (0, 500, "ignore"), (None, None, None)]


def test_a_line_without_joined_cameras_runs_as_before(lines):
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "side", "station": "own"}])
    assert runtime.assembly.stations == () and runtime.counter.event_sink is None
    assert runtime.aux_counters["side"].event_sink is None

    async def steps():
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.01)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert "stations" not in payload and "reject_camera_id" not in payload
    assert "reject_camera_id" not in plc_event and "joined_cameras" not in plc_event
    assert plc_event["delay_from_crossing"] is False


# ── Matching ──────────────────────────────────────────────────────────────────

def test_a_downstream_joined_camera_rejects_the_product_once(lines, monkeypatch):
    runtime = _line(lines, [{"camera_id": "top"}, _joined("side", 400, 150)])
    side = runtime.aux_counters["side"]
    assert side.event_sink is not None and runtime.counter.event_sink is not None
    seen = []

    async def steps():
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.2)
        seen.append(_totals(runtime.counter))      # not counted while it waits for the side camera
        await asyncio.sleep(0.2)
        _cross(side, "side", is_defect=True)        # 400 ms later
        await asyncio.sleep(0.02)
        seen.append(_totals(runtime.counter))
        await asyncio.sleep(0.3)                    # nothing else comes out of it

    _run(steps)
    assert seen == [(0, 0, 0), (1, 0, 1)]
    (payload, plc_event), = runtime.sent              # one message and one PLC event
    assert (payload["result"], payload["reject_reason"], payload["reject_camera_id"]) == ("REJECTED", REASON_VISION, "side")
    assert payload["vision_result"] == "PASSED" and payload["camera_id"] == "top" and payload["class_name"] == "bottle"
    row = _station(payload, "side")
    assert (row["result"], row["class_name"], row["confidence"]) == ("reject", "scratch", 0.87)
    assert 350 <= row["travel_ms"] <= 500 and set(row) == {"camera_id", "camera_name", "result", "class_name", "confidence", "travel_ms"}
    assert (plc_event["result"], plc_event["reject_reason"], plc_event["reject_camera_id"]) == ("reject", REASON_VISION, "side")
    assert plc_event["delay_from_crossing"] is True and plc_event["counting_camera"] is True
    assert plc_event["joined_cameras"] == ["side"]
    # The joined camera counted nothing on its own.
    assert _totals(side) == (0, 0, 0)
    assert runtime.assembly.stats("side") == {"matched": 1, "unmatched": 0, "rejects": 1, "no_result": 0}
    # A card naming the joined camera fires for the product; one naming an own station would not.
    line_id = runtime.id
    assert card_fires({"line_id": line_id, "trigger": "cross_line", "condition": "reject", "camera_id": "side"}, plc_event)
    assert card_fires({"line_id": line_id, "trigger": "cross_line", "condition": "reject"}, plc_event)
    assert not card_fires({"line_id": line_id, "trigger": "cross_line", "condition": "any", "camera_id": "other"}, plc_event)


def test_an_upstream_joined_camera_is_matched_the_same_way(lines):
    runtime = _line(lines, [{"camera_id": "top"}, _joined("before", -300, 150)])
    before = runtime.aux_counters["before"]

    async def steps():
        _cross(before, "before", is_defect=True)   # it sees the product first
        await asyncio.sleep(0.3)
        _cross(runtime.counter, "top")             # 300 ms later: the result is known at once
        await asyncio.sleep(0.02)
        _cross(before, "before")
        await asyncio.sleep(0.3)
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.02)

    _run(steps)
    results = [(p["result"], p["reject_reason"], p["reject_camera_id"], _station(p, "before")["result"]) for p, _ in runtime.sent]
    assert results == [("REJECTED", REASON_VISION, "before", "reject"), ("PASSED", None, None, "good")]
    assert all(-400 <= _station(p, "before")["travel_ms"] <= -250 for p, _ in runtime.sent)
    assert _totals(runtime.counter) == (2, 1, 1)


@pytest.mark.parametrize("missing, expected", [
    ("reject", ("REJECTED", REASON_STATION_NO_RESULT, "side", 5)),
    ("ignore", ("PASSED", None, None, 0)),
])
def test_a_joined_camera_that_sees_nothing(lines, missing, expected):
    runtime = _line(lines, [{"camera_id": "top"}, _joined("side", 100, 80, missing)])

    async def steps():
        _cross(runtime.counter, "top")
        await asyncio.sleep(0.1)
        assert _totals(runtime.counter) == (0, 0, 0)
        await asyncio.sleep(0.15)    # past 100 + 80 ms

    _run(steps)
    (payload, plc_event), = runtime.sent
    reason_code = PLCDispatcherService.write_value({"value_source": "reject_reason_code"}, plc_event)
    assert (payload["result"], payload["reject_reason"], payload["reject_camera_id"], reason_code) == expected
    assert payload["stations"] == [{"camera_id": "side", "camera_name": None, "result": "no_result",
                                    "class_name": None, "confidence": None, "travel_ms": None}]
    assert runtime.assembly.stats("side")["no_result"] == 1


def test_two_products_a_second_apart_get_their_own_results(lines):
    runtime = _line(lines, [{"camera_id": "top"}, _joined("side", 400, 300), _joined("under", 200, 300)])
    side, under = runtime.aux_counters["side"], runtime.aux_counters["under"]

    async def steps():
        _cross(runtime.counter, "top")                  # product A at 0 s
        await asyncio.sleep(0.2)
        _cross(under, "under")                          # A under: good
        await asyncio.sleep(0.2)
        _cross(side, "side", is_defect=True)            # A side: reject
        await asyncio.sleep(0.6)
        _cross(runtime.counter, "top")                  # product B at 1 s
        await asyncio.sleep(0.2)
        _cross(under, "under", is_defect=True)          # B under: reject
        await asyncio.sleep(0.2)
        _cross(side, "side")                            # B side: good
        await asyncio.sleep(0.05)

    _run(steps)
    a, b = (payload for payload, _ in runtime.sent)
    assert (a["reject_camera_id"], _station(a, "side")["result"], _station(a, "under")["result"]) == ("side", "reject", "good")
    assert (b["reject_camera_id"], _station(b, "side")["result"], _station(b, "under")["result"]) == ("under", "good", "reject")
    # The stations come in card order.
    assert [row["camera_id"] for row in a["stations"]] == ["side", "under"]
    assert _totals(runtime.counter) == (2, 0, 2)
    for camera_id in ("side", "under"):
        assert runtime.assembly.stats(camera_id) == {"matched": 2, "unmatched": 0, "rejects": 1, "no_result": 0}


def test_a_crossing_that_matches_no_product_is_dropped_and_counted(lines):
    runtime = _line(lines, [{"camera_id": "top"}, _joined("side", 200, 100)])

    async def steps():
        _cross(runtime.aux_counters["side"], "side", is_defect=True)   # no product before it
        await asyncio.sleep(0.05)

    _run(steps)
    assert runtime.sent == [] and _totals(runtime.counter) == (0, 0, 0)
    assert runtime.assembly.stats("side")["unmatched"] == 1
    row = next(c for c in lines.summary(runtime)["cameras"] if c["camera_id"] == "side")
    assert row["station"] == "join" and "counts" not in row
    assert row["station_stats"] == {"matched": 0, "unmatched": 1, "rejects": 0, "no_result": 0}
    assert (row["join_offset_ms"], row["join_window_ms"], row["join_missing"]) == (200, 100, "ignore")
    runtime.reset()
    assert runtime.assembly.stats("side")["unmatched"] == 0


def test_joined_camera_sync_and_a_code_check_give_one_result(lines):
    runtime = _line(lines, [
        {"camera_id": "top"}, _joined("side", 150, 100),
        {"camera_id": "qr", "role": "qr", "product_list_id": LIST, "qr_action": "accept_listed", "qr_no_read": "reject"},
    ])
    assert runtime.sync_enabled
    side = runtime.aux_counters["side"]
    waiting = []

    async def steps():
        for code, side_defect in (("LISTED", False), ("LISTED", True), ("OTHER", False)):
            runtime.on_qr_read("qr", code, "QR_CODE")
            await asyncio.sleep(0.01)
            _cross(runtime.counter, "top")
            await asyncio.sleep(0.05)
            waiting.append(len(runtime.sent))   # paired with its code, still waiting for the side camera
            await asyncio.sleep(0.1)
            _cross(side, "side", is_defect=side_defect)
            await asyncio.sleep(0.05)

    _run(steps)
    assert waiting == [0, 1, 2]
    results = [(p["result"], p["reject_reason"], p["reject_camera_id"], p["qr_code"], _station(p, "side")["result"])
               for p, _ in runtime.sent]
    assert results == [
        ("PASSED", None, None, "LISTED", "good"),
        ("REJECTED", REASON_VISION, "side", "LISTED", "reject"),
        ("REJECTED", REASON_NOT_LISTED, "qr", "OTHER", "good"),
    ]
    assert all(e["delay_from_crossing"] and e["qr_paired"] for _, e in runtime.sent)
    assert [e["reject_camera_id"] for _, e in runtime.sent] == [None, "side", "qr"]
    assert runtime.qr_stats["code_rejects"] == 1 and _totals(runtime.counter) == (3, 1, 2)


def test_an_own_station_still_counts_on_its_own(lines):
    runtime = _line(lines, [{"camera_id": "top"}, {"camera_id": "own", "station": "own"}, _joined("side", 50, 50)])
    own = runtime.aux_counters["own"]
    assert own.event_sink is None

    async def steps():
        _cross(own, "own", is_defect=True)
        await asyncio.sleep(0.01)

    _run(steps)
    (payload, plc_event), = runtime.sent
    assert (payload["camera_id"], payload["result"], plc_event["counting_camera"]) == ("own", "REJECTED", False)
    assert "stations" not in payload and "joined_cameras" not in plc_event
    assert _totals(own) == (1, 0, 1) and _totals(runtime.counter) == (0, 0, 0)
    line_id = runtime.id
    assert card_fires({"line_id": line_id, "trigger": "cross_line", "condition": "reject", "camera_id": "own"}, plc_event)
    assert not card_fires({"line_id": line_id, "trigger": "cross_line", "condition": "reject"}, plc_event)
    assert not card_fires({"line_id": line_id, "trigger": "cross_line", "condition": "reject", "camera_id": "side"}, plc_event)


def test_the_assembly_keeps_what_a_product_waited_for_when_the_settings_change():
    assembly = ProductAssembly()
    assembly.configure([JoinedStation("side", 0.4, 0.1, "reject")])
    assert assembly.add_product({"is_defect": False}, 10.0, code_waiting=False) == []
    assembly.configure([])   # the camera no longer joins
    assert assembly.next_deadline() == pytest.approx(10.5)
    (product,) = assembly.expire(10.6)
    assert product.results() == {"side": None} and product.missing() == {"side": "reject"}


# ── Messages ──────────────────────────────────────────────────────────────────

def test_the_message_fields_and_the_reject_camera_placeholder():
    payload = {"event": "WIRELINE_OBJECT_CROSSED", "camera_id": "top", "camera_name": "Top", "result": "REJECTED",
               "reject_reason": REASON_VISION, "reject_camera_id": "side",
               "stations": [{"camera_id": "side", "camera_name": "Side", "result": "reject", "class_name": "scratch",
                             "confidence": 0.8, "travel_ms": 402}]}
    assert build_message({"fields": ["stations", "reject_camera_id"]}, payload) == {
        "stations": payload["stations"], "reject_camera_id": "side"}
    assert render_template("{result} by {reject_camera}", payload) == "REJECTED by side"
    # Without joined cameras the vision camera that found the defect rejected it.
    assert render_template("{reject_camera}", {**payload, "reject_camera_id": None}) == "Top"
    assert render_template("[{reject_camera}]", {"result": "PASSED", "camera_id": "top"}) == "[]"


# ── Warnings ──────────────────────────────────────────────────────────────────

def _warning_line(*cameras, plc_actions=(), sync=None):
    line = normalize_line({"name": "A", "sync": sync or {"window_ms": 500}, "cameras": [{"camera_id": "top", "counting": True}, *cameras]})
    return {**line, "plc_actions": list(plc_actions)}


def test_the_travel_delay_warning_names_the_card_and_the_camera():
    gate = {"id": "g", "name": "Reject gate", "trigger": "cross_line", "condition": "reject", "delay_ms": 500}
    lamp = {"id": "l", "name": "Good lamp", "trigger": "cross_line", "condition": "good", "delay_ms": 0}
    line = _warning_line(_joined("side", 400, 300), _joined("under", 100, 300), plc_actions=[gate, lamp])
    (warning,) = line_warnings(line)
    assert "'Reject gate'" in warning and "500 ms" in warning and "Camera 2" in warning and "700 ms" in warning
    assert line_warnings({**line, "plc_actions": [{**gate, "delay_ms": 800}, lamp]}) == []
    # An upstream camera's result is there when the counting camera sees the product.
    assert line_warnings(_warning_line(_joined("before", -400, 300), plc_actions=[{**gate, "delay_ms": 0}])) == []
    # With a code check too, the longer of the two waits is named.
    reader = {"camera_id": "qr", "role": "qr", "qr_action": "accept_listed", "product_list_id": LIST}
    (sync_first,) = line_warnings(_warning_line(_joined("side", 100, 100), reader, plc_actions=[{**gate, "delay_ms": 300}]))
    assert "Sync window (500 ms)" in sync_first
    (join_first,) = line_warnings(_warning_line(_joined("side", 400, 300), reader, plc_actions=[gate]))
    assert "Camera 2" in join_first and "700 ms" in join_first


def test_a_match_window_that_reaches_the_next_product_is_warned_about():
    line = _warning_line(_joined("side", 400, 300), {"camera_id": "own", "station": "own"})
    assert line_warnings(line) == [] and line_warnings(line, products_per_minute=60) == []
    (warning,) = line_warnings(line, products_per_minute=150)   # 400 ms apart, less than 2 x 300 ms
    assert "Camera 2" in warning and "300 ms" in warning and "400 ms apart" in warning and "shorter than 200 ms" in warning


# ── API ───────────────────────────────────────────────────────────────────────

def test_line_api_saves_a_joined_camera(client, admin_headers, vision_model):
    line = client.post("/api/v1/lines", json={"name": "Joined line"}, headers=admin_headers).json()
    try:
        saved = client.put(f"/api/v1/lines/{line['id']}", json={
            "cameras": [
                {"camera_id": "join-top", "model_id": vision_model, "counting": True},
                {"camera_id": "join-side", "model_id": vision_model, **_joined("join-side", 400, 300, "reject")},
            ],
            "plc_actions": [{"id": "join-gate", "name": "Gate", "trigger": "cross_line", "condition": "reject", "delay_ms": 200}],
        }, headers=admin_headers)
        assert saved.status_code == 200, saved.text
        body = saved.json()
        side = body["cameras"][1]
        assert (side["station"], side["join_offset_ms"], side["join_window_ms"], side["join_missing"]) == ("join", 400, 300, "reject")
        (warning,) = body["warnings"]
        assert "'Gate'" in warning and "Camera 2" in warning and "700 ms" in warning
        row = body["status"]["cameras"][1]
        assert row["station"] == "join" and row["station_stats"]["matched"] == 0

        bad = client.put(f"/api/v1/lines/{line['id']}", json={
            "cameras": [{"camera_id": "join-top", "model_id": vision_model, "counting": True},
                        {"camera_id": "join-side", "model_id": vision_model, **_joined("join-side", 0, 20)}],
        }, headers=admin_headers)
        assert bad.status_code == 422 and "Camera 2" in bad.json()["detail"] and "match window" in bad.json()["detail"]
    finally:
        client.delete(f"/api/v1/lines/{line['id']}", headers=admin_headers)


def test_line_1_keeps_working_with_a_joined_camera(lines):
    SettingsPersistenceService.save_line({"id": PRIMARY_LINE_ID, "name": "Line 1", "cameras": [
        {"camera_id": "top", "counting": True, "model_id": "model-test"},
        {"model_id": "model-test", **_joined("side", 50, 50)},
    ]})
    lines.apply_state()
    runtime = lines.get(PRIMARY_LINE_ID)
    assert runtime.counter is counting_service and counting_service.event_sink is not None
    sent = []
    counting_service.dispatch_event = lambda payload, plc_event, is_defect: sent.append(payload)
    try:
        async def steps():
            _cross(counting_service, "top")
            await asyncio.sleep(0.03)
            _cross(runtime.aux_counters["side"], "side")
            await asyncio.sleep(0.02)

        _run(steps)
    finally:
        del counting_service.dispatch_event
    (payload,) = sent
    assert payload["result"] == "PASSED" and _station(payload, "side")["result"] == "good"
