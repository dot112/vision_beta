from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.qr_engine import QREngine
from app.schemas.qr import BarcodeDecodeResponse, LiveCameraBarcodeResponse
from app.services.camera_service import CameraService
from app.utils.logger import get_logger

logger = get_logger(__name__)

_qr_engine = QREngine()
_qr_engine_lock = threading.Lock()


def _decode_qr(image_input: Any) -> BarcodeDecodeResponse:
    # OpenCV detector instances are shared and are not guaranteed to be thread-safe.
    with _qr_engine_lock:
        return _qr_engine.decode(image_input)


class QRService:
    @staticmethod
    async def decode_image(image_bytes: bytes) -> BarcodeDecodeResponse:
        return await asyncio.to_thread(_decode_qr, image_bytes)

    @staticmethod
    async def decode_live_camera(db: AsyncSession, camera_id: str) -> LiveCameraBarcodeResponse:
        from app.state.application_state import app_state
        cam_start = time.perf_counter()
        driver = app_state.cameras.get(camera_id)
        if not driver or not driver.is_connected:
            raise ValueError(f"Camera {camera_id} is not connected")

        success, mat, _ = driver.grab_raw_frame()
        if not success or mat is None:
            raise ValueError(f"Failed to acquire raw frame from camera {camera_id}")
        acq_ms = round((time.perf_counter() - cam_start) * 1000, 2)

        decode_response = await asyncio.to_thread(_decode_qr, mat)
        camera = await CameraService.get_camera_by_id(db, camera_id)
        camera_name = camera.name if camera else "Camera"

        total_latency = round(acq_ms + decode_response.decode_time_ms, 2)

        return LiveCameraBarcodeResponse(
            camera_id=camera_id,
            camera_name=camera_name,
            total_found=decode_response.total_found,
            codes=decode_response.codes,
            decode_time_ms=decode_response.decode_time_ms,
            acquisition_time_ms=acq_ms,
            total_latency_ms=total_latency,
        )

    @staticmethod
    async def get_annotated_frame(db: AsyncSession, camera_id: str) -> bytes:
        from app.state.application_state import app_state
        driver = app_state.cameras.get(camera_id)
        if not driver or not driver.is_connected:
            raise ValueError(f"Camera {camera_id} is not connected")

        success, mat, _ = driver.grab_raw_frame()
        if not success or mat is None:
            raise ValueError("Frame grab failed")

        decode_response = await asyncio.to_thread(_decode_qr, mat)
        return await asyncio.to_thread(
            _qr_engine.draw_annotations,
            mat,
            decode_response.codes,
            decode_response.decode_time_ms,
        )
