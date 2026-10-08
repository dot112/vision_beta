"""The Sparkplug service: a node for each channel that has Sparkplug on, and every
production line published through it as a device."""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import ANY

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services.counting_service import CountingService, counting_service
from app.services.line_config import upgrade_state
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.send_dispatcher_service import SendDispatcherService
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService
from app.services.sparkplug_service import SparkplugService
from tests.mqtt_broker import Broker
from tests.sparkplug_helpers import channel, decoded, kinds, quiet, until, values, wait_until


@pytest.fixture
def broker():
    made = Broker()
    yield made
    made.close()


def fresh_sparkplug(monkeypatch):
    """The service as it is before anything was started."""
    for name, value in (("_nodes", {}), ("_signatures", {}), ("_channels", {}), ("_last", {}), ("_devices", {}),
                        ("_kept", {}), ("_failed", set()), ("_due", {}), ("_tasks", set()), ("_task", None), ("_change", None),
                        ("_camera_names", {}), ("_names_at", 0.0), ("_names_asked", set())):
        monkeypatch.setattr(SparkplugService, name, value)
    # In a full run the API test client's app is alive in another thread, and its
    # publish loop would give the lines to these tests' nodes at moments of its
    # own. Here a pass happens only when a test asks for one (publish_now).
    monkeypatch.setattr(SparkplugService, "_tick", classmethod(lambda cls: None))
    # No camera table in these tests: a test that needs names sets its own loader.
    monkeypatch.setattr(SparkplugService, "_load_camera_names", staticmethod(_no_camera_names))


async def _no_camera_names():
    return {}


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file and line manager, and a Sparkplug service with no nodes."""
    monkeypatch.setattr(persistence_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(persistence_module, "STATE_FILE", str(tmp_path / "system_state.json"))
    state = copy.deepcopy(DEFAULT_STATE)
    upgrade_state(state)
    monkeypatch.setattr(SettingsPersistenceService, "_state", state)
    monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
    monkeypatch.setattr(PLCDispatcherService, "_cards", [])
    monkeypatch.setattr(PLCDispatcherService, "_states", {})
    monkeypatch.setattr(SendDispatcherService, "_cards", [])
    monkeypatch.setattr(SendDispatcherService, "_status", {})
    manager = LineManager()
    monkeypatch.setattr(line_module, "line_manager", manager)
    manager.apply_state()
    saved_config = counting_service.config
    counting_service.reset_counts()
    fresh_sparkplug(monkeypatch)
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)
    counting_service.reset_counts()


class _CrossingTracker:
    def __init__(self, is_defect=False):
        self.is_defect = is_defect
        self.objects, self._recently_counted, self.next_id = {}, set(), 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, "defect" if self.is_defect else "bottle", self.is_defect, 0.9)]


def cross(counter, is_defect=False):
    """One product crosses the count lines of Line 1's camera."""
    counter._trackers["vis"] = _CrossingTracker(is_defect)
    counter.process_frame([], 640, 480, camera_rotation=90, camera_id="vis")


def run(steps):
    """Run the steps on an event loop the counters know, and stop every node afterwards."""
    async def scenario():
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            await steps()
        finally:
            await SparkplugService.stop()
            CountingService.set_event_loop(None)

    asyncio.run(scenario())


def add_line(manager, name):
    saved = SettingsPersistenceService.save_line({"name": name})
    manager.apply_state()
    return saved["id"]


def test_a_saved_channel_publishes_every_line_and_follows_changes(lines, broker):
    async def steps():
        packing = add_line(lines, "Packing")
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 3)
        assert kinds(broker) == [("NBIRTH", None), ("DBIRTH", "Line 1"), ("DBIRTH", "Packing")]
        birth = values(decoded(broker)[1][1])
        assert {"Line/Running", "Line/Name", "Counts/Inspected", "Last Product/Result", "Alarms/Active Count", "Commands/Reset Counters"} <= set(birth)
        assert (birth["Line/Name"], birth["Counts/Inspected"], birth["Last Product/Result"]) == ("Line 1", 0, "")
        assert SparkplugService.device_of("line-1") == "Line 1" and SparkplugService.line_of("Packing") == packing
        assert SparkplugService.status() == {"m1": {"connected": True, "host_online": None, "devices": 2, "last_birth_at": ANY, "last_error": ""}}

        # A class that is counted for the first time is a new metric: a new birth for that line.
        cross(counting_service)
        SparkplugService.publish_now()
        cross(counting_service, is_defect=True)
        SparkplugService.publish_now()
        await until(lambda: len(decoded(broker)) == 5)
        assert kinds(broker)[3:] == [("DBIRTH", "Line 1"), ("DBIRTH", "Line 1")]
        assert values(decoded(broker)[4][1])["Counts/Class/defect"] == 1

        # After that a product only changes values: one DDATA with what changed.
        cross(counting_service)
        SparkplugService.publish_now()
        await until(lambda: len(decoded(broker)) == 6)
        topic, data = decoded(broker)[5]
        changed = values(data)
        assert (topic.kind, topic.device) == ("DDATA", "Line 1")
        assert (changed["Counts/Inspected"], changed["Counts/Good"], changed["Counts/Class/bottle"]) == (3, 2, 2)
        assert changed["Last Product/Result"] == "PASSED" and changed["Last Product/Class"] == "bottle"
        assert "Line/Name" not in changed and "Counts/Rejected" not in changed
        SparkplugService.publish_now()
        cross_free = await quiet(broker, 0.2)

        # A renamed line is a new device; the old one is gone.
        SettingsPersistenceService.save_line({"id": packing, "name": "Boxing"})
        lines.apply_state()
        SparkplugService.publish_now()
        await until(lambda: ("DBIRTH", "Boxing") in kinds(broker))
        assert [k for k in kinds(broker)[cross_free:] if k[0] in ("DDEATH", "DBIRTH")] == [("DDEATH", "Packing"), ("DBIRTH", "Boxing")]

        # Sparkplug switched off, the channel switched off, the channel deleted: each ends the node with an NDEATH.
        for ending in (lambda: SparkplugService.apply(channel(broker, sparkplug_enabled=False)),
                       lambda: SparkplugService.apply(channel(broker, enabled=False)),
                       lambda: SparkplugService.drop("m1")):
            before = kinds(broker).count(("NDEATH", None))
            ending()
            await until(lambda: kinds(broker).count(("NDEATH", None)) == before + 1 and SparkplugService.status() == {},
                        why=lambda: (before, kinds(broker)[-6:], SparkplugService.status(), len(broker.connects)))
            SparkplugService.apply(channel(broker))
            await until(lambda: "m1" in SparkplugService.status() and SparkplugService.status()["m1"]["devices"] == 2)

    run(steps)


def test_a_changed_channel_gets_a_new_node(lines, broker):
    async def steps():
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 2)
        SparkplugService.apply(channel(broker, sparkplug_node_id="N2"))
        await until(lambda: len(decoded(broker)) == 5)
        topics = [(t.kind, t.node, t.device) for t, _ in decoded(broker)]
        assert topics[2:] == [("NDEATH", "N", None), ("NBIRTH", "N2", None), ("DBIRTH", "N2", "Line 1")]
        assert len(broker.connects) == 2

        # A new name, the commands switch and the interval do not need a new connection.
        SparkplugService.apply(channel(broker, sparkplug_node_id="N2", name="Renamed", sparkplug_allow_commands=True,
                                       sparkplug_interval_ms=500, birth_topic="plant/status"))
        await asyncio.sleep(0.5)
        node = SparkplugService._nodes["m1"]
        assert len(broker.connects) == 2 and (node.allow_commands, node.interval) == (True, 0.5)

    run(steps)


def test_the_event_hook_never_raises_and_keeps_the_latest(lines, broker):
    async def steps():
        SparkplugService.note_event(None)
        SparkplugService.note_event("text")
        SparkplugService.note_event({"event": "WIRELINE_OBJECT_CROSSED"})   # no line
        SparkplugService.note_event({"line_id": "line-1", "event": "QR_CODE_READ", "code": "A", "qr_status": "known"})
        SparkplugService.note_event({"line_id": "line-1", "event": "QR_CODE_READ", "code": "B", "qr_status": "unknown"})
        SparkplugService.note_event({"line_id": "line-1", "event": "LINE_STARTED"})
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 2)
        birth = values(decoded(broker)[1][1])
        assert (birth["Last Code/Text"], birth["Last Code/Status"], birth["Last Product/Result"]) == ("B", "unknown", "")

    run(steps)


def test_a_line_that_cannot_be_read_keeps_its_last_values(lines, broker, monkeypatch):
    async def steps():
        add_line(lines, "Packing")
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 3)
        summary = lines.summary

        def broken(runtime):
            if runtime.name == "Packing":
                raise RuntimeError("camera removed at this moment")
            return summary(runtime)

        monkeypatch.setattr(lines, "summary", broken)
        SparkplugService.publish_now()
        cross(counting_service)
        SparkplugService.publish_now()
        await until(lambda: len(decoded(broker)) == 4)
        assert kinds(broker)[3] == ("DBIRTH", "Line 1")          # the other line is still published
        assert await quiet(broker, 0.2) == 4 and not any(kind == "DDEATH" for kind, _ in kinds(broker))
        assert SparkplugService.status()["m1"]["devices"] == 2   # Packing is still a device

    run(steps)


def test_a_camera_tag_has_the_cameras_name_whether_or_not_it_is_connected(lines, broker, monkeypatch):
    # A camera that is not connected has no driver to ask for its name. The tag
    # must not be named after the camera's id until it connects: a host would
    # then keep two tags for one camera.
    async def names():
        return {"cam-top": "Top camera"}

    async def steps():
        monkeypatch.setattr(SparkplugService, "_load_camera_names", staticmethod(names))
        SettingsPersistenceService.save_line({"id": "line-1", "name": "Line 1", "cameras": [
            {"camera_id": "cam-top", "role": "qr"}, {"camera_id": "cam-unlisted", "role": "qr"}]})
        lines.apply_state()
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 2)
        birth = values(decoded(broker)[1][1])
        assert birth["Cameras/Top camera/Connected"] is False and "Cameras/cam-top/Connected" not in birth
        assert birth["Cameras/cam-unlisted/Connected"] is False      # not in the camera list: its id is all there is

    run(steps)


def test_class_tags_are_named_as_the_counter_names_its_classes(lines, broker):
    # A model's class names are kept as the model writes them ("Bottle"); the
    # counter counts them in lower case. The tag listed at birth has to be the
    # one the counts arrive on.
    async def steps():
        SettingsPersistenceService.save_line({"id": "line-1", "name": "Line 1", "cameras": [
            {"camera_id": "vis", "role": "vision", "counting": True, "model_id": "model-x", "expected_classes": ["Bottle", " Can "],
             "defect_classes": ["NG"]}]})
        lines.apply_state()
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 2)
        birth = values(decoded(broker)[1][1])
        assert (birth["Counts/Class/bottle"], birth["Counts/Class/can"], birth["Counts/Class/ng"]) == (0, 0, 0)
        assert not any(name in birth for name in ("Counts/Class/Bottle", "Counts/Class/NG", "Counts/Class/Can"))
        counting_service.count_product("Bottle", reject=False)
        counting_service.count_product("Bottle", reject=False)
        SparkplugService.publish_now()
        await until(lambda: len(decoded(broker)) == 3)
        topic, data = decoded(broker)[2]
        assert topic.kind == "DDATA" and values(data)["Counts/Class/bottle"] == 2      # data on the tag of the birth, no new birth

    run(steps)


def test_a_camera_list_that_cannot_be_read_does_not_stop_publishing(lines, broker, monkeypatch):
    async def failing():
        raise RuntimeError("database is busy")

    async def steps():
        monkeypatch.setattr(SparkplugService, "_load_camera_names", staticmethod(failing))
        SparkplugService.apply(channel(broker))
        await until(lambda: len(decoded(broker)) == 2)
        assert kinds(broker) == [("NBIRTH", None), ("DBIRTH", "Line 1")]

    run(steps)


def test_health_and_the_channel_test_report_the_node(client, admin_headers, broker):
    saved = client.post("/api/v1/system/endpoints", headers=admin_headers, json={
        "name": "Spb health broker", "protocol": "mqtt", "host": "127.0.0.1", "port": broker.port, "client_id": "vision_health",
        "sparkplug_enabled": True, "sparkplug_group_id": "G", "sparkplug_node_id": "HealthNode"})
    assert saved.status_code == 200, saved.text
    channel_id = saved.json()["id"]
    try:
        wait_until(lambda: any(kind == "DBIRTH" for kind, _ in kinds(broker)))
        health = client.get("/api/v1/telemetry/health", headers=admin_headers).json()
        entry = next(c for c in health["components"]["mqtt"]["channels"] if c["id"] == channel_id)
        assert entry["sparkplug"] == {"connected": True, "host_online": None, "devices": ANY, "last_birth_at": ANY, "last_error": ""}
        assert entry["sparkplug"]["devices"] >= 1
        # The channel list carries the same state, for the channel's card on the dashboard.
        listed = {e["id"]: e for e in client.get("/api/v1/system/endpoints", headers=admin_headers).json()}
        assert listed[channel_id]["sparkplug_state"] == {**entry["sparkplug"], "devices": ANY}
        assert all("sparkplug_state" not in e for e in listed.values() if e["id"] != channel_id)
        tested =client.post(f"/api/v1/system/endpoints/{channel_id}/test", headers=admin_headers)
        assert tested.status_code == 200 and "Sparkplug B: " in tested.json()["message"] and "line(s) published" in tested.json()["message"]
    finally:
        client.delete(f"/api/v1/system/endpoints/{channel_id}", headers=admin_headers)
    wait_until(lambda: kinds(broker)[-1] == ("NDEATH", None))
    assert decoded(broker)[-1][0].node == "HealthNode"
    health = client.get("/api/v1/telemetry/health", headers=admin_headers).json()
    assert all(c["id"] != channel_id for c in health["components"]["mqtt"]["channels"])
