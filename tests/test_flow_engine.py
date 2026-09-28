"""Flow engine: trigger matching, node logic, graph walking and PLC output safety."""
from __future__ import annotations

import asyncio

import pytest

from app.engines import flow_engine
from app.engines.flow_engine import FlowEngine, _eval_condition, _node_matches, _topic_match


class FakePLC:
    """Replaces ModbusService so coil and register writes are recorded, not sent."""

    def __init__(self):
        self.writes = []
        self.fail = False

    async def write_coil(self, address, value):
        self.writes.append(("coil", address, value))
        return (False, "PLC offline") if self.fail else (True, None)

    async def write_register(self, address, value):
        self.writes.append(("register", address, value))
        return (False, "PLC offline") if self.fail else (True, None)


@pytest.fixture
def plc(monkeypatch):
    from app.services import modbus_service

    fake = FakePLC()
    monkeypatch.setattr(modbus_service.ModbusService, "write_coil", staticmethod(fake.write_coil))
    monkeypatch.setattr(modbus_service.ModbusService, "write_register", staticmethod(fake.write_register))
    return fake


@pytest.fixture
def pulses(monkeypatch):
    """Collect debug pulses and keep runs from touching the flows table."""
    events = []
    flow_engine.register_debug_sink(events.append)

    async def no_db(flow_id):
        return None

    monkeypatch.setattr(flow_engine, "_inc_exec", no_db)
    flow_engine._debounce_last.clear()
    yield events
    flow_engine.unregister_debug_sink(events.append)
    flow_engine._debounce_last.clear()


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "config": config}


def _link(src, dst, port="out:0"):
    return {"from_node": src, "from_port": port, "to_node": dst, "to_port": "in:0"}


def _run(flow, event_type="wireline_cross", payload=None):
    engine = FlowEngine()
    compiled = engine._compile_flow({"id": "f1", "name": "flow", **flow})
    asyncio.run(engine._run_flow("f1", compiled, event_type, dict(payload or {})))


def _statuses(events):
    return [(e["node_id"], e["status"]) for e in events]


# ── Pure helpers ──────────────────────────────────────────────────────────────

class TestConditions:
    @pytest.mark.parametrize("payload, op, value, expected", [
        ({"x": 5}, ">", "3", True),
        ({"x": 5}, "<=", "5", True),
        ({"x": "Scratch"}, "==", "scratch", True),
        ({"x": "dent"}, "!=", "scratch", True),
        ({"x": "LOT7-001"}, "contains", "lot7", True),
        ({"x": "dent"}, "in_list", "scratch, dent", True),
        ({"x": "chip"}, "in_list", "scratch,dent", False),
        ({"x": "abc"}, ">", "1", False),
        ({}, "==", "1", False),
        ({"x": 1}, "~=", "1", False),
    ])
    def test_eval_condition(self, payload, op, value, expected):
        assert _eval_condition(payload, "x", op, value) is expected

    @pytest.mark.parametrize("pattern, topic, expected", [
        ("#", "any/topic", True),
        ("line1/+/reject", "line1/cam2/reject", True),
        ("line1/#", "line1/cam2/reject", True),
        ("line1/+", "line1/cam2/reject", False),
        ("line2/+/reject", "line1/cam2/reject", False),
    ])
    def test_topic_match(self, pattern, topic, expected):
        assert _topic_match(pattern, topic) is expected


class TestTriggerMatching:
    def test_wireline_trigger_filters_by_line(self):
        node = _node("t", "event_wireline", line="2")
        assert _node_matches(node, "wireline_cross", {"line_index": 2}) is True
        assert _node_matches(node, "wireline_cross", {"line_index": 1}) is False

    def test_wireline_trigger_filters_by_class(self):
        node = _node("t", "counter_in", class_filter="Bottle, can")
        assert _node_matches(node, "wireline_cross", {"class_name": "bottle"}) is True
        assert _node_matches(node, "wireline_cross", {"class_name": "cup"}) is False

    def test_mqtt_trigger_filters_by_topic(self):
        node = _node("t", "mqtt_in", topic_pattern="plant/+/stop")
        assert _node_matches(node, "mqtt_in", {"topic": "plant/line1/stop"}) is True
        assert _node_matches(node, "mqtt_in", {"topic": "plant/line1/start"}) is False

    def test_trigger_ignores_other_event_types(self):
        assert _node_matches(_node("t", "event_qr"), "wireline_cross", {}) is False
        assert _node_matches(_node("t", "event_qr"), "qr_code", {}) is True


# ── Running flows ─────────────────────────────────────────────────────────────

class TestRunFlow:
    def test_defect_on_line_pulses_reject_coil(self, plc, pulses):
        flow = {
            "nodes": [
                _node("trg", "event_wireline", line="any"),
                _node("chk", "if", field="is_defect", operator="==", value="true"),
                _node("out", "modbus_out", address=4, mode="pulse", pulse_ms=1),
            ],
            "links": [_link("trg", "chk"), _link("chk", "out", "out:0")],
        }
        _run(flow, payload={"is_defect": True, "class_name": "bottle", "line_index": 1})

        assert plc.writes == [("coil", 4, True), ("coil", 4, False)]
        assert ("out", "executed") in _statuses(pulses)

    def test_good_part_takes_false_branch(self, plc, pulses):
        flow = {
            "nodes": [
                _node("trg", "event_wireline"),
                _node("chk", "if", field="is_defect", operator="==", value="true"),
                _node("reject", "modbus_out", address=4, mode="on"),
                _node("accept", "modbus_out", address=5, mode="on"),
            ],
            "links": [_link("trg", "chk"), _link("chk", "reject", "out:0"), _link("chk", "accept", "out:1")],
        }
        _run(flow, payload={"is_defect": False})
        assert plc.writes == [("coil", 5, True)]

    def test_threshold_gate_blocks_low_confidence(self, plc, pulses):
        flow = {
            "nodes": [
                _node("trg", "event_detection"),
                _node("gate", "threshold", field="confidence", min=0.8, max=1.0),
                _node("out", "modbus_out", address=1, mode="on"),
            ],
            "links": [_link("trg", "gate"), _link("gate", "out")],
        }
        _run(flow, "detection", {"confidence": 0.4})
        assert plc.writes == []
        assert ("gate", "blocked") in _statuses(pulses)

        _run(flow, "detection", {"confidence": 0.95})
        assert plc.writes == [("coil", 1, True)]

    def test_and_gate_requires_every_condition(self, plc, pulses):
        flow = {
            "nodes": [
                _node("trg", "event_detection"),
                _node("and", "and", conditions=[
                    {"field": "class_name", "operator": "==", "value": "scratch"},
                    {"field": "confidence", "operator": ">=", "value": "0.5"},
                ]),
                _node("out", "modbus_out", address=2, mode="register", value=7),
            ],
            "links": [_link("trg", "and"), _link("and", "out")],
        }
        _run(flow, "detection", {"class_name": "scratch", "confidence": 0.3})
        assert plc.writes == []
        _run(flow, "detection", {"class_name": "scratch", "confidence": 0.9})
        assert plc.writes == [("register", 2, 7)]

    def test_empty_and_gate_blocks(self, plc, pulses):
        flow = {
            "nodes": [_node("trg", "event_detection"), _node("and", "and"), _node("out", "modbus_out", address=2, mode="on")],
            "links": [_link("trg", "and"), _link("and", "out")],
        }
        _run(flow, "detection", {})
        assert plc.writes == []

    def test_switch_routes_by_case(self, plc, pulses):
        flow = {
            "nodes": [
                _node("trg", "event_detection"),
                _node("sw", "switch", field="class_name", cases="scratch,dent"),
                _node("o0", "modbus_out", address=10, mode="on"),
                _node("o1", "modbus_out", address=11, mode="on"),
                _node("o2", "modbus_out", address=12, mode="on"),
            ],
            "links": [_link("trg", "sw"), _link("sw", "o0", "out:0"), _link("sw", "o1", "out:1"), _link("sw", "o2", "out:2")],
        }
        _run(flow, "detection", {"class_name": "Dent"})
        _run(flow, "detection", {"class_name": "chip"})
        assert plc.writes == [("coil", 11, True), ("coil", 12, True)]

    def test_debounce_suppresses_repeat_within_cooldown(self, plc, pulses):
        flow = {
            "nodes": [_node("trg", "event_detection"), _node("db", "debounce", cooldown_ms=60_000), _node("out", "modbus_out", address=3, mode="on")],
            "links": [_link("trg", "db"), _link("db", "out")],
        }
        _run(flow, "detection", {})
        _run(flow, "detection", {})
        assert plc.writes == [("coil", 3, True)]

    def test_test_mode_never_writes_to_plc(self, plc, pulses):
        flow = {
            "nodes": [_node("trg", "event_detection"), _node("out", "modbus_out", address=3, mode="on")],
            "links": [_link("trg", "out")],
        }
        _run(flow, "detection", {"_test": True})
        assert plc.writes == []
        assert ("out", "simulated") in _statuses(pulses)

    def test_plc_failure_stops_downstream_nodes(self, plc, pulses):
        plc.fail = True
        flow = {
            "nodes": [
                _node("trg", "event_detection"),
                _node("out", "modbus_out", address=3, mode="on"),
                _node("next", "modbus_out", address=9, mode="on"),
            ],
            "links": [_link("trg", "out"), _link("out", "next")],
        }
        _run(flow, "detection", {})
        assert plc.writes == [("coil", 3, True)]
        assert ("out", "error") in _statuses(pulses)

    def test_pulse_duration_is_bounded(self, plc, pulses):
        flow = {
            "nodes": [_node("trg", "event_detection"), _node("out", "modbus_out", address=3, mode="pulse", pulse_ms=0)],
            "links": [_link("trg", "out")],
        }
        _run(flow, "detection", {})
        assert plc.writes == []

    def test_cycle_does_not_loop_forever(self, plc, pulses):
        flow = {
            "nodes": [_node("trg", "event_detection"), _node("a", "delay", delay_ms=0), _node("b", "delay", delay_ms=0)],
            "links": [_link("trg", "a"), _link("a", "b"), _link("b", "a")],
        }
        _run(flow, "detection", {})
        assert [nid for nid, _ in _statuses(pulses)] == ["trg", "a", "b"]

    def test_unknown_node_type_is_reported(self, plc, pulses):
        flow = {"nodes": [_node("trg", "event_detection"), _node("x", "teleport")], "links": [_link("trg", "x")]}
        _run(flow, "detection", {})
        assert ("x", "error") in _statuses(pulses)

    def test_prototype_wire_format_is_supported(self, plc, pulses):
        flow = {
            "nodes": [_node("trg", "event_detection"), _node("out", "modbus_out", address=6, mode="on")],
            "wires": [{"from": {"node": "trg", "port": "out:0"}, "to": {"node": "out", "port": "in:0"}}],
        }
        _run(flow, "detection", {})
        assert plc.writes == [("coil", 6, True)]


class TestEngineLifecycle:
    def test_dispatch_runs_active_flows_only(self, plc, pulses):
        async def scenario():
            engine = FlowEngine()
            out = [_node("trg", "event_detection"), _node("out", "modbus_out", address=1, mode="on")]
            await engine.load_flow({"id": "on", "is_active": True, "nodes": out, "links": [_link("trg", "out")]})
            await engine.load_flow({"id": "off", "is_active": False, "nodes": out, "links": [_link("trg", "out")]})
            await engine.dispatch("detection", {})
            await asyncio.gather(*engine._tasks)
            await engine.shutdown()

        asyncio.run(scenario())
        assert plc.writes == [("coil", 1, True)]

    def test_unload_stops_future_dispatch(self, plc, pulses):
        async def scenario():
            engine = FlowEngine()
            flow = {"id": "f", "nodes": [_node("trg", "event_detection"), _node("out", "modbus_out", address=1, mode="on")],
                    "links": [_link("trg", "out")]}
            await engine.load_flow(flow)
            await engine.unload_flow("f")
            await engine.dispatch("detection", {})
            assert not engine._tasks

        asyncio.run(scenario())
        assert plc.writes == []

    def test_dispatch_test_suppresses_outputs(self, plc, pulses):
        async def scenario():
            engine = FlowEngine()
            flow = {"id": "f", "is_active": False,
                    "nodes": [_node("trg", "event_detection"), _node("out", "modbus_out", address=1, mode="on")],
                    "links": [_link("trg", "out")]}
            assert await engine.dispatch_test(flow, "detection", {}) is True
            await asyncio.gather(*engine._tasks)

        asyncio.run(scenario())
        assert plc.writes == []
        assert ("out", "simulated") in _statuses(pulses)
