from __future__ import annotations

import asyncio
import collections
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.engines.tracker import WirelineTracker
from app.schemas.counting import CountingConfig, CountingStatsResponse
from app.schemas.vision import DetectionItem
from app.utils.logger import get_logger

logger = get_logger(__name__)


class _TelemetryDispatcher:
    """
    Dedicated background worker for dispatching MQTT, TCP, and Webhook events.
    Completely isolates the FastAPI web server & video streaming loop from network latency or timeouts.
    Each destination gets its own lane: its events are sent in order, while a slow
    or unreachable destination (a webhook timing out, a TCP host that is down) no
    longer holds up delivery to the others.
    """
    _MAX_PENDING = 300

    def __init__(self):
        self._dropped = 0
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._drained = threading.Event()
        self._drained.set()
        self._lanes: Dict[Any, collections.deque] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._worker, name="TelemetryDispatcher", daemon=True)
        self._thread.start()

    def submit(self, task_callable, key: Any = None) -> None:
        """Queue a coroutine function; tasks with the same key run one at a time, in order."""
        with self._pending_lock:
            full = self._pending >= self._MAX_PENDING
            if full:
                self._dropped += 1
                dropped = self._dropped
            else:
                self._pending += 1
                self._drained.clear()
        if full:
            if dropped == 1 or dropped % 100 == 0:
                logger.error("Telemetry queue full; dropped %d event(s)", dropped)
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, key, task_callable)
        except RuntimeError:  # dispatcher already stopped
            self._task_done()

    def _task_done(self) -> None:
        with self._pending_lock:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self._drained.set()

    def _enqueue(self, key: Any, task_callable) -> None:
        # Runs on the dispatcher loop, so lanes need no lock.
        lane = self._lanes.get(key)
        if lane is not None:
            lane.append(task_callable)
            return
        lane = collections.deque([task_callable])
        self._lanes[key] = lane
        self._loop.create_task(self._drain_lane(key, lane))

    async def _drain_lane(self, key: Any, lane: collections.deque) -> None:
        while lane:
            task_callable = lane.popleft()
            try:
                await task_callable()
            except Exception as e:
                logger.debug("Telemetry dispatch error: %s", e)
            finally:
                self._task_done()
        del self._lanes[key]

    def _worker(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            leftover = asyncio.all_tasks(self._loop)
            for task in leftover:
                task.cancel()
            if leftover:
                self._loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
            self._loop.close()

    def wait_drained(self, timeout: float) -> bool:
        """Wait until every queued event went out (or the timeout). True when they all did."""
        return self._drained.wait(timeout=timeout)

    def stop(self) -> None:
        # Give queued events up to 5 s to go out, then stop the loop.
        self.wait_drained(timeout=5.0)
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:  # already stopped
            return
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)


_telemetry_dispatcher = _TelemetryDispatcher()


def send_to_channels(payload: Dict[str, Any], protocol: str = "all", endpoint_id: str = "all") -> int:
    """Queue a payload for saved MQTT, TCP and webhook channels on the shared dispatcher.

    protocol and endpoint_id narrow the targets ("all" = every enabled channel).
    Returns the number of channels the payload was queued for.
    """
    from app.services.settings_persistence_service import SettingsPersistenceService

    protocol = (protocol or "all").lower()
    queued = 0
    for ep in SettingsPersistenceService.get_endpoints():
        proto = str(ep.get("protocol", "")).lower()
        if proto not in ("mqtt", "tcp", "webhook") or ep.get("enabled", True) is not True:
            continue
        if protocol != "all" and proto != protocol:
            continue
        if endpoint_id not in (None, "", "all") and ep.get("id") != endpoint_id:
            continue
        if proto == "mqtt" and ep.get("topic"):
            # Only a channel that still holds a topic from before send cards named
            # theirs: there is no card here to name one.
            from app.services.mqtt_service import MQTTChannels
            topic = ep["topic"]
            _telemetry_dispatcher.submit(lambda e=ep, t=topic: MQTTChannels.publish(e, t, payload), key=("mqtt", ep.get("id"), topic))
        elif proto == "tcp" and ep.get("host") and ep.get("port"):
            from app.services.tcp_channels import TcpChannels, frame
            data = frame(json.dumps(payload), ep)
            _telemetry_dispatcher.submit(lambda e=ep, d=data: TcpChannels.send(e, d), key=("tcp", ep.get("host"), ep.get("port")))
        elif proto == "webhook" and ep.get("url"):
            url = str(ep["url"])
            _telemetry_dispatcher.submit(lambda u=url: CountingService._dispatch_webhook(u, payload), key=("webhook", url))
        else:
            continue
        queued += 1
    return queued


class CountingService:
    """
    Industrial Counting & Defect PPM Telemetry Service.
    Manages 2-wireline crossing, per-class counts, Products Per Minute (PPM), rejected count, and yield.
    """
    _main_loop: Optional[asyncio.AbstractEventLoop] = None
    _http_client: Optional[httpx.AsyncClient] = None

    @classmethod
    def set_event_loop(cls, loop: asyncio.AbstractEventLoop) -> None:
        cls._main_loop = loop

    @classmethod
    def get_event_loop(cls) -> Optional[asyncio.AbstractEventLoop]:
        if cls._main_loop is not None and not cls._main_loop.is_closed():
            return cls._main_loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def __init__(
        self,
        line_id: str = "line-1",
        line_name: str = "Line 1",
        camera_id: Optional[str] = None,
        dispatch_telemetry: bool = True,
    ):
        # The production line this counter belongs to. camera_id is set for a
        # line's second vision camera, whose counter runs apart from the line
        # totals; the line's counting camera leaves it None.
        self.line_id = line_id
        self.line_name = line_name
        self.camera_id = camera_id
        # False: this counter's products are not reported to the line's send
        # cards (a counter that belongs to no running line, as in tests).
        self.dispatch_telemetry = dispatch_telemetry
        # When set, a product that crossed the count line is handed here, not yet
        # counted, instead of being counted at once: a line with Sync on pairs
        # it with its code first, decides its one result, then calls
        # finish_crossing() itself.
        self.event_sink: Optional[Any] = None
        # When set, called as (camera_id, line_number, track_id, class_name)
        # each time a product crosses wire line 1 or 2, before it is counted:
        # a QR reader set to capture on a crossing takes its picture then.
        self.line_crossing_sink: Optional[Any] = None
        self.config = CountingConfig()
        self.tracker = WirelineTracker(
            track_high_thresh=self.config.track_high_thresh,
            track_low_thresh=self.config.track_low_thresh,
            match_threshold=self.config.match_threshold,
            max_missed_frames=self.config.max_missed_frames,
            max_speed_pixels=self.config.max_speed_pixels,
            min_hits=self.config.min_hits,
            position_tolerance=self.config.position_tolerance,
        )
        self._trackers: Dict[str, WirelineTracker] = {"default": self.tracker}
        self._tracker_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.counts_by_class: Dict[str, int] = {}
        # How many of each class's products were rejected, so resetting one class
        # takes its good and its rejected products off the totals.
        self.rejects_by_class: Dict[str, int] = {}
        self.total_inspected: int = 0
        self.good_count: int = 0
        self.rejected_count: int = 0
        self._inspection_timestamps: collections.deque[float] = collections.deque()

    def get_tracker(self, camera_id: Optional[str] = None) -> WirelineTracker:
        """The tracker process_frame() feeds for camera_id, else the default tracker."""
        if camera_id:
            tracker = self._trackers.get(camera_id)
            if tracker is not None:
                return tracker
        return self.tracker

    async def shutdown(self) -> None:
        # Queued messages go out first; then the TCP channels close their kept
        # connections and listeners, which live on the dispatcher's loop.
        _telemetry_dispatcher.wait_drained(timeout=5.0)
        try:
            from app.services.tcp_channels import TcpChannels
            await TcpChannels.close_all()
        except Exception as exc:
            logger.warning("TCP channels did not close cleanly: %s", exc)
        _telemetry_dispatcher.stop()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    @property
    def products_per_minute(self) -> float:
        """Calculates throughput rate: Products Per Minute (PPM) based on 60-second sliding window."""
        now = time.time()
        # The inference thread adds to the window while an API request reads it.
        with self._count_lock:
            # Evict timestamps older than 60 seconds
            while self._inspection_timestamps and (now - self._inspection_timestamps[0]) > 60.0:
                self._inspection_timestamps.popleft()
            window_count = len(self._inspection_timestamps)
            earliest = self._inspection_timestamps[0] if window_count else now
        if window_count == 0:
            return 0.0

        # If data covers less than 60s, compute instantaneous rate
        elapsed = max(now - earliest, 1.0)
        if elapsed < 60.0:
            rate = (window_count / elapsed) * 60.0
            return round(min(rate, float(window_count * 60)), 1)

        return round(float(window_count), 1)

    @property
    def defect_ppm(self) -> float:
        """Calculates Defect Parts Per Million (PPM)."""
        if self.total_inspected == 0:
            return 0.0
        return round((self.rejected_count / self.total_inspected) * 1_000_000, 2)

    @property
    def yield_percentage(self) -> float:
        """Calculates production yield percentage (0.0 to 100.0%)."""
        if self.total_inspected == 0:
            return 100.0
        return round((self.good_count / self.total_inspected) * 100.0, 2)

    def process_frame(
        self,
        detections: List[DetectionItem],
        frame_w: int,
        frame_h: int,
        camera_rotation: Optional[int] = None,
        camera_flip_h: Optional[bool] = None,
        camera_flip_v: Optional[bool] = None,
        camera_id: Optional[str] = None,
    ) -> List[Tuple[int, str, bool]]:
        """
        Feeds detections into tracker and updates counting counters on line crossings.

        camera_rotation and the flips are accepted for older callers and not
        used: products leave the picture where their flow leads (tracker.exit_edge).
        """
        tracker_key = camera_id or "default"
        config = self.config
        with self._tracker_lock:
            tracker = self._trackers.get(tracker_key)
            if tracker is None:
                tracker = WirelineTracker(
                    track_high_thresh=config.track_high_thresh,
                    track_low_thresh=config.track_low_thresh,
                    match_threshold=config.match_threshold,
                    max_missed_frames=config.max_missed_frames,
                    max_speed_pixels=config.max_speed_pixels,
                    min_hits=config.min_hits,
                    position_tolerance=config.position_tolerance,
                )
                self._trackers[tracker_key] = tracker
        events = tracker.update(
            detections=detections,
            frame_w=frame_w,
            frame_h=frame_h,
            line1_rel=config.line1_position,
            line2_rel=config.line2_position,
            orientation=config.orientation,
            direction=config.direction,
            expected_classes=config.expected_classes,
            defect_classes=config.defect_classes,
            track_high_thresh=config.track_high_thresh,
            track_low_thresh=config.track_low_thresh,
            match_threshold=config.match_threshold,
            max_missed_frames=config.max_missed_frames,
            max_speed_pixels=config.max_speed_pixels,
            min_hits=config.min_hits,
            position_tolerance=config.position_tolerance,
            name_based_defects=config.name_based_defects,
        )

        crossing_sink = self.line_crossing_sink
        if crossing_sink is not None:
            for track_id, class_name, line_number in list(getattr(tracker, "line_crossings", ()) or ()):
                try:
                    crossing_sink(camera_id, line_number, track_id, class_name)
                except Exception as exc:
                    logger.warning("Line %s: wire line %d crossing hand-off failed: %s", self.line_id, line_number, exc)

        now = time.time()
        for event in events:
            crossing = self._crossing(event, camera_id, now)
            sink = self.event_sink
            if sink is not None:
                try:
                    sink(crossing)
                    continue
                except Exception as sink_err:
                    logger.warning("Line %s sync hand-off failed, counting the product on its own: %s", self.line_id, sink_err)
            self.finish_crossing(crossing)
        return events

    def _crossing(self, event: Tuple[Any, ...], camera_id: Optional[str], now: float) -> Dict[str, Any]:
        """What is known of a product when it crosses the count line. Nothing is counted yet."""
        bbox = event[4] if len(event) > 4 else None
        smooth_center = event[6] if len(event) > 6 else None
        velocity = event[7] if len(event) > 7 else None

        bbox_dict = None
        if bbox is not None:
            if hasattr(bbox, "model_dump"):
                bbox_dict = bbox.model_dump()
            elif hasattr(bbox, "dict"):
                bbox_dict = bbox.dict()
            elif isinstance(bbox, dict):
                bbox_dict = bbox
            elif isinstance(bbox, (tuple, list)) and len(bbox) >= 4:
                bbox_dict = {
                    "x1": int(round(bbox[0])),
                    "y1": int(round(bbox[1])),
                    "x2": int(round(bbox[2])),
                    "y2": int(round(bbox[3])),
                }
        return {
            "track_id": event[0],
            "class_name": event[1],
            # What the vision camera says. The product's result may still become
            # a reject because of its code (finish_crossing).
            "is_defect": bool(event[2]),
            "confidence": event[3] if len(event) > 3 else 1.0,
            "bbox": bbox_dict,
            "polygon": event[5] if len(event) > 5 else None,
            "sc_x": round(float(smooth_center[0]), 2) if smooth_center else 0.0,
            "sc_y": round(float(smooth_center[1]), 2) if smooth_center else 0.0,
            "vel_x": round(float(velocity[0]), 2) if velocity else 0.0,
            "vel_y": round(float(velocity[1]), 2) if velocity else 0.0,
            "camera_id": camera_id or self.camera_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time": now,
            "crossed_at": time.monotonic(),
        }

    def finish_crossing(
        self,
        crossing: Dict[str, Any],
        reject: Optional[bool] = None,
        reason: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
        plc_fields: Optional[Dict[str, Any]] = None,
        delay_from_crossing: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Count one product with its final result and send its one event.

        ``reject`` and ``reason`` are the product's result when a code check
        took part in it; left out, the vision camera's result stands.
        ``fields`` / ``plc_fields`` add the paired code to the message and to
        the PLC event. ``delay_from_crossing`` makes a PLC card's travel delay
        run from the moment of the crossing, not from this call.
        Returns (payload, plc_event).
        """
        from app.services.line_config import REASON_VISION

        vision_reject = bool(crossing["is_defect"])
        is_reject = vision_reject if reject is None else bool(reject)
        if is_reject and reason is None and vision_reject:
            reason = REASON_VISION
        if not is_reject:
            reason = None
        track_id = crossing["track_id"]
        class_name = crossing["class_name"]
        cname_clean = str(class_name).strip().lower()
        with self._count_lock:
            self.counts_by_class[cname_clean] = self.counts_by_class.get(cname_clean, 0) + 1
            self.total_inspected += 1
            current_total = self.total_inspected
            self._inspection_timestamps.append(crossing["time"])
            if is_reject:
                self.rejected_count += 1
                self.rejects_by_class[cname_clean] = self.rejects_by_class.get(cname_clean, 0) + 1
            else:
                self.good_count += 1

        logger.info(
            "COUNT EVENT -> Track #%s | Class: '%s' | Reject: %s%s | Total: %d | SmoothCenter: (%.1f, %.1f) | Vel: (%.1f, %.1f)",
            track_id, class_name, is_reject, f" ({reason})" if reason else "", current_total,
            crossing["sc_x"], crossing["sc_y"], crossing["vel_x"], crossing["vel_y"],
        )

        # Comprehensive, Standardized Industrial Reading JSON Payload
        payload = {
            "event": "WIRELINE_OBJECT_CROSSED",
            "timestamp": crossing["timestamp"],
            "track_id": track_id,
            "class_name": class_name,
            # The product's one result: the vision camera and, where a camera
            # checks codes, the code together.
            "result": "REJECTED" if is_reject else "PASSED",
            "is_defect": is_reject,
            "reject_reason": reason,
            "vision_result": "REJECTED" if vision_reject else "PASSED",
            "confidence": round(float(crossing["confidence"]), 4),
            "bbox": crossing["bbox"],
            "polygon": crossing["polygon"],
            "smooth_center": {"x": crossing["sc_x"], "y": crossing["sc_y"]},
            "velocity": {"vx": crossing["vel_x"], "vy": crossing["vel_y"]},
            "smooth_center_x": crossing["sc_x"],
            "smooth_center_y": crossing["sc_y"],
            "velocity_x": crossing["vel_x"],
            "velocity_y": crossing["vel_y"],
            "counts": dict(self.counts_by_class),
            **self._metrics(),
            "line_id": self.line_id,
            "line_name": self.line_name,
            "camera_id": crossing["camera_id"],
        }
        plc_event = {
            "event_id": f"{track_id}_{current_total}",
            "event_type": "crossing",
            "line_id": self.line_id,
            "camera_id": crossing["camera_id"],
            # A second vision camera only fires cards that name it.
            "counting_camera": self.camera_id is None,
            "result": "reject" if is_reject else "good",
            "reject_reason": reason,
            "vision_result": "reject" if vision_reject else "good",
            "good_count": self.good_count,
            "reject_count": self.rejected_count,
            "detected_classes": [class_name] if class_name else [],
            "timestamp": crossing["time"],
            "crossed_at": crossing["crossed_at"],
            "delay_from_crossing": bool(delay_from_crossing),
        }
        if fields:
            payload.update(fields)
        if plc_fields:
            plc_event.update(plc_fields)
        self.dispatch_event(payload, plc_event, is_reject)
        return payload, plc_event

    def _metrics(self) -> Dict[str, Any]:
        """The line totals, as every message carries them (nested and flat)."""
        metrics = {
            "total_inspected": self.total_inspected,
            "good_count": self.good_count,
            "rejected_count": self.rejected_count,
            "products_per_minute": self.products_per_minute,
            "defect_ppm": self.defect_ppm,
            "yield_percentage": self.yield_percentage,
        }
        return {"metrics": dict(metrics), **metrics}

    def count_product(self, name: str, reject: bool) -> Dict[str, Any]:
        """Count one product that no vision camera saw cross a line: on a line with
        only a code reader, each code read is one product. Returns the totals after it."""
        key = str(name or "").strip().lower() or "product"
        with self._count_lock:
            self.counts_by_class[key] = self.counts_by_class.get(key, 0) + 1
            self.total_inspected += 1
            self._inspection_timestamps.append(time.time())
            if reject:
                self.rejected_count += 1
                self.rejects_by_class[key] = self.rejects_by_class.get(key, 0) + 1
            else:
                self.good_count += 1
        return self._metrics()

    def dispatch_event(self, payload: Dict[str, Any], plc_event: Dict[str, Any], is_defect: bool) -> None:
        """Send one product's event to the event bus, the line's PLC cards and its send cards."""
        loop = self.get_event_loop()
        if not (loop and loop.is_running()):
            return
        # 1. Internal Event Bus & Visual Action Engine
        try:
            from app.events.event_bus import event_bus
            asyncio.run_coroutine_threadsafe(event_bus.publish("wireline_cross", payload), loop)
            asyncio.run_coroutine_threadsafe(event_bus.publish("reading", payload), loop)
        except Exception as eb_err:
            logger.debug("EventBus dispatch error: %s", eb_err)

        # 1b. PLC Action Dispatcher: the line's enabled PLC action cards
        try:
            from app.services.plc_dispatcher_service import PLCDispatcherService
            asyncio.run_coroutine_threadsafe(PLCDispatcherService.evaluate(plc_event), loop)
        except Exception as plc_err:
            logger.debug("PLC dispatcher evaluate error: %s", plc_err)

        # 2. The line's send cards: messages to other systems
        self.send(plc_event, payload)

    def send(self, event: Dict[str, Any], payload: Dict[str, Any]) -> int:
        """Hand an event of this counter's line to the line's send cards. Returns the messages queued."""
        if not self.dispatch_telemetry:
            return 0
        # Sparkplug B publishes the line's latest product and code as tags.
        try:
            from app.services.sparkplug_service import SparkplugService
            SparkplugService.note_event(payload)
        except Exception as spb_err:
            logger.debug("Sparkplug event note error: %s", spb_err)
        try:
            from app.services.send_dispatcher_service import SendDispatcherService
            return SendDispatcherService.evaluate(event, payload)
        except Exception as send_err:
            logger.debug("Send card evaluate error: %s", send_err)
            return 0

    @classmethod
    def http_client(cls) -> httpx.AsyncClient:
        """The shared client for webhook messages (made on first use, closed at shutdown)."""
        if cls._http_client is None or cls._http_client.is_closed:
            cls._http_client = httpx.AsyncClient(timeout=3.0, follow_redirects=False)
        return cls._http_client

    def get_current_reading_payload(self) -> Dict[str, Any]:
        """Returns current live snapshot of counting & inspection telemetry as JSON."""
        return {
            "event": "INSPECTION_READING",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counts": dict(self.counts_by_class),
            **self._metrics(),
            "active_tracks_count": sum(len(tr.objects) for tr in self._trackers.values()),
        }

    @classmethod
    async def _dispatch_webhook(cls, url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        try:
            await cls.http_client().post(url, json=payload, headers=headers or {})
        except Exception as exc:
            logger.debug("Webhook dispatch error (%s): %s", url, exc)

    def get_stats(self) -> CountingStatsResponse:
        ordered_counts: Dict[str, int] = {}
        for c in (self.config.expected_classes or []):
            if c:
                name = str(c).strip().lower()
                ordered_counts[name] = self.counts_by_class.get(name, 0)

        for k, v in self.counts_by_class.items():
            k_clean = str(k).strip().lower()
            if k_clean not in ordered_counts:
                ordered_counts[k_clean] = v

        tracks_list = []
        try:
            from app.schemas.counting import TrackInfo
            for tracker in self._trackers.values():
                for obj in tracker.objects.values():
                    if hasattr(obj, "to_track_info"):
                        tracks_list.append(obj.to_track_info())
        except Exception as te:
            logger.debug("Failed to build tracks list: %s", te)

        return CountingStatsResponse(
            total_inspected=self.total_inspected,
            good_count=self.good_count,
            rejected_count=self.rejected_count,
            defect_ppm=self.defect_ppm,
            products_per_minute=self.products_per_minute,
            yield_percentage=self.yield_percentage,
            counts_by_class=ordered_counts,
            expected_classes=self.config.expected_classes,
            defect_classes=self.config.defect_classes,
            active_tracks_count=sum(len(tr.objects) for tr in self._trackers.values()),
            tracks=tracks_list,
            config=self.config,
        )

    def get_active_tracks(self) -> List[Any]:
        """Returns list of TrackInfo for all currently active tracks."""
        tracks = []
        try:
            for tracker in self._trackers.values():
                for obj in tracker.objects.values():
                    if hasattr(obj, "to_track_info"):
                        tracks.append(obj.to_track_info())
        except Exception as te:
            logger.debug("Failed to get active tracks: %s", te)
        return tracks


    def update_config(self, new_config: CountingConfig) -> CountingConfig:
        self.config = new_config
        logger.info("Updated counting config: Expected classes = %s | Lines = (%.2f, %.2f)",
                    self.config.expected_classes, self.config.line1_position, self.config.line2_position)
        return self.config

    @property
    def last_count_at(self) -> float:
        """Wall-clock time of the last counted product (0 if none in the last minute or since a reset)."""
        with self._count_lock:
            stamps = self._inspection_timestamps
            return stamps[-1] if stamps else 0.0

    def reset_counts(self, reset_all: bool = True, classes_to_reset: Optional[List[str]] = None) -> CountingStatsResponse:
        if reset_all:
            with self._count_lock:
                self.counts_by_class.clear()
                self.rejects_by_class.clear()
                self.total_inspected = 0
                self.good_count = 0
                self.rejected_count = 0
                self._inspection_timestamps.clear()
            for tracker in self._trackers.values():
                tracker.objects.clear()
                tracker._recently_counted.clear()
        elif classes_to_reset:
            with self._count_lock:
                for c in classes_to_reset:
                    name = str(c).strip().lower()
                    count = self.counts_by_class.pop(name, 0)
                    rejected = min(count, self.rejects_by_class.pop(name, 0))
                    # The class's products leave the totals, each where it was counted.
                    self.total_inspected = max(0, self.total_inspected - count)
                    self.rejected_count = max(0, self.rejected_count - rejected)
                    self.good_count = max(0, self.good_count - (count - rejected))

        logger.info("Counting metrics reset.")
        return self.get_stats()


counting_service = CountingService()
