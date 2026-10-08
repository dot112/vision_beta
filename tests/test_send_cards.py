"""Send cards: what a line reports to other systems, and when.

A card has one channel, a trigger and condition (the same check as a PLC
action card) and the contents of its message.
"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import threading
import time

import pytest

import app.services.line_service as line_module
import app.services.settings_persistence_service as persistence_module
from app.services import send_dispatcher_service as send_module
from app.services.card_triggers import card_fires, line_state_event, normalize_trigger
from app.services.counting_service import CountingService, _telemetry_dispatcher, counting_service
from app.services.line_config import (
    OLD_SEND_KEYS,
    PRIMARY_LINE_ID,
    SCHEMA_VERSION,
    normalize_send_card,
    normalize_send_cards,
    send_cards_from_old_settings,
    upgrade_state,
)
from app.services.line_service import LineManager
from app.services.plc_dispatcher_service import PLCDispatcherService, _normalize_card
from app.services.send_dispatcher_service import MESSAGE_FIELDS, SendDispatcherService, build_message, sample_payload
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

API = "/api/v1/send"

# ── Events, as the line runtime makes them ────────────────────────────────────

GOOD = {"event_type": "crossing", "line_id": "line-1", "camera_id": "vis", "counting_camera": True, "result": "good",
        "good_count": 5, "reject_count": 1, "detected_classes": ["bottle"]}
REJECT = {**GOOD, "result": "reject", "reject_count": 2, "detected_classes": ["defect"]}
GOOD_WITH_CODE = {**GOOD, "qr_code": "SKU-1", "qr_status": "known", "qr_paired": True}
GOOD_NO_CODE = {**GOOD, "qr_code": None, "qr_status": "no_read", "qr_paired": False}
CODE_KNOWN = {"event_type": "qr_read", "line_id": "line-1", "camera_id": "qr", "qr_code": "SKU-1", "qr_status": "known"}
CODE_UNKNOWN = {**CODE_KNOWN, "qr_code": "X", "qr_status": "unknown"}
NO_CODE = {**CODE_KNOWN, "qr_code": None, "qr_status": "no_read"}
SECOND_CAMERA = {**GOOD, "camera_id": "vis-2", "counting_camera": False}
STARTED = line_state_event("line-1", "Line 1", True)
STOPPED = line_state_event("line-1", "Line 1", False)
EVENTS = {"good": GOOD, "reject": REJECT, "good+code": GOOD_WITH_CODE, "good, no code": GOOD_NO_CODE, "code known": CODE_KNOWN,
          "code unknown": CODE_UNKNOWN, "no code": NO_CODE, "second camera": SECOND_CAMERA, "started": STARTED, "stopped": STOPPED}

PAYLOAD = {
    "event": "WIRELINE_OBJECT_CROSSED", "timestamp": "2026-10-05T10:00:00+00:00", "track_id": 7, "class_name": "bottle",
    "result": "PASSED", "is_defect": False, "reject_reason": None, "vision_result": "PASSED", "confidence": 0.95,
    "bbox": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}, "counts": {"bottle": 1},
    "metrics": {"total_inspected": 1}, "total_inspected": 1, "line_id": "line-1", "line_name": "Line 1", "camera_id": "vis",
}


def _fires(card, names=EVENTS):
    card = normalize_trigger({"line_id": "line-1", **card})
    return [name for name, event in names.items() if card_fires(card, event)]


# ── One trigger check for PLC cards and send cards ────────────────────────────

@pytest.mark.parametrize("card", [
    {"trigger": "cross_line", "condition": "reject"},
    {"trigger": "cross_line", "condition": "good"},
    {"trigger": "cross_line", "condition": "any"},
    {"trigger": "cross_line", "condition": "class", "set_value": "defect"},
    {"trigger": "good_counter", "condition": "counter_reach", "set_value": "5"},
    {"trigger": "reject_counter", "condition": "counter_modulo", "set_value": "2"},
    {"trigger": "class", "condition": "class_present", "set_value": "bottle"},
    {"trigger": "qr_read", "condition": "any"},
    {"trigger": "qr_known", "condition": "any"},
    {"trigger": "qr_unknown", "condition": "any"},
    {"trigger": "qr_no_read", "condition": "any"},
    {"trigger": "line_state", "condition": "on"},
    {"trigger": "line_state", "condition": "toggled"},
    {"trigger": "cross_line", "condition": "any", "camera_id": "vis-2"},
])
def test_a_plc_card_and_a_send_card_fire_for_the_same_events(card):
    plc = _normalize_card({"id": "p", "line_id": "line-1", **card, "delay_ms": 400, "operation": "pulse"})
    send = normalize_trigger({"line_id": "line-1", **normalize_send_card({"id": "s", **card})})
    for name, event in EVENTS.items():
        assert card_fires(plc, event) == card_fires(send, event), name


def test_what_each_trigger_fires_for():
    assert _fires({"trigger": "cross_line", "condition": "reject"}) == ["reject"]
    assert _fires({"trigger": "cross_line", "condition": "good"}) == ["good", "good+code", "good, no code"]
    assert _fires({"trigger": "cross_line", "condition": "any"}) == ["good", "reject", "good+code", "good, no code"]
    # A code card fires for a code read on its own and for a product's event that carries its code...
    assert _fires({"trigger": "qr_read", "condition": "any"}) == ["good+code", "code known", "code unknown"]
    assert _fires({"trigger": "qr_no_read", "condition": "any"}) == ["good, no code", "no code"]
    # ...and with "unpaired" only for codes that were not paired with a product.
    assert _fires({"trigger": "qr_read", "condition": "unpaired"}) == ["code known", "code unknown"]
    assert _fires({"trigger": "qr_known", "condition": "unpaired"}) == ["code known"]
    assert _fires({"trigger": "qr_no_read", "condition": "unpaired"}) == ["no code"]
    # A code that is itself the product (a line with only a reader that checks codes) is not "paired".
    alone = {"event_type": "code_product", "line_id": "line-1", "camera_id": "qr", "result": "reject", "qr_code": "X", "qr_status": "unknown"}
    assert _fires({"trigger": "qr_unknown", "condition": "unpaired"}, {"reader alone": alone}) == ["reader alone"]
    assert _fires({"trigger": "cross_line", "condition": "reject"}, {"reader alone": alone}) == ["reader alone"]
    assert _fires({"trigger": "line_state", "condition": "on"}) == ["started"]
    assert _fires({"trigger": "line_state", "condition": "off"}) == ["stopped"]
    # A second vision camera only fires cards that name it.
    assert _fires({"trigger": "cross_line", "condition": "any", "camera_id": "vis-2"}) == ["second camera"]
    # Another line's events never fire a card.
    assert _fires({"trigger": "cross_line", "condition": "any", "line_id": "line-2"}) == []


# ── Card validation and message contents ──────────────────────────────────────

def test_send_card_validation():
    card = normalize_send_card({"name": "  To MES ", "endpoint_id": "tcp-1", "fields": ["event", "event", " result "]})
    assert card["id"].startswith("send_") and card["name"] == "To MES"
    assert (card["enabled"], card["trigger"], card["condition"], card["fields"]) == (True, "cross_line", "any", ["event", "result"])
    assert "delay_ms" not in normalize_send_card({"delay_ms": 400, "operation": "pulse", "target_address": "Q0.0"})  # PLC-only parts
    assert normalize_send_card({"fields": []})["fields"] is None  # nothing picked = everything
    assert normalize_send_card({"trigger": "alarm", "alarm_codes": "camera.disconnected, *"})["alarm_codes"] == ["camera.disconnected", "*"]
    for bad in ({"trigger": "sometimes"}, {"fields": "event"}, {"fields": [1]}, {"id": "no spaces"}, {"enabled": "yes"}, "card"):
        with pytest.raises(ValueError):
            normalize_send_card(bad)
    with pytest.raises(ValueError, match="same id"):
        normalize_send_cards([{"id": "a"}, {"id": "a"}])
    with pytest.raises(ValueError, match="at most"):
        normalize_send_cards([{} for _ in range(40)])


def test_a_message_holds_the_fields_its_card_picked():
    assert build_message({"fields": None}, PAYLOAD) is PAYLOAD
    assert build_message({"fields": ["event", "result", "reject_reason"]}, PAYLOAD) == {
        "event": "WIRELINE_OBJECT_CROSSED", "result": "PASSED", "reject_reason": None}
    # "line" and "qr" stand for several keys of the message.
    assert build_message({"fields": ["line"]}, PAYLOAD) == {"line_id": "line-1", "line_name": "Line 1"}
    read = {"event": "QR_CODE_READ", "code": "SKU-1", "format": "EAN13", "known": True, "product_name": "Widget", "camera_id": "qr"}
    assert build_message({"fields": ["qr"]}, read) == {"code": "SKU-1", "format": "EAN13", "known": True, "product_name": "Widget"}
    synced = {**PAYLOAD, "qr_code": "SKU-1", "qr_status": "known", "product_name": "Widget"}
    assert build_message({"fields": ["track_id", "qr"]}, synced) == {
        "track_id": 7, "qr_code": "SKU-1", "qr_status": "known", "product_name": "Widget"}
    # None of those names is a key of a message, so an old field list is never read as one of them.
    assert build_message({"fields": ["code"]}, read) == {"code": "SKU-1"}
    assert not any(name in keys for name, keys in MESSAGE_FIELDS.items() if len(keys) > 1 for keys in MESSAGE_FIELDS.values())
    # A field list from before send cards names message keys directly and means the same.
    assert build_message({"fields": ["event", "track_id", "confidence"]}, PAYLOAD) == {
        "event": "WIRELINE_OBJECT_CROSSED", "track_id": 7, "confidence": 0.95}
    # Fields that do not occur in this kind of event: the whole event, not an empty message.
    assert build_message({"fields": ["alarm"]}, PAYLOAD) is PAYLOAD
    assert {"line_state", "reject_reason"} <= set(MESSAGE_FIELDS)


def test_example_messages_for_the_test_button():
    assert sample_payload({"trigger": "cross_line", "condition": "reject"})["reject_reason"] == "vision_class"
    assert sample_payload({"trigger": "cross_line", "condition": "good"})["result"] == "PASSED"
    assert sample_payload({"trigger": "qr_unknown"})["qr_status"] == "unknown"
    assert sample_payload({"trigger": "qr_no_read"})["event"] == "QR_CODE_NO_READ"
    assert sample_payload({"trigger": "line_state", "condition": "off"})["line_state"] == "stopped"
    assert sample_payload({"trigger": "alarm", "condition": "raised"})["alarm_raised"] is True


# ── The upgrade: old settings become cards that send the same ─────────────────

CHANNELS = [
    {"id": "m1", "name": "Broker", "protocol": "mqtt", "enabled": True, "topic": "plant/line"},
    {"id": "t1", "name": "MES", "protocol": "tcp", "enabled": True, "host": "10.0.0.5", "port": 9000},
    {"id": "t2", "name": "SCADA", "protocol": "tcp", "enabled": False, "host": "10.0.0.6", "port": 9000},
    {"id": "w1", "name": "Hook", "protocol": "webhook", "enabled": True, "url": "https://example.test/hook"},
    {"id": "p1", "name": "PLC", "protocol": "plc", "enabled": True, "host": "10.0.0.9", "port": 502},
]


def _cards(trigger, channels=CHANNELS, **state):
    state = {"communication_endpoints": copy.deepcopy(channels), **state}
    cards = send_cards_from_old_settings(trigger, state)
    return cards, state


def _summary(cards):
    return [(c["endpoint_id"], c["trigger"], c["condition"], c["fields"]) for c in cards]


def test_each_protocol_that_was_switched_on_becomes_a_card():
    cards, _ = _cards({
        "send_mqtt": True, "mqtt_endpoint_id": "m1", "mqtt_dispatch_trigger": "passed", "mqtt_dispatched_fields": ["event", "track_id", "result"],
        "send_tcp": True, "tcp_endpoint_id": "all", "tcp_dispatch_trigger": "rejected", "tcp_dispatched_fields": ["event", "track_id", "confidence"],
        "send_webhook": False, "webhook_endpoint_id": "w1",
        "dispatch_trigger": "both", "dispatched_fields": ["event"],
    })
    assert _summary(cards) == [
        ("m1", "cross_line", "good", ["event", "track_id", "result"]),
        # "All TCP channels": one card per channel, also the one that is switched off (it sends once it is on).
        ("t1", "cross_line", "reject", ["event", "track_id", "confidence"]),
        ("t2", "cross_line", "reject", ["event", "track_id", "confidence"]),
    ]
    assert [c["name"] for c in cards] == ["Results to Broker", "Results to MES", "Results to SCADA"]
    assert len({c["id"] for c in cards}) == 3 and all(c["enabled"] for c in cards)
    assert normalize_send_cards(cards) == [{k: v for k, v in c.items()} for c in normalize_send_cards(cards)]

    # The cards send what the settings sent: passed products to MQTT with three fields, rejects to TCP.
    mqtt, tcp = normalize_trigger(cards[0]), normalize_trigger(cards[1])
    passed, rejected = {**GOOD, "line_id": PRIMARY_LINE_ID}, {**REJECT, "line_id": PRIMARY_LINE_ID}
    assert card_fires(mqtt, passed) and not card_fires(mqtt, rejected)
    assert card_fires(tcp, rejected) and not card_fires(tcp, passed)
    assert set(build_message(mqtt, PAYLOAD)) == {"event", "track_id", "result"}
    assert set(build_message(tcp, PAYLOAD)) == {"event", "track_id", "confidence"}


def test_default_trigger_and_fields_fall_back_as_they_did():
    # No per-protocol values: the shared ones apply; no field list: the whole message.
    cards, _ = _cards({"send_webhook": True, "webhook_endpoint_id": "w1", "dispatch_trigger": "rejected"})
    assert _summary(cards) == [("w1", "cross_line", "reject", None)]
    cards, _ = _cards({"send_webhook": True, "webhook_endpoint_id": "w1", "webhook_dispatched_fields": []})
    assert _summary(cards) == [("w1", "cross_line", "any", None)]
    # A channel that was chosen and has since been deleted: nothing was sent, and no card is made.
    assert _cards({"send_tcp": True, "tcp_endpoint_id": "gone"})[0] == []
    assert _cards({"send_mqtt": False, "send_tcp": False})[0] == []


def test_code_read_settings_become_code_cards():
    cards, _ = _cards({"mqtt_qr_dispatch": "all", "mqtt_endpoint_id": "m1", "tcp_qr_dispatch": "unknown", "tcp_endpoint_id": "t1",
                       "webhook_qr_dispatch": "off"})
    assert _summary(cards) == [
        ("m1", "qr_read", "unpaired", None),
        ("m1", "qr_no_read", "unpaired", None),   # only "every read" also sent the missing codes
        ("t1", "qr_unknown", "unpaired", None),
    ]
    by_event = {name: [c["endpoint_id"] for c in cards if card_fires(normalize_trigger({**c, "line_id": "line-1"}), event)]
                for name, event in EVENTS.items()}
    assert by_event["code known"] == ["m1"]
    assert by_event["code unknown"] == ["m1", "t1"]
    assert by_event["no code"] == ["m1"]
    # As before, a code paired with a product is not sent a second time on its own.
    assert by_event["good+code"] == [] and by_event["good, no code"] == [] and by_event["good"] == []


def test_addresses_version_1_kept_in_the_settings_become_channels():
    cards, state = _cards({"send_tcp": True, "tcp_host": "10.1.1.1", "tcp_port": 9100,
                           "send_webhook": True, "webhook_url": "https://old.example/hook",
                           "send_mqtt": True, "mqtt_topic": "old/topic"}, channels=[],
                          mqtt={"host": "broker.local", "port": 1883})
    made = {ep["id"]: ep for ep in state["communication_endpoints"]}
    assert (made["tcp-from-settings"]["host"], made["tcp-from-settings"]["port"]) == ("10.1.1.1", 9100)
    assert made["webhook-from-settings"]["url"] == "https://old.example/hook"
    assert (made["mqtt-from-settings"]["host"], made["mqtt-from-settings"]["topic"]) == ("broker.local", "old/topic")
    assert sorted(c["endpoint_id"] for c in cards) == ["mqtt-from-settings", "tcp-from-settings", "webhook-from-settings"]
    # Without an address there was nothing to send to: no channel and no card.
    cards, state = _cards({"send_tcp": True, "send_mqtt": True, "send_webhook": True}, channels=[])
    assert cards == [] and state["communication_endpoints"] == []
    # "All channels" with no channel of that kind sent nothing either.
    cards, state = _cards({"send_tcp": True, "tcp_endpoint_id": "all", "tcp_host": "10.1.1.1", "tcp_port": 9100}, channels=[])
    assert cards == [] and state["communication_endpoints"] == []


def test_upgrade_moves_every_lines_settings_to_cards():
    state = {
        "schema_version": 4, "camera_auto_connect": False, "communication_endpoints": copy.deepcopy(CHANNELS),
        "action_trigger": {"line1_position": 0.3, "expected_classes": ["can"], "send_tcp": True, "tcp_endpoint_id": "t1",
                           "tcp_host": "127.0.0.1", "tcp_port": 9000, "mqtt_qr_dispatch": "known", "mqtt_endpoint_id": "all"},
        "plc_actions": [{"id": "gate"}],
        "lines": [
            {"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "cameras": []},
            {"id": "line-2", "name": "Line 2", "enabled": True, "cameras": [],
             "action_trigger": {"line1_position": 0.4, "send_webhook": True, "webhook_endpoint_id": "w1",
                                "webhook_dispatch_trigger": "rejected"}, "plc_actions": []},
            {"id": "line-3", "name": "Line 3", "enabled": True, "cameras": []},
        ],
    }
    assert upgrade_state(state) and state["schema_version"] == SCHEMA_VERSION
    assert _summary(state["send_actions"]) == [("m1", "qr_known", "unpaired", None), ("t1", "cross_line", "any", None)]
    assert _summary(state["lines"][1]["send_actions"]) == [("w1", "cross_line", "reject", None)]
    assert state["lines"][2]["send_actions"] == [] and "send_actions" not in state["lines"][0]
    # The count lines stay; the old send settings are gone (and, since version 6, the
    # class lists: these lines have no vision camera to carry them).
    assert state["action_trigger"] == {"line1_position": 0.3}
    assert state["lines"][1]["action_trigger"] == {"line1_position": 0.4}
    assert state["plc_actions"] == [{"id": "gate"}]
    assert not any(key in state["action_trigger"] for key in OLD_SEND_KEYS)
    # A second load changes nothing.
    again = copy.deepcopy(state)
    assert upgrade_state(state) is False and state == again


# ── Sending ───────────────────────────────────────────────────────────────────

class _Listener:
    """A local TCP server that keeps the JSON lines it receives."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.messages = []
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.server.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            with conn:
                data = b""
                conn.settimeout(2)
                try:
                    while chunk := conn.recv(65536):
                        data += chunk
                except OSError:
                    pass
            self.messages += [json.loads(line) for line in data.decode().splitlines() if line.strip()]

    def wait(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while len(self.messages) < count and time.time() < deadline:
            time.sleep(0.02)
        return self.messages

    def close(self):
        self._stop = True
        self.thread.join(timeout=2)
        self.server.close()


@pytest.fixture
def lines(monkeypatch, tmp_path):
    """Throwaway settings file, line manager and send dispatcher."""
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
    yield manager
    counting_service.event_sink = None
    counting_service.update_config(saved_config)
    counting_service.reset_counts()


@pytest.fixture
def listeners():
    made = []

    def make():
        made.append(_Listener())
        return made[-1]

    yield make
    for listener in made:
        listener.close()


def _channel(state_id, listener, name):
    SettingsPersistenceService._state.setdefault("communication_endpoints", []).append(
        {"id": state_id, "name": name, "protocol": "tcp", "enabled": True, "host": "127.0.0.1", "port": listener.port})


def _set_cards(cards, line_id=PRIMARY_LINE_ID):
    saved = SettingsPersistenceService.replace_line_send_actions(line_id, cards)
    SendDispatcherService.set_cards(saved, line_id=line_id)
    return saved


def _drain():
    assert _telemetry_dispatcher._drained.wait(timeout=5.0)


class _CrossingTracker:
    def __init__(self, is_defect=False):
        self.is_defect = is_defect
        self.objects, self._recently_counted, self.next_id = {}, set(), 1

    def update(self, **kwargs):
        self.next_id += 1
        return [(self.next_id, "defect" if self.is_defect else "bottle", self.is_defect, 0.9)]


def _cross(counter, camera_id="vis", is_defect=False):
    counter._trackers[camera_id] = _CrossingTracker(is_defect)
    counter.process_frame([], 640, 480, camera_rotation=90, camera_id=camera_id)


def _with_loop(steps):
    async def main():
        CountingService.set_event_loop(asyncio.get_running_loop())
        try:
            await steps()
        finally:
            CountingService.set_event_loop(None)

    asyncio.run(main())


def test_two_cards_send_their_own_message_to_their_own_channel(lines, listeners):
    mes, scada = listeners(), listeners()
    _channel("tcp-mes", mes, "MES")
    _channel("tcp-scada", scada, "SCADA")
    _set_cards([
        {"id": "all", "name": "Every product to MES", "endpoint_id": "tcp-mes", "trigger": "cross_line", "condition": "any",
         "fields": ["event", "line", "result", "reject_reason", "metrics"]},
        {"id": "rejects", "name": "Rejects to SCADA", "endpoint_id": "tcp-scada", "trigger": "cross_line", "condition": "reject",
         "fields": ["track_id", "class_name", "line_state"]},
        {"id": "off", "name": "Switched off", "enabled": False, "endpoint_id": "tcp-scada", "trigger": "cross_line", "condition": "any"},
    ])

    async def steps():
        _cross(counting_service)                   # a good product
        _cross(counting_service, is_defect=True)   # a reject
        await asyncio.sleep(0.05)

    _with_loop(steps)
    assert len(mes.wait(2)) == 2 and len(scada.wait(1)) == 1
    _drain()
    assert [(m["event"], m["line_id"], m["result"], m["reject_reason"], m["metrics"]["total_inspected"]) for m in mes.messages] == [
        ("WIRELINE_OBJECT_CROSSED", "line-1", "PASSED", None, 1),
        ("WIRELINE_OBJECT_CROSSED", "line-1", "REJECTED", "vision_class", 2),
    ]
    assert all(set(m) == {"event", "line_id", "line_name", "result", "reject_reason", "metrics"} for m in mes.messages)
    assert scada.messages == [{"track_id": scada.messages[0]["track_id"], "class_name": "defect", "line_state": "started"}]

    status = SendDispatcherService.get_status(PRIMARY_LINE_ID)
    assert status["all"]["sent_count"] == 2 and status["rejects"]["sent_count"] == 1 and status["off"] == {}
    assert status["all"]["last_result"]["success"] is True


def test_a_reject_by_code_goes_to_the_reject_card_with_its_reason(lines, listeners, monkeypatch):
    from app.services.product_service import product_catalog

    monkeypatch.setattr(product_catalog, "_lists", {"list-1": {"LISTED": {"code": "LISTED", "name": "Widget"}}})
    monkeypatch.setattr(product_catalog, "_names", {"list-1": "List 1"})
    rejects, codes = listeners(), listeners()
    line = SettingsPersistenceService.save_line({"name": "Checked", "enabled": True, "sync": {"window_ms": 60}, "cameras": [
        {"camera_id": "vis", "model_id": "model-test"},
        {"camera_id": "qr", "role": "qr", "product_list_id": "list-1", "qr_action": "accept_listed"},
    ]})
    lines.apply_state()
    _channel("tcp-rejects", rejects, "Rejects")
    _channel("tcp-codes", codes, "Codes")
    _set_cards([
        {"id": "r", "endpoint_id": "tcp-rejects", "trigger": "cross_line", "condition": "reject", "fields": ["result", "reject_reason", "qr"]},
        {"id": "c", "endpoint_id": "tcp-codes", "trigger": "qr_read", "condition": "any", "fields": ["qr", "result"]},
    ], line_id=line["id"])
    runtime = lines.get(line["id"])

    async def steps():
        runtime.on_qr_read("qr", "LISTED", "EAN13")
        await asyncio.sleep(0.01)
        _cross(runtime.counter)          # good product, listed code: good
        await asyncio.sleep(0.01)
        runtime.on_qr_read("qr", "OTHER", "EAN13")
        await asyncio.sleep(0.01)
        _cross(runtime.counter)          # good product, code not in the list: reject
        await asyncio.sleep(0.05)

    _with_loop(steps)
    assert rejects.wait(1) == [{"result": "REJECTED", "reject_reason": "code_not_in_list",
                                "qr_code": "OTHER", "qr_format": "EAN13", "qr_status": "unknown", "qr_paired": True, "product_name": None}]
    # The code card fires once per product, with the product's final result.
    assert [(m["qr_code"], m["result"]) for m in codes.wait(2)] == [("LISTED", "PASSED"), ("OTHER", "REJECTED")]
    _drain()


def test_a_code_read_card_fires_for_a_reader_without_sync(lines, listeners):
    codes = listeners()
    line = SettingsPersistenceService.save_line({"name": "Reader", "enabled": True, "cameras": [{"camera_id": "qr", "role": "qr"}]})
    lines.apply_state()
    _channel("tcp-codes", codes, "Codes")
    _set_cards([{"id": "c", "endpoint_id": "tcp-codes", "trigger": "qr_unknown", "condition": "any"}], line_id=line["id"])
    runtime = lines.get(line["id"])

    async def steps():
        runtime.on_qr_read("qr", "ABC-1", "QR_CODE")
        await asyncio.sleep(0.05)

    _with_loop(steps)
    (message,) = codes.wait(1)
    assert (message["event"], message["code"], message["qr_status"], message["line_name"]) == ("QR_CODE_READ", "ABC-1", "unknown", "Reader")
    assert message["line_state"] == "started"
    _drain()


def test_line_start_and_stop_send_their_cards(lines, listeners):
    target = listeners()
    _channel("tcp-state", target, "State")
    _set_cards([
        {"id": "s", "endpoint_id": "tcp-state", "trigger": "line_state", "condition": "toggled", "fields": ["event", "line", "line_state"]},
        {"id": "p", "endpoint_id": "tcp-state", "trigger": "cross_line", "condition": "any"},
    ])
    assert SendDispatcherService.line_state_changed(line_state_event(PRIMARY_LINE_ID, "Line 1", False)) == 1
    assert SendDispatcherService.line_state_changed(line_state_event(PRIMARY_LINE_ID, "Line 1", True)) == 1
    assert target.wait(2) == [
        {"event": "LINE_STOPPED", "line_id": "line-1", "line_name": "Line 1", "line_state": "stopped"},
        {"event": "LINE_STARTED", "line_id": "line-1", "line_name": "Line 1", "line_state": "started"},
    ]
    _drain()


def test_an_alarm_sends_its_card(lines, listeners):
    target = listeners()
    _channel("tcp-alarm", target, "Alarms")
    _set_cards([{"id": "a", "endpoint_id": "tcp-alarm", "trigger": "alarm", "condition": "raised",
                 "alarm_codes": ["camera.disconnected"], "fields": ["event", "alarm", "line"]}])
    alarm = {"id": "x1", "code": "camera.disconnected", "source": "camera:c1", "severity": "critical",
             "message": "Camera lost", "details": {"line_id": PRIMARY_LINE_ID}}
    SendDispatcherService._on_alarm(alarm, raised=True)
    SendDispatcherService._on_alarm({**alarm, "code": "plc.action_failed"}, raised=True)   # not picked
    SendDispatcherService._on_alarm(alarm, raised=False)                                    # the card is for "raised"
    (message,) = target.wait(1)
    assert message == {"event": "ALARM_RAISED", "alarm_code": "camera.disconnected", "alarm_message": "Camera lost",
                       "alarm_severity": "critical", "alarm_source": "camera:c1", "alarm_raised": True,
                       "line_id": "line-1", "line_name": "Line 1"}
    _drain()
    assert len(target.messages) == 1


def test_a_card_whose_channel_is_off_or_gone_sends_nothing_and_says_why(lines, listeners):
    target = listeners()
    _channel("tcp-off", target, "Off")
    SettingsPersistenceService._state["communication_endpoints"][-1]["enabled"] = False
    SettingsPersistenceService._state["communication_endpoints"].append({"id": "plc-1", "name": "PLC", "protocol": "plc", "enabled": True})
    _set_cards([
        {"id": "off", "endpoint_id": "tcp-off"}, {"id": "gone", "endpoint_id": "tcp-deleted"},
        {"id": "none", "endpoint_id": ""}, {"id": "plc", "endpoint_id": "plc-1"},
    ])
    assert SendDispatcherService.evaluate({**GOOD, "line_id": PRIMARY_LINE_ID}, dict(PAYLOAD)) == 0
    _drain()
    status = SendDispatcherService.get_status()
    assert target.messages == []
    assert "switched off" in status["off"]["last_result"]["message"]
    assert "no longer exists" in status["gone"]["last_result"]["message"]
    assert "No channel" in status["none"]["last_result"]["message"]
    assert "not an MQTT, TCP or webhook" in status["plc"]["last_result"]["message"]
    assert all(entry["sent_count"] == 0 for entry in status.values())


def test_a_second_vision_camera_only_sends_cards_that_name_it(lines, listeners):
    target = listeners()
    line = SettingsPersistenceService.save_line({"name": "Two cameras", "enabled": True, "cameras": [
        {"camera_id": "vis", "counting": True, "model_id": "model-test"}, {"camera_id": "vis-2", "model_id": "model-test"}]})
    lines.apply_state()
    _channel("tcp-two", target, "Two")
    _set_cards([
        {"id": "line", "endpoint_id": "tcp-two", "trigger": "cross_line", "condition": "any", "fields": ["camera_id"]},
        {"id": "second", "endpoint_id": "tcp-two", "trigger": "cross_line", "condition": "any", "camera_id": "vis-2", "fields": ["camera_id", "event"]},
    ], line_id=line["id"])
    runtime = lines.get(line["id"])

    async def steps():
        _cross(runtime.counter, "vis")
        _cross(runtime.counter_for("vis-2"), "vis-2")
        await asyncio.sleep(0.05)

    _with_loop(steps)
    assert sorted(target.wait(2), key=len) == [{"camera_id": "vis"}, {"camera_id": "vis-2", "event": "WIRELINE_OBJECT_CROSSED"}]
    _drain()


def test_old_send_settings_sent_by_an_older_client_are_not_kept(lines):
    SettingsPersistenceService.update_settings({"action_trigger": {"line1_position": 0.25, "send_mqtt": True, "tcp_qr_dispatch": "all"}})
    assert SettingsPersistenceService.get_state()["action_trigger"]["line1_position"] == 0.25
    assert not any(key in SettingsPersistenceService.get_state()["action_trigger"] for key in OLD_SEND_KEYS)
    line = SettingsPersistenceService.save_line({"name": "Other", "action_trigger": {"line2_position": 0.7, "send_webhook": True}})
    assert SettingsPersistenceService.get_line(line["id"])["action_trigger"] == {"line2_position": 0.7}


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def api_channels(client, admin_headers, listeners):
    """Two TCP channels with listeners, and a PLC channel; removed afterwards."""
    made = {}

    def add(name, protocol="tcp", **fields):
        res = client.post("/api/v1/system/endpoints", json={"name": name, "protocol": protocol, **fields}, headers=admin_headers)
        assert res.status_code == 200, res.text
        made[name] = res.json()["id"]
        return res.json()["id"]

    first, second = listeners(), listeners()
    add("Send test MES", host="127.0.0.1", port=first.port)
    add("Send test SCADA", host="127.0.0.1", port=second.port)
    add("Send test PLC", protocol="plc", host="127.0.0.1", port=502)
    yield made, first, second
    client.post(f"{API}/actions/batch", json=[], headers=admin_headers)
    for endpoint_id in made.values():
        client.delete(f"/api/v1/system/endpoints/{endpoint_id}", headers=admin_headers)


def test_send_cards_api(client, admin_headers, api_channels):
    channels, mes, scada = api_channels
    fields = client.get(f"{API}/fields", headers=admin_headers).json()["fields"]
    assert {"line_state", "reject_reason"} <= {f["id"] for f in fields} and all(f["label"] and f["keys"] for f in fields)

    cards = [
        {"id": "send_api_1", "name": "Rejects to MES", "endpoint_id": channels["Send test MES"], "trigger": "cross_line",
         "condition": "reject", "fields": ["event", "result", "reject_reason"], "delay_ms": 300},
        {"id": "send_api_2", "name": "Codes to SCADA", "endpoint_id": channels["Send test SCADA"], "trigger": "qr_read", "condition": "any"},
    ]
    saved = client.post(f"{API}/actions/batch", json=cards, headers=admin_headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] == 2 and "delay_ms" not in saved.json()["cards"][0]
    listed = client.get(f"{API}/actions", headers=admin_headers).json()
    assert [(c["id"], c["name"], c["sent_count"], c["last_sent_at"]) for c in listed] == [
        ("send_api_1", "Rejects to MES", 0, None), ("send_api_2", "Codes to SCADA", 0, None)]
    # The cards are part of the line, and other lines have their own.
    assert [c["id"] for c in client.get(f"/api/v1/lines/{PRIMARY_LINE_ID}", headers=admin_headers).json()["send_actions"]] == ["send_api_1", "send_api_2"]
    assert client.get(f"{API}/actions?line_id=nope", headers=admin_headers).status_code == 404
    used = {e["name"]: e["used_by"] for e in client.get("/api/v1/system/endpoints", headers=admin_headers).json()}
    assert used["Send test MES"] == ["Line 1"] and used["Send test PLC"] == []

    # Test: each card sends its own message to its own channel, once, marked as a test.
    first = client.post(f"{API}/actions/send_api_1/test", json={}, headers=admin_headers)
    assert first.status_code == 200 and first.json()["success"] is True, first.text
    assert mes.wait(1) == [{"event": "WIRELINE_OBJECT_CROSSED", "result": "REJECTED", "reject_reason": "vision_class", "test": True}]
    # A card can be tried before it is applied: the card in the request is the one sent.
    draft = {**cards[1], "trigger": "line_state", "condition": "off", "fields": ["event", "line_state"]}
    second = client.post(f"{API}/actions/send_api_2/test", json={"card": draft}, headers=admin_headers)
    assert second.json()["success"] is True
    assert scada.wait(1) == [{"event": "LINE_STOPPED", "line_state": "stopped", "test": True}]
    assert len(mes.messages) == 1
    assert client.post(f"{API}/actions/send_missing/test", json={}, headers=admin_headers).status_code == 404

    # A card's channel must be one a message can be sent to, and must exist.
    for bad_channel, word in ((channels["Send test PLC"], "not an MQTT, TCP or webhook"), ("tcp-nope", "does not exist")):
        bad = client.post(f"{API}/actions/batch", json=[{**cards[0], "endpoint_id": bad_channel}], headers=admin_headers)
        assert bad.status_code == 422 and word in bad.json()["detail"], bad.text
    bad = client.post(f"{API}/actions/batch", json=[{**cards[0], "camera_id": "not-on-the-line"}], headers=admin_headers)
    assert bad.status_code == 422 and "camera" in bad.json()["detail"]
    bad = client.post(f"{API}/actions/batch", json=[{**cards[0], "trigger": "sometimes"}], headers=admin_headers)
    assert bad.status_code == 422
    assert len(client.get(f"{API}/actions", headers=admin_headers).json()) == 2  # refused saves change nothing

    # A channel a card sends to cannot be deleted.
    refused = client.delete(f"/api/v1/system/endpoints/{channels['Send test MES']}", headers=admin_headers)
    assert refused.status_code == 409 and "Line 1" in refused.json()["detail"]


def test_a_line_copy_gets_its_own_copies_of_the_send_cards(client, admin_headers, api_channels, vision_model):
    channels, _, _ = api_channels
    line = client.post("/api/v1/lines", json={"name": "Send source", "cameras": [{"camera_id": "send-cam", "model_id": vision_model}], "send_actions": [
        {"id": "send_src", "name": "Results", "endpoint_id": channels["Send test MES"], "camera_id": "send-cam"}]}, headers=admin_headers)
    assert line.status_code == 201, line.text
    copy_ = client.post(f"/api/v1/lines/{line.json()['id']}/clone", json={"name": "Send copy"}, headers=admin_headers)
    try:
        assert copy_.status_code == 201, copy_.text
        (card,) = copy_.json()["send_actions"]
        assert card["id"] != "send_src" and card["name"] == "Results" and card["endpoint_id"] == channels["Send test MES"]
        assert "camera_id" not in card  # cameras are not copied
        scoped = client.get(f"{API}/actions?line_id={copy_.json()['id']}", headers=admin_headers).json()
        assert [c["id"] for c in scoped] == [card["id"]]
        # A line cannot be saved with a card that names another line's camera.
        bad = client.put(f"/api/v1/lines/{copy_.json()['id']}", json={"send_actions": [{**card, "camera_id": "send-cam"}]}, headers=admin_headers)
        assert bad.status_code == 422
    finally:
        for made in (copy_, line):
            if made.status_code == 201:
                client.delete(f"/api/v1/lines/{made.json()['id']}", headers=admin_headers)


def test_api_key_scopes_for_send_cards():
    from app.security.api_key_scopes import required_api_key_scopes

    assert required_api_key_scopes("GET", "/api/v1/send/actions") == {"configuration:read"}
    assert required_api_key_scopes("POST", "/api/v1/send/actions/batch") == {"configuration:write"}
    assert required_api_key_scopes("POST", "/api/v1/send/actions/send_1/test") == {"integrations:operate"}


def test_starting_and_stopping_a_line_sends_its_line_state_cards(client, admin_headers, api_channels):
    channels, mes, _ = api_channels
    line = client.post("/api/v1/lines", json={"name": "Send state", "send_actions": [
        {"id": "send_state", "endpoint_id": channels["Send test MES"], "trigger": "line_state", "condition": "toggled",
         "fields": ["event", "line_state", "line"]}]}, headers=admin_headers).json()
    try:
        assert client.post(f"/api/v1/lines/{line['id']}/start", headers=admin_headers).status_code == 200
        assert client.post(f"/api/v1/lines/{line['id']}/stop", headers=admin_headers).status_code == 200
        assert [(m["event"], m["line_state"], m["line_name"]) for m in mes.wait(2)] == [
            ("LINE_STARTED", "started", "Send state"), ("LINE_STOPPED", "stopped", "Send state")]
    finally:
        client.delete(f"/api/v1/lines/{line['id']}", headers=admin_headers)
    assert send_module.SendDispatcherService.get_status(line["id"]) == {}
