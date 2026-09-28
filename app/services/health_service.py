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

            if not entry["connected"]:
                entry["status"] = DOWN
                entry["error"] = getattr(driver, "last_error", None) or "camera is not connected"
                self._camera_progress.pop(camera_id, None)
                alarm_manager.raise_alarm(
                    HealthAlarmCode.CAMERA_DISCONNECTED, source,
                    f"Camera '{name}' is not connected: {entry['error']}", AlarmSeverity.CRITICAL,
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
                    f"Camera '{name}' has not delivered a new frame for {age:.1f}s", AlarmSeverity.CRITICAL,
                    {"frame_id": frame_id},
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

    def check_inference(self, frames_flowing: bool) -> Dict[str, Any]:
        now = self._clock()
        source = "inference"
        result: Dict[str, Any] = {
            "processed_frames": app_state.processed_frames,
            "detection_count": app_state.detection_count,
        }

        try:
            from app.services.vision_service import ContinuousVisionRunner, _get_active_engine
            engine, model_name, model_id = _get_active_engine()
            runner_alive = bool(ContinuousVisionRunner._running and ContinuousVisionRunner._thread
                                and ContinuousVisionRunner._thread.is_alive())
        except Exception as exc:
            logger.exception("Inference health check failed")
            result.update(status=DOWN, error=f"{type(exc).__name__}: {exc}")
            return result

        result.update(model=model_name, model_id=model_id, model_loaded=bool(engine.is_loaded),
                      runner_alive=runner_alive)

        processed = app_state.processed_frames
        last_count, changed_at = self._inference_progress
        if processed != last_count or not frames_flowing:
            changed_at = now
        self._inference_progress = (processed, changed_at)
        stalled_for = now - changed_at
        result["seconds_since_inference"] = round(stalled_for, 2)

        problems: List[Tuple[str, str]] = []
        if not engine.is_loaded:
            problems.append((HealthAlarmCode.INFERENCE_MODEL_NOT_LOADED, "No inference model is loaded"))
        if not runner_alive:
            problems.append((HealthAlarmCode.INFERENCE_RUNNER_STOPPED, "The continuous vision runner is not running"))
        if engine.is_loaded and runner_alive and stalled_for > settings.HEALTH_INFERENCE_STALE_SECONDS:
            problems.append((HealthAlarmCode.INFERENCE_STALLED,
                             f"Frames are arriving but nothing has been inferred for {stalled_for:.1f}s"))

        # Without a streaming camera, inference not running is expected, not a fault.
        raised = {code for code, _ in problems} if frames_flowing else set()
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
        elif not frames_flowing:
            result["status"] = IDLE
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

    # ── Database / MQTT ───────────────────────────────────────────────────────

    def check_database(self) -> Dict[str, Any]:
        return {"status": OK if app_state.db_ready else DOWN}

    def check_mqtt(self) -> Dict[str, Any]:
        # MQTT is optional; a missing broker connection is reported, not treated as a fault.
        return {"status": OK if app_state.mqtt_connected else IDLE, "connected": bool(app_state.mqtt_connected)}

    # ── Aggregate ─────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Run every check (updating alarms) and return the full health report."""
        cameras = self.check_cameras()
        frames_flowing = any(c["status"] == OK for c in cameras["cameras"])
        components = {
            "database": self.check_database(),
            "cameras": cameras,
            "inference": self.check_inference(frames_flowing),
            "plc": self.check_plc(),
            "mqtt": self.check_mqtt(),
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
        try:
            from app.engines.flow_engine import FlowEngine
            from app.services.plc_dispatcher_service import PLCDispatcherService
            metrics["flow_runs_active"] = len(FlowEngine.get()._tasks)
            metrics["plc_dispatches_in_flight"] = len(PLCDispatcherService._dispatch_tasks)
        except Exception as exc:
            logger.warning("Could not read engine metrics: %s", exc)
        try:
            import psutil
            proc = psutil.Process()
            metrics["process_cpu_percent"] = proc.cpu_percent(interval=None)
            metrics["process_memory_rss_bytes"] = proc.memory_info().rss
            metrics["process_threads"] = proc.num_threads()
        except Exception as exc:
            logger.debug("psutil metrics unavailable: %s", exc)
        return metrics


def _latest_frame_id(driver: Any) -> int:
    getter = getattr(driver, "get_latest_frame_id", None)
    try:
        return int(getter() if callable(getter) else getattr(driver, "_latest_frame_id", 0))
    except Exception as exc:
        logger.warning("Could not read frame id from camera %s: %s", getattr(driver, "camera_id", "?"), exc)
        return 0


def to_prometheus(metrics: Dict[str, Any], prefix: str = "vision_server") -> str:
    """Render flat numeric metrics in the Prometheus text exposition format."""
    lines = []
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metric = f"{prefix}_{name}"
        kind = "counter" if name.endswith("_total") else "gauge"
        lines.append(f"# TYPE {metric} {kind}")
        lines.append(f"{metric} {value}")
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
