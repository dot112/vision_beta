"""Component health checks, runtime metrics and the background health watchdog.

Each check reports one of four states:

* ``ok``       — the component is working.
* ``idle``     — nothing is configured or needed (e.g. no camera connected).
* ``degraded`` — some of the component's instances are down.
* ``down``     — the component is not working.

Checks also raise and clear alarms, so a camera that stops delivering frames or
an inference worker that stops producing results shows up in the alarm list
even when nobody is polling ``/health``: the watchdog runs the checks on a
fixed interval.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import settings
from app.events.alarm_events import AlarmSeverity, alarm_manager
from app.state.application_state import app_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

OK, IDLE, DEGRADED, DOWN = "ok", "idle", "degraded", "down"


class HealthAlarmCode:
    CAMERA_DISCONNECTED = "camera.disconnected"
    CAMERA_STALLED = "camera.stalled"
    INFERENCE_MODEL_NOT_LOADED = "inference.model_not_loaded"
    INFERENCE_RUNNER_STOPPED = "inference.runner_stopped"
    INFERENCE_STALLED = "inference.stalled"
    LINE_FPS_LOW = "line.fps_low"


def _rollup(statuses: List[str]) -> str:
    """Combine instance statuses into a component status."""
    if not statuses:
        return IDLE
    if all(s == OK for s in statuses):
        return OK
    if any(s == OK for s in statuses):
        return DEGRADED
    return DOWN


class HealthService:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._started = clock()
        # camera_id -> (last seen frame id, time it last changed)
        self._camera_progress: Dict[str, Tuple[int, float]] = {}
        # (last seen processed frame count, time it last changed)
        self._inference_progress: Tuple[int, float] = (app_state.processed_frames, clock())

    @property
    def uptime_seconds(self) -> float:
        return self._clock() - self._started

    # ── Cameras ───────────────────────────────────────────────────────────────

    def check_cameras(self) -> Dict[str, Any]:
        now = self._clock()
        stale_after = settings.HEALTH_CAMERA_STALE_SECONDS
        cameras: List[Dict[str, Any]] = []

        live_ids = set()
        for camera_id, driver in list(app_state.cameras.items()):
            live_ids.add(camera_id)
            source = f"camera:{camera_id}"
            name = getattr(driver, "name", camera_id)
            entry: Dict[str, Any] = {"id": camera_id, "name": name, "connected": bool(getattr(driver, "is_connected", False))}
            line_id, line_name = _camera_line(camera_id)
            entry["line_id"] = line_id
            # Alarms name the camera's line so a fault on one line is not mistaken for another's.
            where = f" on line '{line_name}'" if line_name else ""
            line_details = {"line_id": line_id} if line_id else {}

            if not entry["connected"]:
                entry["status"] = DOWN
                entry["error"] = getattr(driver, "last_error", None) or "camera is not connected"
                self._camera_progress.pop(camera_id, None)
                alarm_manager.raise_alarm(
                    HealthAlarmCode.CAMERA_DISCONNECTED, source,
                    f"Camera '{name}'{where} is not connected: {entry['error']}", AlarmSeverity.CRITICAL,
                    line_details or None,
                )
                alarm_manager.clear_alarm(HealthAlarmCode.CAMERA_STALLED, source, "camera disconnected")
                cameras.append(entry)
                continue
            alarm_manager.clear_alarm(HealthAlarmCode.CAMERA_DISCONNECTED, source, "camera connected")

            frame_id = _latest_frame_id(driver)
            last_id, changed_at = self._camera_progress.get(camera_id, (None, now))
            if frame_id != last_id:
                changed_at = now
            self._camera_progress[camera_id] = (frame_id, changed_at)
            age = now - changed_at
            entry["frame_id"] = frame_id
            entry["seconds_since_new_frame"] = round(age, 2)

            if age > stale_after:
                entry["status"] = DOWN
                entry["error"] = f"no new frame for {age:.1f}s"
                alarm_manager.raise_alarm(
                    HealthAlarmCode.CAMERA_STALLED, source,
                    f"Camera '{name}'{where} has not delivered a new frame for {age:.1f}s", AlarmSeverity.CRITICAL,
                    {"frame_id": frame_id, **line_details},
                )
            else:
                entry["status"] = OK
                alarm_manager.clear_alarm(HealthAlarmCode.CAMERA_STALLED, source, "frames flowing")
            cameras.append(entry)

        # A camera that was disconnected on purpose leaves app_state; drop its alarms.
        for camera_id in set(self._camera_progress) - live_ids:
            self._camera_progress.pop(camera_id, None)
        for alarm in alarm_manager.active("camera:"):
            if alarm.code in (HealthAlarmCode.CAMERA_DISCONNECTED, HealthAlarmCode.CAMERA_STALLED) \
                    and alarm.source.split(":", 1)[1] not in live_ids:
                alarm_manager.clear_alarm(alarm.code, alarm.source, "camera removed")

        return {"status": _rollup([c["status"] for c in cameras]), "cameras": cameras}

    # ── Inference ─────────────────────────────────────────────────────────────

    def check_inference(self, frames_flowing: bool, flowing: Optional[List[str]] = None) -> Dict[str, Any]:
        """``flowing``: the ids of the cameras that are delivering frames.

        A model runs only on the vision cameras of running lines, each with
        the model picked for it. A camera whose own model is missing has its
        own alarm (camera.model_unavailable); the alarms here are about
        inference as a whole.
        """
        now = self._clock()
        source = "inference"
        result: Dict[str, Any] = {
            "processed_frames": app_state.processed_frames,
            "detection_count": app_state.detection_count,
        }

        try:
            from app.services.line_service import line_manager
            from app.services.vision_service import ContinuousVisionRunner
            models = line_manager.loaded_models()
            # Per streaming camera a running line runs a model on: is its model loaded?
            vision = [state for state in (line_manager.inferring(cid) for cid in flowing or []) if state is not None]
            runner_alive = bool(ContinuousVisionRunner._running and ContinuousVisionRunner._thread
                                and ContinuousVisionRunner._thread.is_alive())
        except Exception as exc:
            logger.exception("Inference health check failed")
            result.update(status=DOWN, error=f"{type(exc).__name__}: {exc}")
            return result

        result.update(model=", ".join(model["name"] for model in models) or None,
                      model_id=models[0]["id"] if models else None,
                      models=models, model_loaded=bool(models), runner_alive=runner_alive)

        # Frames that should be going through a model: a streaming vision camera whose model is loaded.
        inferring = any(vision)
        processed = app_state.processed_frames
        last_count, changed_at = self._inference_progress
        if processed != last_count or not inferring:
            changed_at = now
        self._inference_progress = (processed, changed_at)
        stalled_for = now - changed_at
        result["seconds_since_inference"] = round(stalled_for, 2)

        problems: List[Tuple[str, str]] = []
        expected = set()
        if not models and vision:
            problems.append((HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED, "No inference model is loaded"))
            expected.add(HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED)
        if not runner_alive:
            problems.append((HealthAlarmCode.INFERENCE_RUNNER_STOPPED, "The continuous vision runner is not running"))
            expected.add(HealthAlarmCode.INFERENCE_RUNNER_STOPPED)
        if inferring and runner_alive and stalled_for > settings.HEALTH_INFERENCE_STALE_SECONDS:
            problems.append((HealthAlarmCode.INFERENCE_STALLED,
                             f"Frames are arriving but nothing has been inferred for {stalled_for:.1f}s"))
            expected.add(HealthAlarmCode.INFERENCE_STALLED)

        # Without a streaming camera, inference not running is expected, not a fault.
        raised = expected if frames_flowing else set()
        for code, message in problems:
            if code in raised:
                alarm_manager.raise_alarm(code, source, message, AlarmSeverity.CRITICAL)
        for code in (HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED, HealthAlarmCode.INFERENCE_RUNNER_STOPPED,
                     HealthAlarmCode.INFERENCE_STALLED):
            if code not in raised:
                alarm_manager.clear_alarm(code, source, "inference healthy or idle")

        if raised:
            result["status"] = DOWN
            result["error"] = "; ".join(message for code, message in problems if code in raised)
        elif not vision:
            result["status"] = IDLE  # no streaming camera runs a model
        else:
            result["status"] = OK
        return result

    # ── PLC ───────────────────────────────────────────────────────────────────

    def check_plc(self) -> Dict[str, Any]:
        from app.hardware.plc.factory import _driver_pool

        endpoints = []
        for endpoint_id, driver in list(_driver_pool.items()):
            connected = bool(driver.is_connected)
            entry = {
                "id": endpoint_id,
                "name": driver._ep.get("name", endpoint_id),
                "protocol": driver.protocol,
                "connected": connected,
                "status": OK if connected else DOWN,
            }
            if not connected and driver.last_connect_error:
                entry["error"] = driver.last_connect_error
            endpoints.append(entry)
        return {"status": _rollup([e["status"] for e in endpoints]), "endpoints": endpoints}

    # ── Production lines ──────────────────────────────────────────────────────

    def check_lines(self) -> Dict[str, Any]:
        """Per-line rollup; raises an alarm when a running line drops below its minimum frame rate."""
        from app.services.line_service import line_manager

        rows = []
        live_sources = set()
        for runtime in line_manager.all():
            summary = line_manager.summary(runtime)
            source = f"line:{runtime.id}"
            live_sources.add(source)
            state = summary["state"]
            status = {"running": OK, "stopped": IDLE, "no_camera": IDLE, "idle": IDLE}.get(state, DOWN)
            min_fps = float(runtime.min_fps or 0.0)
            fps = summary["processed_fps"]
            # Checked only once the line has run past the frame-rate window.
            if state == "running" and min_fps > 0 and self.uptime_seconds > 10 and fps < min_fps:
                status = DEGRADED
                alarm_manager.raise_alarm(
                    HealthAlarmCode.LINE_FPS_LOW, source,
                    f"Line '{runtime.name}' is processing {fps:.1f} frames/s, below its minimum of {min_fps:g}; counts may be missed",
                    AlarmSeverity.WARNING, {"line_id": runtime.id, "processed_fps": fps, "min_fps": min_fps},
                )
            else:
                alarm_manager.clear_alarm(HealthAlarmCode.LINE_FPS_LOW, source, "frame rate recovered or line not running")
            rows.append({
                "id": runtime.id,
                "name": runtime.name,
                "state": state,
                "status": status,
                "processed_fps": fps,
                "min_fps": min_fps,
                "cameras": summary["cameras"],
                "active_alarms": summary["active_alarms"],
            })
        for alarm in alarm_manager.active("line:"):
            if alarm.source not in live_sources:
                alarm_manager.clear_source(alarm.source, "line removed")
        # A stopped line is not a fault; only running lines count toward the rollup.
        return {"status": _rollup([r["status"] for r in rows if r["status"] != IDLE]), "lines": rows}

    # ── Database / MQTT ───────────────────────────────────────────────────────

    def check_database(self) -> Dict[str, Any]:
        return {"status": OK if app_state.db_ready else DOWN}

    def check_mqtt(self) -> Dict[str, Any]:
        # MQTT is optional; a missing broker connection is reported, not treated as a fault.
        from app.services.mqtt_service import MQTTChannels
        from app.services.sparkplug_service import SparkplugService
        channels = {channel_id: {"id": channel_id, **state} for channel_id, state in MQTTChannels.status().items()}
        # A channel with Sparkplug B on is also an edge node, on a connection of its own.
        for channel_id, node in SparkplugService.status().items():
            channels.setdefault(channel_id, {"id": channel_id, "connected": False})["sparkplug"] = node
        return {
            "status": OK if app_state.mqtt_connected else IDLE,
            "connected": bool(app_state.mqtt_connected),
            # One broker connection per MQTT channel under Connections.
            "channels": list(channels.values()),
        }

    # ── Aggregate ─────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Run every check (updating alarms) and return the full health report."""
        cameras = self.check_cameras()
        flowing = [c["id"] for c in cameras["cameras"] if c["status"] == OK]
        components = {
            "database": self.check_database(),
            "cameras": cameras,
            "inference": self.check_inference(bool(flowing), flowing),
            "plc": self.check_plc(),
            "mqtt": self.check_mqtt(),
            "lines": self.check_lines(),
        }
        alarm_counts = alarm_manager.counts()

        if components["database"]["status"] == DOWN:
            overall = DOWN
        elif any(c["status"] in (DOWN, DEGRADED) for c in components.values()) or alarm_counts["critical"]:
            overall = DEGRADED
        else:
            overall = OK

        return {
            "status": overall,
            "version": settings.APP_VERSION,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "components": components,
            "alarms": alarm_counts,
        }

    def summary(self) -> Dict[str, Any]:
        """Public health view: statuses and counts only, no hosts or error text."""
        full = self.snapshot()
        return {
            "status": full["status"],
            "version": full["version"],
            "uptime_seconds": full["uptime_seconds"],
            "components": {name: comp["status"] for name, comp in full["components"].items()},
            "alarms": full["alarms"],
        }

    def metrics(self) -> Dict[str, Any]:
        """Counters and gauges for dashboards and scrapers."""
        from app.hardware.plc.factory import _driver_pool

        alarm_counts = alarm_manager.counts()
        metrics: Dict[str, Any] = {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "processed_frames_total": app_state.processed_frames,
            "detections_total": app_state.detection_count,
            "cameras_connected": sum(1 for d in list(app_state.cameras.values()) if getattr(d, "is_connected", False)),
            "cameras_count": len(app_state.cameras),
            "plc_endpoints_connected": sum(1 for d in list(_driver_pool.values()) if d.is_connected),
            "plc_endpoints_count": len(_driver_pool),
            "alarms_active_info": alarm_counts["info"],
            "alarms_active_warning": alarm_counts["warning"],
            "alarms_active_critical": alarm_counts["critical"],
            "alarms_unacknowledged": alarm_counts["unacknowledged"],
            "alarms_raised_total": alarm_manager.total_raised,
        }
        from app.services.camera_service import CameraReconnector
        from app.utils.threads import restart_counts
        # Background threads restarted after a crash (the traceback is in the log).
        metrics["worker_restarts_total"] = sum(restart_counts().values())
        metrics["cameras_reconnecting"] = len(CameraReconnector.pending())
        try:
            from app.engines.flow_engine import FlowEngine
            from app.services.plc_dispatcher_service import PLCDispatcherService
            metrics["flow_runs_active"] = len(FlowEngine.get()._tasks)
            metrics["plc_dispatches_in_flight"] = len(PLCDispatcherService._dispatch_tasks)
        except Exception as exc:
            logger.warning("Could not read engine metrics: %s", exc)
        try:
            from app.services.line_service import line_manager
            from app.services.plc_dispatcher_service import PLCDispatcherService
            per_line = []
            for runtime in line_manager.all():
                summary = line_manager.summary(runtime)
                per_line.append({
                    "line": runtime.id,
                    "name": runtime.name,
                    "state": summary["state"],
                    "inspected_total": summary["total_inspected"],
                    "good_total": summary["good_count"],
                    "rejected_total": summary["rejected_count"],
                    "products_per_minute": summary["products_per_minute"],
                    "yield_percentage": summary["yield_percentage"],
                    "processed_fps": summary["processed_fps"],
                    "qr_reads_total": summary["qr"]["codes_read"],
                    "qr_unknown_total": summary["qr"]["unknown"],
                    "qr_no_reads_total": summary["qr"]["no_reads"],
                    "alarms_active": summary["active_alarms"],
                    "plc_dispatches_in_flight": len(PLCDispatcherService._line_tasks.get(runtime.id, ())),
                })
            metrics["lines"] = per_line
        except Exception as exc:
            logger.warning("Could not read per-line metrics: %s", exc)
        try:
            import psutil
            proc = psutil.Process()
            metrics["process_cpu_percent"] = proc.cpu_percent(interval=None)
            metrics["process_memory_rss_bytes"] = proc.memory_info().rss
            metrics["process_threads"] = proc.num_threads()
        except Exception as exc:
            logger.debug("psutil metrics unavailable: %s", exc)
        return metrics


def _camera_line(camera_id: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        from app.services.line_service import line_manager
        routed = line_manager.route(camera_id)
    except Exception:
        return None, None
    return (routed[0].id, routed[0].name) if routed else (None, None)


def _latest_frame_id(driver: Any) -> int:
    getter = getattr(driver, "get_latest_frame_id", None)
    try:
        return int(getter() if callable(getter) else getattr(driver, "_latest_frame_id", 0))
    except Exception as exc:
        logger.warning("Could not read frame id from camera %s: %s", getattr(driver, "camera_id", "?"), exc)
        return 0


def _label_value(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def to_prometheus(metrics: Dict[str, Any], prefix: str = "vision_server") -> str:
    """Render numeric metrics in the Prometheus text exposition format.

    Per-line metrics (the ``lines`` list) become ``{prefix}_line_*`` series
    with a ``line`` label.
    """
    lines = []
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metric = f"{prefix}_{name}"
        kind = "counter" if name.endswith("_total") else "gauge"
        lines.append(f"# TYPE {metric} {kind}")
        lines.append(f"{metric} {value}")
    per_line = metrics.get("lines") or []
    names = []
    for row in per_line:
        for key, value in row.items():
            if key not in names and not isinstance(value, bool) and isinstance(value, (int, float)):
                names.append(key)
    for key in names:
        metric = f"{prefix}_line_{key}"
        kind = "counter" if key.endswith("_total") else "gauge"
        lines.append(f"# TYPE {metric} {kind}")
        for row in per_line:
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            lines.append(f'{metric}{{line="{_label_value(row["line"])}",line_name="{_label_value(row.get("name", ""))}"}} {value}')
    return "\n".join(lines) + "\n"


health_service = HealthService()


class HealthMonitor:
    """Runs the health checks on an interval so alarms fire without polling."""

    _task: Optional[asyncio.Task] = None

    @classmethod
    def start(cls) -> None:
        if cls._task is None or cls._task.done():
            cls._task = asyncio.create_task(cls._loop(), name="health_monitor")

    @classmethod
    async def stop(cls) -> None:
        task, cls._task = cls._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @classmethod
    async def _loop(cls) -> None:
        interval = max(0.5, settings.HEALTH_CHECK_INTERVAL_SECONDS)
        while True:
            try:
                health_service.snapshot()
            except Exception:
                logger.exception("Health monitor check failed")
            await asyncio.sleep(interval)
