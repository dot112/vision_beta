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
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from app.services.counting_service import CountingService, counting_service
from app.services.line_config import (
    DEFAULT_SYNC_WINDOW_MS,
    PRIMARY_LINE_ID,
    code_checks,
    code_verdict,
    product_verdict,
    reads_codes,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_FPS_WINDOW_SECONDS = 5.0
# What a camera without a loaded model reports as its model.
NO_MODEL_NAME = "No model"


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

    ``crossing_key`` and ``read_key`` may name the product each side belongs
    to (a QR capture triggered by a crossing knows the product's track). When
    both sides have one, only equal keys pair; otherwise the time decides.
    """

    def __init__(self, window: float, crossing_key: Optional[Callable[[Any], Any]] = None,
                 read_key: Optional[Callable[[Any], Any]] = None):
        self.window = max(0.0, float(window))
        self._crossings: Deque[_Pending] = collections.deque()
        self._reads: Deque[_Pending] = collections.deque()
        self._crossing_key = crossing_key
        self._read_key = read_key

    @staticmethod
    def _key(fn: Optional[Callable[[Any], Any]], item: Any) -> Any:
        if fn is None:
            return None
        try:
            return fn(item)
        except Exception:
            return None

    def _pick(self, queue: Deque[_Pending], t: float, key: Any, key_fn: Optional[Callable[[Any], Any]]) -> Optional[_Pending]:
        first = None
        for pending in queue:
            if abs(t - pending.t) > self.window:
                continue
            other = self._key(key_fn, pending.item)
            if key is not None and other is not None:
                if other == key:
                    return pending
                continue  # another product
            if first is None:
                first = pending
        return first

    def add_crossing(self, t: float, crossing: Any) -> List[Tuple[str, Any, Any]]:
        out = self.expire(t)
        pending = self._pick(self._reads, t, self._key(self._crossing_key, crossing), self._read_key)
        if pending is not None:
            self._reads.remove(pending)
            out.append(("paired", crossing, pending.item))
            return out
        self._crossings.append(_Pending(t, crossing))
        return out

    def add_read(self, t: float, read: Any) -> List[Tuple[str, Any, Any]]:
        out = self.expire(t)
        pending = self._pick(self._crossings, t, self._key(self._read_key, read), self._crossing_key)
        if pending is not None:
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
        # The settings last given to each counter, by its camera id (None: the line's counter).
        self._saved_configs: Dict[Optional[str], Any] = {}
        self.cameras: List[Dict[str, Any]] = []
        self.enabled = True
        self.min_fps = 0.0
        self.yield_target = 0.0
        self.sync_enabled = False
        # Cameras whose codes decide good or reject: {camera_id: {"action", "no_read"}}.
        self.code_checks: Dict[str, Dict[str, str]] = {}
        # A crossing is the counter's record of a product that crossed the count
        # line and is not counted yet; a triggered capture's read carries the
        # track of the product that triggered it.
        self.pairer = SyncPairer(
            DEFAULT_SYNC_WINDOW_MS / 1000.0,
            crossing_key=lambda crossing: crossing.get("track_id"),
            read_key=lambda read: read.get("track_id"),
        )
        # QR readers that take one picture per product: {camera_id: (wire line, delay s)}
        self.capture_triggers: Dict[str, Tuple[int, float]] = {}
        self.last_capture: Optional[Dict[str, Any]] = None
        self._capture_jpeg: Optional[bytes] = None
        self._capture_seq = 0
        self.qr_recent: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        self.qr_stats = {"codes_read": 0, "known": 0, "unknown": 0, "no_reads": 0, "unpaired": 0, "code_rejects": 0}
        self._frames: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._sync_timer: Optional[asyncio.TimerHandle] = None
        self._qr_seq = 0

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure(self, line: Dict[str, Any], trigger: Dict[str, Any]) -> None:
        from app.services.settings_persistence_service import counting_config_from_dict

        self.name = line.get("name") or self.id
        self.enabled = bool(line.get("enabled", True))
        self.min_fps = float(line.get("min_fps") or 0.0)
        self.yield_target = float(line.get("yield_target") or 0.0)
        self.cameras = copy.deepcopy(line.get("cameras") or [])
        self.counter.line_id = self.id
        self.counter.line_name = self.name

        # The count lines are the line's; which classes count as products and
        # which as defects are each vision camera's own (picked from its model).
        counting = self.camera_entry(self.counting_camera_id) if self.counting_camera_id else None
        self._set_config(self.counter, counting_config_from_dict(trigger or {}, counting))

        aux_ids = {c["camera_id"] for c in self.cameras if c.get("role") == "vision" and not c.get("counting")}
        for cid in list(self.aux_counters):
            if cid not in aux_ids:
                self.aux_counters.pop(cid)
                self._saved_configs.pop(cid, None)
        for cam in self.cameras:
            cid = cam["camera_id"]
            if cid not in aux_ids:
                continue
            aux = self.aux_counters.get(cid)
            if aux is None:
                # A second vision camera counts on its own and only fires PLC and
                # send cards that name it; the line totals come from the counting camera.
                aux = CountingService(line_id=self.id, line_name=self.name, camera_id=cid)
                self.aux_counters[cid] = aux
            aux.line_name = self.name
            config = counting_config_from_dict(trigger or {}, cam)
            overrides = {k: cam[k] for k in ("line1_position", "line2_position", "orientation") if k in cam}
            self._set_config(aux, config.model_copy(update=overrides) if overrides else config)

        self.code_checks = code_checks(self.cameras)
        sync = line.get("sync") or {}
        was_on = self.sync_enabled
        self.sync_enabled = bool(sync.get("enabled")) and self.has_role("vision") and self.has_code_reader
        self.pairer.window = int(sync.get("window_ms") or DEFAULT_SYNC_WINDOW_MS) / 1000.0
        self.counter.event_sink = self._on_crossing if self.sync_enabled else None
        if was_on and not self.sync_enabled:
            self._run_on_loop(self._flush_sync)

        self.capture_triggers = {
            c["camera_id"]: (1 if c.get("qr_trigger") == "line1" else 2, int(c.get("qr_trigger_delay_ms") or 0) / 1000.0)
            for c in self.cameras
            if reads_codes(c) and c.get("qr_trigger") in ("line1", "line2")
        }
        self.counter.line_crossing_sink = self._on_wire_crossing if self.capture_triggers else None

    def _set_config(self, counter: CountingService, config: Any) -> None:
        """Give a counter its saved settings, when they changed since they were last given.

        A counter changed while running (POST /counting/config) keeps that
        until the saved settings of its line change.
        """
        if self._saved_configs.get(counter.camera_id) != config:
            self._saved_configs[counter.camera_id] = config
            counter.update_config(config)

    def qr_triggered(self, camera_id: str) -> bool:
        """True when this QR reader decodes one picture per product instead of every frame."""
        return camera_id in self.capture_triggers

    def has_role(self, role: str) -> bool:
        return any(c.get("role") == role for c in self.cameras)

    @property
    def has_code_reader(self) -> bool:
        """A QR reader or a vision camera with Read codes on."""
        return any(reads_codes(c) for c in self.cameras)

    def reads_codes(self, camera_id: str) -> bool:
        entry = self.camera_entry(camera_id)
        return bool(entry and reads_codes(entry))

    def camera_entry(self, camera_id: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.cameras if c.get("camera_id") == camera_id), None)

    def product_list_id(self, camera_id: str) -> Optional[str]:
        """The product list a camera checks its codes against (None: no list, every code is unknown)."""
        entry = self.camera_entry(camera_id)
        return entry.get("product_list_id") if entry else None

    def model_id_for(self, camera_id: Optional[str]) -> Optional[str]:
        """The model a vision camera of this line runs (None: it has none, or it is not one)."""
        entry = self.camera_entry(camera_id) if camera_id else None
        return entry.get("model_id") if entry and entry.get("role") == "vision" else None

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

    def on_qr_read(self, camera_id: str, code: str, code_format: str, t: Optional[float] = None,
                   track_id: Optional[int] = None) -> Dict[str, Any]:
        """Called by a QR reader's thread for each new read. Returns the read record."""
        from app.services.product_service import product_catalog

        list_id = self.product_list_id(camera_id)
        product = product_catalog.lookup(code, list_id)
        read = {
            "code": code,
            "format": code_format,
            "known": product is not None,
            "product_name": product.get("name") if product else None,
            "product_list_id": list_id,
            "camera_id": camera_id,
            "t": time.monotonic() if t is None else t,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "track_id": track_id,
        }
        with self._lock:
            self.qr_stats["codes_read"] += 1
            self.qr_stats["known" if read["known"] else "unknown"] += 1
        self._run_on_loop(self._handle_read, read)
        return read

    # ── Captures on a wire line crossing ──────────────────────────────────────

    def _on_wire_crossing(self, camera_id: Optional[str], line_number: int, track_id: int, class_name: str) -> None:
        """Counter hook (inference thread): a product crossed wire line 1 or 2."""
        counting = self.counting_camera_id
        if counting and camera_id and camera_id != counting:
            return
        from app.services.qr_service import QRReaderPipeline

        for qr_camera, (wire_line, delay) in list(self.capture_triggers.items()):
            if wire_line == line_number:
                QRReaderPipeline.get_worker(qr_camera).request_capture(
                    track_id=track_id, class_name=class_name, wire_line=line_number, delay=delay,
                )

    def on_qr_capture(self, camera_id: str, capture: Dict[str, Any], jpeg: Optional[bytes]) -> Dict[str, Any]:
        """A QR reader took its picture (QR reader thread). Keeps it for the dashboard and reports its codes.

        A test capture (capture["test"]) is only kept for viewing: it sends no
        read, no-read or PLC event.
        """
        with self._lock:
            self._capture_seq += 1
            capture = {**capture, "id": self._capture_seq}
            self.last_capture = capture
            self._capture_jpeg = jpeg
        if capture.get("test"):
            return capture
        if capture.get("codes"):
            for code in self._deciding_first(camera_id, capture["codes"]):
                self.on_qr_read(camera_id, code["code"], code["format"], t=capture.get("t"), track_id=capture.get("track_id"))
        elif not self.sync_enabled:
            # With Sync on, the product's crossing goes out as a no-read when
            # no code pairs with it; without Sync, say so here.
            self._run_on_loop(self._emit_no_read, camera_id, capture)
        return capture

    def _deciding_first(self, camera_id: str, codes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The codes of one picture, the one that decides the product's result first.

        The first code pairs with the product. A product may carry several
        codes; where the camera checks them against a list, the code that is
        in the list decides: one listed code passes "accept only listed codes",
        and one listed code rejects under "reject listed codes".
        """
        if camera_id not in self.code_checks or len(codes) < 2:
            return codes
        return sorted(codes, key=lambda code: not code.get("known"))

    def capture_image(self) -> Tuple[Optional[Dict[str, Any]], Optional[bytes]]:
        with self._lock:
            return (dict(self.last_capture) if self.last_capture else None), self._capture_jpeg

    def _emit_no_read(self, camera_id: str, capture: Dict[str, Any]) -> None:
        with self._lock:
            self.qr_stats["no_reads"] += 1
        self._remember(None, "no_read", paired=None, class_name=capture.get("class_name"), camera_id=camera_id)
        self._qr_seq += 1
        payload = {
            "event": "QR_CODE_NO_READ",
            "timestamp": capture.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            "line_id": self.id,
            "line_name": self.name,
            "camera_id": camera_id,
            "code": None,
            "format": None,
            "qr_status": "no_read",
            "known": False,
            "product_name": None,
            "paired": None,
            "track_id": capture.get("track_id"),
            "class_name": capture.get("class_name"),
        }
        plc_event = {
            "event_id": f"qr_{self.id}_{self._qr_seq}",
            "event_type": "qr_read",
            "line_id": self.id,
            "camera_id": camera_id,
            "qr_code": None,
            "qr_status": "no_read",
            "qr_paired": None,
            "timestamp": time.time(),
        }
        self._publish(("qr_code",), payload, plc_event)

    def _handle_read(self, read: Dict[str, Any]) -> None:
        if self.sync_enabled:
            self._process(self.pairer.add_read(read["t"], read))
            self._schedule_expiry()
        elif read["camera_id"] in self.code_checks and not self.has_role("vision"):
            # No vision camera on the line: the code alone decides, and each read is one product.
            self._emit_code_product(read)
        else:
            self._emit_qr(read, paired=None)

    def _on_crossing(self, crossing: Dict[str, Any]) -> None:
        """Counter event sink while Sync is on (called from an inference thread).

        The product is not counted yet: it is held until its code arrives or
        the Sync window ends, then counted once with its final result.
        """
        t = time.monotonic()
        if self._loop() is None:
            # No loop to wait on: count it on the vision result rather than lose it.
            self.counter.finish_crossing(crossing)
            return
        self._run_on_loop(self._handle_crossing, crossing, t)

    def _handle_crossing(self, crossing: Dict[str, Any], t: float) -> None:
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
                # One result per product: the vision camera's and, where a
                # camera checks codes, the paired code's (or the missing code's).
                reject, reason = product_verdict(
                    bool(crossing["is_defect"]), read if kind == "paired" else None, self.code_checks,
                )
                result = self._result(reject) if self.code_checks else None
                if kind == "paired":
                    status = "known" if read["known"] else "unknown"
                    fields = {
                        "qr_code": read["code"],
                        "qr_format": read["format"],
                        "qr_status": status,
                        "product_name": read["product_name"],
                        "qr_paired": True,
                    }
                    self._remember(read, status, paired=True, class_name=crossing.get("class_name"),
                                   result=result, reason=reason)
                else:
                    status = "no_read"
                    fields = {"qr_code": None, "qr_format": None, "qr_status": "no_read", "product_name": None, "qr_paired": False}
                    with self._lock:
                        self.qr_stats["no_reads"] += 1
                    self._remember(None, "no_read", paired=False, class_name=crossing.get("class_name"),
                                   camera_id=crossing.get("camera_id"), result=result, reason=reason)
                if reject and reason not in (None, "vision_class"):
                    with self._lock:
                        self.qr_stats["code_rejects"] += 1
                self.counter.finish_crossing(
                    crossing,
                    reject=reject,
                    reason=reason,
                    fields=fields,
                    plc_fields={"qr_code": fields["qr_code"], "qr_status": status, "qr_paired": fields["qr_paired"]},
                    # With a code check the result may come as late as the Sync
                    # window; a reject gate's travel delay still runs from the
                    # crossing, where the product was.
                    delay_from_crossing=bool(self.code_checks),
                )
            except Exception:
                logger.exception("Line %s: failed to send a synced event", self.id)

    @staticmethod
    def _result(reject: bool) -> str:
        return "reject" if reject else "good"

    def _remember(self, read: Optional[Dict[str, Any]], status: str, paired: Optional[bool],
                  class_name: Optional[str] = None, camera_id: Optional[str] = None,
                  result: Optional[str] = None, reason: Optional[str] = None) -> None:
        entry = {
            "timestamp": read["timestamp"] if read else datetime.now(timezone.utc).isoformat(),
            "code": read["code"] if read else None,
            "format": read["format"] if read else None,
            "status": status,
            "product_name": read["product_name"] if read else None,
            "camera_id": read["camera_id"] if read else camera_id,
            "paired": paired,
            "class_name": class_name,
            # The product's result where a camera checks codes ("good" / "reject"
            # and why); None where codes are only reported.
            "result": result,
            "reject_reason": reason,
        }
        with self._lock:
            self.qr_recent.appendleft(entry)

    def _qr_events(self, read: Dict[str, Any], status: str, paired: Optional[bool]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """The message and the PLC event of one code read."""
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
        return payload, plc_event

    def _publish(self, topics: Tuple[str, ...], payload: Dict[str, Any], plc_event: Dict[str, Any]) -> None:
        """Send one code event to the event bus, the line's PLC cards and its send cards."""
        loop = self._loop()
        if loop is not None:
            try:
                from app.events.event_bus import event_bus
                from app.services.plc_dispatcher_service import PLCDispatcherService
                for topic in topics:
                    asyncio.run_coroutine_threadsafe(event_bus.publish(topic, payload), loop)
                asyncio.run_coroutine_threadsafe(PLCDispatcherService.evaluate(plc_event), loop)
            except Exception as exc:
                logger.debug("QR event dispatch error on line %s: %s", self.id, exc)
        self.counter.send(plc_event, payload)

    def _emit_qr(self, read: Dict[str, Any], paired: Optional[bool]) -> None:
        status = "known" if read["known"] else "unknown"
        self._remember(read, status, paired)
        payload, plc_event = self._qr_events(read, status, paired)
        self._publish(("qr_code",), payload, plc_event)

    def _emit_code_product(self, read: Dict[str, Any]) -> None:
        """A code read on a line without a vision camera, by a camera that checks codes:
        the read is one product, good or reject, counted like any other product."""
        reason = code_verdict(self.code_checks[read["camera_id"]]["action"], read["known"])
        reject = reason is not None
        status = "known" if read["known"] else "unknown"
        self._remember(read, status, paired=None, result=self._result(reject), reason=reason)
        if reject:
            with self._lock:
                self.qr_stats["code_rejects"] += 1
        totals = self.counter.count_product(read["product_name"] or "code not in list", reject)
        payload, plc_event = self._qr_events(read, status, paired=None)
        payload.update({
            "result": "REJECTED" if reject else "PASSED",
            "is_defect": reject,
            "reject_reason": reason,
            "qr_code": read["code"],
            **totals,
        })
        # One event for the product: it fires the good / reject and counter cards
        # like a product seen by a vision camera, and the code cards as a read does.
        plc_event.update({
            "event_type": "code_product",
            "result": self._result(reject),
            "reject_reason": reason,
            "good_count": totals["good_count"],
            "reject_count": totals["rejected_count"],
            "detected_classes": [],
            "crossed_at": read["t"],
            "delay_from_crossing": True,
        })
        self._publish(("qr_code", "reading"), payload, plc_event)

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

def pick_default_camera(cameras: List[Any], owned: Any, selected_id: Optional[str]) -> Optional[Any]:
    """The camera that feeds Line 1 while Line 1 has none of its own.

    ``cameras`` are the saved cameras, the one changed last first. Cameras in
    ``owned`` belong to a line and are left out. Of the rest: the one selected
    on the Cameras page, else the one last connected, else the first.
    """
    free = [cam for cam in cameras if cam.id not in owned]
    if not free:
        return None
    return (
        next((cam for cam in free if cam.id == selected_id), None)
        or next((cam for cam in free if cam.is_active), None)
        or free[0]
    )


class LineManager:
    def __init__(self) -> None:
        self._lines: Dict[str, LineRuntime] = {}
        self._order: List[str] = []
        self._camera_map: Dict[str, Tuple[str, str]] = {}
        self._lock = threading.RLock()
        # model_id -> {"engine", "name"}: one loaded copy per model, shared by
        # every camera that runs it
        self._engines: Dict[str, Dict[str, Any]] = {}
        # model_id -> why a model a camera runs could not be loaded
        self._model_problems: Dict[str, str] = {}
        # model_id -> name, of every model a load was tried for
        self._model_names: Dict[str, str] = {}
        # A model a detect call named that no camera runs: (model_id, entry).
        # One at a time, so calls cannot fill the GPU with models.
        self._api_model: Optional[Tuple[str, Dict[str, Any]]] = None

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
                runtime.configure(line, trigger)
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

    _no_model: Any = None

    @classmethod
    def no_model_engine(cls) -> Any:
        """An engine without a model: it draws a camera's video and detects nothing."""
        if cls._no_model is None:
            from app.engines.inference_engine import InferenceEngine
            cls._no_model = InferenceEngine(device="cpu")
        return cls._no_model

    def model_for_camera(self, camera_id: Optional[str]) -> Optional[str]:
        """The model picked for a vision camera on Line setup."""
        routed = self.route(camera_id) if camera_id else None
        return routed[0].model_id_for(camera_id) if routed else None

    def default_model_id(self) -> Optional[str]:
        """The model of Line 1's counting camera: what version 1 called the active model."""
        primary = self.primary()
        return primary.model_id_for(primary.counting_camera_id)

    def engine_for_camera(self, camera_id: Optional[str]) -> Tuple[Any, str, Optional[str]]:
        """(engine, model name, model id) for a camera: the model picked for it on Line setup.

        A camera without a model, or whose model is not loaded, gets the
        engine without a model (check ``engine.is_loaded`` before predicting).
        """
        model_id = self.model_for_camera(camera_id)
        entry = self._engines.get(model_id) if model_id else None
        if entry is None:
            return self.no_model_engine(), NO_MODEL_NAME, None
        return entry["engine"], entry["name"], model_id

    async def api_engine(self, model_id: Optional[str] = None, camera_id: Optional[str] = None) -> Tuple[Any, str, str]:
        """(engine, model name, model id) for a detect call.

        The model the call names; else the camera's own; else the model of
        Line 1's counting camera, which is what version 1 called the active
        model. Raises ValueError, saying what to do, when there is none or it
        cannot be loaded.
        """
        wanted = model_id or self.model_for_camera(camera_id) or self.default_model_id()
        if not wanted:
            raise ValueError(
                "No vision model to run: pick a model for Line 1's vision camera on Line setup, "
                "or name one with model_id"
            )
        entry = self._engines.get(wanted)
        if entry is None and self._api_model is not None and self._api_model[0] == wanted:
            entry = self._api_model[1]
        if entry is None and wanted in self._model_problems:
            raise ValueError(f"The vision model is not loaded: {self._model_problems[wanted]}")
        if entry is None:
            entry, problem = await self._load_engine(wanted)
            if entry is None:
                raise ValueError(f"The vision model cannot be used: {problem}")
            previous, self._api_model = self._api_model, (wanted, entry)
            if previous is not None:
                self._release(previous[1])
        return entry["engine"], entry["name"], wanted

    @staticmethod
    def _release(entry: Optional[Dict[str, Any]]) -> None:
        """Free the engine of a model that was dropped.

        Leaving it to the garbage collector could free a GPU session while
        another model runs, which ends the process on DirectML.
        """
        close = getattr(entry["engine"], "close", None) if entry else None
        if close is not None:
            close()

    async def _load_engine(self, model_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Load one model of the AI models page. Returns (entry, None), or (None, why it could not be loaded)."""
        from app.config import settings
        from app.db.models.model import VisionModel
        from app.db.session import AsyncSessionLocal
        from app.engines.inference_engine import InferenceEngine
        import os

        try:
            async with AsyncSessionLocal() as db:
                model = await db.get(VisionModel, model_id)
            if model is None:
                return None, "the model no longer exists"
            self._model_names[model_id] = model.name
            if not model.file_path or not os.path.exists(model.file_path):
                return None, "the model's file is missing"
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
                return None, "the model's file could not be loaded; the server log says why"
            task = (model.metadata_json or {}).get("task")
            if task:
                engine.task = str(task).strip().lower()
            if engine.classes and list(engine.classes) != list(model.classes or []):
                # The class names written in the file are the ones the cameras' lists are picked from.
                async with AsyncSessionLocal() as db:
                    saved = await db.get(VisionModel, model_id)
                    if saved is not None:
                        saved.classes = list(engine.classes)
                        await db.commit()
            logger.info("Loaded vision model '%s'", model.name)
            return {"engine": engine, "name": model.name}, None
        except Exception as exc:
            logger.exception("Could not load vision model %s", model_id)
            return None, f"{type(exc).__name__}: {exc}"

    async def refresh_models(self) -> Dict[str, str]:
        """Load every model a vision camera runs (one copy each) and drop the ones no camera runs.

        Returns {model_id: problem} for the models that could not be loaded.
        Nothing is detected on their cameras: each such camera of a running
        line raises an alarm, and so does a vision camera without a model.
        """
        wanted = {
            cam["model_id"]
            for runtime in self.all()
            for cam in runtime.cameras
            if cam.get("role") == "vision" and cam.get("model_id")
        }
        problems: Dict[str, str] = {}
        for model_id in wanted - set(self._engines):
            if self._api_model is not None and self._api_model[0] == model_id:
                entry, problem = self._api_model[1], None
                self._api_model = None
            else:
                entry, problem = await self._load_engine(model_id)
            if entry is None:
                problems[model_id] = problem or "the model could not be loaded"
                logger.warning("Vision model %s is not loaded (%s); nothing is detected on its cameras",
                               self._model_names.get(model_id, model_id), problems[model_id])
                continue
            self._engines[model_id] = entry
        for model_id in set(self._engines) - wanted:
            self._release(self._engines.pop(model_id, None))
        self._model_problems = problems
        self._report_model_alarms()
        return problems

    def _report_model_alarms(self) -> None:
        """One alarm for each vision camera of a running line that has no model it can run."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
        from app.state.application_state import app_state

        live = set()
        for runtime in self.all():
            if not runtime.enabled:
                continue
            for cam in runtime.cameras:
                if cam.get("role") != "vision":
                    continue
                model_id = cam.get("model_id")
                if not model_id:
                    problem = "has no vision model"
                elif model_id not in self._engines:
                    name = self._model_names.get(model_id)
                    named = f" '{name}'" if name else ""
                    problem = f"cannot run its vision model{named}: {self._model_problems.get(model_id, 'it is not loaded')}"
                else:
                    continue
                source = f"camera_model:{cam['camera_id']}"
                live.add(source)
                camera_name = getattr(app_state.cameras.get(cam["camera_id"]), "name", None) or cam["camera_id"]
                alarm_manager.raise_alarm(
                    AlarmCode.CAMERA_MODEL_UNAVAILABLE, source,
                    f"Camera '{camera_name}' on line '{runtime.name}' {problem}. Nothing is detected or counted on it.",
                    AlarmSeverity.CRITICAL,
                    {"line_id": runtime.id, "camera_id": cam["camera_id"], "model_id": model_id},
                )
        for alarm in alarm_manager.active("camera_model:"):
            if alarm.source not in live:
                alarm_manager.clear_alarm(alarm.code, alarm.source, "the camera has a loaded model, was removed, or its line stopped")

    def loaded_models(self) -> List[Dict[str, str]]:
        """The models in memory: [{"id", "name"}]."""
        return [
            {"id": model_id, "name": entry["name"]}
            for model_id, entry in list(self._engines.items())
            if getattr(entry["engine"], "is_loaded", True)
        ]

    def model_state(self, model_id: Optional[str]) -> Dict[str, Any]:
        """Whether a model is in memory and, when a camera runs it and it is not, why."""
        loaded = bool(model_id) and model_id in self._engines
        return {"loaded": loaded, "error": None if loaded else self._model_problems.get(model_id or "")}

    def model_name(self, model_id: Optional[str]) -> Optional[str]:
        entry = self._engines.get(model_id or "")
        return entry["name"] if entry else self._model_names.get(model_id or "")

    def set_model_task(self, model_id: str, task: str) -> None:
        """A model's detect / segment switch, applied to its loaded copy at once."""
        entries = [self._engines.get(model_id)]
        if self._api_model is not None and self._api_model[0] == model_id:
            entries.append(self._api_model[1])
        for entry in entries:
            if entry is not None:
                entry["engine"].task = task

    def forget_model(self, model_id: str) -> None:
        """A model was deleted: drop the copy a detect call loaded and what was known of it."""
        if self._api_model is not None and self._api_model[0] == model_id:
            self._release(self._api_model[1])
            self._api_model = None
        self._model_names.pop(model_id, None)

    def inferring(self, camera_id: str) -> Optional[bool]:
        """None: no running line runs a model on this camera. Else whether its model is loaded."""
        routed = self.route(camera_id)
        if routed is None or routed[1] != "vision" or not routed[0].enabled:
            return None
        return self.model_for_camera(camera_id) in self._engines

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
        """Load the models the vision cameras run and connect the cameras of running lines."""
        await self.refresh_models()
        await self.connect_on_startup()

    async def connect_on_startup(self) -> None:
        """Connect the cameras of every running line, when the Cameras page switch
        "Connect cameras when the server starts" is on.

        This is the only place cameras are connected at startup. A camera that
        belongs to no line is left alone, with one exception: while Line 1 has
        no camera assigned it is fed by the last active camera, so that one is
        connected, as before production lines existed.
        """
        from app.services.settings_persistence_service import SettingsPersistenceService

        if not SettingsPersistenceService.camera_auto_connect():
            return
        for runtime in self.all():
            if not runtime.enabled:
                continue
            try:
                if runtime.cameras:
                    await self.connect_line(runtime.id)
                elif runtime.id == PRIMARY_LINE_ID:
                    await self._connect_last_active_camera()
            except Exception:
                logger.exception("Could not connect the cameras of line '%s'", runtime.name)

    async def _connect_last_active_camera(self) -> None:
        """Line 1 without cameras: connect the camera that feeds it by default.

        The camera last selected on the Cameras page, else the one last
        connected, else the one changed last. Cameras of other lines are not
        candidates: they feed their own line.
        """
        from sqlalchemy import select

        from app.db.models.camera import Camera
        from app.db.session import AsyncSessionLocal
        from app.services.camera_service import CameraReconnector, CameraService
        from app.services.settings_persistence_service import SettingsPersistenceService

        with self._lock:
            owned = set(self._camera_map)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Camera).order_by(Camera.updated_at.desc()))
            target = pick_default_camera(
                list(result.scalars().all()), owned, SettingsPersistenceService.get_active_camera_id(),
            )
            if target is None:
                logger.info("No camera to connect for Line 1 on startup")
                return
            target_id, target_name = target.id, target.name
            ok, err = await CameraService.connect_camera(db, target_id)
        if ok:
            SettingsPersistenceService.set_active_camera_id(target_id)
            logger.info("Connected the last active camera '%s' [%s] for Line 1 on startup", target_name, target_id)
        else:
            logger.warning("Camera '%s' did not connect on startup, retrying in the background: %s", target_name, err)
            CameraReconnector.want(target_id)

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
            row = {
                "camera_id": cam["camera_id"],
                "name": getattr(driver, "name", None),
                "role": cam.get("role", "vision"),
                "counting": bool(cam.get("counting")),
                "connected": bool(getattr(driver, "is_connected", False)),
                "processed_fps": runtime.fps(cam["camera_id"]),
            }
            if row["role"] == "vision":
                # The model picked for the camera, and whether it is in memory.
                model_state = self.model_state(cam.get("model_id"))
                row["model_id"] = cam.get("model_id")
                row["model_name"] = self.model_name(cam.get("model_id"))
                row["model_loaded"] = model_state["loaded"]
                row["model_error"] = model_state["error"]
                row["expected_classes"] = list(cam.get("expected_classes") or [])
                row["defect_classes"] = list(cam.get("defect_classes") or [])
            if reads_codes(cam):
                if row["role"] == "vision":
                    row["read_codes"] = True
                row["qr_trigger"] = cam.get("qr_trigger", "continuous")
                row["qr_trigger_delay_ms"] = int(cam.get("qr_trigger_delay_ms") or 0)
                row["code_type"] = cam.get("code_type", "all")
                row["product_list_id"] = cam.get("product_list_id")
                row["qr_action"] = cam.get("qr_action", "report")
                row["qr_no_read"] = cam.get("qr_no_read", "ignore")
            camera_rows.append(row)
        alarms = [a for a in alarm_manager.active() if (a.details or {}).get("line_id") == runtime.id]
        with runtime._lock:
            qr_stats = dict(runtime.qr_stats)
            last_capture_id = runtime.last_capture["id"] if runtime.last_capture else None
        return {
            "id": runtime.id,
            "name": runtime.name,
            "enabled": runtime.enabled,
            "state": runtime.state(cams, fallback),
            # The models of the line's vision cameras, each once.
            "model_name": ", ".join(dict.fromkeys(
                row["model_name"] for row in camera_rows if row.get("model_name")
            )) or None,
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
            "yield_target": runtime.yield_target,
            "has_qr": runtime.has_code_reader,
            "qr": qr_stats,
            "last_capture_id": last_capture_id,
            "active_alarms": len(alarms),
        }

    def overview(self) -> List[Dict[str, Any]]:
        return [self.summary(r) for r in self.all()]


line_manager = LineManager()
