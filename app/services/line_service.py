"""Production lines at runtime.

The server keeps one ``LineRuntime`` per production line and sends every
camera frame to the line that owns the camera. A line has its own counters,
trackers, QR reads and Sync pairing; lines never read each other's counters or
fire each other's PLC cards. The PLC connections, the PLC dispatcher and the
telemetry dispatcher stay shared.

Line 1 uses the module-level ``counting_service`` as its counter, so code that
still talks to ``counting_service`` directly keeps working and answers for
Line 1.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.services.counting_service import CountingService, counting_service
from app.services.line_config import DEFAULT_SYNC_WINDOW_MS, PRIMARY_LINE_ID
from app.utils.logger import get_logger

logger = get_logger(__name__)

_FPS_WINDOW_SECONDS = 5.0


# ── Sync pairing ──────────────────────────────────────────────────────────────

@dataclass
class _Pending:
    t: float
    item: Any


class SyncPairer:
    """Pairs counted products with QR reads that happen within ``window`` seconds.

    Pure bookkeeping with no clock or threads of its own: callers pass the
    time of each crossing and read, and call ``expire(now)`` when
    ``next_deadline()`` is reached. Each call returns what is ready to go
    out, as ``(kind, crossing, read)`` tuples where kind is ``"paired"``,
    ``"no_read"`` (a crossing that got no code in time) or ``"unpaired"``
    (a code that no crossing claimed).
    """

    def __init__(self, window: float):
        self.window = max(0.0, float(window))
        self._crossings: Deque[_Pending] = collections.deque()
        self._reads: Deque[_Pending] = collections.deque()

    def add_crossing(self, t: float, crossing: Any) -> List[Tuple[str, Any, Any]]:
        out = self.expire(t)
        for pending in self._reads:
            if abs(t - pending.t) <= self.window:
                self._reads.remove(pending)
                out.append(("paired", crossing, pending.item))
                return out
        self._crossings.append(_Pending(t, crossing))
        return out

    def add_read(self, t: float, read: Any) -> List[Tuple[str, Any, Any]]:
        out = self.expire(t)
        for pending in self._crossings:
            if abs(t - pending.t) <= self.window:
                self._crossings.remove(pending)
                out.append(("paired", pending.item, read))
                return out
        self._reads.append(_Pending(t, read))
        return out

    def expire(self, now: float) -> List[Tuple[str, Any, Any]]:
        out: List[Tuple[str, Any, Any]] = []
        while self._crossings and now - self._crossings[0].t > self.window:
            out.append(("no_read", self._crossings.popleft().item, None))
        while self._reads and now - self._reads[0].t > self.window:
            out.append(("unpaired", None, self._reads.popleft().item))
        return out

    def flush(self) -> List[Tuple[str, Any, Any]]:
        """Everything still waiting, as no-reads and unpaired codes (used when Sync is turned off)."""
        out = [("no_read", p.item, None) for p in self._crossings]
        out += [("unpaired", None, p.item) for p in self._reads]
        self._crossings.clear()
        self._reads.clear()
        return out

    def next_deadline(self) -> Optional[float]:
        times = [q[0].t for q in (self._crossings, self._reads) if q]
        return min(times) + self.window if times else None

    @property
    def waiting(self) -> int:
        return len(self._crossings) + len(self._reads)


# ── One line ──────────────────────────────────────────────────────────────────

class LineRuntime:
    def __init__(self, line_id: str, name: str, counter: CountingService):
        self.id = line_id
        self.name = name
        self.counter = counter
        self.aux_counters: Dict[str, CountingService] = {}
        self.cameras: List[Dict[str, Any]] = []
        self.enabled = True
        self.auto_connect = True
        self.model_id: Optional[str] = None
        self.min_fps = 0.0
        self.sync_enabled = False
        self.pairer = SyncPairer(DEFAULT_SYNC_WINDOW_MS / 1000.0)
        self.qr_recent: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        self.qr_stats = {"codes_read": 0, "known": 0, "unknown": 0, "no_reads": 0, "unpaired": 0}
        self._frames: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._sync_timer: Optional[asyncio.TimerHandle] = None
        self._qr_seq = 0

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure(self, line: Dict[str, Any], trigger: Dict[str, Any], is_primary: bool) -> None:
        from app.services.settings_persistence_service import counting_config_from_dict

        self.name = line.get("name") or self.id
        self.enabled = bool(line.get("enabled", True))
        self.auto_connect = bool(line.get("auto_connect", True))
        self.model_id = line.get("model_id")
        self.min_fps = float(line.get("min_fps") or 0.0)
        self.cameras = copy.deepcopy(line.get("cameras") or [])
        self.counter.line_id = self.id
        self.counter.line_name = self.name

        config = counting_config_from_dict(trigger or {})
        if not is_primary:
            # Line 1's counter is configured by the settings service, as in version 1.
            self.counter.update_config(config)

        aux_ids = {c["camera_id"] for c in self.cameras if c.get("role") == "vision" and not c.get("counting")}
        for cid in list(self.aux_counters):
            if cid not in aux_ids:
                self.aux_counters.pop(cid)
        for cam in self.cameras:
            cid = cam["camera_id"]
            if cid not in aux_ids:
                continue
            aux = self.aux_counters.get(cid)
            if aux is None:
                # A second vision camera counts on its own and only fires PLC
                # cards that name it; the line totals come from the counting camera.
                aux = CountingService(line_id=self.id, line_name=self.name, camera_id=cid, dispatch_telemetry=False)
                self.aux_counters[cid] = aux
            aux.line_name = self.name
            overrides = {k: cam[k] for k in ("line1_position", "line2_position", "orientation") if k in cam}
            aux.update_config(config.model_copy(update=overrides) if overrides else config)

        sync = line.get("sync") or {}
        was_on = self.sync_enabled
        self.sync_enabled = bool(sync.get("enabled")) and self.has_role("vision") and self.has_role("qr")
        self.pairer.window = int(sync.get("window_ms") or DEFAULT_SYNC_WINDOW_MS) / 1000.0
        self.counter.event_sink = self._on_crossing if self.sync_enabled else None
        if was_on and not self.sync_enabled:
            self._run_on_loop(self._flush_sync)

    def has_role(self, role: str) -> bool:
        return any(c.get("role") == role for c in self.cameras)

    def camera_entry(self, camera_id: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.cameras if c.get("camera_id") == camera_id), None)

    def counter_for(self, camera_id: Optional[str]) -> CountingService:
        if camera_id and camera_id in self.aux_counters:
            return self.aux_counters[camera_id]
        return self.counter

    @property
    def counting_camera_id(self) -> Optional[str]:
        return next((c["camera_id"] for c in self.cameras if c.get("role") == "vision" and c.get("counting")), None)

    # ── Frame rate ────────────────────────────────────────────────────────────

    def record_frame(self, camera_id: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._frames.setdefault(camera_id, collections.deque())
            q.append(now)
            while q and now - q[0] > _FPS_WINDOW_SECONDS:
                q.popleft()

    def fps(self, camera_id: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._frames.get(camera_id)
            if not q:
                return 0.0
            recent = [t for t in q if now - t <= _FPS_WINDOW_SECONDS]
        if len(recent) < 2:
            return 0.0
        span = max(now - recent[0], 1e-3)
        return round(len(recent) / span, 1)

    def processed_fps(self) -> float:
        """Frames per second the line's counting camera (or its busiest camera) gets through."""
        cid = self.counting_camera_id
        if cid:
            return self.fps(cid)
        with self._lock:
            ids = list(self._frames)
        return max((self.fps(i) for i in ids), default=0.0)

    # ── State ─────────────────────────────────────────────────────────────────

    def state(self, cameras: Dict[str, Any], fallback_cameras: List[str]) -> str:
        """The line's state for the overview.

        'stopped'   the line is switched off;
        'no_camera' no camera is assigned (and none feeds Line 1 by default);
        'idle'      none of its cameras is connected yet;
        'fault'     a camera lost its link, or only some of its cameras are connected;
        'running'   every camera is connected.
        """
        if not self.enabled:
            return "stopped"
        ids = [c["camera_id"] for c in self.cameras] or fallback_cameras
        if not ids:
            return "no_camera"
        present = [i for i in ids if i in cameras]
        connected = [i for i in present if getattr(cameras.get(i), "is_connected", False)]
        if len(connected) < len(present):
            return "fault"
        if not connected:
            return "idle"
        return "running" if len(connected) == len(ids) else "fault"

    # ── Loop helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _loop() -> Optional[asyncio.AbstractEventLoop]:
        loop = CountingService.get_event_loop()
        return loop if loop and loop.is_running() else None

    def _run_on_loop(self, fn, *args) -> None:
        loop = self._loop()
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn(*args)
        else:
            loop.call_soon_threadsafe(fn, *args)

    # ── QR reads ──────────────────────────────────────────────────────────────

    def on_qr_read(self, camera_id: str, code: str, code_format: str, t: Optional[float] = None) -> Dict[str, Any]:
        """Called by a QR reader's thread for each new read. Returns the read record."""
        from app.services.product_service import product_catalog

        product = product_catalog.lookup(code)
        read = {
            "code": code,
            "format": code_format,
            "known": product is not None,
            "product_name": product.get("name") if product else None,
            "camera_id": camera_id,
            "t": time.monotonic() if t is None else t,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.qr_stats["codes_read"] += 1
            self.qr_stats["known" if read["known"] else "unknown"] += 1
        self._run_on_loop(self._handle_read, read)
        return read

    def _handle_read(self, read: Dict[str, Any]) -> None:
        if self.sync_enabled:
            self._process(self.pairer.add_read(read["t"], read))
            self._schedule_expiry()
        else:
            self._emit_qr(read, paired=None)

    def _on_crossing(self, payload: Dict[str, Any], plc_event: Dict[str, Any], is_defect: bool) -> None:
        """Counter event sink while Sync is on (called from an inference thread)."""
        t = time.monotonic()
        if self._loop() is None:
            # No loop to wait on: send it unpaired rather than lose it.
            self.counter.dispatch_event(payload, plc_event, is_defect)
            return
        self._run_on_loop(self._handle_crossing, (payload, plc_event, is_defect), t)

    def _handle_crossing(self, crossing: Tuple[Dict[str, Any], Dict[str, Any], bool], t: float) -> None:
        self._process(self.pairer.add_crossing(t, crossing))
        self._schedule_expiry()

    def _schedule_expiry(self) -> None:
        loop = self._loop()
        if self._sync_timer is not None:
            self._sync_timer.cancel()
            self._sync_timer = None
        deadline = self.pairer.next_deadline()
        if loop is None or deadline is None:
            return
        # A little past the deadline so the oldest entry has expired when it fires.
        self._sync_timer = loop.call_later(max(0.0, deadline - time.monotonic()) + 0.005, self._expire)

    def _expire(self) -> None:
        self._sync_timer = None
        self._process(self.pairer.expire(time.monotonic()))
        self._schedule_expiry()

    def _flush_sync(self) -> None:
        if self._sync_timer is not None:
            self._sync_timer.cancel()
            self._sync_timer = None
        self._process(self.pairer.flush())

    def _process(self, emits: List[Tuple[str, Any, Any]]) -> None:
        for kind, crossing, read in emits:
            try:
                if kind == "unpaired":
                    with self._lock:
                        self.qr_stats["unpaired"] += 1
                    self._emit_qr(read, paired=False)
                    continue
                payload, plc_event, is_defect = crossing
                payload = dict(payload)
                plc_event = dict(plc_event)
                if kind == "paired":
                    status = "known" if read["known"] else "unknown"
                    fields = {
                        "qr_code": read["code"],
                        "qr_format": read["format"],
                        "qr_status": status,
                        "product_name": read["product_name"],
                        "qr_paired": True,
                    }
                    self._remember(read, status, paired=True, class_name=payload.get("class_name"))
                else:
                    status = "no_read"
                    fields = {"qr_code": None, "qr_format": None, "qr_status": "no_read", "product_name": None, "qr_paired": False}
                    with self._lock:
                        self.qr_stats["no_reads"] += 1
                    self._remember(None, "no_read", paired=False, class_name=payload.get("class_name"),
                                   camera_id=payload.get("camera_id"))
                payload.update(fields)
                plc_event.update({"qr_code": fields["qr_code"], "qr_status": status, "qr_paired": fields["qr_paired"]})
                self.counter.dispatch_event(payload, plc_event, is_defect)
            except Exception:
                logger.exception("Line %s: failed to send a synced event", self.id)

    def _remember(self, read: Optional[Dict[str, Any]], status: str, paired: Optional[bool],
                  class_name: Optional[str] = None, camera_id: Optional[str] = None) -> None:
        entry = {
            "timestamp": read["timestamp"] if read else datetime.now(timezone.utc).isoformat(),
            "code": read["code"] if read else None,
            "format": read["format"] if read else None,
            "status": status,
            "product_name": read["product_name"] if read else None,
            "camera_id": read["camera_id"] if read else camera_id,
            "paired": paired,
            "class_name": class_name,
        }
        with self._lock:
            self.qr_recent.appendleft(entry)

    def _emit_qr(self, read: Dict[str, Any], paired: Optional[bool]) -> None:
        status = "known" if read["known"] else "unknown"
        self._remember(read, status, paired)
        self._qr_seq += 1
        payload = {
            "event": "QR_CODE_READ",
            "timestamp": read["timestamp"],
            "line_id": self.id,
            "line_name": self.name,
            "camera_id": read["camera_id"],
            "code": read["code"],
            "format": read["format"],
            "qr_status": status,
            "known": read["known"],
            "product_name": read["product_name"],
            "paired": paired,
        }
        plc_event = {
            "event_id": f"qr_{self.id}_{self._qr_seq}",
            "event_type": "qr_read",
            "line_id": self.id,
            "camera_id": read["camera_id"],
            "qr_code": read["code"],
            "qr_status": status,
            "qr_paired": paired,
            "timestamp": time.time(),
        }
        loop = self._loop()
        if loop is not None:
            try:
                from app.events.event_bus import event_bus
                from app.services.plc_dispatcher_service import PLCDispatcherService
                asyncio.run_coroutine_threadsafe(event_bus.publish("qr_code", payload), loop)
                asyncio.run_coroutine_threadsafe(PLCDispatcherService.evaluate(plc_event), loop)
            except Exception as exc:
                logger.debug("QR event dispatch error on line %s: %s", self.id, exc)
        self.counter.dispatch_qr(payload, read["known"])

    def recent_reads(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.qr_recent)[:limit]

    def reset(self) -> None:
        self.counter.reset_counts()
        for aux in self.aux_counters.values():
            aux.reset_counts()
        with self._lock:
            for key in self.qr_stats:
                self.qr_stats[key] = 0
            self.qr_recent.clear()


# ── All lines ─────────────────────────────────────────────────────────────────

class LineManager:
    def __init__(self) -> None:
        self._lines: Dict[str, LineRuntime] = {}
        self._order: List[str] = []
        self._camera_map: Dict[str, Tuple[str, str]] = {}
        self._lock = threading.RLock()
        # model_id -> loaded engine, shared by every line that picks the model
        self._engines: Dict[str, Dict[str, Any]] = {}

    # ── Configuration ─────────────────────────────────────────────────────────

    def apply_state(self) -> None:
        """Rebuild runtimes from the saved lines; counters of existing lines are kept."""
        from app.services.settings_persistence_service import SettingsPersistenceService

        lines = SettingsPersistenceService.get_lines(with_logic=False)
        with self._lock:
            seen = []
            camera_map: Dict[str, Tuple[str, str]] = {}
            for line in lines:
                line_id = line["id"]
                runtime = self._lines.get(line_id)
                if runtime is None:
                    counter = counting_service if line_id == PRIMARY_LINE_ID else CountingService(line_id, line.get("name", line_id))
                    runtime = LineRuntime(line_id, line.get("name", line_id), counter)
                    self._lines[line_id] = runtime
                trigger = SettingsPersistenceService.get_line_action_trigger(line_id)
                runtime.configure(line, trigger, is_primary=line_id == PRIMARY_LINE_ID)
                for cam in runtime.cameras:
                    camera_map[cam["camera_id"]] = (line_id, cam.get("role", "vision"))
                seen.append(line_id)
            for gone in set(self._lines) - set(seen):
                runtime = self._lines.pop(gone)
                runtime.counter.event_sink = None
            if PRIMARY_LINE_ID not in self._lines:
                self._lines[PRIMARY_LINE_ID] = LineRuntime(PRIMARY_LINE_ID, "Line 1", counting_service)
                seen.insert(0, PRIMARY_LINE_ID)
            self._order = seen
            self._camera_map = camera_map

    def primary(self) -> LineRuntime:
        with self._lock:
            runtime = self._lines.get(PRIMARY_LINE_ID)
            if runtime is None:
                runtime = LineRuntime(PRIMARY_LINE_ID, "Line 1", counting_service)
                self._lines[PRIMARY_LINE_ID] = runtime
                if PRIMARY_LINE_ID not in self._order:
                    self._order.insert(0, PRIMARY_LINE_ID)
            return runtime

    def get(self, line_id: Optional[str]) -> Optional[LineRuntime]:
        if not line_id:
            return self.primary()
        with self._lock:
            return self._lines.get(line_id)

    def all(self) -> List[LineRuntime]:
        with self._lock:
            if PRIMARY_LINE_ID not in self._lines:
                self.primary()
            return [self._lines[i] for i in self._order if i in self._lines]

    # ── Camera routing ────────────────────────────────────────────────────────

    def route(self, camera_id: str) -> Optional[Tuple[LineRuntime, str]]:
        """(line, role) for a camera. A camera no line claims feeds Line 1 while Line 1 has no cameras."""
        with self._lock:
            owner = self._camera_map.get(camera_id)
            if owner is not None:
                runtime = self._lines.get(owner[0])
                return (runtime, owner[1]) if runtime else None
            primary = self.primary()
            if not primary.cameras:
                return primary, "vision"
            return None

    def fallback_cameras(self, cameras: Dict[str, Any]) -> List[str]:
        """Connected cameras that feed Line 1 because no line claims them."""
        if self.primary().cameras:
            return []
        with self._lock:
            return [cid for cid in cameras if cid not in self._camera_map]

    def counter_for_camera(self, camera_id: Optional[str]) -> CountingService:
        """The counter a camera feeds; Line 1's counter when the camera is not routed."""
        if camera_id:
            routed = self.route(camera_id)
            if routed:
                return routed[0].counter_for(camera_id)
        return counting_service

    def line_id_for_camera(self, camera_id: str) -> Optional[str]:
        routed = self.route(camera_id)
        return routed[0].id if routed else None

    # ── Models ────────────────────────────────────────────────────────────────

    def engine_for_camera(self, camera_id: Optional[str]) -> Tuple[Any, str, Optional[str]]:
        """(engine, model name, model id) for the line that owns the camera."""
        from app.services.vision_service import _get_active_engine

        routed = self.route(camera_id) if camera_id else None
        model_id = routed[0].model_id if routed else None
        if model_id:
            cached = self._engines.get(model_id)
            if cached is not None:
                return cached["engine"], cached["name"], model_id
        return _get_active_engine()

    async def refresh_models(self) -> Dict[str, str]:
        """Load every model a line picks (once each) and drop the ones no line uses.

        Returns {model_id: problem} for models that could not be loaded; those
        lines fall back to the server's active model.
        """
        from app.config import settings
        from app.db.models.model import VisionModel
        from app.db.session import AsyncSessionLocal
        from app.engines.inference_engine import InferenceEngine
        from app.state.application_state import app_state
        import os

        wanted = {ln.model_id for ln in self.all() if ln.model_id}
        problems: Dict[str, str] = {}
        active = app_state.active_model or {}
        for model_id in wanted - set(self._engines):
            if active.get("id") == model_id and active.get("engine") is not None:
                self._engines[model_id] = {"engine": active["engine"], "name": active.get("name", model_id)}
                continue
            try:
                async with AsyncSessionLocal() as db:
                    model = await db.get(VisionModel, model_id)
                if model is None:
                    problems[model_id] = "model not found"
                    continue
                if not model.file_path or not os.path.exists(model.file_path):
                    problems[model_id] = "model file is missing"
                    continue
                engine = await asyncio.to_thread(
                    InferenceEngine,
                    model_path=model.file_path,
                    classes=model.classes,
                    input_size=(model.input_width, model.input_height),
                    confidence_threshold=model.confidence_threshold,
                    nms_threshold=model.nms_threshold,
                    device=settings.INFERENCE_DEVICE,
                )
                if not engine.is_loaded:
                    problems[model_id] = "model failed to load"
                    continue
                task = (model.metadata_json or {}).get("task")
                if task:
                    engine.task = str(task).strip().lower()
                self._engines[model_id] = {"engine": engine, "name": model.name}
                logger.info("Loaded model '%s' for production lines", model.name)
            except Exception as exc:
                logger.exception("Could not load model %s for a production line", model_id)
                problems[model_id] = f"{type(exc).__name__}: {exc}"
        for model_id in set(self._engines) - wanted:
            self._engines.pop(model_id, None)
        for model_id, problem in problems.items():
            logger.warning("Model %s for a production line is unavailable (%s); using the active model", model_id, problem)
        return problems

    def model_name(self, runtime: LineRuntime) -> Optional[str]:
        if runtime.model_id and runtime.model_id in self._engines:
            return self._engines[runtime.model_id]["name"]
        from app.state.application_state import app_state
        active = app_state.active_model or {}
        return active.get("name")

    # ── Cameras ───────────────────────────────────────────────────────────────

    async def connect_line(self, line_id: str) -> Dict[str, Optional[str]]:
        """Connect every camera of a line that is not connected yet. Returns {camera_id: error or None}."""
        from app.db.session import AsyncSessionLocal
        from app.services.camera_service import CameraReconnector, CameraService
        from app.state.application_state import app_state

        runtime = self.get(line_id)
        results: Dict[str, Optional[str]] = {}
        if runtime is None:
            return results
        for cam in runtime.cameras:
            cid = cam["camera_id"]
            if getattr(app_state.cameras.get(cid), "is_connected", False):
                results[cid] = None
                continue
            async with AsyncSessionLocal() as db:
                ok, err = await CameraService.connect_camera(db, cid)
            results[cid] = None if ok else (err or "connection failed")
            if not ok:
                logger.warning("Line '%s': camera %s did not connect, retrying in the background: %s",
                               runtime.name, cid, results[cid])
                CameraReconnector.want(cid)
        return results

    async def disconnect_line(self, line_id: str) -> None:
        from app.db.session import AsyncSessionLocal
        from app.services.camera_service import CameraService

        runtime = self.get(line_id)
        if runtime is None:
            return
        for cam in runtime.cameras:
            async with AsyncSessionLocal() as db:
                await CameraService.disconnect_camera(db, cam["camera_id"])

    async def startup(self) -> None:
        """Load the models lines pick and connect the cameras of running lines set to auto-connect."""
        await self.refresh_models()
        await self.connect_on_startup()

    async def connect_on_startup(self) -> None:
        """Connect the cameras of running lines set to auto-connect."""
        for runtime in self.all():
            if runtime.enabled and runtime.auto_connect and runtime.cameras:
                try:
                    await self.connect_line(runtime.id)
                except Exception:
                    logger.exception("Could not connect the cameras of line '%s'", runtime.name)

    # ── Views ─────────────────────────────────────────────────────────────────

    def summary(self, runtime: LineRuntime) -> Dict[str, Any]:
        from app.events.alarm_events import alarm_manager
        from app.state.application_state import app_state

        cams = dict(app_state.cameras)
        fallback = self.fallback_cameras(cams) if runtime.id == PRIMARY_LINE_ID else []
        counter = runtime.counter
        camera_rows = []
        for cam in runtime.cameras or [{"camera_id": cid, "role": "vision", "counting": True} for cid in fallback]:
            driver = cams.get(cam["camera_id"])
            camera_rows.append({
                "camera_id": cam["camera_id"],
                "name": getattr(driver, "name", None),
                "role": cam.get("role", "vision"),
                "counting": bool(cam.get("counting")),
                "connected": bool(getattr(driver, "is_connected", False)),
                "processed_fps": runtime.fps(cam["camera_id"]),
            })
        alarms = [a for a in alarm_manager.active() if (a.details or {}).get("line_id") == runtime.id]
        with runtime._lock:
            qr_stats = dict(runtime.qr_stats)
        return {
            "id": runtime.id,
            "name": runtime.name,
            "enabled": runtime.enabled,
            "state": runtime.state(cams, fallback),
            "model_id": runtime.model_id,
            "model_name": self.model_name(runtime),
            "sync": {"enabled": runtime.sync_enabled, "window_ms": int(runtime.pairer.window * 1000)},
            "cameras": camera_rows,
            "total_inspected": counter.total_inspected,
            "good_count": counter.good_count,
            "rejected_count": counter.rejected_count,
            "products_per_minute": counter.products_per_minute,
            "yield_percentage": counter.yield_percentage,
            "defect_ppm": counter.defect_ppm,
            "processed_fps": runtime.processed_fps(),
            "min_fps": runtime.min_fps,
            "has_qr": runtime.has_role("qr"),
            "qr": qr_stats,
            "active_alarms": len(alarms),
        }

    def overview(self) -> List[Dict[str, Any]]:
        return [self.summary(r) for r in self.all()]


line_manager = LineManager()
