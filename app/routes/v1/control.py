from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.vision_service import VisionService

router = APIRouter(prefix="/control", tags=["Production Control"])


@router.post("/trigger/{camera_id}", summary="Trigger one inspection on a camera")
async def trigger_inspection(camera_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Triggered by photo-eye sensor or PLC to perform immediate inline part inspection.

    A vision camera with Count mode "One picture per trigger" makes one product
    from its next picture: it is counted, recorded and sent, and ``product``
    says what the picture said. Any other camera is inspected once and nothing
    is counted, as before.
    """
    from app.services.line_service import line_manager
    from app.services.trigger_inspection import trigger_camera, trigger_cameras

    routed = line_manager.route(camera_id)
    if routed is not None and camera_id in trigger_cameras(routed[0]):
        try:
            product = await trigger_camera(routed[0], camera_id, with_detections=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        detections = product.pop("detection_items", [])
        return {
            "status": "triggered",
            "camera_id": camera_id,
            "inspection_result": _judged_picture(camera_id, detections, product),
            "product": product,
        }
    res = await VisionService.detect_live_camera(db, camera_id, queued=False)
    return {
        "status": "triggered",
        "camera_id": camera_id,
        "inspection_result": res.model_dump() if hasattr(res, "model_dump") else res.dict(),
    }


def _judged_picture(camera_id: str, detections: list, product: Dict[str, Any]) -> Dict[str, Any]:
    """The picture a trigger judged, in the shape this endpoint always answered with."""
    from app.services.line_service import line_manager
    from app.state.application_state import app_state

    _, model_name, _ = line_manager.engine_for_camera(camera_id)
    waited = float(product.get("waited_ms") or 0.0)
    inference = float(product.get("inference_time_ms") or 0.0)
    return {
        "camera_id": camera_id,
        "camera_name": getattr(app_state.cameras.get(camera_id), "name", None) or "Camera",
        "model_name": model_name,
        "total_detections": len(detections),
        "detections": detections,
        "inference_time_ms": inference,
        # How long the trigger waited for the camera's next picture.
        "acquisition_time_ms": waited,
        "total_latency_ms": round(waited + inference, 2),
        # No picture is not a pass.
        "passed": product.get("status") != "no_picture" and product.get("result") != "reject",
    }
