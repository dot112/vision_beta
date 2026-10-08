"""Commands from a Sparkplug host: start and stop a line and reset its counters,
only while the channel allows commands."""
from __future__ import annotations

import time
from unittest.mock import ANY

import pytest

import app.services.line_service as line_module
from app.hardware.mqtt.sparkplug_codec import DataType, Metric, encode_payload
from tests.mqtt_broker import Broker
from tests.sparkplug_helpers import decoded, kinds, values, wait_until

LINE_NAME = "Spb cmd line"
CHANNEL_NAME = "Spb cmd broker"


class Plant:
    """A started line that has counted three products, a channel with Sparkplug on, and the broker."""

    def __init__(self, client, headers, broker, allow_commands):
        self.client, self.headers, self.broker = client, headers, broker
        self.line_id = client.post("/api/v1/lines", json={"name": LINE_NAME}, headers=headers).json()["id"]
        assert client.post(f"/api/v1/lines/{self.line_id}/start", headers=headers).status_code == 200
        counter = line_module.line_manager.get(self.line_id).counter
        for _ in range(3):
            counter.count_product("bottle", reject=False)
        saved = client.post("/api/v1/system/endpoints", headers=headers, json={
            "name": CHANNEL_NAME, "protocol": "mqtt", "host": "127.0.0.1", "port": broker.port, "client_id": "vision_cmd",
            "sparkplug_enabled": True, "sparkplug_group_id": "G", "sparkplug_node_id": "CmdNode",
            "sparkplug_allow_commands": allow_commands, "sparkplug_interval_ms": 100})
        assert saved.status_code == 200, saved.text
        self.channel_id = saved.json()["id"]
        wait_until(lambda: ("DBIRTH", LINE_NAME) in kinds(broker) and len(broker.subscriptions) >= 2)

    def close(self):
        self.client.delete(f"/api/v1/system/endpoints/{self.channel_id}", headers=self.headers)
        self.client.delete(f"/api/v1/lines/{self.line_id}", headers=self.headers)

    def line(self):
        return self.client.get(f"/api/v1/lines/{self.line_id}", headers=self.headers).json()

    def messages(self, device=LINE_NAME):
        return [(topic, payload) for topic, payload in decoded(self.broker) if topic.device == device]

    def dcmd(self, name, datatype, value, device=LINE_NAME, answered=True):
        """A host writes one tag. Returns the node's answer: the tag's real value, sent again."""
        before = len(self.messages(device))
        self.broker.publish(f"spBv1.0/G/DCMD/CmdNode/{device}", encode_payload([Metric(name, datatype, value)], timestamp=1))
        if not answered:
            time.sleep(0.5)
            return None

        def answer():
            return next((payload for topic, payload in self.messages(device)[before:]
                         if topic.kind == "DDATA" and [m.name for m in payload.metrics] == [name]), None)

        wait_until(lambda: answer() is not None)
        return answer()

    def metric_now(self, name):
        """The tag's value as a host has it: from the latest message that carries it."""
        return next(values(payload)[name] for _, payload in reversed(self.messages()) if name in values(payload))

    def sparkplug_audit(self):
        logs = self.client.get(f"/api/v1/system/audit-logs?line_id={self.line_id}", headers=self.headers).json()
        return [(entry["username"], entry["role"], entry["action"]) for entry in reversed(logs) if entry["username"].startswith("sparkplug:")]


@pytest.fixture
def broker():
    made = Broker()
    yield made
    made.close()


@pytest.fixture
def plant(client, admin_headers, broker, request):
    made = Plant(client, admin_headers, broker, allow_commands=request.param)
    yield made
    made.close()


@pytest.mark.parametrize("plant", [False], indirect=True)
def test_commands_are_ignored_while_the_switch_is_off(plant):
    answer = plant.dcmd("Line/Running", DataType.Boolean, False)
    assert answer.metrics == (Metric("Line/Running", DataType.Boolean, True, timestamp=ANY),)   # the real value, sent back
    plant.dcmd("Commands/Reset Counters", DataType.Boolean, True)
    assert plant.line()["enabled"] is True and plant.line()["status"]["total_inspected"] == 3
    assert plant.sparkplug_audit() == []


@pytest.mark.parametrize("plant", [True], indirect=True)
def test_start_stop_and_reset_with_the_switch_on(plant):
    answer = plant.dcmd("Line/Running", DataType.Boolean, False)
    assert plant.line()["enabled"] is False and values(answer) == {"Line/Running": False}
    assert plant.metric_now("Line/Running") is False
    plant.dcmd("Line/Running", DataType.Boolean, True)
    # The stop can be sent twice (the answer, then the interval's changed value), so the
    # second command's answer may be that repeat: wait for the start to reach the host.
    assert plant.line()["enabled"] is True
    wait_until(lambda: plant.metric_now("Line/Running") is True)

    assert plant.line()["status"]["total_inspected"] == 3
    answer = plant.dcmd("Commands/Reset Counters", DataType.Boolean, True)
    assert plant.line()["status"]["total_inspected"] == 0 and values(answer) == {"Commands/Reset Counters": False}
    wait_until(lambda: plant.metric_now("Counts/Inspected") == 0)

    who = f"sparkplug:{CHANNEL_NAME}"
    assert plant.sparkplug_audit() == [(who, "SPARKPLUG", "STOP_LINE"), (who, "SPARKPLUG", "START_LINE"), (who, "SPARKPLUG", "RESET_COUNTERS")]


@pytest.mark.parametrize("plant", [True], indirect=True)
def test_what_is_not_a_command_changes_nothing(plant):
    plant.dcmd("Line/Running", DataType.String, "true")
    plant.dcmd("Line/Running", DataType.Int32, 0)
    plant.dcmd("Commands/Reset Counters", DataType.Boolean, False)
    plant.dcmd("Counts/Good", DataType.Int64, 0)
    plant.dcmd("No such tag", DataType.Boolean, True, answered=False)
    plant.dcmd("Line/Running", DataType.Boolean, False, device="No such line", answered=False)
    assert plant.line()["enabled"] is True and plant.line()["status"]["total_inspected"] == 3
    assert plant.sparkplug_audit() == []
    # The node is still publishing: a product counted now reaches the host.
    line_module.line_manager.get(plant.line_id).counter.count_product("bottle", reject=False)
    wait_until(lambda: plant.metric_now("Counts/Inspected") == 4)


def test_the_dashboard_routes_still_start_stop_and_reset(client, admin_headers):
    line_id = client.post("/api/v1/lines", json={"name": "Spb route line"}, headers=admin_headers).json()["id"]
    try:
        started = client.post(f"/api/v1/lines/{line_id}/start", headers=admin_headers)
        assert started.status_code == 200 and started.json()["enabled"] is True and started.json()["camera_errors"] == {}
        line_module.line_manager.get(line_id).counter.count_product("bottle", reject=True)
        reset = client.post(f"/api/v1/counting/reset?line_id={line_id}", json={}, headers=admin_headers)
        assert reset.status_code == 200 and reset.json()["total_inspected"] == 0
        stopped = client.post(f"/api/v1/lines/{line_id}/stop", headers=admin_headers)
        assert stopped.status_code == 200 and stopped.json()["enabled"] is False and "camera_errors" not in stopped.json()
        assert client.post("/api/v1/lines/no-such-line/start", headers=admin_headers).status_code == 404
        assert client.post("/api/v1/lines/no-such-line/stop", headers=admin_headers).status_code == 404
        logs = client.get(f"/api/v1/system/audit-logs?line_id={line_id}", headers=admin_headers).json()
        assert [(entry["username"], entry["action"]) for entry in reversed(logs)][-3:] == [
            ("admin", "START_LINE"), ("admin", "RESET_COUNTERS"), ("admin", "STOP_LINE")]
    finally:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)
