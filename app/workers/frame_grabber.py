from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from app.hardware.camera.base import BaseCamera
from app.utils.logger import get_logger
from app.workers.ring_buffer import RingBuffer

logger = get_logger(__name__)


class FrameGrabberWorker:
    """
    Dedicated background worker pulling camera frames continuously at native sensor FPS.
    """

    def __init__(self, camera: BaseCamera, buffer_size: int = 15):
        self.camera = camera
        self.ring_buffer = RingBuffer(capacity=buffer_size)
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self.fps = 0.0
        self._frame_count = 0

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("FrameGrabber worker started for camera '%s'", self.camera.name)

    def stop(self) -> None:
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        logger.info("FrameGrabber worker stopped for camera '%s'", self.camera.name)

    def _run(self) -> None:
        last_calc = time.perf_counter()
        while self.is_running:
            if not self.camera.is_connected:
                time.sleep(0.05)
                continue

            success, frame_bytes = self.camera.grab_frame()
            if success and frame_bytes:
                self.ring_buffer.push(frame_bytes)
                self._frame_count += 1

            now = time.perf_counter()
            if now - last_calc >= 1.0:
                self.fps = round(self._frame_count / (now - last_calc), 1)
                self._frame_count = 0
                last_calc = now

            time.sleep(0.005)

    def get_latest_frame(self) -> Optional[bytes]:
        return self.ring_buffer.get_latest()
