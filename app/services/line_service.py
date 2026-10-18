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
import functools
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from app.services.counting_service import CountingService, counting_service
from app.services.line_config import (
    CODE_REASONS,
    DEFAULT_PICTURE_WAIT_MS,
    DEFAULT_SYNC_WINDOW_MS,
    PRIMARY_LINE_ID,
    REASON_INSPECTION_TIMEOUT,
    REASON_PRODUCT_CHECK,
    REASON_SORTING_MISMATCH,
    REASON_VISION,
    camera_station,
    code_checks,
    code_verdict,
    join_settings,
    joined_cameras,
    picture_source,
    product_result,
    product_reader,
    product_sensors,
    reads_codes,
    sorting_lane,
)
from app.services.trigger_pairing import PicturePairer

# Sorting verification remembers the line's last products that had a code.
SORTING_MEMORY = 200
# What each camera in trigger mode counts (trigger_view, count_trigger).
TRIGGER_COUNTERS = ("triggers", "pictures", "results", "missing", "late", "extra")
from app.utils.logger import get_logger

logger = get_logger(__name__)

_FPS_WINDOW_SECONDS = 5.0
# What a camera without a loaded model reports as its model.
NO_MODEL_NAME = "No model"

# Camera trigger with a trigger notice (LineRuntime.trigger_notice / trigger_picture).
# The clocks of the notices and pictures (a test runs them on a clock of its own): they are paired
# on the monotonic clock; a product's time is on the wall clock.
_now = time.monotonic
_wall = time.time
# What waits is expired this long after its window ("Wait for the picture") is over.
PAIRING_MARGIN_S = 0.01
# A paired picture the model is still busy for is offered to it again this often.
PAIR_RETRY_S = 0.02
# Each waiting picture holds a whole frame (about 6 MB at 1080p): at most this many wait for a notice,
# and at most this many paired pictures wait for the model, per camera.
MAX_WAITING_PICTURES = 32
MAX_QUEUED_PAIRS = 64
# Pictures in a row that no notice claimed before camera.picture_unpaired is raised.
UNPAIRED_ALARM_AFTER = 3


def pairing_time(wall: float) -> float:
    """A camera driver's wall-clock picture time (``arrived``) on the clock notices and pictures are paired on."""
    return _now() - (_wall() - wall)


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

    @staticmethod
    def _comparable(key: Any, other: Any) -> bool:
        """Both sides name their product, the same way. Keys of two kinds, ``(kind, value)`` with
        different kinds (a request number and a track), do not decide: the time does."""
        if key is None or other is None:
            return False
        if isinstance(key, tuple) and isinstance(other, tuple) and key[:1] != other[:1]:
            return False
        return True

    def _pick(self, queue: Deque[_Pending], t: float, key: Any, key_fn: Optional[Callable[[Any], Any]]) -> Optional[_Pending]:
        first = None
        for pending in queue:
            if abs(t - pending.t) > self.window:
                continue
            other = self._key(key_fn, pending.item)
            if self._comparable(key, other):
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


# ── Joined cameras ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class JoinedStation:
    """A vision camera whose result joins the counting camera's product."""
    camera_id: str
    offset: float   # s from the counting camera to this camera (negative: this camera is before it)
    window: float   # s: how far from the expected time a crossing still matches
    missing: str    # "reject" or "ignore": what a product this camera saw nothing of gets


class _Product:
    """A product the counting camera saw cross, waiting for its code and its joined cameras."""

    __slots__ = ("crossing", "t", "stations", "code_waiting", "kind", "read", "seen", "times")

    def __init__(self, crossing: Dict[str, Any], t: float, stations: Tuple[JoinedStation, ...], code_waiting: bool):
        self.crossing = crossing
        self.t = t
        # The joined cameras as they were set when the product crossed: a change
        # of the settings meanwhile does not change what it waits for.
        self.stations = stations
        self.code_waiting = code_waiting
        self.kind: Optional[str] = None   # "paired" / "no_read" once Sync is done with it; None without Sync
        self.read: Optional[Dict[str, Any]] = None
        self.seen: Dict[str, Optional[Dict[str, Any]]] = {}  # camera -> its crossing, or None: saw nothing
        self.times: Dict[str, float] = {}

    def deadline(self, station: JoinedStation) -> float:
        return self.t + station.offset + station.window

    def done(self) -> bool:
        return not self.code_waiting and all(st.camera_id in self.seen for st in self.stations)

    def results(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """What each joined camera saw of the product, in card order (product_verdict's ``stations``)."""
        return {st.camera_id: self.seen.get(st.camera_id) for st in self.stations}

    def missing(self) -> Dict[str, str]:
        return {st.camera_id: st.missing for st in self.stations}


class ProductAssembly:
    """One result per product from the counting camera, its code and the joined cameras.

    Bookkeeping only, like SyncPairer: the caller passes monotonic times and
    calls expire(now) when next_deadline() is reached; every call returns the
    products that are complete, oldest first.

    A joined camera's crossing at ``t_j`` matches the product the counting
    camera saw at ``t_c`` when ``|t_j - offset - t_c| <= window`` (the closest
    product that has no result from that camera yet). A camera before the
    counting camera (negative offset) sees a product first: its crossing waits
    until no product can match it any more, then it is dropped as unmatched.
    A product is complete when its code is done (with Sync on) and every joined
    camera has answered or passed its deadline, ``t_c + offset + window``.
    """

    def __init__(self) -> None:
        self.stations: Tuple[JoinedStation, ...] = ()
        self._products: List[_Product] = []
        self._waiting: Dict[str, Deque[Tuple[float, Dict[str, Any]]]] = {}
        self._stats: Dict[str, Dict[str, int]] = {}
        self._stats_lock = threading.Lock()

    def configure(self, stations: List[JoinedStation]) -> None:
        self.stations = tuple(stations)
        ids = {st.camera_id for st in self.stations}
        for camera_id in list(self._waiting):
            if camera_id not in ids:
                del self._waiting[camera_id]
        with self._stats_lock:
            for camera_id in ids:
                self._stats.setdefault(camera_id, {"matched": 0, "unmatched": 0, "rejects": 0, "no_result": 0})

    def station(self, camera_id: str) -> Optional[JoinedStation]:
        return next((st for st in self.stations if st.camera_id == camera_id), None)

    def holds(self, crossing: Dict[str, Any]) -> bool:
        return any(product.crossing is crossing for product in self._products)

    @property
    def waiting(self) -> int:
        return len(self._products) + sum(len(queue) for queue in self._waiting.values())

    def _count(self, camera_id: str, key: str) -> None:
        with self._stats_lock:
            if camera_id in self._stats:
                self._stats[camera_id][key] += 1

    def _match(self, product: _Product, station: JoinedStation, crossing: Dict[str, Any], t: float) -> None:
        product.seen[station.camera_id] = crossing
        product.times[station.camera_id] = t
        self._count(station.camera_id, "matched")
        if crossing.get("is_defect"):
            self._count(station.camera_id, "rejects")

    def add_product(self, crossing: Dict[str, Any], t: float, code_waiting: bool) -> List[_Product]:
        """The counting camera saw a product cross at ``t``."""
        product = _Product(crossing, t, self.stations, code_waiting)
        for station in self.stations:
            queue = self._waiting.get(station.camera_id)
            if not queue:
                continue
            gaps = [(abs(t_j - station.offset - t), index) for index, (t_j, _) in enumerate(queue)]
            gap, index = min(gaps)
            if gap <= station.window:
                t_j, seen = queue[index]
                del queue[index]
                self._match(product, station, seen, t_j)
        self._products.append(product)
        return self.expire(t)

    def add_station(self, camera_id: str, crossing: Dict[str, Any], t: float) -> List[_Product]:
        """A joined camera saw a product cross at ``t``."""
        station = self.station(camera_id)
        if station is None:
            return self.expire(t)
        expected = t - station.offset
        candidates = [
            (abs(expected - product.t), order, product)
            for order, product in enumerate(self._products)
            if station in product.stations and camera_id not in product.seen
            and abs(expected - product.t) <= station.window
        ]
        if candidates:
            self._match(min(candidates, key=lambda c: c[:2])[2], station, crossing, t)
        else:
            self._waiting.setdefault(camera_id, collections.deque()).append((t, crossing))
        return self.expire(t)

    def code_result(self, crossing: Dict[str, Any], kind: str, read: Optional[Dict[str, Any]], now: float) -> List[_Product]:
        """Sync is done with a product: its code (``kind`` "paired") or none ("no_read")."""
        for product in self._products:
            if product.crossing is crossing:
                product.code_waiting = False
                product.kind = kind
                product.read = read if kind == "paired" else None
                break
        return self.expire(now)

    def expire(self, now: float) -> List[_Product]:
        for station in self.stations:
            queue = self._waiting.get(station.camera_id)
            # A crossing no product can claim any more: the counting camera
            # would have seen its product by t_j - offset + window.
            while queue and now > queue[0][0] - station.offset + station.window:
                queue.popleft()
                self._count(station.camera_id, "unmatched")
        finished: List[_Product] = []
        for product in list(self._products):
            for station in product.stations:
                if station.camera_id not in product.seen and now > product.deadline(station):
                    product.seen[station.camera_id] = None
                    self._count(station.camera_id, "no_result")
            if product.done():
                self._products.remove(product)
                finished.append(product)
        return finished

    def next_deadline(self) -> Optional[float]:
        times = [
            product.deadline(station)
            for product in self._products
            for station in product.stations
            if station.camera_id not in product.seen
        ]
        times += [
            queue[0][0] - station.offset + station.window
            for station in self.stations
            for queue in (self._waiting.get(station.camera_id),)
            if queue
        ]
        return min(times) if times else None

    def stats(self, camera_id: str) -> Optional[Dict[str, int]]:
        """matched, unmatched (crossings no product claimed), rejects, no_result (products it saw nothing of)."""
        with self._stats_lock:
            found = self._stats.get(camera_id)
            return dict(found) if found is not None else None

    def reset_stats(self) -> None:
        with self._stats_lock:
            for stats in self._stats.values():
                for key in stats:
                    stats[key] = 0


@dataclass
class CodeActor:
    """Who the audit log names for a batch a code set: the camera (as PLCActor names a PLC channel)."""
    username: str                # "camera:<camera name>"
    role: str = "CAMERA"
    clearance_level: int = 2


def _camera_name(camera_id: Optional[str]) -> Optional[str]:
    from app.state.application_state import app_state

    return getattr(app_state.cameras.get(camera_id or ""), "name", None)


def _carton(sequence: Any) -> str:
    """How a message names a product: by its sequence number when it has one."""
    return f"sequence {sequence}" if sequence is not None else "the product (no sequence)"


def _pair_key(item: Dict[str, Any]) -> Any:
    """Which product a crossing or a read belongs to, for Sync: the result handshake's request
    both carry (one "Take a picture" request), else the track a wire line capture carries."""
    if item.get("request_id") is not None:
        return ("request", item["request_id"])
    return ("track", item["track_id"]) if item.get("track_id") is not None else None


class _LoopTimer:
    """``loop.call_later`` that can be started and cancelled from any thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, delay: float, fn: Callable[[], None]) -> None:
        self._fn = fn
        self._cancelled = False
        self._handle: Optional[asyncio.TimerHandle] = None

        def start() -> None:
            if not self._cancelled:
                self._handle = loop.call_later(max(0.0, delay), self._fire)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            start()
        else:
            loop.call_soon_threadsafe(start)

    def _fire(self) -> None:
        if not self._cancelled:
            try:
                self._fn()
            except Exception:
                logger.exception("A line timer failed")

    def cancel(self) -> None:
        self._cancelled = True
        if self._handle is not None:
            self._handle.cancel()


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
        # The line's product checks that are switched on: reject a product whose rules pass.
        self.product_checks: List[Dict[str, Any]] = []
        # A crossing is the counter's record of a product that crossed the count
        # line and is not counted yet; a triggered capture's read carries the
        # track of the product that triggered it, or the result handshake's request.
        self.pairer = SyncPairer(DEFAULT_SYNC_WINDOW_MS / 1000.0, crossing_key=_pair_key, read_key=_pair_key)
        # QR readers that take one picture per product: {camera_id: (wire line, delay s)}
        self.capture_triggers: Dict[str, Tuple[int, float]] = {}
        # Code readers that take one picture each time a PLC signal asks.
        self.signal_readers: set = set()
        # The line's PLC signal cards. With a product sensor among them the line gets its
        # products from the PLC, not from a vision camera.
        self.plc_inputs: List[Dict[str, Any]] = []
        self.has_product_sensor = False
        self._sensor_seq = 0
        # Cameras in trigger mode: the track number of the next triggered product, and one
        # trigger at a time per camera (trigger_inspection).
        self._trigger_seq = 0
        self._trigger_seq_lock = threading.Lock()
        self._trigger_locks: Dict[str, asyncio.Lock] = {}
        # Per camera in trigger mode: its latest capture and that capture's JPEG (None when no picture
        # came), and its counters. A capture id goes up by one with each capture of the line.
        self._trigger_captures: Dict[str, Dict[str, Any]] = {}
        self._trigger_jpegs: Dict[str, Optional[bytes]] = {}
        self._trigger_capture_seq = 0
        self._trigger_counters: Dict[str, Dict[str, int]] = {}
        # Cameras that take their own pictures with a trigger notice: (pairer, its window in s) each while
        # the line runs; the paired pictures waiting for the model, as (picture, the notice's context, the
        # picture's wall-clock time); the timer that offers them again; and per camera the pictures in a
        # row no notice claimed, counted from the latest paired picture's time.
        self._pairers: Dict[str, Tuple[PicturePairer, float]] = {}
        self._pair_queues: Dict[str, Deque[Tuple[Any, Dict[str, Any], float]]] = {}
        self._pair_retry: Dict[str, Any] = {}
        self._unpaired: Dict[str, int] = {}
        self._last_paired_at: Dict[str, float] = {}
        # Per camera, the edges (pairing-clock times) of its notice inputs that are still being read
        # (notice_edge): nothing that could pair with them is expired meanwhile.
        self._held: Dict[str, List[float]] = {}
        self._pair_lock = threading.RLock()
        # The line's batch (work order) number, "" for none.
        self.batch = ""
        # The input whose value is each carton's sequence number (None: the line has none).
        self.sequence_input_id: Optional[str] = None
        # Capture request -> result -> the PLC's acknowledge (switched off unless the line sets it).
        from app.services.handshake import Handshake
        self.handshake = Handshake(schedule=self._schedule_timer, on_result_timeout=self._handshake_result_timeout,
                                   on_ack_timeout=self._handshake_ack_timeout)
        # Batches from codes that the line refused (said once each).
        self._batch_errors: set = set()
        self.last_capture: Optional[Dict[str, Any]] = None
        self._capture_jpeg: Optional[bytes] = None
        self._capture_seq = 0
        self.qr_recent: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        # Sorting verification: {camera_id: "accepted" / "rejected"}, the last products that had a
        # code ({"code", "sequence", "result", "at", "verified"}), and what the cameras found.
        self.sorting_lanes: Dict[str, str] = {}
        self._sorting_memory: Deque[Dict[str, Any]] = collections.deque(maxlen=SORTING_MEMORY)
        self._sorting_stats = {"verified": 0, "mismatches": 0, "unmatched": 0}
        self.qr_stats = {"codes_read": 0, "known": 0, "unknown": 0, "no_reads": 0, "unpaired": 0, "code_rejects": 0}
        # Products waiting for the vision cameras that join the counting camera's result.
        self.assembly = ProductAssembly()
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
            self._set_config(aux, counting_config_from_dict(trigger or {}, cam))

        # A joined camera's products are matched to the counting camera's: it counts nothing on its own.
        stations = []
        for cam in joined_cameras(self.cameras) if self.counting_camera_id else []:
            offset, window, missing = join_settings(cam)
            stations.append(JoinedStation(cam["camera_id"], offset / 1000.0, window / 1000.0, missing))
        self.assembly.configure(stations)
        for cid, aux in self.aux_counters.items():
            aux.event_sink = self._joined_sink(cid) if self.assembly.station(cid) else None

        self.plc_inputs = copy.deepcopy(line.get("plc_inputs") or [])
        self.has_product_sensor = bool(product_sensors(line))
        self.set_batch(str(line.get("batch") or ""))
        self.sequence_input_id = line.get("sequence_input_id") or None
        self.handshake.configure(line.get("handshake"))
        # Each product's one event passes the line first when the line adds to it or waits for it.
        self.sorting_lanes = {c["camera_id"]: sorting_lane(c) for c in self.cameras if sorting_lane(c)}
        self.counter.product_hook = self._product_finished \
            if self.sequence_input_id or self.handshake.enabled or self.sorting_lanes else None
        # A product whose capture request already got its one result (a timeout) is not counted again.
        self.counter.product_gate = self._admit_product if self.handshake.enabled else None

        self.code_checks = code_checks(self.cameras)
        self.product_checks = [c for c in copy.deepcopy(line.get("product_checks") or []) if c.get("enabled", True)]
        sync = line.get("sync") or {}
        was_on = self.sync_enabled
        # Sync pairs each product with its code: the products come from a vision camera or a PLC sensor.
        self.sync_enabled = bool(sync.get("enabled")) and self.has_code_reader \
            and (self.has_role("vision") or self.has_product_sensor)
        self.pairer.window = int(sync.get("window_ms") or DEFAULT_SYNC_WINDOW_MS) / 1000.0
        self.counter.event_sink = self._on_crossing if self.sync_enabled or self.assembly.stations else None
        if was_on and not self.sync_enabled:
            self._run_on_loop(self._flush_sync)

        self.capture_triggers = {
            c["camera_id"]: (1 if c.get("qr_trigger") == "line1" else 2, int(c.get("qr_trigger_delay_ms") or 0) / 1000.0)
            for c in self.cameras
            if reads_codes(c) and c.get("qr_trigger") in ("line1", "line2")
        }
        self.counter.line_crossing_sink = self._on_wire_crossing if self.capture_triggers else None
        self.signal_readers = {
            c["camera_id"] for c in self.cameras if reads_codes(c) and c.get("qr_trigger") == "signal"
        }
        self._configure_pairing()

    def set_batch(self, batch: str) -> None:
        """The line's batch number from now on, for its own products and its own stations'."""
        self.batch = batch or ""
        self.counter.batch = self.batch
        for aux in self.aux_counters.values():
            aux.batch = self.batch

    def _with_batch(self, payload: Dict[str, Any], plc_event: Dict[str, Any]) -> None:
        """Add the batch to a code event's message and PLC event, while the line has one."""
        if self.batch:
            payload["batch"] = self.batch
            plc_event["batch"] = self.batch

    def _set_config(self, counter: CountingService, config: Any) -> None:
        """Give a counter its saved settings, when they changed since they were last given.

        A counter changed while running (POST /counting/config) keeps that
        until the saved settings of its line change.
        """
        if self._saved_configs.get(counter.camera_id) != config:
            self._saved_configs[counter.camera_id] = config
            counter.update_config(config)

    def qr_triggered(self, camera_id: str) -> bool:
        """True when this QR reader decodes one picture per product (or per PLC signal) instead of every frame."""
        return camera_id in self.capture_triggers or camera_id in self.signal_readers

    def has_role(self, role: str) -> bool:
        return any(c.get("role") == role for c in self.cameras)

    @property
    def has_code_reader(self) -> bool:
        """A QR reader or a vision camera with Read codes on."""
        return any(product_reader(c) for c in self.cameras)

    def reads_codes(self, camera_id: str) -> bool:
        entry = self.camera_entry(camera_id)
        return bool(entry and reads_codes(entry))

    def camera_entry(self, camera_id: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.cameras if c.get("camera_id") == camera_id), None)

    def product_list_id(self, camera_id: str) -> Optional[str]:
        """The product list a camera checks its codes against (None: no list, every code is unknown)."""
        entry = self.camera_entry(camera_id)
        return entry.get("product_list_id") if entry else None

    def confidence_for(self, camera_id: Optional[str]) -> Optional[float]:
        """The model confidence a vision camera of this line asks for (None: the model's own threshold)."""
        entry = self.camera_entry(camera_id) if camera_id else None
        value = entry.get("confidence") if entry and entry.get("role") == "vision" else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

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

    def infers_on_demand(self, camera_id: str) -> bool:
        """True for a vision camera on One picture per trigger whose picture the system takes: its model
        runs only on the picture a trigger takes, not on every frame (trigger_inspection)."""
        entry = self.camera_entry(camera_id)
        if not entry or entry.get("role", "vision") != "vision" or entry.get("count_mode") != "trigger":
            return False
        from app.state.application_state import app_state
        return picture_source(entry, getattr(app_state.cameras.get(camera_id), "trigger_mode", "off")) == "system"

    def has_continuous_camera(self) -> bool:
        """Whether a camera of the line goes through its model or code reader frame after frame, so its
        frame rate means something. A line without cameras of its own counts as one (Line 1 takes the
        cameras no line has)."""
        return not self.cameras or any(not self.infers_on_demand(c["camera_id"]) for c in self.cameras)

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
        """Frames per second the line's counting camera (or its busiest camera) gets through.

        A camera whose model runs only on a trigger's picture is left out: it gets through as many
        pictures as triggers come, which is no frame rate."""
        cid = self.counting_camera_id
        if cid and not self.infers_on_demand(cid):
            return self.fps(cid)
        with self._lock:
            ids = list(self._frames)
        return max((self.fps(i) for i in ids if not self.infers_on_demand(i)), default=0.0)

    # ── State ─────────────────────────────────────────────────────────────────

    def state(self, cameras: Dict[str, Any], fallback_cameras: List[str]) -> str:
        """The line's state for the overview.

        'stopped'   the line is switched off;
        'no_camera' no camera is assigned (and none feeds Line 1 by default);
        'idle'      none of its cameras is connected yet;
        'fault'     a camera lost its link, or only some of its cameras are connected;
        'running'   every camera is connected, or the line has no camera and
                    gets its products from a PLC product sensor that is switched on.
        """
        if not self.enabled:
            return "stopped"
        ids = [c["camera_id"] for c in self.cameras] or fallback_cameras
        if not ids:
            sensor = any(card.get("action") == "product_sensor" and card.get("enabled", True) is True
                         for card in self.plc_inputs)
            return "running" if sensor else "no_camera"
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
                   track_id: Optional[int] = None, sequence: Optional[Any] = None,
                   request_id: Optional[int] = None) -> Dict[str, Any]:
        """Called by a QR reader's thread for each new read of a triggered picture. Returns the read record.

        ``sequence`` / ``request_id``: what the "Take a picture" input that asked for the picture carries."""
        read = self._make_read(camera_id, code, code_format, t=t, track_id=track_id)
        if camera_id in self.sorting_lanes:
            # A sorting verification camera: the read checks a product's outfeed, nothing else.
            self._run_on_loop(self._verify_sorting, read)
            return read
        for key, value in (("sequence", sequence), ("request_id", request_id)):
            if value is not None:
                read[key] = value
        with self._lock:
            self.qr_stats["codes_read"] += 1
            self.qr_stats["known" if read["known"] else "unknown"] += 1
        self._run_on_loop(self._handle_read, read)
        return read

    def show_read(self, camera_id: str, code: str, code_format: str) -> Dict[str, Any]:
        """A code a camera read Continuously: it is only shown (the line's recent reads), nothing else.

        The exception: a sorting verification camera verifies Continuously too (it reports, it never decides)."""
        read = self._make_read(camera_id, code, code_format)
        if camera_id in self.sorting_lanes:
            self._run_on_loop(self._verify_sorting, read)
            return read
        self._remember(read, "known" if read["known"] else "unknown", paired=None, shown_only=True)
        return read

    def _make_read(self, camera_id: str, code: str, code_format: str, t: Optional[float] = None,
                   track_id: Optional[int] = None) -> Dict[str, Any]:
        """One read: the code, and what it says by its list's code format: the product it stands
        for, its product ID and batch, and why it does not fit (``code_problem``)."""
        from app.services.product_service import product_catalog

        list_id = self.product_list_id(camera_id)
        found = product_catalog.match(code, list_id)
        product = found.product
        return {
            "code": code,
            "format": code_format,
            "known": product is not None,
            "product_name": product.get("name") if product else None,
            "product_list_id": list_id,
            # The product the code stands for, with its values (None for an unknown code).
            "product": product_catalog.product_info(product, list_id) if product else None,
            "camera_id": camera_id,
            "t": time.monotonic() if t is None else t,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "track_id": track_id,
            "product_id": found.product_id,
            "code_batch": found.batch,
            "code_problem": found.problem,
        }

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
                self.on_qr_read(camera_id, code["code"], code["format"], t=capture.get("t"), track_id=capture.get("track_id"),
                                sequence=capture.get("sequence"), request_id=capture.get("request_id"))
        elif not self.sync_enabled and camera_id not in self.sorting_lanes:
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

    # ── Trigger cameras: the latest picture and the counters ─────────────────

    def remember_trigger_capture(self, camera_id: str, answer: Dict[str, Any], jpeg: Optional[bytes]) -> int:
        """Keep the latest capture of a camera in trigger mode; returns its id (it goes up with each capture).

        ``answer``: ``status`` ("inspected", "nothing" or "no_picture"), ``class_name``, ``result``,
        ``reject_reason`` and ``trigger_no``. ``jpeg``: the picture with its boxes, None when no
        picture came. A new capture replaces the camera's last one.
        """
        with self._lock:
            self._trigger_capture_seq += 1
            capture_id = self._trigger_capture_seq
            self._trigger_captures[camera_id] = {
                "id": capture_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "class_name": answer.get("class_name"),
                "result": answer.get("result"),
                "reason": answer.get("reject_reason"),
                "trigger_no": answer.get("trigger_no"),
                "has_picture": jpeg is not None,
            }
            self._trigger_jpegs[camera_id] = jpeg
        return capture_id

    def trigger_capture(self, camera_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[bytes]]:
        """The camera's latest capture and its JPEG; (None, None) before its first."""
        with self._lock:
            capture = self._trigger_captures.get(camera_id)
            return (dict(capture) if capture else None), self._trigger_jpegs.get(camera_id)

    def count_trigger(self, camera_id: str, key: str, n: int = 1) -> None:
        """Add ``n`` to one of a trigger camera's counters (one of TRIGGER_COUNTERS)."""
        if key not in TRIGGER_COUNTERS:
            raise ValueError(f"'{key}' is not a trigger counter; use one of {', '.join(TRIGGER_COUNTERS)}")
        with self._lock:
            counters = self._trigger_counters.setdefault(camera_id, dict.fromkeys(TRIGGER_COUNTERS, 0))
            counters[key] += n

    def trigger_view(self) -> List[Dict[str, Any]]:
        """One row per vision camera in count mode trigger, in card order, for the line's status.

        ``{camera_id, picture_source, notice_trigger, capture, counters}``. The notice is the number of
        the trigger that tells the server a camera took a picture; it is only used when the camera takes
        the picture itself, so it is None for a picture from the system.
        """
        from app.services.trigger_inspection import trigger_cameras
        from app.state.application_state import app_state

        wanted = set(trigger_cameras(self))
        rows = []
        for cam in self.cameras:
            camera_id = cam["camera_id"]
            if camera_id not in wanted:
                continue
            source = picture_source(cam, getattr(app_state.cameras.get(camera_id), "trigger_mode", "off"))
            with self._lock:
                capture = self._trigger_captures.get(camera_id)
                counters = self._trigger_counters.get(camera_id) or {}
                rows.append({
                    "camera_id": camera_id,
                    "picture_source": source,
                    "notice_trigger": cam.get("notice_trigger") if source == "camera" else None,
                    "capture": dict(capture) if capture else None,
                    "counters": {key: counters.get(key, 0) for key in TRIGGER_COUNTERS},
                })
        return rows

    # ── Camera trigger with a trigger notice ──────────────────────────────────
    # The camera takes each picture on its own input; the "Take a picture" input with the card's notice
    # number tells the server one is coming. A notice and a picture at most Wait for the picture (W) apart,
    # in either order, are one product (trigger_pairing). The notices come on the event loop, the pictures
    # from the camera runner, the model's results on the camera's inference thread.

    def _configure_pairing(self) -> None:
        """One pairer per vision camera on One picture per trigger whose card names a trigger notice and does
        not say the system takes its picture, with the card's Wait for the picture; none while the line is
        stopped. A camera whose window did not change keeps what waits for it; one whose window changed
        while the line runs answers what waited (_change_window). On a stop, or for a camera that has no
        notice any more, whatever waited is dropped and nothing is made of it."""
        wanted: Dict[str, float] = {}
        if self.enabled:
            for cam in self.cameras:
                if cam.get("role", "vision") == "vision" and cam.get("count_mode") == "trigger" \
                        and cam.get("notice_trigger") is not None and cam.get("picture_source") != "system":
                    wanted[cam["camera_id"]] = int(cam.get("picture_wait_ms") or DEFAULT_PICTURE_WAIT_MS) / 1000.0
        for camera_id in self._paired_cameras():
            pairing, window = self._pairers.get(camera_id), wanted.get(camera_id)
            if pairing is None or window is None:
                self._drop_pairing(camera_id)
            elif pairing[1] != window:
                self._change_window(camera_id, pairing, window)
        for camera_id, window in wanted.items():
            if camera_id not in self._pairers:
                self._pairers[camera_id] = (PicturePairer(window), window)

    def _change_window(self, camera_id: str, old: Tuple[PicturePairer, float], window: float) -> None:
        """Wait for the picture changed while the line runs. The camera pairs with the new window from now
        on; what waited under the old one gets its answer now: each waiting notice is a "no picture"
        reject with its context and time, and each waiting picture is dropped and counted extra. The
        pairs waiting for the model do not depend on the window: they go on."""
        from app.services.trigger_inspection import no_picture_product

        self._pairers[camera_id] = (PicturePairer(window), window)
        left = old[0].expire(float("inf"))
        for notice in left.missing:
            no_picture_product(self, camera_id, f"its Wait for the picture changed from {round(old[1] * 1000)} ms "
                                                "before the picture came", **notice.context)
        dropped = len(left.late) + len(left.extra)
        if dropped:
            self.count_trigger(camera_id, "extra", dropped)

    def _drop_pairing(self, camera_id: str) -> None:
        """Forget the notices and pictures that wait for a camera, and its pairs waiting for the model."""
        pairing = self._pairers.pop(camera_id, None)
        if pairing is not None:
            pairing[0].clear()
        with self._pair_lock:
            self._pair_queues.pop(camera_id, None)
            retry = self._pair_retry.pop(camera_id, None)
            self._unpaired.pop(camera_id, None)
            self._last_paired_at.pop(camera_id, None)
            self._held.pop(camera_id, None)
        if retry is not None:
            retry.cancel()

    def stop_pairing(self) -> None:
        """Forget everything that waits for a notice or for the model (the line is gone)."""
        for camera_id in self._paired_cameras():
            self._drop_pairing(camera_id)

    def _paired_cameras(self) -> set:
        """The cameras with a pairer or with pairs waiting for the model."""
        with self._pair_lock:
            return set(self._pairers) | set(self._pair_queues)

    def has_notice(self, camera_id: str) -> bool:
        """True for a camera of the running line that takes its own pictures (Picture taken by: Camera trigger)
        and has a trigger notice: each of its pictures waits for a notice, and only a paired one is inspected."""
        if camera_id not in self._pairers:
            return False
        from app.state.application_state import app_state

        entry = self.camera_entry(camera_id) or {}
        return picture_source(entry, getattr(app_state.cameras.get(camera_id), "trigger_mode", "off")) == "camera"

    def notice_edge(self, trigger_no: Optional[int]) -> Tuple[float, float]:
        """A "Take a picture" input with this trigger number saw its edge now. Returns the time, on the
        pairing clock and the wall clock: the notice's time (trigger_notice ``seen_at`` / ``seen_wall``).

        The input reads its product sequence (and opens its handshake request) before its notices are
        given. Meanwhile the cameras whose notice it is expire nothing that could still pair with it,
        so a picture taken at the edge waits for the notice however long the read takes. Each camera's
        hold ends with its notice, or with notice_edge_done.
        """
        seen = (_now(), _wall())
        if trigger_no is not None:
            with self._pair_lock:
                for cam in self.cameras:
                    if cam["camera_id"] in self._pairers and cam.get("notice_trigger") == trigger_no:
                        self._held.setdefault(cam["camera_id"], []).append(seen[0])
        return seen

    def notice_edge_done(self, seen: Tuple[float, float]) -> None:
        """The input whose edge was seen at ``seen`` is done: its cameras' holds that are left end
        (it gave no notice: a duplicate request, a refusal, an error)."""
        for camera_id in self._release_edge(seen[0]):
            pairing = self._pairers.get(camera_id)
            if pairing is not None:
                self._expire_later(camera_id, pairing[1])

    def _release_edge(self, at: float, camera_id: Optional[str] = None) -> List[str]:
        """End the hold of one edge, for one camera or all; returns the cameras it held."""
        released = []
        with self._pair_lock:
            for held_id in [camera_id] if camera_id is not None else list(self._held):
                held = self._held.get(held_id)
                if held and at in held:
                    held.remove(at)
                    if not held:
                        del self._held[held_id]
                    released.append(held_id)
        return released

    def trigger_notice(self, camera_id: str, trigger_no: int, context: Dict[str, Any]) -> None:
        """The "Take a picture" input that is this camera's trigger notice fired: one picture is coming.

        ``context``: what the input carries (``sequence``, ``request_id``), and ``seen_at`` / ``seen_wall``,
        when its edge was seen (notice_edge; now when left out). The notice has that time and keeps
        ``{"trigger_no", "sequence", "request_id", "at"}`` (``at``: the wall-clock time), the arguments of
        no_picture_product. A waiting picture at most Wait for the picture from it takes it at once; else
        it waits for its picture until that long after its time (_expire_pairing). The paired picture
        goes to the model, and its product keeps the context. Counts ``triggers``. Ends the camera's
        hold for this edge. Raises ValueError for a camera without a notice on this line, or a stopped line.
        """
        pairing = self._pairers.get(camera_id)
        if pairing is None or not self.enabled:
            raise ValueError(f"Camera '{camera_id}' has no trigger notice on running line '{self.name}'")
        pairer, window = pairing
        self.count_trigger(camera_id, "triggers")
        seen_at = context.get("seen_at")
        at = _now() if seen_at is None else seen_at
        wall = _wall() if context.get("seen_wall") is None else context["seen_wall"]
        notice_context = {"trigger_no": trigger_no, "sequence": context.get("sequence"),
                          "request_id": context.get("request_id"), "at": wall}
        pair = pairer.add_notice(at, notice_context)
        released = seen_at is not None and bool(self._release_edge(seen_at, camera_id))
        if pair is not None:
            self._paired(camera_id, *pair)
        if pair is None or released:
            self._expire_later(camera_id, window)       # what the hold kept goes now in its turn

    def trigger_picture(self, camera_id: str, frame: Any, frame_id: int, arrived: float) -> None:
        """A picture the camera took itself, handed over by the camera runner in the order taken.

        ``arrived``: when it arrived, on the pairing clock (pairing_time of the driver's time). Counts
        ``pictures``. A waiting notice it fits takes it and it goes to the model; else it waits for a
        notice up to Wait for the picture, and then counts late or extra (_expire_pairing). When
        MAX_WAITING_PICTURES wait already, it is extra at once: each one holds a whole frame.
        """
        pairing = self._pairers.get(camera_id)
        if pairing is None or not self.enabled:
            return
        pairer, window = pairing
        self.count_trigger(camera_id, "pictures")
        if pairer.waiting()[1] >= MAX_WAITING_PICTURES:
            self._expire_pairing(camera_id)
            if pairer.waiting()[1] >= MAX_WAITING_PICTURES:
                self.count_trigger(camera_id, "extra")
                self._note_unpaired(camera_id, [arrived])
                return
        pair = pairer.add_picture(arrived, frame, frame_id)
        if pair is not None:
            self._paired(camera_id, *pair)
        else:
            self._expire_later(camera_id, window)

    def _expire_later(self, camera_id: str, window: float) -> None:
        """Expire what waits for this camera once its window is over, on a line timer: a camera that is not
        connected (the runner skips it) still gets its answers. Without the line's loop, what is overdue
        goes now."""
        if self._schedule_timer(window + PAIRING_MARGIN_S, lambda: self._expire_pairing(camera_id)) is None:
            self._expire_pairing(camera_id)

    def _expire_pairing(self, camera_id: str) -> None:
        """What waited longer than Wait for the picture: each notice without its picture is a "no picture"
        reject with the notice's context; each picture without a notice counts late or extra, and is
        not inspected. While a notice input of the camera is still being read (notice_edge), what could
        pair with its edge is kept: this runs as if it were the moment of that edge."""
        from app.services.trigger_inspection import no_picture_product

        pairing = self._pairers.get(camera_id)
        if pairing is None or not self.enabled:
            return
        pairer, window = pairing
        now = _now()
        with self._pair_lock:
            held = self._held.get(camera_id)
            if held:
                now = min(now, min(held))
        expired = pairer.expire(now)
        for notice in expired.missing:
            no_picture_product(self, camera_id, f"no picture within {round(window * 1000)} ms of the notice",
                               **notice.context)
        if expired.late:
            self.count_trigger(camera_id, "late", len(expired.late))
        if expired.extra:
            self.count_trigger(camera_id, "extra", len(expired.extra))
        if expired.late or expired.extra:
            self._note_unpaired(camera_id, [picture.at for picture in expired.late + expired.extra])

    def _note_unpaired(self, camera_id: str, times: List[float]) -> None:
        """Pictures no notice claimed, by their times. UNPAIRED_ALARM_AFTER in a row (taken after the latest
        paired picture) raise camera.picture_unpaired; the next paired picture clears it (_paired)."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
        from app.state.application_state import app_state

        with self._pair_lock:
            last = self._last_paired_at.get(camera_id)
            streak = self._unpaired.get(camera_id, 0) + sum(1 for t in times if last is None or t > last)
            self._unpaired[camera_id] = streak
        if streak < UNPAIRED_ALARM_AFTER:
            return
        name = getattr(app_state.cameras.get(camera_id), "name", None) or camera_id
        alarm_manager.raise_alarm(
            AlarmCode.CAMERA_PICTURE_UNPAIRED, f"camera:{camera_id}",
            f"Camera '{name}' on line '{self.name}' took {streak} pictures in a row that no trigger notice "
            "claimed (late or extra); they were not inspected",
            AlarmSeverity.WARNING, {"line_id": self.id, "camera_id": camera_id, "unpaired": streak},
        )

    def _paired(self, camera_id: str, notice: Any, picture: Any) -> None:
        """A notice and a picture belong together: the picture waits for the camera's model with the notice's
        context (drain_pairs), and the run of unpaired pictures ends. When MAX_QUEUED_PAIRS wait already,
        the oldest gets its answer now: a "no picture" reject."""
        from app.events.alarm_events import AlarmCode, alarm_manager
        from app.services.trigger_inspection import no_picture_product

        arrived = _wall() - (_now() - picture.at)      # the picture's time on the wall clock: its product's time
        with self._pair_lock:
            self._unpaired[camera_id] = 0
            self._last_paired_at[camera_id] = max(picture.at, self._last_paired_at.get(camera_id, picture.at))
            queue = self._pair_queues.setdefault(camera_id, collections.deque())
            dropped = queue.popleft() if len(queue) >= MAX_QUEUED_PAIRS else None
            queue.append((picture, notice.context, arrived))
        alarm_manager.clear_alarm(AlarmCode.CAMERA_PICTURE_UNPAIRED, f"camera:{camera_id}",
                                  "a picture was paired with its trigger notice")
        if dropped is not None:
            no_picture_product(self, camera_id, f"the model fell behind: more than {MAX_QUEUED_PAIRS} pictures "
                                                "waited for it", **dropped[1])
        self.drain_pairs(camera_id)

    def drain_pairs(self, camera_id: str) -> None:
        """Give the camera's oldest paired picture to its model when the model is free, with its notice's context.

        Called when a pair is made, by the camera runner on each pass (_push_triggered), and by a line timer
        while pairs wait, so pairs do not wait for a camera that disconnected. One at a time, in order."""
        from app.services.vision_service import CameraStreamPipeline, _hardware_result

        with self._pair_lock:
            queue = self._pair_queues.get(camera_id)
            if not queue:
                return
            picture, context, arrived = queue[0]
            worker = CameraStreamPipeline.get_worker(camera_id)
            if worker.submit_frame_if_idle(picture.frame, picture.frame_id, conf_thresh=self.confidence_for(camera_id),
                                           copy=False, arrived_at=arrived,
                                           on_result=functools.partial(_hardware_result, camera_id, context=context)):
                queue.popleft()
            waiting = bool(queue)
        if waiting:
            self._retry_pairs_later(camera_id)

    def _retry_pairs_later(self, camera_id: str) -> None:
        with self._pair_lock:
            if self._pair_retry.get(camera_id) is None:
                self._pair_retry[camera_id] = self._schedule_timer(PAIR_RETRY_S, lambda: self._retry_pairs(camera_id))

    def _retry_pairs(self, camera_id: str) -> None:
        with self._pair_lock:
            self._pair_retry.pop(camera_id, None)
        self.drain_pairs(camera_id)

    def _record(self, payload: Dict[str, Any], kind: str, counted: bool, **extra: Any) -> None:
        """Keep one product or code read in the product records (not for a counter that sends nothing)."""
        if self.counter.dispatch_telemetry:
            from app.services.production_records_service import record_event
            record_event(payload, kind, counted, **extra)

    def _emit_no_read(self, camera_id: str, capture: Dict[str, Any]) -> None:
        with self._lock:
            self.qr_stats["no_reads"] += 1
        self._remember(None, "no_read", paired=None, class_name=capture.get("class_name"), camera_id=camera_id,
                       sequence=capture.get("sequence"))
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
        self._with_batch(payload, plc_event)
        if capture.get("sequence") is not None:
            payload["sequence"] = plc_event["sequence"] = capture["sequence"]
        self._record(payload, "code", counted=False, product_list_id=self.product_list_id(camera_id))
        self._publish(("qr_code",), payload, plc_event)

    def _handle_read(self, read: Dict[str, Any]) -> None:
        self._batch_from_code(read)
        if self.sync_enabled:
            self._process(self.pairer.add_read(read["t"], read))
            self._schedule_expiry()
        elif read["camera_id"] in self.code_checks and not self.has_role("vision"):
            # No vision camera on the line: the code alone decides, and each read is one product.
            # (A line with a PLC product sensor never comes here: a camera that checks codes
            # switches its Sync on, so its reads are paired above.)
            self._emit_code_product(read)
        else:
            self._emit_qr(read, paired=None)

    def _batch_from_code(self, read: Dict[str, Any]) -> None:
        """A camera set to it makes a listed product's batch the line's batch (written to the audit log).

        Only a triggered read comes here: a code read Continuously is only shown.
        """
        batch = read.get("code_batch")
        entry = self.camera_entry(read["camera_id"]) or {}
        if not entry.get("code_sets_batch") or not read.get("product") or not batch or batch == self.batch:
            return
        from app.routes.v1.lines import set_line_batch

        try:
            set_line_batch(self.id, batch, CodeActor(f"camera:{_camera_name(read['camera_id']) or read['camera_id']}"))
        except Exception as exc:
            # The carton keeps its own batch; the line keeps its batch. Said once per batch.
            if batch not in self._batch_errors:
                self._batch_errors.add(batch)
                logger.warning("Line %s: the batch %r of a code could not become the line's batch: %s", self.id, batch, exc)

    def sensor_product(self) -> None:
        """A PLC sensor saw one product (a line without a vision camera). Called on the event loop.

        It is counted, recorded and sent like a product a camera saw cross its
        count line; with Sync on it waits for its code first. Its track number
        is the line's own count of sensor products, so every product has its
        own event id, also after the counters were reset.
        """
        if not self.enabled:
            return
        self._sensor_seq += 1
        self.counter.emit_crossing(self.counter.make_crossing("product", track_id=self._sensor_seq))

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
        if self.assembly.stations:
            # The product waits for its joined cameras (and, with Sync on, for its code too).
            self._finish_products(self.assembly.add_product(crossing, t, code_waiting=self.sync_enabled))
            if not self.sync_enabled:
                self._schedule_expiry()
                return
        self._process(self.pairer.add_crossing(t, crossing))
        self._schedule_expiry()

    def _joined_sink(self, camera_id: str) -> Callable[[Dict[str, Any]], None]:
        return lambda crossing: self._on_joined_crossing(camera_id, crossing)

    def _on_joined_crossing(self, camera_id: str, crossing: Dict[str, Any]) -> None:
        """Event sink of a joined camera's counter (inference thread): its crossing is matched
        to one of the counting camera's products and is not counted on its own."""
        t = time.monotonic()
        if self._loop() is None:
            return  # nothing to match it to: the counting camera counts on its own result
        self._run_on_loop(self._handle_joined_crossing, camera_id, crossing, t)

    def _handle_joined_crossing(self, camera_id: str, crossing: Dict[str, Any], t: float) -> None:
        self._finish_products(self.assembly.add_station(camera_id, crossing, t))
        self._schedule_expiry()

    def _schedule_expiry(self) -> None:
        loop = self._loop()
        if self._sync_timer is not None:
            self._sync_timer.cancel()
            self._sync_timer = None
        deadlines = [d for d in (self.pairer.next_deadline(), self.assembly.next_deadline()) if d is not None]
        if loop is None or not deadlines:
            return
        # A little past the deadline so the oldest entry has expired when it fires.
        self._sync_timer = loop.call_later(max(0.0, min(deadlines) - time.monotonic()) + 0.005, self._expire)

    def _expire(self) -> None:
        self._sync_timer = None
        now = time.monotonic()
        self._process(self.pairer.expire(now))
        self._finish_products(self.assembly.expire(now))
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
                if self.assembly.holds(crossing):
                    # The product also waits for its joined cameras.
                    self._finish_products(self.assembly.code_result(crossing, kind, read, time.monotonic()))
                    continue
                self._finish_product(crossing, kind, read)
            except Exception:
                logger.exception("Line %s: failed to send a synced event", self.id)

    def _finish_products(self, products: List[_Product]) -> None:
        for product in products:
            try:
                self._finish_product(product.crossing, product.kind, product.read, product)
            except Exception:
                logger.exception("Line %s: failed to send a product's event", self.id)

    def _finish_product(self, crossing: Dict[str, Any], kind: Optional[str], read: Optional[Dict[str, Any]],
                        product: Optional[_Product] = None) -> None:
        """Count one product with its one result and send its one event.

        ``kind``: "paired" or "no_read" when Sync paired it with its code (or
        found none); None without Sync. ``product``: what its joined cameras
        saw, on a line that has them.
        """
        # The request this product answers: the vision camera's (it made the product), else its code's.
        if kind == "paired":
            for key in ("sequence", "request_id"):
                if crossing.get(key) is None and read.get(key) is not None:
                    crossing[key] = read[key]
        if self.handshake.enabled and not self._admit_product(crossing):
            return
        sequence = self._product_sequence(crossing)
        # One result per product: the vision camera's, the joined cameras' and,
        # where a camera checks codes, the paired code's (or the missing code's);
        # last, the line's product checks on the product of a known code.
        check = self._failed_check(read, crossing) if kind == "paired" else None
        reject, reason, reject_camera = product_result(
            bool(crossing["is_defect"]), read if kind == "paired" else None, self.code_checks,
            product.results() if product else None, product.missing() if product else None,
            crossing.get("camera_id"), product_check=check,
        )
        check = check if reason == REASON_PRODUCT_CHECK else None
        if reason == REASON_VISION:
            # What that camera's own picture said, when it said more than "a defect class"
            # (a triggered picture in which nothing was found).
            seen = crossing if reject_camera == crossing.get("camera_id") else \
                (product.seen.get(reject_camera) if product else None)
            reason = (seen or {}).get("reject_reason") or reason
        result = self._result(reject) if self.code_checks or self.product_checks else None
        fields: Dict[str, Any] = {}
        plc_fields: Dict[str, Any] = {}
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
                           result=result, reason=reason, product_check=check, sequence=sequence)
        elif kind == "no_read":
            status = "no_read"
            fields = {"qr_code": None, "qr_format": None, "qr_status": "no_read", "product_name": None, "qr_paired": False}
            with self._lock:
                self.qr_stats["no_reads"] += 1
            self._remember(None, "no_read", paired=False, class_name=crossing.get("class_name"),
                           camera_id=crossing.get("camera_id"), result=result, reason=reason, sequence=sequence)
        if kind is not None:
            plc_fields = {"qr_code": fields["qr_code"], "qr_status": status, "qr_paired": fields["qr_paired"]}
        if kind == "paired" and read.get("product"):
            fields["product"] = plc_fields["product"] = read["product"]
        if kind == "paired":
            # The carton's product ID, and its batch in place of the line's (applied after it).
            code_fields, code_plc_fields = self._code_fields(read)
            fields.update(code_fields)
            plc_fields.update(code_plc_fields)
        if check:
            fields["product_check"] = plc_fields["product_check"] = check
        if reject and reason in CODE_REASONS:
            with self._lock:
                self.qr_stats["code_rejects"] += 1
        if product is not None:
            fields["stations"] = self._station_rows(product)
            fields["reject_camera_id"] = reject_camera
            plc_fields["reject_camera_id"] = reject_camera
            # A card that names a joined camera fires for the products it takes part in.
            plc_fields["joined_cameras"] = [st.camera_id for st in product.stations]
        self.counter.finish_crossing(
            crossing,
            reject=reject,
            reason=reason,
            fields=fields or None,
            plc_fields=plc_fields or None,
            record={"product_list_id": read.get("product_list_id")} if kind == "paired" else None,
            # With a code check or joined cameras the result may come some time
            # after the crossing; a reject gate's travel delay still runs from
            # the crossing, where the product was.
            delay_from_crossing=bool(self.code_checks) or product is not None,
        )

    def _sequence_now(self) -> Any:
        """The last value the line's sequence input read; None without one, or while it cannot be read."""
        if not self.sequence_input_id:
            return None
        from app.services.plc_input_service import PLCInputService
        return PLCInputService.value_of(self.id, self.sequence_input_id)

    def _product_sequence(self, item: Dict[str, Any]) -> Any:
        """A product's (or a code-only product's read's) sequence number, kept in ``item``.

        The one its "Take a picture" request carried; a product made without one (a moving line)
        takes the sequence input's value of this moment."""
        if item.get("sequence") is None:
            now = self._sequence_now()
            if now is not None:
                item["sequence"] = now
        return item.get("sequence")

    def _product_finished(self, crossing: Dict[str, Any], payload: Dict[str, Any], plc_event: Dict[str, Any]) -> None:
        """The line's counter hook: each product's one event, before it is recorded and sent."""
        sequence = self._product_sequence(crossing)
        if sequence is not None:
            payload["sequence"] = plc_event["sequence"] = sequence
        self._handshake_result(crossing, plc_event.get("result"))
        self._sorting_note(plc_event.get("qr_code"), sequence, plc_event.get("result"))

    # ── Sorting verification ──────────────────────────────────────────────────

    def _sorting_note(self, code: Optional[str], sequence: Any, result: Optional[str]) -> None:
        """Remember a product that had a code, for the line's sorting verification cameras."""
        if not self.sorting_lanes or not code or result not in ("good", "reject"):
            return
        with self._lock:
            self._sorting_memory.append({"code": code, "sequence": sequence, "result": result,
                                         "at": time.time(), "verified": None})

    def sorting_counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._sorting_stats)

    def _verify_sorting(self, read: Dict[str, Any]) -> None:
        """A sorting verification camera read a code at its outfeed (on the loop).

        The oldest product with that code that is not verified yet (cartons leave in the order
        they were inspected) should be in this outfeed: accepted for a good product, rejected for
        a reject. A mismatch raises an alarm, fires the "Sorting mismatch" cards and is recorded.
        """
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager

        lane = self.sorting_lanes.get(read["camera_id"])
        if lane is None:
            return
        with self._lock:
            entry = next((e for e in self._sorting_memory if e["code"] == read["code"] and e["verified"] is None), None)
            if entry is not None:
                entry["verified"] = lane
                expected = "accepted" if entry["result"] == "good" else "rejected"
                outcome = "verified" if expected == lane else "mismatch"
                self._sorting_stats["verified" if outcome == "verified" else "mismatches"] += 1
            else:
                expected, outcome = None, "unmatched"
                self._sorting_stats["unmatched"] += 1
        sequence = entry["sequence"] if entry is not None else None
        self._remember(read, "known" if read["known"] else "unknown", paired=None, sequence=sequence,
                       verification={"sorting_lane": lane, "verification": outcome, "expected_lane": expected})
        payload: Dict[str, Any] = {
            "event": {"verified": "SORTING_VERIFIED", "mismatch": "SORTING_MISMATCH"}.get(outcome, "SORTING_UNMATCHED"),
            "timestamp": read["timestamp"],
            "line_id": self.id,
            "line_name": self.name,
            "camera_id": read["camera_id"],
            "code": read["code"],
            "format": read["format"],
            "seen_lane": lane,
        }
        if outcome == "unmatched":
            logger.info("Line '%s': the %s outfeed camera read %r, which no product waiting for its "
                        "verification has", self.name, lane, read["code"])
            self._publish_bus(("sorting",), payload)
            return
        payload["expected_lane"] = expected
        payload["product_result"] = "PASSED" if entry["result"] == "good" else "REJECTED"
        if sequence is not None:
            payload["sequence"] = sequence
        plc_event: Dict[str, Any] = {
            "event_type": "sorting_verified" if outcome == "verified" else "sorting_mismatch",
            "event_id": f"sorting_{self.id}_{time.time_ns()}",
            "line_id": self.id,
            "camera_id": read["camera_id"],
            "qr_code": read["code"],
            "expected_lane": expected,
            "seen_lane": lane,
            "timestamp": time.time(),
        }
        if sequence is not None:
            plc_event["sequence"] = sequence
        if outcome == "mismatch":
            logger.warning("Line '%s': %s (code %r, %s) is in the %s outfeed, expected the %s outfeed",
                           self.name, _carton(sequence), read["code"], entry["result"], lane, expected)
            alarm_manager.raise_alarm(
                AlarmCode.LINE_SORTING_MISMATCH, self._alarm_source("sorting"),
                f"Line '{self.name}': {_carton(sequence)} (code {read['code']}) "
                f"is in the {lane} outfeed; its result sends it to the {expected} outfeed.",
                AlarmSeverity.CRITICAL,
                {"line_id": self.id, "camera_id": read["camera_id"], "code": read["code"], "sequence": sequence,
                 "expected_lane": expected, "seen_lane": lane},
            )
            self._record(payload, "verification", counted=False, result="reject",
                         reject_reason=REASON_SORTING_MISMATCH, product_list_id=read.get("product_list_id"))
        self._publish(("sorting",), payload, plc_event)

    def _publish_bus(self, topics: Tuple[str, ...], payload: Dict[str, Any]) -> None:
        """An event for the event bus only (dashboards, flows): no output action or send card fires."""
        loop = self._loop()
        if loop is None:
            return
        try:
            from app.events.event_bus import event_bus
            for topic in topics:
                asyncio.run_coroutine_threadsafe(event_bus.publish(topic, payload), loop)
        except Exception as exc:
            logger.debug("Event bus error on line %s: %s", self.id, exc)

    # ── Result handshake ──────────────────────────────────────────────────────

    def _schedule_timer(self, delay: float, fn: Callable[[], None]) -> Optional[_LoopTimer]:
        loop = self._loop()
        return _LoopTimer(loop, delay, fn) if loop is not None else None

    def _alarm_source(self, what: str) -> str:
        return f"line:{self.id}:{what}"

    def capture_request(self, input_id: Optional[str], sequence: Any) -> Optional[Dict[str, Any]]:
        """A "Take a picture" input fired with this sequence: its request in the line's result handshake.

        None when the handshake is off or this is not its capture input. ``action`` "duplicate":
        the sequence is still in the handshake, so no picture is taken. A capture that skipped the
        ack, or came before the last result, is reported as a fault and goes ahead.
        """
        if not input_id or input_id != self.handshake.capture_input_id:
            return None
        answer = self.handshake.capture(sequence)
        if answer is None:
            return None
        if answer["action"] == "duplicate":
            logger.warning("Line '%s': the PLC asked again for sequence %s, which is still in the result handshake; "
                           "ignored (no picture)", self.name, sequence)
        elif answer["fault"]:
            self._handshake_fault(answer["fault"], sequence)
        return answer

    def on_input_change(self, input_id: Optional[str], old: Any, new: Any, edge: str = "rising") -> None:
        """An accepted change of one of the line's inputs (the PLC input service, on the loop).

        The acknowledge input's own edge decides what acknowledges: Off to on (the default), or
        On to off for a PLC that drops its capture request as the acknowledge."""
        from app.services.plc_input_service import edge_matches

        if not input_id or input_id != self.handshake.ack_input_id or not edge_matches(edge or "rising", old, new):
            return
        from app.events.alarm_events import AlarmCode, alarm_manager

        # The PLC acknowledged: a sorting fault it was shown is acknowledged with it.
        alarm_manager.clear_alarm(AlarmCode.LINE_SORTING_MISMATCH, self._alarm_source("sorting"), "the PLC acknowledged")
        if self.handshake.ack() == "done":
            for code in (AlarmCode.LINE_ACK_TIMEOUT, AlarmCode.LINE_HANDSHAKE_FAULT):
                alarm_manager.clear_alarm(code, self._alarm_source("handshake"), "the PLC acknowledged the result")
            return
        logger.warning("Line '%s': the PLC acknowledged with no result waiting for its acknowledge (handshake %s); "
                       "ignored", self.name, self.handshake.state)

    def _admit_product(self, item: Dict[str, Any]) -> bool:
        """False for a product whose capture request timed out: the timeout gave its one result."""
        if item.get("handshake_timeout") or not self.handshake.is_closed(item.get("request_id")):
            return True
        self.handshake.note_late()
        logger.warning("Line '%s': the result for sequence %s came after its result timeout; the timeout's result "
                       "stands and this one is not counted", self.name, item.get("sequence"))
        return False

    def _handshake_result(self, item: Dict[str, Any], result: Optional[str]) -> None:
        """A product's one result went out: the handshake waits for the PLC's acknowledge now."""
        if not self.handshake.enabled or result not in ("good", "reject"):
            return
        timeout = bool(item.get("handshake_timeout"))
        if self.handshake.result(item.get("request_id"), result, timeout=timeout) and not timeout:
            from app.events.alarm_events import AlarmCode, alarm_manager
            alarm_manager.clear_alarm(AlarmCode.LINE_INSPECTION_TIMEOUT, self._alarm_source("handshake"),
                                      "a result came in time")

    def _handshake_result_timeout(self, request: Any, policy: str) -> None:
        """No result in time for a capture request (handshake timer, on the loop): a reject, or a fault."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager

        timeout_ms = self.handshake.settings.get("result_timeout_ms")
        if policy == "fault":
            alarm_manager.raise_alarm(
                AlarmCode.LINE_INSPECTION_TIMEOUT, self._alarm_source("handshake"),
                f"Line '{self.name}': no inspection result for {_carton(request.sequence)} within {timeout_ms} ms.",
                AlarmSeverity.CRITICAL, {"line_id": self.id, "sequence": request.sequence},
            )
            self._publish_handshake_fault("inspection_timeout", request.sequence)
            return
        logger.warning("Line '%s': no inspection result for %s within %s ms; it is rejected",
                       self.name, _carton(request.sequence), timeout_ms)
        with self._trigger_seq_lock:
            self._trigger_seq += 1
            track_id = self._trigger_seq
        crossing = self.counter.make_crossing("inspection timeout", True, track_id=track_id,
                                              camera_id=self.counting_camera_id)
        crossing.update({"reject_reason": REASON_INSPECTION_TIMEOUT, "request_id": request.id, "handshake_timeout": True})
        if request.sequence is not None:
            crossing["sequence"] = request.sequence
        self._remember(None, "no_result", paired=None, class_name=crossing["class_name"], result="reject",
                       reason=REASON_INSPECTION_TIMEOUT, sequence=request.sequence)
        self.counter.finish_crossing(crossing, reject=True, reason=REASON_INSPECTION_TIMEOUT)

    def _handshake_ack_timeout(self, request: Any) -> None:
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager

        alarm_manager.raise_alarm(
            AlarmCode.LINE_ACK_TIMEOUT, self._alarm_source("handshake"),
            f"Line '{self.name}': the PLC did not acknowledge the result of {_carton(request.sequence)} within "
            f"{self.handshake.settings.get('ack_timeout_ms')} ms.",
            AlarmSeverity.WARNING, {"line_id": self.id, "sequence": request.sequence},
        )
        self._publish_handshake_fault("ack_timeout", request.sequence)

    def _handshake_fault(self, fault: str, sequence: Any) -> None:
        """The PLC asked for a picture out of step: an alarm and the "Handshake fault" cards."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager

        what = ("before it acknowledged the last result" if fault == "skipped_ack"
                else "before the last request had its result")
        alarm_manager.raise_alarm(
            AlarmCode.LINE_HANDSHAKE_FAULT, self._alarm_source("handshake"),
            f"Line '{self.name}': the PLC asked for a picture ({_carton(sequence)}) {what}.",
            AlarmSeverity.WARNING, {"line_id": self.id, "sequence": sequence, "fault": fault},
        )
        self._publish_handshake_fault(fault, sequence)

    def _publish_handshake_fault(self, fault: str, sequence: Any) -> None:
        """The event the "Handshake fault" output actions and send cards fire on."""
        payload: Dict[str, Any] = {
            "event": "HANDSHAKE_FAULT",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "line_id": self.id,
            "line_name": self.name,
            "fault": fault,
        }
        plc_event: Dict[str, Any] = {
            "event_type": "handshake_fault",
            "event_id": f"handshake_{self.id}_{time.time_ns()}",
            "line_id": self.id,
            "fault": fault,
            "timestamp": time.time(),
        }
        if sequence is not None:
            payload["sequence"] = plc_event["sequence"] = sequence
        self._publish(("handshake",), payload, plc_event)

    @staticmethod
    def _station_rows(product: _Product) -> List[Dict[str, Any]]:
        """The message's ``stations``: what each joined camera saw of the product, in card order."""
        rows = []
        for station in product.stations:
            seen = product.seen.get(station.camera_id)
            rows.append({
                "camera_id": station.camera_id,
                "camera_name": _camera_name(station.camera_id),
                "result": "no_result" if seen is None else ("reject" if seen.get("is_defect") else "good"),
                "class_name": seen.get("class_name") if seen else None,
                "confidence": round(float(seen.get("confidence") or 0.0), 4) if seen else None,
                # How long after the counting camera this camera saw it (negative: before).
                "travel_ms": int(round((product.times[station.camera_id] - product.t) * 1000)) if seen else None,
            })
        return rows

    @staticmethod
    def _result(reject: bool) -> str:
        return "reject" if reject else "good"

    def _failed_check(self, read: Optional[Dict[str, Any]], crossing: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """The name of the first product check whose rules pass for this item, or None.

        Only an item whose code is known has a product to check. The rules see what
        is known before the result: the class and confidence the camera saw, the code,
        the product's values, the inputs, the counts before this product, the batch.
        """
        if not self.product_checks or not read or not read.get("known") or not read.get("product"):
            return None
        try:
            return self._run_checks(read, crossing)
        except Exception:
            # The product is still counted, on its other results: a check that cannot run rejects nothing.
            logger.exception("Line %s: the product checks could not run", self.id)
            return None

    def _run_checks(self, read: Dict[str, Any], crossing: Optional[Dict[str, Any]]) -> Optional[str]:
        from app.services import event_values
        from app.services.rules import rules_pass

        event: Dict[str, Any] = {
            "event_type": "product_check",
            "line_id": self.id,
            "camera_id": (crossing or {}).get("camera_id") or read.get("camera_id"),
            "qr_code": read.get("code"),
            "qr_status": "known",
            "product": read["product"],
        }
        # The carton's own product ID and batch (the line's batch stays for a carton without one).
        event.update(self._code_fields(read)[1])
        sequence = (crossing or {}).get("sequence")
        sequence = read.get("sequence") if sequence is None else sequence
        if sequence is not None:
            event["sequence"] = sequence
        if crossing:
            event["class_name"] = crossing.get("class_name")
            if crossing.get("confidence") is not None:
                event["confidence"] = round(float(crossing["confidence"]), 4)
        values = event_values.values_for(event)
        for check in self.product_checks:
            if check.get("only_when") and rules_pass(check["only_when"], values):
                return check.get("name") or check.get("id")
        return None

    def _remember(self, read: Optional[Dict[str, Any]], status: str, paired: Optional[bool],
                  class_name: Optional[str] = None, camera_id: Optional[str] = None,
                  result: Optional[str] = None, reason: Optional[str] = None,
                  product_check: Optional[str] = None, shown_only: bool = False,
                  sequence: Optional[Any] = None, verification: Optional[Dict[str, Any]] = None) -> None:
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
        if product_check:
            entry["product_check"] = product_check
        if read and read.get("product_id") is not None:
            entry["product_id"] = read["product_id"]
        if read and read.get("code_batch"):
            entry["batch"] = read["code_batch"]
        sequence = (read or {}).get("sequence") if sequence is None else sequence
        if sequence is not None:
            entry["sequence"] = sequence
        if shown_only:
            # A camera that reads Continuously: the code went nowhere but this list.
            entry["shown_only"] = True
        if verification:
            # A sorting verification read: the outfeed it saw, what was expected, what it found.
            entry.update(verification)
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
        if read.get("product"):
            payload["product"] = plc_event["product"] = read["product"]
        self._with_batch(payload, plc_event)
        code_fields, code_plc_fields = self._code_fields(read)
        payload.update(code_fields)
        plc_event.update(code_plc_fields)
        if read.get("sequence") is not None:
            payload["sequence"] = plc_event["sequence"] = read["sequence"]
        return payload, plc_event

    @staticmethod
    def _code_fields(read: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """What a code says of its carton, for its message and its PLC event: the product ID, the
        carton's own batch (it replaces the line's for this carton), and, in the message, why the
        code does not fit its list's format. Applied after the line's batch.

        The PLC event always has the product ID (rules and writes use it); a message only where it
        is not the whole code, so a list that reads whole codes sends the messages it sent.
        """
        fields: Dict[str, Any] = {}
        plc_fields: Dict[str, Any] = {}
        product_id = read.get("product_id")
        if product_id is not None:
            plc_fields["product_id"] = product_id
            if product_id != str(read.get("code") or "").strip():
                fields["product_id"] = product_id
        if read.get("code_batch"):
            fields["batch"] = plc_fields["batch"] = read["code_batch"]
        if read.get("code_problem"):
            fields["code_problem"] = read["code_problem"]
        return fields, plc_fields

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
        self._record(payload, "code", counted=False, product_list_id=read.get("product_list_id"))
        self._publish(("qr_code",), payload, plc_event)

    def _emit_code_product(self, read: Dict[str, Any]) -> None:
        """A code read on a line without a vision camera, by a camera that checks codes:
        the read is one product, good or reject, counted like any other product."""
        if self.handshake.enabled and not self._admit_product(read):
            return
        self._product_sequence(read)
        reason = code_verdict(self.code_checks[read["camera_id"]]["action"], read["known"])
        check = self._failed_check(read) if reason is None else None
        if check:
            reason = REASON_PRODUCT_CHECK
        reject = reason is not None
        status = "known" if read["known"] else "unknown"
        self._remember(read, status, paired=None, result=self._result(reject), reason=reason, product_check=check)
        if reject:
            with self._lock:
                self.qr_stats["code_rejects"] += 1
        class_name = read["product_name"] or "code not in list"
        totals = self.counter.count_product(class_name, reject)
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
        if check:
            payload["product_check"] = plc_event["product_check"] = check
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
        # The product's class in the records is the name its count is kept under.
        self._handshake_result(read, self._result(reject))
        self._sorting_note(read["code"], read.get("sequence"), self._result(reject))
        self._record(payload, "product", counted=True, class_name=class_name, track_id=read.get("track_id"),
                     product_list_id=read.get("product_list_id"))
        self._publish(("qr_code", "reading"), payload, plc_event)

    def recent_reads(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.qr_recent)[:limit]

    def reset(self) -> None:
        self.counter.reset_counts()
        for aux in self.aux_counters.values():
            aux.reset_counts()
        self.assembly.reset_stats()
        self.handshake.reset_counts()
        with self._lock:
            for key in self.qr_stats:
                self.qr_stats[key] = 0
            self.qr_recent.clear()
            # The products waiting for their sorting verification are forgotten too.
            self._sorting_memory.clear()
            for key in self._sorting_stats:
                self._sorting_stats[key] = 0
            # The trigger cameras' counts start again; their latest picture stays.
            self._trigger_counters.clear()
        from app.events.alarm_events import AlarmCode, alarm_manager
        alarm_manager.clear_alarm(AlarmCode.LINE_SORTING_MISMATCH, self._alarm_source("sorting"), "the counters were reset")


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


def _warm_up(engine: Any, name: str) -> None:
    """One run of a model on a black picture of its input size (LineManager._warm_on_demand_engines).

    A failure is only logged: the model's first trigger then waits for its first run."""
    import numpy as np

    try:
        width, height = getattr(engine, "input_size", None) or (640, 640)
        engine.predict_mat(np.zeros((int(height), int(width), 3), np.uint8))
        logger.info("Vision model '%s' warmed up for the cameras that run it only on a trigger", name)
    except Exception as exc:
        logger.warning("Vision model '%s' could not be warmed up; its first trigger may wait for it: %s: %s",
                       name, type(exc).__name__, exc)


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
        # The engines warmed up for the cameras that infer only on a trigger, and the warm-ups running.
        self._warmed: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._warm_tasks: set = set()

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
                runtime.handshake.stop()
                runtime.stop_pairing()
            if PRIMARY_LINE_ID not in self._lines:
                self._lines[PRIMARY_LINE_ID] = LineRuntime(PRIMARY_LINE_ID, "Line 1", counting_service)
                seen.insert(0, PRIMARY_LINE_ID)
            self._order = seen
            self._camera_map = camera_map
        self._report_show_only_alarms()

    def _report_show_only_alarms(self) -> None:
        """line.codes_show_only for each running line with a camera the version 9 upgrade flagged.

        Such a camera reads codes Continuously, which now only shows them. The
        alarm says what the line does until the user chooses when it reads codes.
        """
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager

        live = set()
        for runtime in self.all():
            flagged = [(i, cam) for i, cam in enumerate(runtime.cameras) if cam.get("qr_trigger_review") is True]
            if not runtime.enabled or not flagged:
                continue
            source = f"line:{runtime.id}"
            live.add(source)
            names = " and ".join(f"Camera {i + 1}" for i, _ in flagged)
            has_source = runtime.has_role("vision") or any(
                card.get("action") == "product_sensor" and card.get("enabled", True) is True for card in runtime.plc_inputs
            )
            checked = [runtime.code_checks[cam["camera_id"]] for _, cam in flagged if cam["camera_id"] in runtime.code_checks]
            if not has_source and checked:
                effect = "Until then the line counts nothing: no picture is read."
            elif runtime.sync_enabled and any(check["no_read"] == "reject" for check in checked):
                effect = "Until then every product is rejected: no code is read (When no code is read: reject)."
            elif runtime.sync_enabled and checked:
                effect = "Until then products pass without a code (When no code is read: ignore)."
            else:
                effect = "Until then their codes are only shown."
            alarm_manager.raise_alarm(
                AlarmCode.LINE_CODES_SHOW_ONLY, source,
                f"Line '{runtime.name}': {names} only show their codes since the settings upgrade (Read codes "
                f"'Continuously'). {effect} Choose when they read codes on Line setup.",
                AlarmSeverity.WARNING,
                {"line_id": runtime.id, "camera_ids": [cam["camera_id"] for _, cam in flagged]},
            )
        for alarm in alarm_manager.active("line:"):
            if alarm.code == AlarmCode.LINE_CODES_SHOW_ONLY and alarm.source not in live:
                alarm_manager.clear_alarm(alarm.code, alarm.source, "no camera of the line only shows codes, or it stopped")

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
        self._warm_on_demand_engines()
        return problems

    def _warm_on_demand_engines(self) -> None:
        """Run each model a camera of a running line infers on demand once, now, off the event loop.

        Such a camera's model gets no frame until a trigger comes, and a model's first run (on a GPU
        above all) can take seconds: the first trigger would wait for it. Each engine is warmed once,
        when it was loaded or its camera's line started (this runs after every refresh_models).
        """
        for runtime in self.all():
            if not runtime.enabled:
                continue
            for cam in runtime.cameras:
                entry = self._engines.get(cam.get("model_id"))
                if entry is None or entry["engine"] in self._warmed or not runtime.infers_on_demand(cam["camera_id"]):
                    continue
                self._warmed.add(entry["engine"])
                task = asyncio.get_running_loop().create_task(asyncio.to_thread(_warm_up, entry["engine"], entry["name"]))
                self._warm_tasks.add(task)
                task.add_done_callback(self._warm_tasks.discard)

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
        """None: no running line runs a model on this camera frame after frame (a camera whose model
        runs only on a trigger's picture neither). Else whether its model is loaded."""
        routed = self.route(camera_id)
        if routed is None or routed[1] != "vision" or not routed[0].enabled or routed[0].infers_on_demand(camera_id):
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
                station = camera_station(cam)
                aux = runtime.aux_counters.get(cam["camera_id"])
                if station:
                    row["station"] = station
                joined = runtime.assembly.stats(cam["camera_id"]) if station == "join" else None
                if joined is not None:
                    # A joined camera counts nothing on its own: how its crossings matched the line's products.
                    offset, window, missing = join_settings(cam)
                    row.update({"join_offset_ms": offset, "join_window_ms": window, "join_missing": missing,
                                "station_stats": joined})
                elif aux is not None:
                    row["counts"] = {
                        "total_inspected": aux.total_inspected,
                        "good_count": aux.good_count,
                        "rejected_count": aux.rejected_count,
                    }
            if reads_codes(cam):
                if row["role"] == "vision":
                    row["read_codes"] = True
                row["qr_trigger"] = cam.get("qr_trigger", "continuous")
                row["qr_trigger_delay_ms"] = int(cam.get("qr_trigger_delay_ms") or 0)
                row["code_type"] = cam.get("code_type", "all")
                row["product_list_id"] = cam.get("product_list_id")
                row["qr_action"] = cam.get("qr_action", "report")
                row["qr_no_read"] = cam.get("qr_no_read", "ignore")
                if sorting_lane(cam):
                    row["sorting_lane"] = sorting_lane(cam)
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
            "batch": runtime.batch or None,
            "has_qr": any(reads_codes(c) for c in runtime.cameras),
            "qr": qr_stats,
            "last_capture_id": last_capture_id,
            "active_alarms": len(alarms),
            # The result handshake, when the line has it switched on.
            "handshake": runtime.handshake.view() if runtime.handshake.enabled else None,
            # Sorting verification at the outfeeds, when a camera of the line verifies one.
            "sorting": runtime.sorting_counts() if runtime.sorting_lanes else None,
            # Per vision camera in trigger mode: where its picture comes from, its latest capture and counters.
            "trigger_cameras": runtime.trigger_view(),
        }

    def overview(self) -> List[Dict[str, Any]]:
        return [self.summary(r) for r in self.all()]


line_manager = LineManager()
