"""Helpers shared by the Sparkplug tests: a channel record, and reading what a test broker got."""
from __future__ import annotations

import asyncio
import time

from app.hardware.mqtt.sparkplug_codec import decode_payload, parse_topic


def channel(broker_or_port, **settings):
    """An MQTT channel with Sparkplug on (group G, node N), pointing at a test broker."""
    port = broker_or_port if isinstance(broker_or_port, int) else broker_or_port.port
    return {"id": "m1", "name": "Plant broker", "protocol": "mqtt", "enabled": True, "host": "127.0.0.1", "port": port,
            "client_id": "vision_m1", "sparkplug_enabled": True, "sparkplug_group_id": "G", "sparkplug_node_id": "N",
            "sparkplug_host_id": "", "sparkplug_allow_commands": False, "sparkplug_interval_ms": 100, **settings}


def decoded(broker):
    """The Sparkplug messages the broker got from a node, in order: (topic parts, payload)."""
    return [(parse_topic(m["topic"]), decode_payload(m["raw"])) for m in list(broker.messages) if parse_topic(m["topic"])]


def kinds(broker):
    return [(t.kind, t.device) for t, _ in decoded(broker)]


def will_bdseq(connect):
    return decode_payload(connect["will"]["payload"]).metrics[0].value


def values(payload):
    return {m.name: m.value for m in payload.metrics}


async def until(condition, timeout=15.0, why=None):
    """Wait until the condition holds. ``why`` is called on a timeout, for the failure's message."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting: {why() if why else ''}")
        await asyncio.sleep(0.02)


def wait_until(condition, timeout=15.0):
    """`until` for a test that is not a coroutine."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)


async def quiet(broker, seconds=0.4):
    """Wait, and return how many Sparkplug messages there are afterwards."""
    await asyncio.sleep(seconds)
    return len(decoded(broker))
