"""Pattern checks (role "match"): one checker thread per camera and its latest result.

A pattern check set to Continuously is fed the newest frame whenever it is free (the frame runner); its
results are only drawn on the video. One on One picture per trigger gets its pictures from the trigger
paths (trigger_inspection, the hardware path), through the same ``submit_frame_if_idle(...,
on_result=...)`` as a vision camera's inference worker, and its result becomes a product there.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from app.engines.pattern_check import PatternResult, check_picture, error_result
from app.services.match_references import ReferenceMissing, load_set
from app.utils.logger import LogThrottle, get_logger
from app.utils.threads import run_supervised

logger = get_logger(__name__)
_error_log = LogThrottle(60.0)

NOT_A_CHECK = "the camera is not a pattern check on a line"


def _entry(camera_id: str) -> Tuple[Any, Dict[str, Any]]:
    """(the camera's line runtime or None, its entry on that line or {})."""
    from app.services.line_service import line_manager

    routed = line_manager.route(camera_id)
    entry = (routed[0].camera_entry(camera_id) if routed else None) or {}
    return (routed[0] if routed else None), entry


def check_for_camera(camera_id: str, mat: np.ndarray) -> PatternResult:
    """Check one picture of a camera with its saved pattern check settings and its reference set."""
    _, entry = _entry(camera_id)
    match = entry.get("match") if entry.get("role") == "match" else None
    height, width = mat.shape[:2]
    if not match:
        return error_result(NOT_A_CHECK, width, height)
    try:
        ref = load_set(match["reference_id"])
    except ReferenceMissing as missing:
        return error_result(str(missing), width, height)
    return check_picture(ref, match, mat)


class PatternCheckWorker:
    """One pattern check camera's thread: one picture in flight at a time."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._input_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._trigger = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._pending: Optional[Tuple[Any, int, Optional[float], Any, Any]] = None
        self._latest: Optional[PatternResult] = None
        self.last_inferred_frame: Optional[Any] = None
        self.checks = 0
        name = f"PatternCheck-{camera_id[:8]}"
        self._thread = threading.Thread(target=run_supervised, args=(name, self._loop, self._stop),
                                        kwargs={"kind": "pattern_check"}, name=name, daemon=True)
        self._thread.start()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def submit_frame_if_idle(self, mat: Any, frame_id: int, conf_thresh: Optional[float] = None, copy: bool = False,
                             arrived_at: Optional[float] = None,
                             on_result: Optional[Callable[[int, PatternResult, float, Optional[float]], None]] = None,
                             on_failure: Optional[Callable[[int, str], None]] = None) -> bool:
        """Take a picture unless one is being checked (the shape of the vision worker's, for the trigger paths).
        ``on_result(frame id, result, ms, arrived at)`` gets the result; ``on_failure(frame id, why)`` is
        called when checking raised."""
        if mat is None:
            return False
        with self._input_lock:
            if self._busy:
                return False
            self._busy = True
            self._pending = (mat.copy() if copy else mat, frame_id, arrived_at, on_result, on_failure)
        self._trigger.set()
        return True

    def latest(self) -> Optional[PatternResult]:
        with self._state_lock:
            return self._latest

    def _alarm(self, runtime: Any, result: PatternResult) -> None:
        """camera.model_unavailable while the check cannot run on a running line; cleared by a result without
        an error."""
        from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
        from app.state.application_state import app_state

        source = f"camera:{self.camera_id}"
        if result.error and result.error != NOT_A_CHECK and runtime is not None and runtime.enabled:
            name = getattr(app_state.cameras.get(self.camera_id), "name", None) or self.camera_id
            alarm_manager.raise_alarm(
                AlarmCode.CAMERA_MODEL_UNAVAILABLE, source,
                f"Camera '{name}' (pattern check) on line '{runtime.name}' cannot check: {result.error}",
                AlarmSeverity.WARNING, {"line_id": runtime.id, "camera_id": self.camera_id},
            )
        elif not result.error:
            alarm_manager.clear_alarm(AlarmCode.CAMERA_MODEL_UNAVAILABLE, source, "the pattern check checks again")

    def _check(self, mat: Any, frame_id: int, arrived_at: Optional[float], on_result: Any) -> None:
        from app.state.application_state import app_state

        result = check_for_camera(self.camera_id, mat)
        self.last_inferred_frame = mat
        self.checks += 1
        with self._state_lock:
            self._latest = result
        runtime, entry = _entry(self.camera_id)
        on_line = runtime is not None and entry.get("role") == "match"
        self._alarm(runtime if on_line else None, result)
        if on_result is not None:
            on_result(frame_id, result, result.ms, arrived_at)
            return
        if on_line and runtime.enabled:
            runtime.record_frame(self.camera_id)
            app_state.processed_frames += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._trigger.wait(timeout=0.1)
            self._trigger.clear()
            with self._input_lock:
                pending, self._pending = self._pending, None
            if pending is None:
                continue
            mat, frame_id, arrived_at, on_result, on_failure = pending
            try:
                self._check(mat, frame_id, arrived_at, on_result)
            except Exception as exc:  # noqa: BLE001 - one bad picture never stops the checker
                _error_log.log(logger, logging.WARNING, self.camera_id, "Pattern check failed on camera %s: %s: %s",
                               self.camera_id, type(exc).__name__, exc)
                if on_failure is not None:
                    on_failure(frame_id, f"{type(exc).__name__}: {exc}")
            finally:
                with self._input_lock:
                    self._busy = False

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


class PatternCheckPipeline:
    """The pattern checks, one per camera with the pattern check role."""

    _workers: Dict[str, PatternCheckWorker] = {}
    _lock = threading.Lock()

    @classmethod
    def get_worker(cls, camera_id: str) -> PatternCheckWorker:
        with cls._lock:
            worker = cls._workers.get(camera_id)
            if worker is None:
                worker = cls._workers[camera_id] = PatternCheckWorker(camera_id)
            return worker

    @classmethod
    def peek(cls, camera_id: str) -> Optional[PatternCheckWorker]:
        with cls._lock:
            return cls._workers.get(camera_id)

    @classmethod
    def remove_camera(cls, camera_id: str) -> None:
        with cls._lock:
            worker = cls._workers.pop(camera_id, None)
        if worker:
            worker.stop()

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            workers = list(cls._workers.values())
            cls._workers.clear()
        for worker in workers:
            worker.stop()
