"""
Trigger inspection
==================
A vision camera in trigger mode ("One picture per trigger") does not track
moving products across count lines. Each trigger makes one product from one
picture: for indexing machines, rotary tables and parts that stand still.

A trigger comes from a PLC signal card with the action "capture", from
``POST /api/v1/lines/{id}/trigger`` or from ``/api/v1/control/trigger/{camera}``.
It waits for the next picture the camera's model gets through, decides what
that picture says, and hands the product to the line as a crossing: it is
counted, recorded and sent like a product a tracking camera saw, and goes
through Sync and the joined cameras the same way.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.services.line_config import REASON_NOTHING_DETECTED, REASON_VISION
from app.utils.logger import get_logger

logger = get_logger(__name__)

# How long a trigger waits for the camera's next picture.
TRIGGER_TIMEOUT_S = 2.0
# The class a product is counted under when nothing was found and that rejects it.
NOTHING_CLASS = "nothing detected"

_POLL_S = 0.01


def inspection_verdict(detections: List[Any], config: Any) -> Optional[Dict[str, Any]]:
    """What one picture says of the product in it, by the camera's own counting settings.

    A defect class rejects the product. Else a product class makes it good
    (any detection, when the camera has no list of products). Else nothing was
    found, and the camera's "When nothing is found" decides: no product (None),
    a reject with the reason ``nothing_detected``, or a good product.
    Returns ``{"class_name", "is_defect", "confidence", "bbox", "reject_reason"}``.
    """
    from app.engines.tracker import is_defect_class

    def found(detection: Any, is_defect: bool) -> Dict[str, Any]:
        bbox = getattr(detection, "bbox", None)
        return {
            "class_name": detection.class_name,
            "is_defect": is_defect,
            "confidence": float(detection.confidence),
            "bbox": bbox.model_dump() if hasattr(bbox, "model_dump") else bbox,
            "reject_reason": None,
        }

    defects = [d for d in detections if is_defect_class(d.class_name, config.defect_classes, config.name_based_defects)]
    if defects:
        return found(max(defects, key=lambda d: d.confidence), True)
    expected = {str(name).strip().lower() for name in config.expected_classes or []}
    products = [d for d in detections if not expected or str(d.class_name).strip().lower() in expected]
    if products:
        return found(max(products, key=lambda d: d.confidence), False)
    if config.no_product == "reject":
        return {"class_name": NOTHING_CLASS, "is_defect": True, "confidence": 1.0, "bbox": None,
                "reject_reason": REASON_NOTHING_DETECTED}
    if config.no_product == "good":
        return {"class_name": "product", "is_defect": False, "confidence": 1.0, "bbox": None, "reject_reason": None}
    return None


def trigger_cameras(runtime: Any) -> List[str]:
    """The line's vision cameras in trigger mode, in card order."""
    return [
        cam["camera_id"] for cam in runtime.cameras
        if cam.get("role", "vision") == "vision" and cam.get("count_mode") == "trigger"
    ]


async def trigger_camera(runtime: Any, camera_id: str, timeout: Optional[float] = None,
                         with_detections: bool = False) -> Dict[str, Any]:
    """Make one product from the next picture of a camera in trigger mode.

    Returns what the camera's own picture said: ``status`` is ``"inspected"``
    (a product was made), ``"nothing"`` (nothing was found and that is
    ignored) or ``"no_frame"`` (no new picture came in time; nothing is
    counted). On a line with Sync or joined cameras the product's final result
    may still change after this returns. ``with_detections`` adds the picture's
    detections as ``detection_items``.

    Raises ValueError when the line is stopped, the camera is not connected,
    has no loaded model, or is not a vision camera in trigger mode.
    """
    import app.services.line_service as line_service
    from app.services.vision_service import CameraStreamPipeline
    from app.state.application_state import app_state

    if camera_id not in trigger_cameras(runtime):
        raise ValueError(f"Camera '{camera_id}' is not a vision camera of this line with Count mode 'One picture per trigger'")
    if not runtime.enabled:
        raise ValueError(f"Line '{runtime.name}' is stopped; start it to inspect")
    if not getattr(app_state.cameras.get(camera_id), "is_connected", False):
        raise ValueError(f"Camera '{camera_id}' is not connected")
    engine, _, _ = line_service.line_manager.engine_for_camera(camera_id)
    if not getattr(engine, "is_loaded", True):
        raise ValueError(f"Camera '{camera_id}' has no vision model loaded, so its picture cannot be inspected")
    timeout = TRIGGER_TIMEOUT_S if timeout is None else timeout

    # One trigger at a time per camera: two triggers take two pictures.
    async with runtime._trigger_locks.setdefault(camera_id, asyncio.Lock()):
        worker = CameraStreamPipeline.get_worker(camera_id)
        started = time.monotonic()
        # A picture the model is working on now was taken before the trigger, when the
        # part may not have been in place yet: the one after it is the one that counts.
        wanted = worker.result_seq + (2 if worker.is_busy else 1)
        answer: Dict[str, Any] = {
            "camera_id": camera_id, "status": "no_frame", "result": None, "class_name": None,
            "reject_reason": None, "track_id": None, "detections": 0, "inference_time_ms": 0.0, "waited_ms": 0.0,
        }
        while worker.result_seq < wanted:
            if time.monotonic() - started >= timeout:
                answer["waited_ms"] = round((time.monotonic() - started) * 1000, 2)
                logger.warning("Trigger on camera %s of line '%s': no new picture within %.1f s; nothing was counted",
                               camera_id, runtime.name, timeout)
                if with_detections:
                    answer["detection_items"] = []
                return answer
            await asyncio.sleep(_POLL_S)
        detections, inference_ms = worker.get_latest_inference()
        answer.update({
            "status": "nothing",
            "detections": len(detections),
            "inference_time_ms": round(float(inference_ms), 2),
            "waited_ms": round((time.monotonic() - started) * 1000, 2),
        })
        if with_detections:
            answer["detection_items"] = [d.model_dump() if hasattr(d, "model_dump") else d for d in detections]

        counter = runtime.counter_for(camera_id)
        verdict = inspection_verdict(detections, counter.config)
        if verdict is None:
            return answer
        runtime._trigger_seq += 1
        crossing = counter.make_crossing(
            verdict["class_name"], verdict["is_defect"], track_id=runtime._trigger_seq,
            confidence=verdict["confidence"], bbox=verdict["bbox"], camera_id=camera_id,
        )
        if verdict["reject_reason"]:
            crossing["reject_reason"] = verdict["reject_reason"]
        counter.emit_crossing(crossing)
        answer.update({
            "status": "inspected",
            "result": "reject" if verdict["is_defect"] else "good",
            "class_name": verdict["class_name"],
            "reject_reason": (verdict["reject_reason"] or REASON_VISION) if verdict["is_defect"] else None,
            "track_id": runtime._trigger_seq,
        })
        return answer
