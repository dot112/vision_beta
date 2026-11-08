"""Line setup's held pictures: one still picture per camera to set up its text fields or its pattern check on
(the text reader's and the pattern check's setup routes share it). A held picture is dropped after
HELD_TTL_S unused.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional, Tuple

import numpy as np
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

# A held picture is dropped after this long unused.
HELD_TTL_S = 600.0
# A camera switched off is not connected for a picture (CameraService.grab_raw_frame): its card says how.
NOT_CONNECTED = "The camera is not connected: switch on Connect under Camera"
NOT_HELD = "No picture is held for this camera"
_held: Dict[str, Tuple[np.ndarray, float]] = {}
_held_lock = threading.Lock()
_now: Callable[[], float] = time.monotonic


async def frame(db: AsyncSession, camera_id: str) -> np.ndarray:
    """The camera's newest picture (a copy: it is kept or checked while the camera goes on). A camera that is
    not connected is connected first, as for the count lines' picture: a camera is set up on a stopped line."""
    from app.services.camera_service import CameraService
    from app.state.application_state import app_state

    driver = app_state.cameras.get(camera_id)
    if driver is not None and getattr(driver, "is_connected", False) and hasattr(driver, "get_latest_raw_mat"):
        ok, mat, _ = driver.get_latest_raw_mat(copy=True)
        error = "The camera has no picture yet"
    else:
        ok, mat, _, error = await CameraService.grab_raw_frame(db, camera_id)
        if error == "Camera is offline or not found":
            error = NOT_CONNECTED
    if not ok or mat is None:
        raise HTTPException(status_code=404, detail=error or "The camera has no picture yet")
    return np.array(mat, copy=True)


def hold(camera_id: str, mat: np.ndarray) -> None:
    with _held_lock:
        _held[camera_id] = (mat, _now())


def held(camera_id: str) -> Optional[np.ndarray]:
    """The camera's held picture, still in use (a use keeps it another HELD_TTL_S), or None."""
    now = _now()
    with _held_lock:
        for gone in [cid for cid, (_, used) in _held.items() if now - used > HELD_TTL_S]:
            del _held[gone]
        kept = _held.get(camera_id)
        if kept is None:
            return None
        _held[camera_id] = (kept[0], now)
        return kept[0]


def jpeg(mat: np.ndarray) -> Response:
    import cv2

    ok, buf = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise HTTPException(status_code=500, detail="The picture could not be encoded")
    return Response(content=buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
