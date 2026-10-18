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

A camera whose picture the system takes runs its model only on that picture,
never on the frames in between (the frame runner leaves it out). On a trigger
the server waits the card's Picture delay, then:

- an industrial camera on ``software`` trigger is sent its trigger command, and
  the picture that command made is the product's picture;
- any other camera (IP, USB, industrial on ``off``) gives the first frame that
  arrives after that moment.

``hardware``: a signal on an industrial camera's own input takes each picture,
and the frame runner makes a product of every one (``product_from_result``).
The server cannot trigger such a camera.

A trigger whose picture never comes while the line runs (no frame in time, the
camera not connected) still gets one answer: a product rejected with the reason
"no picture" (``no_picture_product``).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from app.services.line_config import REASON_NO_PICTURE, REASON_NOTHING_DETECTED, REASON_VISION, picture_source
from app.utils.logger import get_logger

logger = get_logger(__name__)

# How long a trigger waits for the camera's picture, after its Picture delay (and then for the model).
TRIGGER_TIMEOUT_S = 2.0
# The class a product is counted under when nothing was found and that rejects it.
NOTHING_CLASS = "nothing detected"
# The class of the product a trigger makes when its picture never came.
NO_PICTURE_CLASS = "no picture"

_POLL_S = 0.01
# The clock and the sleep of a trigger's waits (a test runs them on a clock of its own).
_now = time.monotonic
_sleep = asyncio.sleep


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


def product_from_result(runtime: Any, camera_id: str, detections: List[Any],
                        arrived_at: Optional[float] = None, trigger_no: Optional[int] = None,
                        sequence: Optional[Any] = None, request_id: Optional[int] = None) -> Dict[str, Any]:
    """Make the product one picture of a camera in trigger mode shows, from what its model found.

    ``arrived_at``: the clock time the picture arrived; it becomes the product's time.
    ``trigger_no``: the number of the input that asked for the picture; the product's message carries it.
    ``sequence`` / ``request_id``: the product sequence the PLC held when it asked, and the line's number
    for that request (its result handshake); the product keeps them.
    Returns ``status`` "inspected" (a product was made) or "nothing" (nothing was found
    and that is ignored), with ``result``, ``class_name``, ``reject_reason``, ``track_id``
    and ``detections`` (how many). Safe to call from a camera's inference thread.
    """
    answer: Dict[str, Any] = {"status": "nothing", "result": None, "class_name": None, "reject_reason": None,
                              "track_id": None, "detections": len(detections)}
    if trigger_no is not None:
        answer["trigger_no"] = trigger_no
    counter = runtime.counter_for(camera_id)
    verdict = inspection_verdict(detections, counter.config)
    if verdict is None:
        return answer
    track_id = _next_track_id(runtime)
    crossing = counter.make_crossing(
        verdict["class_name"], verdict["is_defect"], track_id=track_id,
        confidence=verdict["confidence"], bbox=verdict["bbox"], camera_id=camera_id, arrived_at=arrived_at,
    )
    if verdict["reject_reason"]:
        crossing["reject_reason"] = verdict["reject_reason"]
    _add_context(crossing, trigger_no, sequence, request_id)
    counter.emit_crossing(crossing)
    answer.update({
        "status": "inspected",
        "result": "reject" if verdict["is_defect"] else "good",
        "class_name": verdict["class_name"],
        "reject_reason": (verdict["reject_reason"] or REASON_VISION) if verdict["is_defect"] else None,
        "track_id": track_id,
    })
    return answer


def _next_track_id(runtime: Any) -> int:
    """The track number of the line's next triggered product."""
    with runtime._trigger_seq_lock:
        runtime._trigger_seq += 1
        return runtime._trigger_seq


def _add_context(crossing: Dict[str, Any], trigger_no: Optional[int], sequence: Any, request_id: Optional[int]) -> None:
    """What the request that asked for the picture carries goes with its product."""
    for key, value in (("trigger_no", trigger_no), ("sequence", sequence), ("request_id", request_id)):
        if value is not None:
            crossing[key] = value


def no_picture_product(runtime: Any, camera_id: str, why: str, trigger_no: Optional[int] = None,
                       sequence: Any = None, request_id: Optional[int] = None) -> Dict[str, Any]:
    """The product of a trigger whose picture never came while its line ran: a reject, reason "no picture".

    Whoever waits for the trigger's answer (a PLC's result handshake, a robot) gets one: the product is
    counted, recorded and sent, and fires the line's cards like any rejected product, with the trigger's
    ``trigger_no``, ``sequence`` and ``request_id``. ``why`` says what happened (no frame in time, the
    camera not connected, ...). The camera's ``missing`` counter goes up, the alarm camera.picture_missing
    is raised (the camera's next picture clears it), and its latest capture is one without a picture.
    Returns the answer: ``status`` "no_picture", ``result`` "reject", ``class_name``, ``reject_reason``,
    ``track_id``, ``detections`` (0) and ``trigger_no`` when given. Safe to call from any thread.
    """
    from app.events.alarm_events import AlarmCode, AlarmSeverity, alarm_manager
    from app.state.application_state import app_state

    counter = runtime.counter_for(camera_id)
    track_id = _next_track_id(runtime)
    crossing = counter.make_crossing(NO_PICTURE_CLASS, True, track_id=track_id, camera_id=camera_id)
    crossing["reject_reason"] = REASON_NO_PICTURE
    _add_context(crossing, trigger_no, sequence, request_id)
    counter.emit_crossing(crossing)
    runtime.count_trigger(camera_id, "missing")
    name = getattr(app_state.cameras.get(camera_id), "name", None) or camera_id
    alarm_manager.raise_alarm(
        AlarmCode.CAMERA_PICTURE_MISSING, f"camera:{camera_id}",
        f"Camera '{name}' on line '{runtime.name}' got no picture for a trigger ({why}); "
        "the product was rejected as 'no picture'",
        AlarmSeverity.WARNING, {"line_id": runtime.id, "camera_id": camera_id, "why": why},
    )
    answer: Dict[str, Any] = {"status": "no_picture", "result": "reject", "class_name": NO_PICTURE_CLASS,
                              "reject_reason": REASON_NO_PICTURE, "track_id": track_id, "detections": 0}
    if trigger_no is not None:
        answer["trigger_no"] = trigger_no
    runtime.remember_trigger_capture(camera_id, answer, None)
    return answer


def render_trigger_capture(camera_id: str, frame: Any, detections: List[Any]) -> Optional[bytes]:
    """The picture a trigger camera's product was made from, with the model's boxes and class names drawn.

    ``frame`` is the frame the model inferred (the one handed to it, or the inference worker's
    ``last_inferred_frame`` inside its result callback) and ``detections`` what it found in it.
    It is drawn the way the annotated video draws a camera's picture, by the engine of the
    camera, on a copy: the frame belongs to the camera driver.
    Returns the JPEG, or None on any failure (never raises). It draws and encodes a whole picture:
    run it off the event loop.
    """
    try:
        import cv2

        import app.services.line_service as line_service

        if frame is None:
            return None
        engine = line_service.line_manager.engine_for_camera(camera_id)[0]
        annotated = engine.draw_annotations_mat(frame.copy(), detections, 0.0, draw_wirelines=False, camera_id=camera_id)
        ok, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return buf.tobytes() if ok else None
    except Exception as exc:
        logger.warning("The picture of camera %s could not be drawn: %s: %s", camera_id, type(exc).__name__, exc)
        return None


class _NoPicture(Exception):
    """A trigger's picture did not come; the message says why."""


def _still_running(runtime: Any) -> None:
    """Nothing is made for a trigger once its line is stopped."""
    if not runtime.enabled:
        raise ValueError(f"Line '{runtime.name}' is stopped; start it to inspect")


def _hardware_refusal(camera_id: str) -> ValueError:
    return ValueError(f"Camera '{camera_id}' is triggered by its own input: every picture it takes is one "
                      "product already, so the server cannot trigger it")


def _frame_id(driver: Any) -> int:
    if hasattr(driver, "get_latest_frame_id"):
        return driver.get_latest_frame_id()
    return getattr(driver, "_latest_frame_id", 0)


def _latest_frame(driver: Any) -> Tuple[bool, Any, int]:
    """(ok, the camera's latest frame, its id). The frame is the driver's own: drivers replace a frame, never change it."""
    if hasattr(driver, "get_latest_raw_mat"):
        return driver.get_latest_raw_mat(copy=False)
    return driver.grab_raw_frame()


async def _infer_one(worker: Any, runtime: Any, camera_id: str, mat: Any, frame_id: int, arrived: float,
                     timeout: float) -> Optional[Tuple[List[Any], float, float, Any]]:
    """Run the camera's model on this one picture and wait for what it found.

    Returns (detections, inference ms, the time the picture arrived, the picture), or None when the
    model did not take the picture or get through it within ``timeout``.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def finished(_frame_id: int, detections: List[Any], inference_ms: float, _arrived: Optional[float]) -> None:
        loop.call_soon_threadsafe(lambda: done.done() or done.set_result((detections, inference_ms)))

    deadline = _now() + timeout
    while not worker.submit_frame_if_idle(mat, frame_id, conf_thresh=runtime.confidence_for(camera_id), copy=False,
                                          arrived_at=arrived, on_result=finished):
        if _now() >= deadline:
            return None
        await _sleep(_POLL_S)
    try:
        detections, inference_ms = await asyncio.wait_for(done, timeout)
    except asyncio.TimeoutError:
        return None
    return detections, inference_ms, arrived, mat


async def _software_shot(driver: Any, worker: Any, runtime: Any, camera_id: str,
                         timeout: float) -> Tuple[List[Any], float, float, Any]:
    """Trigger a camera on software trigger and run its model on the picture that trigger made.

    Returns (detections, inference ms, the time the picture arrived, the picture). Raises _NoPicture
    when the camera did not take the trigger (it is reconnecting, it refused the command), no picture
    came within ``timeout`` or the model did not get through it in time.
    """
    try:
        shot = await asyncio.to_thread(driver.take_picture, timeout)
    except RuntimeError as exc:
        raise _NoPicture(f"the camera did not take the trigger: {exc}") from exc
    if shot is None:
        raise _NoPicture(f"no picture came within {timeout:g} s of the trigger")
    mat, frame_id, arrived = shot
    inferred = await _infer_one(worker, runtime, camera_id, mat, frame_id, arrived, timeout)
    if inferred is None:
        raise _NoPicture(f"the model did not get through the picture within {timeout:g} s")
    return inferred


async def _stream_shot(driver: Any, worker: Any, runtime: Any, camera_id: str,
                       timeout: float) -> Tuple[List[Any], float, float, Any]:
    """Run the camera's model on the first frame it sends from now on (an IP or USB camera, or an
    industrial camera that is not triggered).

    Returns (detections, inference ms, the time the frame was taken, the frame). Raises _NoPicture
    when the camera drops, no new frame comes within ``timeout`` or the model does not get through it
    in time, and ValueError when the line is stopped meanwhile.
    """
    start = _frame_id(driver)
    deadline = _now() + timeout
    while True:
        if not getattr(driver, "is_connected", False):
            raise _NoPicture("the camera is not connected")
        ok, mat, frame_id = _latest_frame(driver)
        # A camera that reconnected numbers its frames from 1 again: a lower number is a new frame too.
        if ok and mat is not None and (frame_id > start or 0 < frame_id < start):
            arrived = time.time()
            break
        if _now() >= deadline:
            raise _NoPicture(f"no new picture came within {timeout:g} s")
        await _sleep(_POLL_S)
        _still_running(runtime)
    inferred = await _infer_one(worker, runtime, camera_id, mat, frame_id, arrived, timeout)
    if inferred is None:
        raise _NoPicture(f"the model did not get through the picture within {timeout:g} s")
    return inferred


async def _take_picture(runtime: Any, camera_id: str, timeout: float) -> Tuple[List[Any], float, float, Any]:
    """The picture of one trigger, now, through the camera's model (see _software_shot and _stream_shot)."""
    from app.services.vision_service import CameraStreamPipeline
    from app.state.application_state import app_state

    driver = app_state.cameras.get(camera_id)
    if not getattr(driver, "is_connected", False):
        raise _NoPicture("the camera is not connected")
    mode = getattr(driver, "trigger_mode", "off")
    if mode == "hardware":
        raise _hardware_refusal(camera_id)        # set to Hardware while this trigger waited
    worker = CameraStreamPipeline.get_worker(camera_id)
    if mode == "software":
        # The server takes the picture itself: that picture, and no other, is the product's.
        return await _software_shot(driver, worker, runtime, camera_id, timeout)
    return await _stream_shot(driver, worker, runtime, camera_id, timeout)


def trigger_cameras(runtime: Any) -> List[str]:
    """The line's vision cameras in trigger mode, in card order."""
    return [
        cam["camera_id"] for cam in runtime.cameras
        if cam.get("role", "vision") == "vision" and cam.get("count_mode") == "trigger"
    ]


async def trigger_input(runtime: Any, trigger_no: int) -> List[Dict[str, Any]]:
    """Fire the line's "Take a picture" input with this trigger number, as its signal would.

    Returns what each camera that ticked the trigger did. Raises LookupError when the line has
    no such input switched on, and ValueError with the reasons when a camera could not take its
    picture or no camera uses the trigger.
    """
    from app.services.plc_input_service import PLCInputService

    card = next((c for c in runtime.plc_inputs if c.get("action") == "capture" and c.get("enabled", True) is True
                 and c.get("trigger_no") == trigger_no), None)
    if card is None:
        raise LookupError(trigger_no)
    _still_running(runtime)
    return await PLCInputService._capture({**card, "line_id": runtime.id, "line_name": runtime.name})


async def trigger_camera(runtime: Any, camera_id: str, timeout: Optional[float] = None,
                         with_detections: bool = False, trigger_no: Optional[int] = None,
                         sequence: Optional[Any] = None, request_id: Optional[int] = None) -> Dict[str, Any]:
    """Make one product from the picture a trigger takes with a camera in trigger mode.

    The camera's Picture delay (``picture_delay_ms`` on its card) is waited first. Then an industrial
    camera on software trigger is fired, and any other camera gives the first frame that arrives after
    that moment. ``timeout`` (TRIGGER_TIMEOUT_S), counted after the delay, is how long that picture may
    take to come, and then the model's result. Triggers of one camera are served one after the other,
    each with a picture of its own.

    ``sequence`` / ``request_id``: what the "Take a picture" input that asked carries (the product
    sequence the PLC held, the line's number for the request); the product keeps them.

    Returns what the picture said: ``status`` is ``"inspected"`` (a product was made), ``"nothing"``
    (nothing was found and that is ignored) or ``"no_picture"`` (no picture came in time, or the camera
    is not connected: the product is a reject with the reason "no picture", see no_picture_product).
    On a line with Sync or joined cameras the product's final result may still change after this
    returns. ``with_detections`` adds the picture's detections as ``detection_items``. Each answer
    counts in the camera's trigger counters, and its picture becomes the camera's latest capture.

    Raises ValueError, and makes nothing, when the line is stopped (also while the trigger waits), the
    camera is not a vision camera in trigger mode, is triggered by its own input (hardware trigger, or
    its card's Picture taken by: Camera trigger), or has no loaded model.
    """
    import app.services.line_service as line_service
    from app.events.alarm_events import AlarmCode, alarm_manager
    from app.state.application_state import app_state

    if camera_id not in trigger_cameras(runtime):
        raise ValueError(f"Camera '{camera_id}' is not a vision camera of this line with Count mode 'One picture per trigger'")
    _still_running(runtime)
    entry = runtime.camera_entry(camera_id) or {}
    mode = getattr(app_state.cameras.get(camera_id), "trigger_mode", "off")
    if mode == "hardware":
        raise _hardware_refusal(camera_id)
    if picture_source(entry, mode) == "camera":
        raise ValueError(f"Camera '{camera_id}' takes its pictures itself (Picture taken by: Camera trigger), "
                         "so the server does not take one")
    engine, _, _ = line_service.line_manager.engine_for_camera(camera_id)
    if not getattr(engine, "is_loaded", True):
        raise ValueError(f"Camera '{camera_id}' has no vision model loaded, so its picture cannot be inspected")
    timeout = TRIGGER_TIMEOUT_S if timeout is None else timeout
    delay = int(entry.get("picture_delay_ms") or 0) / 1000.0
    runtime.count_trigger(camera_id, "triggers")
    started = _now()
    answer: Dict[str, Any] = {
        "camera_id": camera_id, "status": "no_picture", "result": None, "class_name": None,
        "reject_reason": None, "track_id": None, "detections": 0, "inference_time_ms": 0.0, "waited_ms": 0.0,
    }
    # The picture is taken Picture delay after this trigger, whatever an earlier trigger of the camera is doing.
    if delay > 0:
        await _sleep(delay)
    _still_running(runtime)

    # One trigger at a time per camera: two triggers take two pictures.
    async with runtime._trigger_locks.setdefault(camera_id, asyncio.Lock()):
        try:
            detections, inference_ms, arrived_at, picture = await _take_picture(runtime, camera_id, timeout)
        except _NoPicture as missing:
            _still_running(runtime)
            answer.update(no_picture_product(runtime, camera_id, str(missing), trigger_no, sequence, request_id))
            answer["waited_ms"] = round((_now() - started) * 1000, 2)
            if with_detections:
                answer["detection_items"] = []
            return answer
        _still_running(runtime)
        answer.update({
            "inference_time_ms": round(float(inference_ms), 2),
            "waited_ms": round((_now() - started) * 1000, 2),
        })
        if with_detections:
            answer["detection_items"] = [d.model_dump() if hasattr(d, "model_dump") else d for d in detections]
        answer.update(product_from_result(runtime, camera_id, detections, arrived_at, trigger_no, sequence, request_id))
        runtime.count_trigger(camera_id, "pictures")
        runtime.count_trigger(camera_id, "results")
        alarm_manager.clear_alarm(AlarmCode.CAMERA_PICTURE_MISSING, f"camera:{camera_id}", "the camera's next picture came")
        # The picture the product was made from, with what the model found in it.
        jpeg = await asyncio.to_thread(render_trigger_capture, camera_id, picture, detections)
        runtime.remember_trigger_capture(camera_id, answer, jpeg)
        return answer
