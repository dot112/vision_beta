from __future__ import annotations

import asyncio
import base64
import json
from sqlalchemy import select
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db.session import AsyncSessionLocal
from app.core.security import decode_access_token
from app.db.models.user import User
from app.services.auth_service import AuthService
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/ws", tags=["Live WebSocket Streams"])
_active_streams: set[int] = set()
_stream_lock = asyncio.Lock()
_MAX_ACTIVE_STREAMS = 16
_WEBSOCKET_PROTOCOL = "industrial-vision-v1"


@router.websocket("/live/{camera_id}")
async def websocket_live_stream(websocket: WebSocket, camera_id: str):
    """
    Live real-time stream delivering annotated camera frames with bounding boxes.
    Sends JSON payload with Base64 JPEG and inference metadata at up to 30 FPS.
    """
    offered_protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    token = ""
    negotiated_protocol = None
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    else:
        # Browser clients can pass a bearer token in Sec-WebSocket-Protocol;
        # select only the fixed protocol so the token is never echoed back.
        token = next((item[7:] for item in offered_protocols if item.startswith("bearer.")), "")
        if token and _WEBSOCKET_PROTOCOL in offered_protocols:
            negotiated_protocol = _WEBSOCKET_PROTOCOL
        elif not settings.DEBUG:
            token = ""  # Query-string tokens leak into access logs and are disabled in production.
        else:
            token = websocket.query_params.get("token", "")
    payload = decode_access_token(token) if token else None
    if not payload or not payload.get("sub"):
        await websocket.close(code=4401, reason="Authentication required")
        return

    async with AsyncSessionLocal() as db:
        user = await db.scalar(select(User).where(User.username == payload["sub"]))
        if not user or not user.is_active:
            await websocket.close(code=4401, reason="Invalid user session")
            return
        session_id = payload.get("sid")
        if session_id and not AuthService.is_session_valid(user.username, session_id):
            await websocket.close(code=4401, reason="Session expired")
            return

    from app.state.application_state import app_state
    driver = app_state.cameras.get(camera_id)
    if not driver or not getattr(driver, "is_connected", False):
        await websocket.close(code=4404, reason="Camera stream is offline")
        return

    stream_id = id(websocket)
    async with _stream_lock:
        if len(_active_streams) >= _MAX_ACTIVE_STREAMS:
            await websocket.close(code=4429, reason="Stream capacity reached")
            return
        _active_streams.add(stream_id)

    if negotiated_protocol:
        await websocket.accept(subprotocol=negotiated_protocol)
    else:
        await websocket.accept()
    logger.info("Authenticated WebSocket connected for live camera stream: %s", camera_id)

    try:
        from app.services.vision_service import CameraStreamPipeline
        publisher = CameraStreamPipeline.get_publisher(camera_id)
        publisher.add_subscriber()
        last_sent_fid: int = -1

        try:
            while not app_state.shutting_down:
                driver = app_state.cameras.get(camera_id)
                if not driver or not getattr(driver, "is_connected", False):
                    break

                # Read pre-encoded JPEG from publisher — zero OpenCV on event loop
                fid = publisher.get_latest_frame_id()
                jpeg = publisher.get_latest_annotated()

                if jpeg and fid != last_sent_fid:
                    last_sent_fid = fid
                    b64_frame = base64.b64encode(jpeg).decode("utf-8")
                    await websocket.send_text(json.dumps({
                        "camera_id": camera_id,
                        "frame_b64": b64_frame,
                    }))

                await asyncio.sleep(0.030)  # ~33 FPS poll cadence

        finally:
            publisher.remove_subscriber()

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for camera: %s", camera_id)
    except Exception as exc:
        logger.warning("WebSocket stream exception: %s", exc)
    finally:
        async with _stream_lock:
            _active_streams.discard(id(websocket))
