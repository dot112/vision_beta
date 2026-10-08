"""The MQTT client's parts that Sparkplug needs: a will that is set again before
every connect, messages handed over as bytes, and subscriptions that keep their QoS."""
from __future__ import annotations

import asyncio

from app.hardware.mqtt.client import MQTTClient
from tests.mqtt_broker import Broker


async def _until(condition, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


def _client(broker, client_id="c1"):
    return MQTTClient(host="127.0.0.1", port=broker.port, client_id=client_id, keepalive=5)


def test_the_will_is_set_again_before_every_connect():
    broker, calls = Broker(), []

    def before(client):
        calls.append(1)
        client.will_topic, client.will_payload, client.will_qos = "t/death", bytes([len(calls)]), 1

    client = _client(broker)
    client.before_connect = before

    async def scenario():
        assert await client.connect()
        broker.drop_clients()
        # paho reconnects by itself, about a second after it notices.
        await _until(lambda: len(broker.connects) == 2 and client.is_connected)
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        broker.close()
    assert [c["will"]["payload"] for c in broker.connects] == [b"\x01", b"\x02"]
    assert broker.connects[0]["will"] == {"topic": "t/death", "payload": b"\x01", "qos": 1, "retain": False}


def test_a_raw_subscription_gets_bytes_and_survives_a_reconnect_at_its_qos():
    broker, got = Broker(), []
    client = _client(broker)

    async def scenario():
        assert await client.connect()
        await client.subscribe_raw("cmd/#", lambda topic, payload: got.append((topic, payload)), qos=1)
        await _until(lambda: ("c1", "cmd/#", 1) in broker.subscriptions)
        broker.publish("cmd/x", b"\xff\x00")
        await _until(lambda: got)
        broker.drop_clients()
        await _until(lambda: broker.subscriptions.count(("c1", "cmd/#", 1)) == 2)
        broker.publish("cmd/y", b"\x01")
        await _until(lambda: len(got) == 2)
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        broker.close()
    assert got == [("cmd/x", b"\xff\x00"), ("cmd/y", b"\x01")]


def test_a_binary_message_does_not_break_json_subscribers():
    broker, json_got, raw_got = Broker(), [], []
    client = _client(broker)

    async def scenario():
        assert await client.connect()
        await client.subscribe("j/#", lambda topic, payload: json_got.append((topic, payload)))
        await client.subscribe_raw("b/#", lambda topic, payload: raw_got.append((topic, payload)))
        await _until(lambda: len(broker.subscriptions) == 2)
        broker.publish("b/x", b"\xff\xfe\x00")
        broker.publish("j/x", b'{"a": 1}')
        await _until(lambda: json_got)
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        broker.close()
    assert json_got == [("j/x", {"a": 1})] and raw_got == [("b/x", b"\xff\xfe\x00")]


def test_bytes_close_message_goes_out_before_the_disconnect():
    broker = Broker()
    client = _client(broker)
    client.close_topic, client.close_payload, client.close_qos = "t/death", b"\x09", 1

    async def scenario():
        assert await client.connect()
        await client.disconnect()

    try:
        asyncio.run(scenario())
        last = broker.wait(1)[-1]
    finally:
        broker.close()
    assert (last["topic"], last["raw"], last["qos"]) == ("t/death", b"\x09", 1)


def test_messages_sent_just_before_the_disconnect_are_not_lost():
    # A socket closed while replies are still unread is reset, and a broker then
    # drops what it had not read yet: the client waits for its goodbye to be
    # acknowledged before it disconnects.
    broker = Broker()
    broker.read_delay = 0.05
    client = _client(broker)
    client.close_topic, client.close_payload, client.close_qos = "t/death", b"bye", 1

    async def scenario():
        assert await client.connect()
        await client.subscribe_raw("cmd/#", lambda topic, payload: None, qos=1)
        for number in range(3):
            assert client.publish_nowait(f"t/{number}", b"x")
        await client.disconnect()

    try:
        asyncio.run(scenario())
        topics = [m["topic"] for m in broker.wait(4, timeout=3.0)]
    finally:
        broker.close()
    assert topics == ["t/0", "t/1", "t/2", "t/death"]


def test_a_subscription_can_refuse_retained_messages():
    # A command that was published with "retain" by mistake would otherwise be
    # delivered, and carried out, again after every reconnect.
    broker, got, kept = Broker(), [], []
    client = _client(broker)

    async def scenario():
        assert await client.connect()
        await client.subscribe_raw("cmd/#", lambda topic, payload: got.append((topic, payload)), qos=1, skip_retained=True)
        await client.subscribe_raw("state/#", lambda topic, payload: kept.append((topic, payload)), qos=1)
        await _until(lambda: len(broker.subscriptions) == 2)
        broker.publish("cmd/x", b"old", retain=True)
        broker.publish("state/x", b"old", retain=True)
        broker.publish("cmd/x", b"new")
        await _until(lambda: got and kept)
        await client.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        broker.close()
    assert got == [("cmd/x", b"new")] and kept == [("state/x", b"old")]
