"""MQTT channels: a channel under Connections is a broker, and each send card names its own topic.

Every channel has its own broker connection, made from all of the channel's
settings. The connection tests run against the small broker of tests/mqtt_broker.py, so
they need nothing installed.
"""
from __future__ import annotations

import asyncio

import pytest

import app.services.mqtt_service as mqtt_module
from app.hardware.mqtt.client import MQTTClient
from app.services.line_config import PRIMARY_LINE_ID, SCHEMA_VERSION, normalize_send_card, upgrade_state
from app.services.mqtt_service import MQTTChannels
from app.services.send_dispatcher_service import deliver
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.mqtt_broker import Broker

API = "/api/v1/send"


# ── The topic is part of the card ─────────────────────────────────────────────

def test_a_card_keeps_its_topic_and_refuses_one_that_cannot_be_published_to():
    assert normalize_send_card({"topic": "  plant/line1/results "})["topic"] == "plant/line1/results"
    assert normalize_send_card({})["topic"] == ""  # a TCP or webhook card has none
    for bad in ("plant/#", "plant/+/results", "a" * 513, "plant/\x00x"):
        with pytest.raises(ValueError, match="topic"):
            normalize_send_card({"topic": bad})


def test_upgrade_gives_each_card_the_topic_of_its_channel():
    state = {
        "schema_version": 6, "camera_auto_connect": False,
        "mqtt": {"host": "10.0.0.5", "port": 1883, "auto_connect": True},
        "communication_endpoints": [
            {"id": "m1", "name": "Plant broker", "protocol": "mqtt", "enabled": True, "host": "10.0.0.5", "port": 1883, "topic": "plant/results"},
            {"id": "t1", "name": "MES", "protocol": "tcp", "enabled": True, "host": "10.0.0.6", "port": 9000, "topic": "not/used"},
        ],
        "send_actions": [{"id": "a", "endpoint_id": "m1"}, {"id": "b", "endpoint_id": "t1"},
                         {"id": "c", "endpoint_id": "m1", "topic": "already/set"}],
        "lines": [
            {"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "cameras": []},
            {"id": "line-2", "name": "Line 2", "enabled": True, "cameras": [], "send_actions": [{"id": "d", "endpoint_id": "m1"}]},
        ],
    }
    assert upgrade_state(state) and state["schema_version"] == SCHEMA_VERSION
    assert [card.get("topic") for card in state["send_actions"]] == ["plant/results", None, "already/set"]
    assert state["lines"][1]["send_actions"][0]["topic"] == "plant/results"
    # The connection that was kept apart from the channels no longer opens for a channel's broker.
    assert state["mqtt"]["auto_connect"] is False


# ── Each channel publishes through its own connection ─────────────────────────

class _FakeClient:
    made = []

    def __init__(self, endpoint):
        self.endpoint = dict(endpoint)
        self.host, self.port = endpoint.get("host"), endpoint.get("port")
        self.is_connected, self.last_error, self.closed, self.sent = True, "", False, []
        self.on_state_change = None
        _FakeClient.made.append(self)

    async def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, qos, retain))
        return self.is_connected

    async def connect(self):
        return self.is_connected

    def close(self):
        self.closed = True

    async def disconnect(self):
        self.closed = True


@pytest.fixture
def fake_clients(monkeypatch):
    _FakeClient.made = []
    monkeypatch.setattr(MQTTChannels, "_clients", {})
    monkeypatch.setattr(mqtt_module, "client_for_channel", lambda endpoint, client_id=None: _FakeClient(endpoint))
    monkeypatch.setattr(MQTTChannels, "_close_later", staticmethod(lambda client: client.close()))
    return _FakeClient.made


PLANT = {"id": "m-plant", "name": "Plant broker", "protocol": "mqtt", "enabled": True, "host": "10.0.0.5", "port": 1883, "qos": 1, "retain": False}
CLOUD = {"id": "m-cloud", "name": "Cloud broker", "protocol": "mqtt", "enabled": True, "host": "broker.example", "port": 8883, "qos": 0, "retain": True}


def test_cards_publish_to_their_own_topic_on_their_own_broker(fake_clients):
    async def scenario():
        return [
            await deliver(PLANT, {"n": 1}, "plant/line1/results"),
            await deliver(PLANT, {"n": 2}, "plant/line1/rejects"),
            await deliver(CLOUD, {"n": 3}, "site/a/results"),
            await deliver(PLANT, {"n": 4}, ""),
        ]

    results = asyncio.run(scenario())
    assert [ok for ok, _ in results] == [True, True, True, False]
    assert results[0][1] == "Published to plant/line1/results" and "topic" in results[3][1]
    assert len(fake_clients) == 2  # one connection per broker, shared by the cards on it
    plant, cloud = fake_clients
    # Each message goes out with its channel's QoS and retain.
    assert plant.sent == [("plant/line1/results", {"n": 1}, 1, False), ("plant/line1/rejects", {"n": 2}, 1, False)]
    assert cloud.sent == [("site/a/results", {"n": 3}, 0, True)]


def test_a_changed_channel_gets_a_new_connection_and_a_removed_one_is_closed(fake_clients):
    first = MQTTChannels.client(PLANT)
    assert MQTTChannels.client({**PLANT, "name": "Renamed", "qos": 2}) is first  # nothing about the connection changed
    second = MQTTChannels.client({**PLANT, "host": "10.0.0.9"})
    assert second is not first and first.closed and not second.closed

    MQTTChannels.apply({**PLANT, "host": "10.0.0.9", "enabled": False})  # switched off
    assert second.closed and MQTTChannels.status() == {}

    MQTTChannels.client(PLANT), MQTTChannels.client(CLOUD)
    MQTTChannels.sync([CLOUD])  # the plant channel was deleted
    assert set(MQTTChannels.status()) == {"m-cloud"} and MQTTChannels.any_connected()


def test_a_broker_that_is_down_is_named_in_the_cards_result(fake_clients):
    async def scenario():
        client = MQTTChannels.client(PLANT)
        client.is_connected, client.last_error = False, "the broker refused the connection: Not authorized"
        return await deliver(PLANT, {"n": 1}, "plant/results")

    ok, detail = asyncio.run(scenario())
    assert not ok and "10.0.0.5:1883" in detail and "Not authorized" in detail


# ── A real connection, to a small broker ──────────────────────────────────────

@pytest.fixture
def brokers(monkeypatch):
    monkeypatch.setattr(MQTTChannels, "_clients", {})
    made = []

    def make(**login):
        made.append(Broker(**login))
        return made[-1]

    yield make
    asyncio.run(MQTTChannels.close_all())
    for broker in made:
        broker.close()


def _channel(channel_id, broker, **settings):
    return {"id": channel_id, "name": channel_id, "protocol": "mqtt", "enabled": True, "host": "127.0.0.1",
            "port": broker.port, "client_id": f"vision_{channel_id}", "qos": 0, "retain": False, **settings}


@pytest.mark.parametrize("version, level", [("3.1.1", 4), ("5.0", 5)])
def test_a_channel_connects_and_publishes_in_both_mqtt_versions(brokers, version, level):
    broker = brokers()
    channel = _channel("m1", broker, protocol_version=version, qos=1, retain=True)
    ok, detail = asyncio.run(deliver(channel, {"result": "PASSED"}, "plant/line1/results"))
    assert ok, detail
    assert [{k: m[k] for k in ("topic", "payload", "qos", "retain")} for m in broker.wait(1)] == [
        {"topic": "plant/line1/results", "payload": {"result": "PASSED"}, "qos": 1, "retain": True}]
    assert [(c["level"], c["client_id"], c["ok"]) for c in broker.connects] == [(level, "vision_m1", True)]
    assert MQTTChannels.status()["m1"]["connected"] is True


def test_two_channels_are_two_brokers_at_once(brokers):
    plant, scada = brokers(), brokers(username="vision", password="secret")
    a = _channel("plant", plant)
    b = _channel("scada", scada, username="vision", password="secret", protocol_version="5.0")

    async def scenario():
        return [await deliver(a, {"n": 1}, "plant/results"), await deliver(b, {"n": 2}, "scada/results"),
                await deliver(a, {"n": 3}, "plant/rejects")]

    assert all(ok for ok, _ in asyncio.run(scenario()))
    assert [m["topic"] for m in plant.wait(2)] == ["plant/results", "plant/rejects"]
    assert [m["topic"] for m in scada.wait(1)] == ["scada/results"]


def test_a_wrong_password_is_reported_with_the_brokers_reason(brokers):
    broker = brokers(username="vision", password="secret")
    for version, word in (("3.1.1", "Bad user name or password"), ("5.0", "Bad user name or password")):
        channel = _channel(f"m-{version}", broker, username="vision", password="wrong", protocol_version=version)
        ok, detail = asyncio.run(deliver(channel, {"n": 1}, "plant/results"))
        assert not ok and word in detail, detail
    assert broker.messages == []


def test_the_channel_test_does_not_take_the_channels_own_connection(brokers, monkeypatch):
    broker = brokers()
    channel = _channel("m1", broker, will_topic="plant/vision/status", will_payload='{"status": "offline"}')
    monkeypatch.setattr(SettingsPersistenceService, "_state", {"communication_endpoints": [channel]})

    async def scenario():
        before = await SettingsPersistenceService._probe_endpoint("m1")   # nothing connected yet: a test connection
        sent = await deliver(channel, {"n": 1}, "plant/results")          # the channel's own connection
        after = await SettingsPersistenceService._probe_endpoint("m1")    # answered from that connection
        return before, sent, after

    before, sent, after = asyncio.run(scenario())
    assert before["success"] and sent[0] and after["success"], (before, sent, after)
    # A broker drops a client when another connects with the same id, so the test used its own.
    assert [c["client_id"] for c in broker.connects] == ["vision_m1_test", "vision_m1"]


def test_a_plain_client_reconnects_after_the_broker_comes_back():
    broker = Broker()
    client = MQTTClient(host="127.0.0.1", port=broker.port, client_id="vision_retry", keepalive=5)

    async def scenario():
        assert await client.publish("plant/results", {"n": 1})
        port = broker.port
        broker.close()
        # paho notices the closed socket and retries on its own, 1 s after the first failure.
        again = await asyncio.to_thread(Broker, port=port)
        try:
            for _ in range(150):
                await asyncio.sleep(0.1)
                if again.connects and client.is_connected and await client.publish("plant/results", {"n": 2}):
                    break
            return again.wait(1)
        finally:
            await client.disconnect()
            again.close()

    assert [m["payload"] for m in asyncio.run(scenario())][-1:] == [{"n": 2}]


# ── API ───────────────────────────────────────────────────────────────────────

def test_a_card_on_an_mqtt_channel_needs_a_topic(client, admin_headers):
    # The channel is saved without a topic: it is only the broker.
    res = client.post("/api/v1/system/endpoints", json={"name": "Topic test broker", "protocol": "mqtt", "host": "127.0.0.1",
                                                        "port": 1, "enabled": False, "qos": 0}, headers=admin_headers)
    assert res.status_code == 200, res.text
    channel = res.json()
    assert channel.get("topic") == "" and channel["qos"] == 0
    try:
        card = {"id": "send_mqtt_1", "name": "Results to broker", "endpoint_id": channel["id"], "trigger": "cross_line", "condition": "any"}
        missing = client.post(f"{API}/actions/batch", json=[card], headers=admin_headers)
        assert missing.status_code == 422 and "topic" in missing.json()["detail"], missing.text
        wild = client.post(f"{API}/actions/batch", json=[{**card, "topic": "plant/#"}], headers=admin_headers)
        assert wild.status_code == 422 and "wildcard" in wild.json()["detail"], wild.text
        # A card that is switched off can wait for its topic.
        assert client.post(f"{API}/actions/batch", json=[{**card, "enabled": False}], headers=admin_headers).status_code == 200
        saved = client.post(f"{API}/actions/batch", json=[{**card, "topic": "plant/line1/results"}], headers=admin_headers)
        assert saved.status_code == 200 and saved.json()["cards"][0]["topic"] == "plant/line1/results", saved.text
        listed = client.get(f"{API}/actions", headers=admin_headers).json()
        assert [c["topic"] for c in listed] == ["plant/line1/results"]
        # The channel is switched off, so the test says so instead of connecting.
        tried = client.post(f"{API}/actions/send_mqtt_1/test", json={}, headers=admin_headers).json()
        assert tried["success"] is False and "switched off" in tried["message"]
    finally:
        client.post(f"{API}/actions/batch", json=[], headers=admin_headers)
        client.delete(f"/api/v1/system/endpoints/{channel['id']}", headers=admin_headers)
