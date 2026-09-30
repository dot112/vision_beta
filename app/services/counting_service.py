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

    def stop(self) -> None:
        # Give queued events up to 5 s to go out, then stop the loop.
        self._drained.wait(timeout=5.0)
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
            from app.services.mqtt_service import MQTTService
            topic = ep["topic"]
            _telemetry_dispatcher.submit(lambda t=topic: MQTTService.publish(t, payload, qos=0), key=("mqtt", topic))
        elif proto == "tcp" and ep.get("host") and ep.get("port"):
            host, port = str(ep["host"]), int(ep["port"])
            _telemetry_dispatcher.submit(lambda h=host, p=port: CountingService._dispatch_tcp(h, p, payload), key=("tcp", host, port))
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
        self.dispatch_telemetry = dispatch_telemetry
        # When set, finished crossing events go here instead of straight out:
        # a line with Sync on pairs them with a QR read first, then calls
        # dispatch_event() itself.
        self.event_sink: Optional[Any] = None
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
        _telemetry_dispatcher.stop()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    @property
    def products_per_minute(self) -> float:
        """Calculates throughput rate: Products Per Minute (PPM) based on 60-second sliding window."""
        now = time.time()
        # Evict timestamps older than 60 seconds
        while self._inspection_timestamps and (now - self._inspection_timestamps[0]) > 60.0:
            self._inspection_timestamps.popleft()

        window_count = len(self._inspection_timestamps)
        if window_count == 0:
            return 0.0

        # If data covers less than 60s, compute instantaneous rate
        earliest = self._inspection_timestamps[0]
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
        """
        if camera_rotation is None:
            try:
                from app.services.camera_service import camera_orientation
                camera_rotation, camera_flip_h, camera_flip_v = camera_orientation(
                    camera_id, camera_flip_h, camera_flip_v
                )
            except Exception as exc:
                logger.debug("Camera orientation lookup failed: %s", exc)

        tracker_key = camera_id or "default"
        with self._tracker_lock:
            tracker = self._trackers.get(tracker_key)
            if tracker is None:
                tracker = WirelineTracker(
                    track_high_thresh=self.config.track_high_thresh,
                    track_low_thresh=self.config.track_low_thresh,
                    match_threshold=self.config.match_threshold,
                    max_missed_frames=self.config.max_missed_frames,
                    max_speed_pixels=self.config.max_speed_pixels,
                    min_hits=self.config.min_hits,
                    position_tolerance=self.config.position_tolerance,
                )
                self._trackers[tracker_key] = tracker
        events = tracker.update(
            detections=detections,
            frame_w=frame_w,
            frame_h=frame_h,
            line1_rel=self.config.line1_position,
            line2_rel=self.config.line2_position,
            orientation=self.config.orientation,
            direction=self.config.direction,
            expected_classes=self.config.expected_classes,
            defect_classes=self.config.defect_classes,
            track_high_thresh=getattr(self.config, "track_high_thresh", 0.50),
            track_low_thresh=getattr(self.config, "track_low_thresh", 0.15),
            match_threshold=getattr(self.config, "match_threshold", 0.70),
            max_missed_frames=getattr(self.config, "max_missed_frames", 15),
            max_speed_pixels=getattr(self.config, "max_speed_pixels", 120.0),
            min_hits=getattr(self.config, "min_hits", 2),
            position_tolerance=getattr(self.config, "position_tolerance", 180.0),
            camera_rotation=camera_rotation,
            camera_flip_h=camera_flip_h,
            camera_flip_v=camera_flip_v,
        )

        now = time.time()
        for event in events:
            track_id = event[0]
            class_name = event[1]
            is_defect = event[2]
            conf = event[3] if len(event) > 3 else 1.0
            bbox = event[4] if len(event) > 4 else None
            poly = event[5] if len(event) > 5 else None
            smooth_center = event[6] if len(event) > 6 else None
            velocity = event[7] if len(event) > 7 else None

            cname_clean = str(class_name).strip().lower()
            with self._count_lock:
                self.counts_by_class[cname_clean] = self.counts_by_class.get(cname_clean, 0) + 1
                self.total_inspected += 1
                current_total = self.total_inspected
                self._inspection_timestamps.append(now)
                if is_defect:
                    self.rejected_count += 1
                else:
                    self.good_count += 1

            sc_x = round(float(smooth_center[0]), 2) if smooth_center else 0.0
            sc_y = round(float(smooth_center[1]), 2) if smooth_center else 0.0
            vel_x = round(float(velocity[0]), 2) if velocity else 0.0
            vel_y = round(float(velocity[1]), 2) if velocity else 0.0

            logger.info(
                "COUNT EVENT -> Track #%d | Class: '%s' | Defect: %s | Total: %d | SmoothCenter: (%.1f, %.1f) | Vel: (%.1f, %.1f)",
                track_id, class_name, is_defect, current_total, sc_x, sc_y, vel_x, vel_y
            )

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

            # Comprehensive, Standardized Industrial Reading JSON Payload
            payload = {
                "event": "WIRELINE_OBJECT_CROSSED",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "track_id": track_id,
                "class_name": class_name,
                "result": "REJECTED" if is_defect else "PASSED",
                "is_defect": is_defect,
                "confidence": round(float(conf), 4),
                "bbox": bbox_dict,
                "polygon": poly,
                "smooth_center": {"x": sc_x, "y": sc_y},
                "velocity": {"vx": vel_x, "vy": vel_y},
                "smooth_center_x": sc_x,
                "smooth_center_y": sc_y,
                "velocity_x": vel_x,
                "velocity_y": vel_y,
                "counts": dict(self.counts_by_class),
                "metrics": {
                    "total_inspected": self.total_inspected,
                    "good_count": self.good_count,
                    "rejected_count": self.rejected_count,
                    "products_per_minute": self.products_per_minute,
                    "defect_ppm": self.defect_ppm,
                    "yield_percentage": self.yield_percentage,
                },
                "total_inspected": self.total_inspected,
                "good_count": self.good_count,
                "rejected_count": self.rejected_count,
                "products_per_minute": self.products_per_minute,
                "defect_ppm": self.defect_ppm,
                "yield_percentage": self.yield_percentage,
                "line_id": self.line_id,
                "line_name": self.line_name,
                "camera_id": camera_id or self.camera_id,
            }
            plc_event = {
                "event_id": f"{track_id}_{self.total_inspected}",
                "event_type": "crossing",
                "line_id": self.line_id,
                "camera_id": camera_id or self.camera_id,
                # A second vision camera only fires cards that name it.
                "counting_camera": self.camera_id is None,
                "result": "reject" if is_defect else "good",
                "good_count": self.good_count,
                "reject_count": self.rejected_count,
                "detected_classes": [class_name] if class_name else [],
                "timestamp": now,
            }

            sink = self.event_sink
            if sink is not None:
                try:
                    sink(payload, plc_event, is_defect)
                    continue
                except Exception as sink_err:
                    logger.warning("Line %s sync hand-off failed, sending event unpaired: %s", self.line_id, sink_err)
            self.dispatch_event(payload, plc_event, is_defect)
        return events

    def dispatch_event(self, payload: Dict[str, Any], plc_event: Dict[str, Any], is_defect: bool) -> None:
        """Send one crossing event to the event bus, the PLC cards and the line's telemetry targets."""
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

        if not self.dispatch_telemetry:
            return
        for protocol, enabled in (
            ("mqtt", self.config.send_mqtt),
            ("tcp", self.config.send_tcp),
            ("webhook", self.config.send_webhook),
        ):
            if not enabled:
                continue
            filtered = self._filter_payload_for_protocol(payload, protocol, is_defect)
            if filtered is not None:
                self._send(protocol, filtered)

    def dispatch_qr(self, payload: Dict[str, Any], known: bool) -> None:
        """Send a QR read to the line's telemetry targets that are set to send QR reads."""
        if not self.dispatch_telemetry:
            return
        for protocol in ("mqtt", "tcp", "webhook"):
            mode = str(getattr(self.config, f"{protocol}_qr_dispatch", "off") or "off").lower()
            if mode == "all" or (mode == "known" and known) or (mode == "unknown" and not known):
                self._send(protocol, payload)

    def _comm_endpoints(self) -> List[Dict[str, Any]]:
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            return list(SettingsPersistenceService.get_state().get("communication_endpoints", []))
        except Exception as ep_err:
            logger.debug("Failed reading communication_endpoints: %s", ep_err)
            return []

    def _targets(self, protocol: str) -> List[Any]:
        """Destinations for one protocol: the selected channel, every enabled channel, or the legacy setting."""
        comm_endpoints = self._comm_endpoints()
        selected = getattr(self.config, f"{protocol}_endpoint_id", None)

        def usable(ep: Dict[str, Any]) -> bool:
            return ep.get("enabled", True) is True and str(ep.get("protocol", "")).lower() == protocol

        if selected and selected != "all":
            chosen = [ep for ep in comm_endpoints if ep.get("id") == selected and usable(ep)][:1]
        else:
            chosen = [ep for ep in comm_endpoints if usable(ep)]

        if protocol == "mqtt":
            topics: List[str] = []
            for ep in chosen:
                if ep.get("topic") and ep["topic"] not in topics:
                    topics.append(ep["topic"])
            if not topics and not selected and self.config.mqtt_topic:
                topics.append(self.config.mqtt_topic)
            return topics
        if protocol == "tcp":
            hosts: List[Tuple[str, int]] = []
            for ep in chosen:
                if ep.get("host") and ep.get("port"):
                    target = (str(ep["host"]), int(ep["port"]))
                    if target not in hosts:
                        hosts.append(target)
            if not hosts and not selected and self.config.tcp_host and self.config.tcp_port:
                hosts.append((str(self.config.tcp_host), int(self.config.tcp_port)))
            return hosts
        hooks: List[Tuple[str, Optional[Dict[str, str]]]] = []
        for ep in chosen:
            if not ep.get("url"):
                continue
            hdrs_dict = None
            if selected and selected != "all":
                # Only an explicitly selected channel sends its saved headers.
                raw_hdrs = ep.get("headers")
                if isinstance(raw_hdrs, dict):
                    hdrs_dict = raw_hdrs
                elif isinstance(raw_hdrs, str) and raw_hdrs.strip():
                    hdrs_dict = {}
                    for line in raw_hdrs.splitlines():
                        if ":" in line:
                            k, _, v = line.partition(":")
                            hdrs_dict[k.strip()] = v.strip()
            hooks.append((str(ep["url"]), hdrs_dict))
        if not hooks and not selected and self.config.webhook_url:
            hooks.append((str(self.config.webhook_url), self.config.webhook_headers))
        return hooks

    def _send(self, protocol: str, payload: Dict[str, Any]) -> None:
        for target in self._targets(protocol):
            try:
                if protocol == "mqtt":
                    from app.services.mqtt_service import MQTTService
                    _telemetry_dispatcher.submit(
                        lambda t=target, p=payload: MQTTService.publish(t, p, qos=0),
                        key=("mqtt", target),
                    )
                elif protocol == "tcp":
                    host, port = target
                    _telemetry_dispatcher.submit(
                        lambda h=host, pt=port, pay=payload: self._dispatch_tcp(h, pt, pay),
                        key=("tcp", host, port),
                    )
                else:
                    url, hdrs = target
                    _telemetry_dispatcher.submit(
                        lambda u=url, pay=payload, h=hdrs: self._dispatch_webhook(u, pay, h),
                        key=("webhook", url),
                    )
            except Exception as exc:
                logger.debug("%s queue dispatch error (%s): %s", protocol, target, exc)

    def _filter_payload_for_protocol(self, payload: Dict[str, Any], protocol: str, is_defect: bool) -> Optional[Dict[str, Any]]:
        """Evaluates protocol-specific trigger condition and filters payload fields."""
        proto = (protocol or "").lower()
        if proto == "mqtt":
            trig = getattr(self.config, "mqtt_dispatch_trigger", getattr(self.config, "dispatch_trigger", "both"))
            fields = getattr(self.config, "mqtt_dispatched_fields", getattr(self.config, "dispatched_fields", None))
        elif proto == "tcp":
            trig = getattr(self.config, "tcp_dispatch_trigger", getattr(self.config, "dispatch_trigger", "both"))
            fields = getattr(self.config, "tcp_dispatched_fields", getattr(self.config, "dispatched_fields", None))
        elif proto == "webhook":
            trig = getattr(self.config, "webhook_dispatch_trigger", getattr(self.config, "dispatch_trigger", "both"))
            fields = getattr(self.config, "webhook_dispatched_fields", getattr(self.config, "dispatched_fields", None))
        else:
            trig = getattr(self.config, "dispatch_trigger", "both")
            fields = getattr(self.config, "dispatched_fields", None)

        trig = str(trig or "both").lower()
        if trig == "passed" and is_defect:
            return None
        if trig == "rejected" and not is_defect:
            return None

        if fields and isinstance(fields, list) and len(fields) > 0:
            filtered = {k: payload[k] for k in fields if k in payload}
            if filtered:
                return filtered
        return payload

    def get_current_reading_payload(self) -> Dict[str, Any]:
        """Returns current live snapshot of counting & inspection telemetry as JSON."""
        return {
            "event": "INSPECTION_READING",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counts": dict(self.counts_by_class),
            "metrics": {
                "total_inspected": self.total_inspected,
                "good_count": self.good_count,
                "rejected_count": self.rejected_count,
                "products_per_minute": self.products_per_minute,
                "defect_ppm": self.defect_ppm,
                "yield_percentage": self.yield_percentage,
            },
            "total_inspected": self.total_inspected,
            "good_count": self.good_count,
            "rejected_count": self.rejected_count,
            "products_per_minute": self.products_per_minute,
            "defect_ppm": self.defect_ppm,
            "yield_percentage": self.yield_percentage,
            "active_tracks_count": sum(len(tr.objects) for tr in self._trackers.values()),
        }

    @staticmethod
    async def _dispatch_tcp(host: str, port: int, payload: Dict[str, Any]) -> None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            logger.debug("TCP dispatch to %s:%d failed: %s", host, port, exc)

    @classmethod
    async def _dispatch_webhook(cls, url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        try:
            if cls._http_client is None or cls._http_client.is_closed:
                cls._http_client = httpx.AsyncClient(timeout=3.0, follow_redirects=False)
            await cls._http_client.post(url, json=payload, headers=headers or {})
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

    def reset_counts(self, reset_all: bool = True, classes_to_reset: Optional[List[str]] = None) -> CountingStatsResponse:
        if reset_all:
            self.counts_by_class.clear()
            self.total_inspected = 0
            self.good_count = 0
            self.rejected_count = 0
            self._inspection_timestamps.clear()
            for tracker in self._trackers.values():
                tracker.objects.clear()
                tracker._recently_counted.clear()
        elif classes_to_reset:
            for c in classes_to_reset:
                if c in self.counts_by_class:
                    del self.counts_by_class[c]
            self.total_inspected = sum(self.counts_by_class.values())

        logger.info("Counting metrics reset.")
        return self.get_stats()


counting_service = CountingService()
