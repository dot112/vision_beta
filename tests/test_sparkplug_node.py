"""The Sparkplug B edge node: one channel's connection, births and deaths, sequence
numbers, changed values, rebirth, host state and the commands it hands on."""
from __future__ import annotations

import asyncio
import json
import socket
import time
from unittest.mock import ANY

import pytest

from app.hardware.mqtt.sparkplug_codec import DataType, Metric, decode_payload, encode_payload
from app.services.sparkplug_service import BdSeqStore, SparkplugNode
from tests.mqtt_broker import Broker
from tests.sparkplug_helpers import channel, decoded, kinds, quiet, until, will_bdseq

LINE = {"Line 1": [Metric("Line/Running", DataType.Boolean, True), Metric("Counts/Good", DataType.Int64, 1)]}


@pytest.fixture
def broker():
    made = Broker()
    yield made
    made.close()


def run(broker, tmp_path, steps, got=None, **settings):
    """Start a node on the broker, run the steps with it, and always stop it."""
    async def scenario():
        on_command = (lambda node, device, metric: got.append((node, device, metric))) if got is not None else None
        node = SparkplugNode(channel(broker, **settings), BdSeqStore(tmp_path / "bdseq.json"), on_command=on_command)
        await node.start()
        try:
            await steps(node)
        finally:
            await node.stop()

    asyncio.run(scenario())


def test_connect_then_births(broker, tmp_path):
    async def steps(node):
        await until(lambda: kinds(broker) == [("NBIRTH", None)])
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2)
        connect = broker.connects[0]
        assert connect["client_id"] == "vision_m1-spb"
        # Short, whatever the channel says: a broker notices a server that lost
        # power after one and a half times this.
        assert connect["keepalive"] == 15
        will = decode_payload(connect["will"]["payload"])
        assert connect["will"]["topic"] == "spBv1.0/G/NDEATH/N" and connect["will"]["qos"] == 1 and will.seq is None
        assert connect["will"]["retain"] is False and [m.name for m in will.metrics] == ["bdSeq"]
        assert {("vision_m1-spb", "spBv1.0/G/NCMD/N", 1), ("vision_m1-spb", "spBv1.0/G/DCMD/N/#", 1)} <= set(broker.subscriptions)
        (t1, nbirth), (t2, dbirth) = decoded(broker)
        assert (t1.group, t1.node, t1.kind, nbirth.seq) == ("G", "N", "NBIRTH", 0)
        assert (t2.kind, t2.device, dbirth.seq) == ("DBIRTH", "Line 1", 1)
        assert nbirth.metrics[0] == Metric("bdSeq", DataType.Int64, will.metrics[0].value, timestamp=ANY)   # the will's bdSeq
        assert [m.name for m in nbirth.metrics] == ["bdSeq", "Node Control/Rebirth", "Node Info/Software Version"]
        assert [m.name for m in dbirth.metrics] == ["Line/Running", "Counts/Good"]
        assert all(m.timestamp for m in dbirth.metrics) and dbirth.timestamp and nbirth.timestamp
        # Fixed by the specification: births and data at QoS 0, not retained.
        assert all((m["qos"], m["retain"]) == (0, False) for m in broker.messages)
        # The node listens for commands before it announces itself.
        order = [kind for kind, _ in broker.log]
        assert order.index("subscribe") < order.index("publish")
        assert node.status() == {"connected": True, "host_online": None, "devices": 1, "last_birth_at": ANY, "last_error": ""}

    run(broker, tmp_path, steps)


def test_only_changed_metrics_are_sent_and_seq_wraps(broker, tmp_path):
    async def steps(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2)
        for good in range(2, 302):
            node.publish_lines({"Line 1": [Metric("Line/Running", DataType.Boolean, True), Metric("Counts/Good", DataType.Int64, good)]})
        await until(lambda: len(decoded(broker)) == 302)
        data = decoded(broker)[2:]
        assert all(t.kind == "DDATA" and t.device == "Line 1" and [m.name for m in p.metrics] == ["Counts/Good"] for t, p in data)
        assert [p.metrics[0].value for _, p in data] == list(range(2, 302))
        assert [p.seq for _, p in decoded(broker)] == [n % 256 for n in range(302)]
        # Nothing changed: nothing is sent.
        node.publish_lines({"Line 1": [Metric("Line/Running", DataType.Boolean, True), Metric("Counts/Good", DataType.Int64, 301)]})
        assert await quiet(broker) == 302

    run(broker, tmp_path, steps)


def test_lines_come_go_and_gain_metrics(broker, tmp_path):
    second = [Metric("Line/Running", DataType.Boolean, False)]

    async def steps(node):
        await until(lambda: kinds(broker) == [("NBIRTH", None)])   # connected: each call from here is sent
        node.publish_lines(LINE)
        node.publish_lines({**LINE, "Line 2": second})
        node.publish_lines({"Line 1": [*LINE["Line 1"], Metric("Counts/Class/can", DataType.Int64, 0)], "Line 2": second})
        node.publish_lines({"Line 1": [*LINE["Line 1"], Metric("Counts/Class/can", DataType.Int64, 0)]})
        await until(lambda: len(decoded(broker)) == 5)
        assert kinds(broker) == [("NBIRTH", None), ("DBIRTH", "Line 1"), ("DBIRTH", "Line 2"), ("DBIRTH", "Line 1"), ("DDEATH", "Line 2")]
        assert [m.name for m in decoded(broker)[3][1].metrics] == ["Line/Running", "Counts/Good", "Counts/Class/can"]
        assert node.status()["devices"] == 1
        # A metric that is gone needs a new birth as well.
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 6)
        assert kinds(broker)[-1] == ("DBIRTH", "Line 1") and len(decoded(broker)[-1][1].metrics) == 2

    run(broker, tmp_path, steps)


def test_rebirth_request(broker, tmp_path):
    async def steps(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2 and len(broker.subscriptions) == 2)
        broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.Boolean, True)], timestamp=1))
        await until(lambda: len(decoded(broker)) == 4)
        (t1, first), _, (t3, again), (t4, device) = decoded(broker)
        assert (t3.kind, again.seq, t4.kind, t4.device, device.seq) == ("NBIRTH", 0, "DBIRTH", "Line 1", 1)
        assert again.metrics[0].value == first.metrics[0].value          # the same bdSeq: it is the same connection
        assert len(broker.connects) == 1
        # What is not a rebirth request is ignored, and the node keeps working.
        broker.publish("spBv1.0/G/NCMD/N", b"\xff\xff")
        broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.String, "true")], timestamp=1))
        broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.Boolean, False)], timestamp=1))
        broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Reboot", DataType.Boolean, True)], timestamp=1))
        assert await quiet(broker) == 4
        node.publish_lines({"Line 1": [Metric("Line/Running", DataType.Boolean, True), Metric("Counts/Good", DataType.Int64, 2)]})
        await until(lambda: len(decoded(broker)) == 5)
        assert kinds(broker)[-1] == ("DDATA", "Line 1")

    run(broker, tmp_path, steps)


def test_a_rebirth_request_without_a_datatype_is_answered(broker, tmp_path):
    async def steps(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2 and len(broker.subscriptions) == 2)
        name = b"Node Control/Rebirth"
        broker.publish("spBv1.0/G/NCMD/N", bytes([0x12, 2 + len(name) + 2, 0x0A, len(name)]) + name + bytes([0x70, 0x01]))
        await until(lambda: len(decoded(broker)) == 4)
        assert kinds(broker)[2:] == [("NBIRTH", None), ("DBIRTH", "Line 1")]

    run(broker, tmp_path, steps)


def test_one_command_message_cannot_flood_the_server(broker, tmp_path):
    from app.services.sparkplug_service import MAX_COMMAND_METRICS

    got = []

    async def steps(node):
        await until(lambda: len(broker.subscriptions) == 2)
        topic = "spBv1.0/G/DCMD/N/Line 1"
        # The same tag a thousand times: the last value, once.
        same = [Metric("Line/Running", DataType.Boolean, n % 2 == 0) for n in range(1000)]
        broker.publish(topic, encode_payload(same, timestamp=1))
        await until(lambda: got)
        await asyncio.sleep(0.3)
        assert got == [(node, "Line 1", Metric("Line/Running", DataType.Boolean, False))]
        # Many tags in one message: only so many are looked at.
        got.clear()
        broker.publish(topic, encode_payload([Metric(f"Tag {n}", DataType.Boolean, True) for n in range(100)], timestamp=1))
        await until(lambda: got)
        await asyncio.sleep(0.3)
        assert len(got) == MAX_COMMAND_METRICS == 16
        # A message far larger than any command is not read at all.
        got.clear()
        broker.publish(topic, encode_payload([Metric("Line/Running", DataType.String, "x" * 70000)], timestamp=1))
        broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.String, "x" * 70000),
                                                             Metric("Node Control/Rebirth", DataType.Boolean, True)], timestamp=1))
        await asyncio.sleep(0.5)
        assert got == [] and not any(kind == "NBIRTH" for kind, _ in kinds(broker)[1:])

    run(broker, tmp_path, steps, got=got)


def test_a_retained_command_is_not_carried_out(broker, tmp_path):
    # Commands are never retained by a host. One that was (a test tool with
    # "retain" ticked) would stop the line again after every reconnect.
    got = []

    async def steps(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2 and len(broker.subscriptions) == 2)
        command = encode_payload([Metric("Line/Running", DataType.Boolean, False)], timestamp=1)
        broker.publish("spBv1.0/G/DCMD/N/Line 1", command, retain=True)
        broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.Boolean, True)], timestamp=1), retain=True)
        assert await quiet(broker) == 2 and got == []
        broker.publish("spBv1.0/G/DCMD/N/Line 1", command)
        await until(lambda: got)

    run(broker, tmp_path, steps, got=got)


def test_device_commands_reach_the_callback(broker, tmp_path):
    got = []

    async def steps(node):
        await until(lambda: len(broker.subscriptions) == 2)
        broker.publish("spBv1.0/G/DCMD/N/Line 1", encode_payload([Metric("Line/Running", DataType.Boolean, False)], timestamp=1))
        broker.publish("spBv1.0/G/DCMD/N/Line 1", b"not a payload")
        broker.publish("spBv1.0/OtherGroup/DCMD/N/Line 1", encode_payload([Metric("Line/Running", DataType.Boolean, False)], timestamp=1))
        await until(lambda: got)
        await asyncio.sleep(0.3)
        assert got == [(node, "Line 1", Metric("Line/Running", DataType.Boolean, False))]

    run(broker, tmp_path, steps, got=got)


def test_resend_sends_the_last_values_again(broker, tmp_path):
    async def steps(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2)
        node.resend("Line 1", ["Line/Running", "No such metric"])
        node.resend("No such line", ["Line/Running"])
        await until(lambda: len(decoded(broker)) == 3)
        topic, payload = decoded(broker)[-1]
        assert (topic.kind, topic.device, payload.seq) == ("DDATA", "Line 1", 2)
        assert payload.metrics == (Metric("Line/Running", DataType.Boolean, True, timestamp=ANY),)
        assert await quiet(broker) == 3

    run(broker, tmp_path, steps)


def test_bdseq_goes_up_on_reconnect_and_survives_a_restart(broker, tmp_path):
    async def first(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2)
        broker.drop_clients()
        # The reconnect births the node and its lines again by itself.
        await until(lambda: len(decoded(broker)) == 4, timeout=20)
        assert kinds(broker)[2:] == [("NBIRTH", None), ("DBIRTH", "Line 1")]
        assert [p.seq for _, p in decoded(broker)[2:]] == [0, 1]
        assert decoded(broker)[2][1].metrics[0].value == 1

    async def second(node):
        await until(lambda: len(broker.connects) == 3 and node.status()["connected"])

    run(broker, tmp_path, first)
    run(broker, tmp_path, second)
    assert [will_bdseq(c) for c in broker.connects] == [0, 1, 2]


def test_a_missing_or_broken_bdseq_file_starts_at_zero(tmp_path):
    path = tmp_path / "data" / "bdseq.json"
    for content in (None, "", "not json", "[1, 2]", '{"m1": "x"}', '{"m1": true}', '{"m1": 999}'):
        if path.exists():
            path.unlink()
        if content is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        assert BdSeqStore(path).next("m1") == 0, content
        assert json.loads(path.read_text(encoding="utf-8")) == {"m1": 0}
    assert BdSeqStore(path).next("m1") == 1 and BdSeqStore(path).next("m2") == 0
    path.write_text('{"m1": 255}', encoding="utf-8")
    assert BdSeqStore(path).next("m1") == 0


def test_primary_host(broker, tmp_path):
    async def steps(node):
        mine = "vision_m1-spb"
        await until(lambda: {(mine, "spBv1.0/STATE/ign", 1), (mine, "STATE/ign", 1)} <= set(broker.subscriptions))
        node.publish_lines(LINE)
        assert await quiet(broker) == 0 and node.status()["host_online"] is False
        broker.publish("spBv1.0/STATE/ign", b'{"online": true, "timestamp": 2000}')
        await until(lambda: len(decoded(broker)) == 2)
        assert kinds(broker) == [("NBIRTH", None), ("DBIRTH", "Line 1")] and node.status()["host_online"] is True
        # Older than the online message: a late copy of an earlier state, ignored.
        broker.publish("spBv1.0/STATE/ign", b'{"online": false, "timestamp": 1000}')
        broker.publish("spBv1.0/STATE/ign", b"not json")
        assert await quiet(broker) == 2 and len(broker.connects) == 1
        # The host went offline: say goodbye, connect again and wait for it.
        broker.publish("spBv1.0/STATE/ign", b'{"online": false, "timestamp": 3000}')
        await until(lambda: len(broker.connects) == 2 and broker.subscriptions.count((mine, "STATE/ign", 1)) == 2, timeout=20)
        assert kinds(broker) == [("NBIRTH", None), ("DBIRTH", "Line 1"), ("NDEATH", None)]
        assert decoded(broker)[-1][1].metrics[0].value == will_bdseq(broker.connects[0])
        assert will_bdseq(broker.connects[1]) == will_bdseq(broker.connects[0]) + 1
        assert await quiet(broker) == 3 and node.status()["host_online"] is False
        # Said twice (the host's own message and its will): still one reconnect.
        broker.publish("spBv1.0/STATE/ign", b'{"online": false, "timestamp": 3001}')
        assert await quiet(broker) == 3 and len(broker.connects) == 2
        broker.publish("spBv1.0/STATE/ign", b'{"online": true, "timestamp": 4000}')
        await until(lambda: len(decoded(broker)) == 5)
        assert kinds(broker)[3:] == [("NBIRTH", None), ("DBIRTH", "Line 1")] and node.status()["host_online"] is True
        assert decoded(broker)[3][1].metrics[0].value == will_bdseq(broker.connects[1])

    run(broker, tmp_path, steps, sparkplug_host_id="ign", keepalive=60)


def test_primary_host_that_speaks_sparkplug_2(broker, tmp_path):
    async def steps(node):
        mine = "vision_m1-spb"
        await until(lambda: (mine, "STATE/ign", 1) in broker.subscriptions)
        node.publish_lines(LINE)
        broker.publish("STATE/ign", b"ONLINE", retain=True)       # a broker hands the host's last state to a new subscriber
        await until(lambda: len(decoded(broker)) == 2)
        assert kinds(broker) == [("NBIRTH", None), ("DBIRTH", "Line 1")] and node.status()["host_online"] is True
        broker.publish("STATE/ign", b"something else")
        assert await quiet(broker) == 2
        broker.publish("STATE/ign", b"OFFLINE")
        await until(lambda: len(broker.connects) == 2 and broker.subscriptions.count((mine, "STATE/ign", 1)) == 2, timeout=20)
        assert kinds(broker)[-1] == ("NDEATH", None)
        broker.publish("STATE/ign", b"ONLINE")
        await until(lambda: len(decoded(broker)) == 5)
        assert kinds(broker)[3:] == [("NBIRTH", None), ("DBIRTH", "Line 1")]

    run(broker, tmp_path, steps, sparkplug_host_id="ign")


def test_a_leftover_state_message_of_the_old_format_does_not_take_the_node_down(broker, tmp_path):
    # A broker can still hold a retained "OFFLINE" from the time the host spoke
    # Sparkplug 2.2. Next to the host's 3.0 message it would mean: born, dead,
    # reconnect, born, dead, for ever.
    async def steps(node):
        mine = "vision_m1-spb"
        await until(lambda: (mine, "STATE/ign", 1) in broker.subscriptions)
        node.publish_lines(LINE)
        broker.publish("spBv1.0/STATE/ign", b'{"online": true, "timestamp": 2000}', retain=True)
        broker.publish("STATE/ign", b"OFFLINE", retain=True)
        await until(lambda: len(decoded(broker)) == 2)
        assert await quiet(broker, 1.0) == 2 and len(broker.connects) == 1
        assert node.status()["host_online"] is True

    run(broker, tmp_path, steps, sparkplug_host_id="ign")


def test_stop_publishes_ndeath(broker, tmp_path):
    async def steps(node):
        node.publish_lines(LINE)
        await until(lambda: len(decoded(broker)) == 2)

    run(broker, tmp_path, steps)
    topic, payload = decoded(broker)[-1]
    assert (topic.kind, topic.group, topic.node, payload.seq) == ("NDEATH", "G", "N", None)
    assert payload.metrics[0].name == "bdSeq" and payload.metrics[0].value == will_bdseq(broker.connects[0])
    assert broker.messages[-1]["qos"] == 1


def test_a_broker_that_is_down_does_not_hold_start_and_is_found_later(tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    made = []

    async def scenario():
        node = SparkplugNode(channel(port), BdSeqStore(tmp_path / "bdseq.json"))
        started = time.monotonic()
        await node.start()
        assert time.monotonic() - started < 1.0
        try:
            node.publish_lines(LINE)
            await until(lambda: node.status()["last_error"], timeout=20)
            assert node.status()["connected"] is False and node.status()["devices"] == 0
            made.append(await asyncio.to_thread(Broker, port=port))
            await until(lambda: len(decoded(made[0])) >= 2, timeout=40)
            assert kinds(made[0])[:2] == [("NBIRTH", None), ("DBIRTH", "Line 1")]
            assert node.status()["connected"] is True and node.status()["last_error"] == ""
        finally:
            await node.stop()

    try:
        asyncio.run(scenario())
    finally:
        for broker in made:
            broker.close()
