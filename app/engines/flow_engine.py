from __future__ import annotations
import asyncio, json, time, uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
from app.utils.logger import get_logger

logger = get_logger(__name__)
_debounce_last: Dict[Tuple[str, str], float] = {}
_event_handlers_registered = False

# ── Operators ──────────────────────────────────────────────────────────
OPS: Dict[str, Callable[[Any, Any], bool]] = {
    "=":  lambda a, b: str(a).lower() == str(b).lower(),
    "==": lambda a, b: str(a).lower() == str(b).lower(),
    "!=": lambda a, b: str(a).lower() != str(b).lower(),
    "<>": lambda a, b: str(a).lower() != str(b).lower(),
    "<":  lambda a, b: float(a) < float(b),
    ">":  lambda a, b: float(a) > float(b),
    "<=": lambda a, b: float(a) <= float(b),
    ">=": lambda a, b: float(a) >= float(b),
    "contains": lambda a, b: str(b).lower() in str(a).lower(),
    "in_list":  lambda a, b: str(a).lower() in [x.strip().lower() for x in str(b).split(",")],
}

def _eval_condition(payload: Dict, field: str, op: str, value: str) -> bool:
    raw = payload.get(field)
    if raw is None:
        return False
    fn = OPS.get(op)
    if fn is None:
        return False
    try:
        return fn(raw, value)
    except (ValueError, TypeError):
        return False

# ── Debug WS sink ──────────────────────────────────────────────────────
_debug_subscribers: List[Callable] = []

def register_debug_sink(fn: Callable) -> None:
    _debug_subscribers.append(fn)

def unregister_debug_sink(fn: Callable) -> None:
    try: _debug_subscribers.remove(fn)
    except ValueError: pass

async def _emit_debug(flow_id: str, node_id: str, status: str, message: str) -> None:
    evt = {"type": "flow_node_pulse", "flow_id": flow_id, "node_id": node_id,
           "status": status, "message": message, "ts": datetime.now(timezone.utc).isoformat()}
    for sink in list(_debug_subscribers):
        try:
            if asyncio.iscoroutinefunction(sink):
                asyncio.create_task(sink(evt))
            else:
                sink(evt)
        except Exception as exc:
            logger.warning("Flow debug sink %r failed: %s: %s", sink, type(exc).__name__, exc)

async def _report_action(flow_id: str, node_id: str, node_type: str, passed: bool, msg: str) -> None:
    """Emit the node pulse and raise/clear the action alarm for this node."""
    await _emit_debug(flow_id, node_id, "executed" if passed else "error", msg)
    source = f"flow:{flow_id}/{node_id}"
    if passed:
        alarm_manager.clear_alarm(AlarmCode.FLOW_ACTION_FAILED, source, "action succeeded")
    else:
        alarm_manager.raise_alarm(
            AlarmCode.FLOW_ACTION_FAILED, source,
            f"Flow output node {node_id} ({node_type}) failed: {msg}",
            AlarmSeverity.WARNING,
            {"flow_id": flow_id, "node_id": node_id, "node_type": node_type},
        )

# ── Action executors ───────────────────────────────────────────────────
async def _exec_modbus(cfg: Dict, ctx: Dict) -> str:
    try:
        from app.services.modbus_service import ModbusService
        addr = int(cfg.get("address", cfg.get("coil_address", 0)))
        mode = str(cfg.get("mode", "pulse")).lower()
        pulse_ms = int(cfg.get("pulse_ms", 80))
        if mode == "pulse" and not 1 <= pulse_ms <= 60_000:
            return "Modbus error: pulse_ms must be between 1 and 60000"
        if mode == "holding_register" or mode == "register":
            val = int(cfg.get("value", 1))
            ok, err = await ModbusService.write_register(addr, val)
            return f"Register {addr}={val}" if ok else f"Modbus error: {err}"
        elif mode == "pulse":
            ok, err = await ModbusService.write_coil(addr, True)
            if not ok:
                return f"Modbus error: {err}"
            try:
                await asyncio.sleep(pulse_ms / 1000.0)
            finally:
                reset_task = asyncio.create_task(ModbusService.write_coil(addr, False))
                try:
                    off_ok, off_err = await asyncio.shield(reset_task)
                except asyncio.CancelledError:
                    await reset_task
                    raise
            return f"Modbus coil {addr} pulsed {pulse_ms}ms" if off_ok else f"Modbus error resetting coil {addr}: {off_err}"
        elif mode in ("on", "true", "1"):
            ok, err = await ModbusService.write_coil(addr, True)
            return f"Modbus coil {addr} ON" if ok else f"Modbus error: {err}"
        else:
            ok, err = await ModbusService.write_coil(addr, False)
            return f"Modbus coil {addr} OFF" if ok else f"Modbus error: {err}"
    except Exception as exc:
        return f"Modbus exception: {exc}"

async def _exec_mqtt(cfg: Dict, ctx: Dict, endpoint: Optional[Dict]) -> str:
    try:
        topic = cfg.get("topic", "factory/actions/reject")
        qos = int(cfg.get("qos", 1))
        payload = {**ctx, "timestamp": datetime.now(timezone.utc).isoformat()}
        if endpoint:
            if endpoint.get("enabled", True) is not True:
                return "MQTT exception: selected endpoint is disabled"
            if str(endpoint.get("protocol", "")).lower() != "mqtt":
                return "MQTT exception: selected endpoint is not an MQTT endpoint"
            from app.hardware.mqtt.client import MQTTClient
            from app.services.mqtt_service import _resolve_cert
            c = MQTTClient(host=endpoint.get("host","localhost"), port=int(endpoint.get("port",1883)),
                           username=endpoint.get("username"), password=endpoint.get("password"),
                           tls_enabled=bool(endpoint.get("tls_enabled", False)),
                           ca_cert_path=_resolve_cert(endpoint.get("ca_cert_filename")),
                           client_cert_path=_resolve_cert(endpoint.get("client_cert_filename")),
                           client_key_path=_resolve_cert(endpoint.get("client_key_filename")))
            try:
                ok = await c.publish(topic, payload, qos=qos)
                return f"MQTT -> '{topic}' via {endpoint.get('name','?')} {'ok' if ok else 'failed'}"
            finally:
                await c.disconnect()
        elif cfg.get("endpoint_id"):
            return "MQTT exception: selected endpoint is missing or disabled"
        else:
            from app.services.mqtt_service import MQTTService
            ok = await MQTTService.publish(topic, payload, qos=qos)
            return f"MQTT -> '{topic}' {'ok' if ok else 'failed'}"
    except Exception as exc:
        return f"MQTT exception: {exc}"

async def _exec_tcp(cfg: Dict, ctx: Dict, endpoint: Optional[Dict]) -> str:
    writer = None
    try:
        if not endpoint:
            return "TCP exception: no configured endpoint selected"
        if endpoint.get("enabled", True) is not True:
            return "TCP exception: selected endpoint is disabled"
        if str(endpoint.get("protocol", "")).lower() != "tcp":
            return "TCP exception: selected endpoint is not a TCP endpoint"
        host = endpoint.get("host", "")
        port = int(endpoint.get("port", 0))
        if not host or not (1 <= port <= 65535):
            return "TCP exception: configured endpoint is invalid"
        payload = {**ctx, "timestamp": datetime.now(timezone.utc).isoformat()}
        msg = (json.dumps(payload) + "\n").encode("utf-8")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.5)
        writer.write(msg)
        await writer.drain()
        return f"TCP {len(msg)}b -> {host}:{port}"
    except Exception as exc:
        return f"TCP exception: {exc}"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as exc:
                logger.debug("TCP output socket did not close cleanly: %s", exc)

async def _exec_webhook(cfg: Dict, ctx: Dict, endpoint: Optional[Dict]) -> str:
    try:
        import httpx
        if not endpoint:
            return "Webhook exception: no configured endpoint selected"
        if endpoint.get("enabled", True) is not True:
            return "Webhook exception: selected endpoint is disabled"
        if str(endpoint.get("protocol", "")).lower() != "webhook":
            return "Webhook exception: selected endpoint is not a webhook endpoint"
        url = endpoint.get("url", "")
        method = str(endpoint.get("method", "POST")).upper()
        raw_hdr = endpoint.get("headers", "")
        headers: Dict[str, str] = {}
        if raw_hdr:
            for line in str(raw_hdr).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":"); headers[k.strip()] = v.strip()
        payload = {**ctx, "timestamp": datetime.now(timezone.utc).isoformat()}
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=False) as client:
            resp = await client.request(method, url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return f"Webhook failed with HTTP {resp.status_code}"
        return f"Webhook {method} via {endpoint.get('name', 'configured endpoint')} -> {resp.status_code}"
    except Exception as exc:
        return f"Webhook exception: {type(exc).__name__}"

# ── Helpers ────────────────────────────────────────────────────────────
EVENT_MAP = {
    "counter_in":      ["wireline_cross", "counter_in", "event_wireline"],
    "event_wireline":  ["wireline_cross", "event_wireline", "counter_in"],
    "event_detection": ["detection", "event_detection"],
    "tcp_in":          ["tcp_in", "event_tcp"],
    "event_tcp":       ["tcp_in", "event_tcp"],
    "mqtt_in":         ["mqtt_in", "event_mqtt"],
    "event_mqtt":      ["mqtt_in", "event_mqtt"],
    "api_in":          ["api_in", "event_api"],
    "event_api":       ["api_in", "event_api"],
    "qr_in":           ["qr_code", "event_qr"],
    "event_qr":        ["qr_code", "event_qr"],
    "timer_in":        ["timer_tick", "event_timer"],
    "event_timer":     ["timer_tick", "event_timer"],
}

def _node_matches(node: Dict, event_type: str, ctx: Dict) -> bool:
    ntype = node.get("type", "")
    cfg   = node.get("config", {})
    if event_type not in EVENT_MAP.get(ntype, [ntype]):
        return False
    if ntype in ("event_wireline", "counter_in"):
        req = cfg.get("line", cfg.get("source"))
        if req not in (None, "any", "", "live_count", "total_count", "stats_snapshot") and str(req) != str(ctx.get("line_index")):
            return False
        class_filter = str(cfg.get("class_filter", "")).strip().lower()
        if class_filter:
            allowed = [c.strip() for c in class_filter.split(",") if c.strip()]
            if ctx.get("class_name", "").lower() not in allowed:
                return False
    if ntype in ("event_mqtt", "mqtt_in"):
        pat = cfg.get("topic_pattern", cfg.get("topic", ""))
        if pat and not _topic_match(pat, ctx.get("topic", "")):
            return False
    return True

def _topic_match(pattern: str, topic: str) -> bool:
    if pattern in ("#", ""): return True
    pp, tp = pattern.split("/"), topic.split("/")
    for i, p in enumerate(pp):
        if p == "#": return True
        if i >= len(tp) or (p != "+" and p != tp[i]): return False
    return len(pp) == len(tp)

def _resolve_endpoint(eid: Optional[str]) -> Optional[Dict]:
    if not eid: return None
    try:
        from app.services.settings_persistence_service import SettingsPersistenceService
        for ep in SettingsPersistenceService.get_state().get("communication_endpoints", []):
            if ep.get("id") == eid: return ep
    except Exception as exc:
        alarm_manager.raise_alarm(
            AlarmCode.FLOW_ENDPOINT_UNRESOLVED, f"endpoint:{eid}",
            f"Could not read communication endpoint {eid} from settings: {type(exc).__name__}: {exc}",
            AlarmSeverity.WARNING,
        )
        return None
    alarm_manager.clear_alarm(AlarmCode.FLOW_ENDPOINT_UNRESOLVED, f"endpoint:{eid}", "settings readable")
    return None

async def _inc_exec(flow_id: str) -> None:
    try:
        from sqlalchemy import update
        from app.db.session import AsyncSessionLocal
        from app.db.models.flow import FlowDefinition
        async with AsyncSessionLocal() as db:
            await db.execute(update(FlowDefinition)
                .where(FlowDefinition.id == flow_id)
                .values(execution_count=FlowDefinition.execution_count + 1,
                        last_executed_at=datetime.now(timezone.utc)))
            await db.commit()
    except Exception as exc:
        logger.warning("Could not update execution count for flow %s: %s", flow_id, exc)

# ── Node executor ──────────────────────────────────────────────────────
async def _execute_node(fid: str, node: Dict, ctx: Dict) -> Tuple[bool, str, Dict]:
    ntype = node.get("type", "")
    cfg   = node.get("config", {})
    nid   = node.get("id", "?")
    uctx  = dict(ctx)
    try:
        # ── Condition / If ────────────────────────────────────────────────────
        if ntype in ("if", "logic_switch"):
            cond = _eval_condition(ctx, cfg.get("field","is_defect"), cfg.get("operator","=="), str(cfg.get("value","true")))
            port = "out:0" if cond else "out:1"
            await _emit_debug(fid, nid, "passed", f"If condition {cfg.get('field')} {cfg.get('operator')} {cfg.get('value')} -> {'TRUE (out:0)' if cond else 'FALSE (out:1)'}")
            return True, port, uctx

        # ── Compare ──────────────────────────────────────────────────────────
        elif ntype in ("compare", "logic_compare"):
            field = cfg.get("field", "total_inspected")
            op = cfg.get("operator", ">")
            val = str(cfg.get("value", "1000"))
            passed = _eval_condition(ctx, field, op, val)
            port = "out:0" if passed else "out:1"
            await _emit_debug(fid, nid, "passed" if passed else "blocked", f"Compare {field} {op} {val} -> {port}")
            return True, port, uctx

        # ── AND / OR Gates ───────────────────────────────────────────────────
        elif ntype in ("and", "logic_and"):
            conditions = cfg.get("conditions") or []
            if not conditions:
                await _emit_debug(fid, nid, "blocked", "AND gate requires configured conditions")
                return False, "out:0", uctx
            passed = all(_eval_condition(ctx, c.get("field", ""), c.get("operator", "=="), str(c.get("value", ""))) for c in conditions)
            await _emit_debug(fid, nid, "passed" if passed else "blocked", f"AND gate -> {passed}")
            return passed, "out:0", uctx

        elif ntype in ("or", "logic_or"):
            conditions = cfg.get("conditions") or []
            if not conditions:
                await _emit_debug(fid, nid, "blocked", "OR gate requires configured conditions")
                return False, "out:0", uctx
            passed = any(_eval_condition(ctx, c.get("field", ""), c.get("operator", "=="), str(c.get("value", ""))) for c in conditions)
            await _emit_debug(fid, nid, "passed" if passed else "blocked", f"OR gate -> {passed}")
            return passed, "out:0", uctx

        # ── Switch ───────────────────────────────────────────────────────────
        elif ntype in ("switch",):
            val = str(ctx.get(cfg.get("field","class_name"), "")).lower()
            cases = [c.strip().lower() for c in str(cfg.get("cases","case1,case2")).split(",")]
            port = f"out:{cases.index(val)}" if val in cases else f"out:{len(cases)}"
            await _emit_debug(fid, nid, "passed", f"Switch '{val}' -> {port}")
            return True, port, uctx

        # ── Class Filter ──────────────────────────────────────────────────────
        elif ntype in ("class_filter", "logic_class_filter"):
            allowed = [c.strip().lower() for c in str(cfg.get("classes","")).split(",") if c.strip()]
            defect_only = bool(cfg.get("defect_only", False))
            cls = str(ctx.get("class_name","")).lower()
            passed = bool(ctx.get("is_defect")) if defect_only else (cls in allowed if allowed else True)
            await _emit_debug(fid, nid, "passed" if passed else "blocked", f"Class filter '{cls}' -> {'PASS' if passed else 'BLOCK'}")
            return passed, "out:0", uctx

        # ── Threshold Gate ───────────────────────────────────────────────────
        elif ntype in ("threshold", "logic_threshold"):
            field = cfg.get("field", "confidence")
            mn, mx = float(cfg.get("min", 0.0)), float(cfg.get("max", 1.0))
            try:
                v = float(ctx.get(field, 0.0))
                passed = mn <= v <= mx
            except (TypeError, ValueError):
                # A non-numeric reading blocks the branch; the pulse below shows the value.
                passed = False
            await _emit_debug(fid, nid, "passed" if passed else "blocked", f"Threshold {field}={ctx.get(field)} in [{mn},{mx}] -> {'PASS' if passed else 'BLOCK'}")
            return passed, "out:0", uctx

        # ── Delay / Timer ─────────────────────────────────────────────────────
        elif ntype in ("delay", "logic_delay"):
            ms = int(cfg.get("delay_ms", 80))
            if ms < 0 or ms > 300_000:
                return False, "out:0", uctx
            await _emit_debug(fid, nid, "passed", f"Delay {ms}ms")
            if ms > 0: await asyncio.sleep(ms / 1000.0)
            return True, "out:0", uctx

        # ── Debounce ──────────────────────────────────────────────────────────
        elif ntype in ("debounce", "logic_debounce"):
            cooldown = max(0, int(cfg.get("cooldown_ms", 200))) / 1000.0
            key = (f"{fid}:test" if ctx.get("_test") else fid, nid)
            now = time.monotonic()
            last = _debounce_last.get(key)
            if last is not None and now - last < cooldown:
                await _emit_debug(fid, nid, "blocked", f"Debounce active for {max(0, cooldown - (now-last)):.3f}s")
                return False, "out:0", uctx
            _debounce_last[key] = now
            await _emit_debug(fid, nid, "passed", f"Debounce {int(cooldown * 1000)}ms")
            return True, "out:0", uctx

        # ── Actuator Outputs ──────────────────────────────────────────────────
        elif ntype in ("modbus_out", "action_modbus_coil", "action_modbus_register"):
            if ctx.get("_test"):
                msg = "TEST MODE: Modbus output suppressed"
                await _emit_debug(fid, nid, "simulated", msg)
                return True, "out:0", uctx
            msg = await _exec_modbus(cfg, ctx)
            passed = not any(word in msg.lower() for word in ("error", "exception", "failed"))
            await _report_action(fid, nid, ntype, passed, msg); return passed, "out:0", uctx

        elif ntype in ("mqtt_out", "action_mqtt_publish"):
            if ctx.get("_test"):
                msg = "TEST MODE: MQTT publish suppressed"
                await _emit_debug(fid, nid, "simulated", msg)
                return True, "out:0", uctx
            ep = _resolve_endpoint(cfg.get("endpoint_id"))
            msg = await _exec_mqtt(cfg, ctx, ep)
            passed = "failed" not in msg.lower() and "exception" not in msg.lower()
            await _report_action(fid, nid, ntype, passed, msg); return passed, "out:0", uctx

        elif ntype in ("tcp_out", "action_tcp_publish"):
            if ctx.get("_test"):
                msg = "TEST MODE: TCP output suppressed"
                await _emit_debug(fid, nid, "simulated", msg)
                return True, "out:0", uctx
            ep = _resolve_endpoint(cfg.get("endpoint_id"))
            msg = await _exec_tcp(cfg, ctx, ep)
            passed = "exception" not in msg.lower()
            await _report_action(fid, nid, ntype, passed, msg); return passed, "out:0", uctx

        elif ntype in ("api_out", "action_webhook_post"):
            if ctx.get("_test"):
                msg = "TEST MODE: webhook request suppressed"
                await _emit_debug(fid, nid, "simulated", msg)
                return True, "out:0", uctx
            ep = _resolve_endpoint(cfg.get("endpoint_id"))
            msg = await _exec_webhook(cfg, ctx, ep)
            passed = "exception" not in msg.lower() and "failed" not in msg.lower()
            await _report_action(fid, nid, ntype, passed, msg); return passed, "out:0", uctx

        elif ntype in ("log_out", "action_dashboard_alert"):
            title = cfg.get("title", f"Event Log: {ctx.get('class_name', 'item')}")
            sev   = cfg.get("severity", "info")
            await _emit_debug(fid, nid, "executed", f"Log: [{sev.upper()}] {title}")
            try:
                from app.events.event_bus import event_bus
                await event_bus.publish("dashboard_alert", {"title": title, "severity": sev, "context": ctx})
            except Exception as exc:
                logger.warning("Flow %s could not publish dashboard alert '%s': %s", fid, title, exc)
            return True, "out:0", uctx

        else:
            await _emit_debug(fid, nid, "error", f"Unknown node type: {ntype}")
            return False, "out:0", uctx
    except Exception as exc:
        await _emit_debug(fid, nid, "error", str(exc))
        logger.exception("FlowEngine node error [%s/%s]", fid, nid)
        if not ctx.get("_test"):
            alarm_manager.raise_alarm(
                AlarmCode.FLOW_NODE_ERROR, f"flow:{fid}/{nid}",
                f"Flow node {nid} ({ntype}) raised {type(exc).__name__}: {exc}",
                AlarmSeverity.WARNING,
                {"flow_id": fid, "node_id": nid, "node_type": ntype},
            )
        return False, "out:0", uctx

# ── FlowEngine ─────────────────────────────────────────────────────────
class FlowEngine:
    _instance: Optional[FlowEngine] = None
    _max_active_runs = 256
    _max_nodes_per_run = 4096

    def __init__(self):
        self._compiled: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()
        self._tasks: Dict[asyncio.Task, str] = {}

    @classmethod
    def get(cls) -> FlowEngine:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _compile_flow(flow_dict: Dict) -> Dict:
        fid = flow_dict["id"]
        nodes = {n["id"]: n for n in (flow_dict.get("nodes") or [])}
        adj: Dict[str, List] = {nid: [] for nid in nodes}
        # Support both 'links' and 'wires' JSON formats
        edges = flow_dict.get("links") or flow_dict.get("wires") or []
        for lnk in edges:
            # Handle prototype wire format: {from: {node, port}, to: {node, port}}
            if "from" in lnk and isinstance(lnk["from"], dict):
                src = lnk["from"].get("node")
                from_port = lnk["from"].get("port", "out:0")
                dst = lnk["to"].get("node")
                to_port = lnk["to"].get("port", "in:0")
            else:
                src = lnk.get("from_node")
                from_port = lnk.get("from_port", "out:0")
                dst = lnk.get("to_node")
                to_port = lnk.get("to_port", "in:0")

            if src in adj:
                adj[src].append({"to_node": dst, "from_port": from_port, "to_port": to_port})
        return {"def": flow_dict, "nodes": nodes, "adj": adj}

    async def load_flow(self, flow_dict: Dict) -> None:
        fid = flow_dict["id"]
        compiled = self._compile_flow(flow_dict)
        async with self._lock:
            self._compiled[fid] = compiled
        logger.info("FlowEngine: loaded '%s' (%s nodes)", flow_dict.get("name", fid), len(compiled["nodes"]))

    async def unload_flow(self, flow_id: str) -> None:
        async with self._lock:
            self._compiled.pop(flow_id, None)
        for key in [key for key in _debounce_last if key[0] in {flow_id, f"{flow_id}:test"}]:
            _debounce_last.pop(key, None)
        # Stop queued downstream nodes after a flow is disabled or replaced.
        for task, active_flow_id in list(self._tasks.items()):
            if active_flow_id == flow_id:
                task.cancel()

    async def dispatch(self, event_type: str, payload: Dict) -> None:
        async with self._lock:
            snap = dict(self._compiled)
        dropped = 0
        for fid, c in snap.items():
            if not c["def"].get("is_active", True):
                continue
            if len(self._tasks) >= self._max_active_runs:
                dropped += 1
                continue
            self._schedule_run(fid, c, event_type, dict(payload))
        if dropped:
            alarm_manager.raise_alarm(
                AlarmCode.FLOW_OVERLOAD, "flow_engine",
                f"FlowEngine dropped {dropped} run(s) for '{event_type}': {self._max_active_runs} active run limit reached",
                AlarmSeverity.WARNING,
                {"event_type": event_type, "dropped": dropped},
            )

    def _schedule_run(self, flow_id: str, compiled: Dict, event_type: str, payload: Dict) -> bool:
        if len(self._tasks) >= self._max_active_runs:
            return False
        task = asyncio.create_task(self._run_flow(flow_id, compiled, event_type, payload))
        self._tasks[task] = flow_id
        task.add_done_callback(self._on_task_done)
        return True

    async def dispatch_test(self, flow_dict: Dict, event_type: str, payload: Dict) -> bool:
        """Run an isolated flow snapshot with all external outputs suppressed."""
        compiled = self._compile_flow({**flow_dict, "is_active": True})
        test_payload = {**payload, "_test": True}
        return self._schedule_run(flow_dict["id"], compiled, event_type, test_payload)

    def _on_task_done(self, task: asyncio.Task) -> None:
        task_flow_id = self._tasks.pop(task, None)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error("FlowEngine run failed: %s", exc, exc_info=(type(exc), exc, exc.__traceback__))
            flow_id = task_flow_id or "unknown"
            alarm_manager.raise_alarm(
                AlarmCode.FLOW_RUN_FAILED, f"flow:{flow_id}",
                f"Flow {flow_id} run aborted with {type(exc).__name__}: {exc}",
                AlarmSeverity.WARNING,
                {"flow_id": flow_id},
            )

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        async with self._lock:
            self._compiled.clear()
        _debounce_last.clear()

    async def _run_flow(self, fid: str, compiled: Dict, event_type: str, ctx: Dict) -> None:
        nodes, adj = compiled["nodes"], compiled["adj"]
        triggers = [n for n in nodes.values() if _node_matches(n, event_type, ctx)]
        if not triggers: return
        for t in triggers:
            await _emit_debug(fid, t["id"], "passed", f"Triggered by {event_type}")
            await self._walk(fid, t["id"], "out:0", nodes, adj, ctx, set(), [0])
        if not ctx.get("_test"):
            await _inc_exec(fid)

    async def _walk(
        self,
        fid: str,
        node_id: str,
        out_port: str,
        nodes: Dict,
        adj: Dict,
        ctx: Dict,
        visited_path: set[str],
        budget: List[int],
    ) -> None:
        if len(visited_path) >= 256:
            logger.warning("FlowEngine stopped over-deep path in flow %s at node %s", fid, node_id)
            return
        visited_path = visited_path | {node_id}
        # Follow only the wires leaving the port this node chose (e.g. an If
        # node's false branch), then run each downstream node exactly once.
        for edge in adj.get(node_id, []):
            if not _port_matches(edge["from_port"], out_port):
                continue
            nxt_id = edge["to_node"]
            nxt = nodes.get(nxt_id)
            if not nxt: continue
            if nxt_id in visited_path:
                logger.warning("FlowEngine stopped cyclic path in flow %s at node %s", fid, nxt_id)
                continue
            if budget[0] >= self._max_nodes_per_run:
                logger.warning("FlowEngine stopped oversized run in flow %s after %s nodes", fid, budget[0])
                return
            budget[0] += 1
            passed, nxt_port, uctx = await _execute_node(fid, nxt, ctx)
            if passed:
                await self._walk(fid, nxt_id, nxt_port, nodes, adj, uctx, visited_path, budget)

def _port_matches(wire_port: str, out_port: str) -> bool:
    """Wires may name ports 'out'/'true'/'false' as well as 'out:N'."""
    aliases = {"out": "out:0", "true": "out:0", "false": "out:1"}
    return aliases.get(wire_port, wire_port) == aliases.get(out_port, out_port)

# ── Bootstrap ──────────────────────────────────────────────────────────
async def bootstrap_flow_engine() -> None:
    global _event_handlers_registered
    from app.db.session import AsyncSessionLocal
    from app.db.models.flow import FlowDefinition
    from app.events.event_bus import event_bus
    from sqlalchemy import select

    engine = FlowEngine.get()

    if not _event_handlers_registered:
        for etype in ["detection","wireline_cross","tcp_in","mqtt_in","api_in","qr_code","timer_tick"]:
            async def _h(data: Dict, _e: str = etype):
                await engine.dispatch(_e, data)
            event_bus.subscribe(etype, _h)
        _event_handlers_registered = True

    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(FlowDefinition).where(FlowDefinition.is_active == True))
            flows = res.scalars().all()
            for f in flows:
                await engine.load_flow({"id": f.id, "name": f.name, "is_active": f.is_active,
                                        "nodes": f.nodes or [], "links": f.links or []})
            logger.info("FlowEngine bootstrapped with %s active flow(s)", len(flows))
        alarm_manager.clear_alarm(AlarmCode.FLOW_BOOTSTRAP_FAILED, "flow_engine", "flows loaded")
    except Exception as exc:
        logger.exception("FlowEngine bootstrap DB error")
        alarm_manager.raise_alarm(
            AlarmCode.FLOW_BOOTSTRAP_FAILED, "flow_engine",
            f"Could not load flows from the database; no flows are running: {type(exc).__name__}: {exc}",
            AlarmSeverity.CRITICAL,
        )
