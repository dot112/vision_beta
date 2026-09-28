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

    def __init__(self):
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
                from app.state.application_state import app_state
                from app.services.settings_persistence_service import SettingsPersistenceService
                cam_id = SettingsPersistenceService.get_active_camera_id()
                driver = app_state.cameras.get(cam_id) if cam_id else None
                if not driver and app_state.cameras:
                    driver = next(iter(app_state.cameras.values()), None)
                if driver:
                    s = getattr(driver, "settings", {})
                    camera_rotation = int(s.get("rotation") or 90) if "rotation" in s else 90
                    camera_flip_h = bool(s.get("flip_h", False))
                    camera_flip_v = bool(s.get("flip_v", False))
            except Exception:
                pass

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
            }

            loop = self.get_event_loop()
            if loop and loop.is_running():
                # 1. Internal Event Bus & Visual Action Engine
                try:
                    from app.events.event_bus import event_bus
                    asyncio.run_coroutine_threadsafe(event_bus.publish("wireline_cross", payload), loop)
                    asyncio.run_coroutine_threadsafe(event_bus.publish("reading", payload), loop)
                except Exception as eb_err:
                    logger.debug("EventBus dispatch error: %s", eb_err)

                # 1b. PLC Action Dispatcher — evaluate all enabled PLC action cards
                try:
                    from app.services.plc_dispatcher_service import PLCDispatcherService
                    plc_event = {
                        "event_id": f"{track_id}_{self.total_inspected}",
                        "result": "reject" if is_defect else "good",
                        "good_count": self.good_count,
                        "reject_count": self.rejected_count,
                        "detected_classes": [class_name] if class_name else [],
                        "timestamp": time.time() if hasattr(self, "_last_ts") else 0.0,
                    }
                    asyncio.run_coroutine_threadsafe(
                        PLCDispatcherService.evaluate(plc_event), loop
                    )
                except Exception as plc_err:
                    logger.debug("PLC dispatcher evaluate error: %s", plc_err)

                comm_endpoints = []
                try:
                    from app.services.settings_persistence_service import SettingsPersistenceService
                    state = SettingsPersistenceService.get_state()
                    comm_endpoints = state.get("communication_endpoints", [])
                except Exception as ep_err:
                    logger.debug("Failed reading communication_endpoints: %s", ep_err)

                # 2. MQTT Broker (toggled ON in Action Trigger)
                if self.config.send_mqtt:
                    mqtt_payload = self._filter_payload_for_protocol(payload, "mqtt", is_defect)
                    if mqtt_payload is not None:
                        mqtt_topics = set()
                        sel_mqtt_id = getattr(self.config, "mqtt_endpoint_id", None)
                        if sel_mqtt_id and sel_mqtt_id != "all":
                            for ep in comm_endpoints:
                                if (
                                    ep.get("id") == sel_mqtt_id
                                    and ep.get("enabled", True) is True
                                    and str(ep.get("protocol", "")).lower() == "mqtt"
                                    and ep.get("topic")
                                ):
                                    mqtt_topics.add(ep.get("topic"))
                                    break
                        else:
                            for ep in comm_endpoints:
                                if ep.get("enabled", True) is True and str(ep.get("protocol", "")).lower() == "mqtt":
                                    t = ep.get("topic")
                                    if t:
                                        mqtt_topics.add(t)
                            if not mqtt_topics and not sel_mqtt_id and self.config.mqtt_topic:
                                mqtt_topics.add(self.config.mqtt_topic)

                        for topic in mqtt_topics:
                            try:
                                from app.services.mqtt_service import MQTTService
                                _telemetry_dispatcher.submit(
                                    lambda t=topic, p=mqtt_payload: MQTTService.publish(t, p, qos=0),
                                    key=("mqtt", topic),
                                )
                            except Exception as exc:
                                logger.debug("MQTT queue dispatch error (%s): %s", topic, exc)

                # 3. TCP Socket (toggled ON in Action Trigger)
                if self.config.send_tcp:
                    tcp_payload = self._filter_payload_for_protocol(payload, "tcp", is_defect)
                    if tcp_payload is not None:
                        tcp_targets = set()
                        sel_tcp_id = getattr(self.config, "tcp_endpoint_id", None)
                        if sel_tcp_id and sel_tcp_id != "all":
                            for ep in comm_endpoints:
                                if (
                                    ep.get("id") == sel_tcp_id
                                    and ep.get("enabled", True) is True
                                    and str(ep.get("protocol", "")).lower() == "tcp"
                                ):
                                    h = ep.get("host")
                                    p = ep.get("port")
                                    if h and p:
                                        tcp_targets.add((str(h), int(p)))
                                    break
                        else:
                            for ep in comm_endpoints:
                                if ep.get("enabled", True) is True and str(ep.get("protocol", "")).lower() == "tcp":
                                    h = ep.get("host")
                                    p = ep.get("port")
                                    if h and p:
                                        tcp_targets.add((str(h), int(p)))
                            if not tcp_targets and not sel_tcp_id and self.config.tcp_host and self.config.tcp_port:
                                tcp_targets.add((str(self.config.tcp_host), int(self.config.tcp_port)))

                        for h, p in tcp_targets:
                            try:
                                _telemetry_dispatcher.submit(
                                    lambda host=h, port=p, pay=tcp_payload: self._dispatch_tcp(host, port, pay),
                                    key=("tcp", h, p),
                                )
                            except Exception as exc:
                                logger.debug("TCP queue dispatch error (%s:%d): %s", h, p, exc)

                # 4. HTTP Webhook API (toggled ON in Action Trigger)
                if self.config.send_webhook:
                    wh_payload = self._filter_payload_for_protocol(payload, "webhook", is_defect)
                    if wh_payload is not None:
                        wh_targets = []
                        sel_wh_id = getattr(self.config, "webhook_endpoint_id", None)
                        if sel_wh_id and sel_wh_id != "all":
                            for ep in comm_endpoints:
                                if (
                                    ep.get("id") == sel_wh_id
                                    and ep.get("enabled", True) is True
                                    and str(ep.get("protocol", "")).lower() == "webhook"
                                    and ep.get("url")
                                ):
                                    raw_hdrs = ep.get("headers")
                                    hdrs_dict = None
                                    if isinstance(raw_hdrs, dict):
                                        hdrs_dict = raw_hdrs
                                    elif isinstance(raw_hdrs, str) and raw_hdrs.strip():
                                        hdrs_dict = {}
                                        for line in raw_hdrs.splitlines():
                                            if ":" in line:
                                                k, _, v = line.partition(":")
                                                hdrs_dict[k.strip()] = v.strip()
                                    wh_targets.append((str(ep.get("url")), hdrs_dict))
                                    break
                        else:
                            for ep in comm_endpoints:
                                if ep.get("enabled", True) is True and str(ep.get("protocol", "")).lower() == "webhook":
                                    u = ep.get("url")
                                    if u:
                                        wh_targets.append((str(u), None))
                            if not wh_targets and not sel_wh_id and self.config.webhook_url:
                                wh_targets.append((str(self.config.webhook_url), self.config.webhook_headers))

                        for u, hdrs in wh_targets:
                            try:
                                _telemetry_dispatcher.submit(
                                    lambda url=u, pay=wh_payload, h=hdrs: self._dispatch_webhook(url, pay, h),
                                    key=("webhook", u),
                                )
                            except Exception as exc:
                                logger.debug("Webhook queue dispatch error (%s): %s", u, exc)
        return events

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
