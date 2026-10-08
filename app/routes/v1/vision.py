from __future__ import annotations

import asyncio
from typing import List, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.detection import DetectionLogResponse
from app.schemas.vision import DetectionResponse, LiveInspectionResponse
from app.services.vision_service import ApiInferenceBusy, VisionService
from app.config import settings
from app.utils.upload_limits import read_upload_limited
from app.state.application_state import app_state

router = APIRouter(prefix="/vision", tags=["Vision Inference"])


def _inference_busy(exc: ApiInferenceBusy) -> HTTPException:
    return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "1"})


@router.post("/detect", response_model=DetectionResponse, summary="Run detection on uploaded image file")
async def detect_image(
    file: UploadFile = File(..., description="Image file (JPEG/PNG/BMP)"),
    confidence_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    nms_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    model_id: Optional[str] = Query(default=None, max_length=64, description="The model to run; without it, the model of Line 1's counting camera"),
    db: AsyncSession = Depends(get_db),
) -> DetectionResponse:
    try:
        if file.content_type and not file.content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Only image uploads are accepted")
        content = await read_upload_limited(file, settings.MAX_IMAGE_UPLOAD_BYTES)
        return await VisionService.detect_image(db, content, confidence_threshold, nms_threshold, model_id=model_id)
    except HTTPException:
        raise
    except ApiInferenceBusy as exc:
        raise _inference_busy(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/detect/camera/{camera_id}", response_model=LiveInspectionResponse, summary="Run real-time inspection on camera frame")
async def detect_live_camera(
    camera_id: str,
    confidence_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    nms_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    model_id: Optional[str] = Query(default=None, max_length=64, description="The model to run; without it, the camera's own model, else the model of Line 1's counting camera"),
    db: AsyncSession = Depends(get_db),
) -> LiveInspectionResponse:
    try:
        return await VisionService.detect_live_camera(db, camera_id, confidence_threshold, nms_threshold, model_id=model_id)
    except ApiInferenceBusy as exc:
        raise _inference_busy(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/annotated/camera/{camera_id}", summary="Get camera frame with bounding boxes overlay")
async def get_annotated_frame(
    camera_id: str,
    confidence_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    max_width: Optional[int] = Query(default=None, ge=160, le=3840, description="Send the picture no wider than this many pixels"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        jpeg_bytes = await VisionService.get_annotated_frame(db, camera_id, confidence_threshold, max_width)
        return Response(content=jpeg_bytes, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except ApiInferenceBusy as exc:
        raise _inference_busy(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _annotated_mjpeg_generator(
    camera_id: str,
    conf_thresh: Optional[float] = None,
    max_width: int = 640,
    jpeg_quality: int = 70,
):
    """Lock-free 30 FPS AI-annotated MJPEG stream via publisher layer (zero OpenCV on event loop)."""
    from app.services.vision_service import CameraStreamPipeline

    async for chunk in CameraStreamPipeline.stream_annotated_mjpeg(camera_id):
        yield chunk


@router.get("/stream/camera/{camera_id}", summary="Live Continuous AI-Annotated Video Feed")
async def live_annotated_stream(
    camera_id: str,
    confidence_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    max_width: int = Query(default=640, ge=160, le=3840),
    quality: int = Query(default=65, ge=20, le=95),
) -> StreamingResponse:
    driver = app_state.cameras.get(camera_id)
    if not driver or not driver.is_connected:
        raise HTTPException(status_code=404, detail="Camera stream is offline or disconnected")
    return StreamingResponse(
        _annotated_mjpeg_generator(camera_id, confidence_threshold, max_width=max_width, jpeg_quality=quality),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/detections", response_model=List[DetectionLogResponse], summary="Query detection history logs")
async def list_detections(
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> List[DetectionLogResponse]:
    return await VisionService.get_detection_history(db, limit)
