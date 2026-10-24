"""Production lines: configure, start, stop and monitor each line on its own."""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.db.models.user import User
from app.dependencies import require_operator, require_supervisor
from app.services.line_config import (
    code_check,
    line_warnings,
    model_warnings,
    plc_address_clashes,
    reads_codes,
    trigger_mode_to_set,
    vision_models,
)

router = APIRouter(prefix="/lines", tags=["Production Lines"])


def _svc():
    from app.services.settings_persistence_service import SettingsPersistenceService
    return SettingsPersistenceService


def _manager():
    from app.services.line_service import line_manager
    return line_manager


def _clashes() -> List[Dict[str, Any]]:
    svc = _svc()
    return plc_address_clashes({ln["id"]: svc.get_line_plc_actions(ln["id"]) for ln in svc.get_lines()})


async def _model_info(line: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """What the server knows of the models a line's vision cameras run: name, classes, loaded or not."""
    from app.db.models.model import VisionModel
    from app.db.session import AsyncSessionLocal

    info: Dict[str, Dict[str, Any]] = {}
    wanted = vision_models(line)
    if not wanted:
        return info
    async with AsyncSessionLocal() as db:
        for model_id in wanted:
            model = await db.get(VisionModel, model_id)
            if model is not None:
                info[model_id] = {
                    "name": model.name,
                    "classes": list(model.classes or []),
                    **_manager().model_state(model_id),
                }
    return info


async def _view(line_id: str, with_logic: bool) -> Dict[str, Any]:
    line = _svc().get_line(line_id, with_logic=with_logic)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    runtime = _manager().get(line_id)
    line["status"] = _manager().summary(runtime) if runtime else None
    if with_logic:
        # How fast the line runs now: a joined camera's match window is checked against the gap between products.
        rate = runtime.counter.products_per_minute if runtime and runtime.enabled else 0.0
        line["warnings"] = [
            await _name_cameras(warning, line.get("cameras"))
            for warning in line_warnings(line, await _model_info(line), products_per_minute=rate,
                                         trigger_modes=await _trigger_modes(line.get("cameras")),
                                         list_formats=_list_formats(), send_protocols=_send_protocols())
        ]
    return line


def _audit(user: Any, action: str, details: str, line_id: Optional[str]) -> None:
    _svc().record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action=action,
        category="LINES",
        details=details,
        line_id=line_id,
    )


async def apply_line(line_id: Optional[str] = None) -> Dict[str, str]:
    """Push saved line settings into the running server and load the models the
    cameras now run. Returns {model_id: problem} for the models that did not load."""
    from app.services.plc_dispatcher_service import PLCDispatcherService

    from app.services.send_dispatcher_service import SendDispatcherService

    svc = _svc()
    svc.save()
    _manager().apply_state()
    if line_id:
        exists = svc.get_line(line_id, with_logic=False) is not None
        PLCDispatcherService.set_cards(svc.get_line_plc_actions(line_id) if exists else [], line_id=line_id)
        SendDispatcherService.set_cards(svc.get_line_send_actions(line_id) if exists else [], line_id=line_id)
    # The lines' PLC signal cards, read again whichever line changed.
    from app.services.plc_input_service import PLCInputService
    PLCInputService.reload()
    return await _manager().refresh_models()


async def _fire_line_state(line: Dict[str, Any], was_running: bool) -> None:
    """Run the line's "Line started/stopped" PLC cards and send cards when its state changed."""
    from app.services.plc_dispatcher_service import PLCDispatcherService, line_state_event
    from app.services.send_dispatcher_service import SendDispatcherService

    running = bool(line.get("enabled"))
    if running != bool(was_running):
        event = line_state_event(line["id"], line.get("name") or line["id"], running)
        await PLCDispatcherService.evaluate(event)
        SendDispatcherService.line_state_changed(event)


def _check_code_types(raw: Dict[str, Any]) -> None:
    """A reader's code type must be one the installed reader supports (GET /qr/code-types)."""
    from app.engines.qr_engine import is_code_type

    cameras = raw.get("cameras")
    for index, cam in enumerate(cameras if isinstance(cameras, list) else []):
        code_type = cam.get("code_type") if isinstance(cam, dict) else None
        if code_type not in (None, "") and not is_code_type(code_type):
            raise HTTPException(
                status_code=422,
                detail=f"Camera {index + 1}: '{code_type}' is not a code type this server's reader supports",
            )


def _check_product_lists(raw: Dict[str, Any]) -> None:
    """A camera that reads codes checks them against a product list that exists (Products page).

    A reader sent without one (a client written for the single list) gets the oldest list.
    """
    from app.services.product_service import product_catalog

    cameras = raw.get("cameras")
    for index, cam in enumerate(cameras if isinstance(cameras, list) else []):
        if not isinstance(cam, dict) or not reads_codes(cam):
            continue
        if "product_list_id" not in cam:
            lists = product_catalog.list_ids()
            cam["product_list_id"] = lists[0] if lists else None
        elif cam["product_list_id"] not in (None, "") and not product_catalog.has_list(cam["product_list_id"]):
            raise HTTPException(
                status_code=422,
                detail=f"Camera {index + 1}: its product list was not found (it may have been deleted). Choose another list.",
            )


async def _check_models(raw: Dict[str, Any]) -> None:
    """A vision camera's model must be one of the models on the AI models page.

    (That every vision camera has one is checked when the line is saved.)
    """
    from app.db.models.model import VisionModel
    from app.db.session import AsyncSessionLocal

    cameras = raw.get("cameras")
    named = [
        (f"Camera {index + 1}", cam.get("model_id"))
        for index, cam in enumerate(cameras if isinstance(cameras, list) else [])
        if isinstance(cam, dict) and str(cam.get("role") or "vision").strip().lower() == "vision"
    ]
    # A client written before models were picked per camera names one for the line.
    named.append(("The line", raw.get("model_id")))
    named = [(label, model_id.strip()) for label, model_id in named if isinstance(model_id, str) and model_id.strip()]
    if not named:
        return
    async with AsyncSessionLocal() as db:
        for label, model_id in named:
            if await db.get(VisionModel, model_id) is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"{label}: its vision model was not found (it may have been deleted). Choose another model.",
                )


def _check_camera_count(raw: Dict[str, Any]) -> None:
    """A line cannot have more cameras than the server connects at once (MAX_CONNECTED_CAMERAS)."""
    from app.config import settings

    cameras = raw.get("cameras")
    limit = int(settings.MAX_CONNECTED_CAMERAS)
    if isinstance(cameras, list) and len(cameras) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"This line has {len(cameras)} cameras, but this server connects at most {limit} cameras at once "
                   f"(MAX_CONNECTED_CAMERAS). Remove a camera, or raise that setting.",
        )


async def _check_picture_sources(raw: Dict[str, Any]) -> None:
    """Camera trigger (the camera takes its own picture) is only for an industrial camera (row type gige)."""
    from app.db.models.camera import Camera
    from app.db.session import AsyncSessionLocal

    cameras = raw.get("cameras")
    asked = [
        (index, str(cam.get("camera_id") or "").strip())
        for index, cam in enumerate(cameras if isinstance(cameras, list) else [])
        if isinstance(cam, dict) and str(cam.get("picture_source") or "").strip().lower() == "camera"
        # The key counts only on a vision camera with One picture per trigger (else the save drops it).
        and str(cam.get("role") or "vision").strip().lower() == "vision"
        and str(cam.get("count_mode") or "trigger").strip().lower() == "trigger"
    ]
    if not asked:
        return
    async with AsyncSessionLocal() as db:
        for index, camera_id in asked:
            row = await db.get(Camera, camera_id) if camera_id else None
            if row is not None and row.type != "gige":
                raise HTTPException(status_code=422, detail=f"Camera {index + 1}: only an industrial camera can trigger itself")


_CAMERA_LABEL = re.compile(r"\bCamera (\d+)\b(?! \()")


async def _camera_names(cameras: Any) -> Dict[str, str]:
    """{camera_id: name} for a line's cameras, from the camera list (Cameras page)."""
    from app.db.models.camera import Camera
    from app.db.session import AsyncSessionLocal
    from app.state.application_state import app_state

    ids = [str(c.get("camera_id")) for c in cameras if isinstance(c, dict) and c.get("camera_id")] \
        if isinstance(cameras, list) else []
    names: Dict[str, str] = {}
    if not ids:
        return names
    try:
        async with AsyncSessionLocal() as db:
            for camera_id in ids:
                row = await db.get(Camera, camera_id)
                if row is not None and row.name:
                    names[camera_id] = row.name
    except Exception:
        pass
    for camera_id in ids:
        driver_name = getattr(app_state.cameras.get(camera_id), "name", None)
        if camera_id not in names and driver_name:
            names[camera_id] = driver_name
    return names


def _list_formats() -> Dict[str, Dict[str, Any]]:
    """Each product list's code format, for the line warnings (a list missing reads whole codes)."""
    from app.services.product_service import product_catalog

    return product_catalog.formats()


def _send_protocols() -> Dict[str, str]:
    """{endpoint_id: protocol} of the communication channels, for the line warnings (who waits for an answer)."""
    return {str(endpoint["id"]): str(endpoint.get("protocol", "")).lower()
            for endpoint in _svc().get_endpoints() if endpoint.get("id")}


async def _trigger_modes(cameras: Any) -> Dict[str, str]:
    """{camera_id: "hardware" | "software"} for the line's industrial cameras that are set to a trigger.

    The Trigger is the camera's own setting (Cameras page), so the line's settings do not hold it.
    """
    from app.db.models.camera import Camera
    from app.db.session import AsyncSessionLocal

    ids = [str(c.get("camera_id")) for c in cameras if isinstance(c, dict) and c.get("camera_id")] \
        if isinstance(cameras, list) else []
    modes: Dict[str, str] = {}
    if not ids:
        return modes
    try:
        async with AsyncSessionLocal() as db:
            for camera_id in ids:
                row = await db.get(Camera, camera_id)
                mode = str((row.settings or {}).get("trigger_mode") or "off").lower() \
                    if row is not None and row.type == "gige" else "off"
                if mode in ("hardware", "software"):
                    modes[camera_id] = mode
    except Exception:
        pass    # the line is shown without these warnings
    return modes


async def _apply_trigger_modes(line: Dict[str, Any]) -> List[str]:
    """Set each industrial vision camera of a saved line to the trigger mode its card asks for.

    A card with Count mode One picture per trigger sets it: from the camera Hardware, from the system
    Software. Any other vision card leaves the camera's mode alone (a tracking camera on Hardware, a camera
    that reads codes), except Software with nothing to trigger it that reads no codes, which is Off
    (``trigger_mode_to_set``).
    Only a mode that differs is set, through the path the camera's Settings use. A QR reader keeps its
    own mode. A camera that cannot be set is a warning, never a refused save:
    ["Camera 2's trigger mode could not be set: ..."].
    """
    from app.db.models.camera import Camera
    from app.db.session import AsyncSessionLocal
    from app.schemas.camera import CameraUpdate
    from app.services.camera_service import CameraService

    cameras = line.get("cameras")
    warnings: List[str] = []
    async with AsyncSessionLocal() as db:
        for index, cam in enumerate(cameras if isinstance(cameras, list) else []):
            if not isinstance(cam, dict) or cam.get("role", "vision") != "vision" or not cam.get("camera_id"):
                continue
            try:
                row = await db.get(Camera, str(cam["camera_id"]))
                if row is None or row.type != "gige":
                    continue
                # A card saved without a picture source follows the camera's mode: Hardware stays Hardware.
                current = str((row.settings or {}).get("trigger_mode") or "off").strip().lower()
                wanted = trigger_mode_to_set(cam, current)
                if wanted is None or wanted == current:
                    continue
                if await CameraService.update_camera(db, row.id, CameraUpdate(settings={"trigger_mode": wanted})) is None:
                    raise LookupError("the camera is not in the camera list")
            except Exception as exc:
                await db.rollback()
                warnings.append(f"Camera {index + 1}'s trigger mode could not be set: {str(exc) or type(exc).__name__}")
    return warnings


async def _name_cameras(text: Optional[str], cameras: Any) -> Optional[str]:
    """A message with each "Camera N" followed by that camera's name: "Camera 2 ('Packing left')"."""
    if not text or not isinstance(cameras, list) or not _CAMERA_LABEL.search(text):
        return text
    names = await _camera_names(cameras)

    def named(match: "re.Match[str]") -> str:
        index = int(match.group(1)) - 1
        cam = cameras[index] if 0 <= index < len(cameras) and isinstance(cameras[index], dict) else {}
        name = names.get(str(cam.get("camera_id") or ""))
        return f"{match.group(0)} ('{name}')" if name else match.group(0)

    return _CAMERA_LABEL.sub(named, text)


async def _save(raw: Dict[str, Any]) -> Dict[str, Any]:
    try:
        _check_camera_count(raw)
        _check_code_types(raw)
        _check_product_lists(raw)
        await _check_models(raw)
        await _check_picture_sources(raw)
        try:
            return _svc().save_line(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException as exc:
        if isinstance(exc.detail, str) and "cameras" in raw:
            exc.detail = await _name_cameras(exc.detail, raw.get("cameras"))
        raise


def _sync_forced(body: Dict[str, Any], saved: Dict[str, Any]) -> Optional[str]:
    """Said once, when a save switched Sync on because a camera now checks codes."""
    asked = body.get("sync") if isinstance(body.get("sync"), dict) else None
    if asked is not None and asked.get("enabled") is False and (saved.get("sync") or {}).get("enabled") \
            and any(code_check(cam) for cam in saved.get("cameras", [])):
        return "Sync was switched on: it pairs each product with its code, so the product gets one result."
    return None


async def _save_warning(body: Dict[str, Any], view: Dict[str, Any], not_set: Optional[List[str]] = None) -> Optional[str]:
    """What the user is told after a save: the first of a model problem, a camera whose trigger mode
    could not be set (``not_set``, all of them), Sync switched on, or a line warning."""
    about_models = model_warnings(view, await _model_info(view))
    first = next(iter(about_models), None)
    if first:
        return await _name_cameras(first, view.get("cameras"))
    if not_set:
        return " ".join(not_set)
    return _sync_forced(body, view) or next(iter(view.get("warnings") or []), None)


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get("", summary="List production lines with state, cameras, counts and alarm count")
async def list_lines(user: User = Depends(require_operator)) -> Dict[str, Any]:
    lines = []
    for line in _svc().get_lines():
        runtime = _manager().get(line["id"])
        line["status"] = _manager().summary(runtime) if runtime else None
        lines.append(line)
    return {"lines": lines, "plc_address_clashes": _clashes()}


@router.get("/overview", summary="Every line's live figures in one call, for the plant overview")
async def lines_overview(user: User = Depends(require_operator)) -> Dict[str, Any]:
    return {"lines": _manager().overview()}


@router.get("/{line_id}", summary="One line's settings, logic and live figures")
async def get_line(line_id: str, user: User = Depends(require_operator)) -> Dict[str, Any]:
    return await _view(line_id, with_logic=True)


@router.get("/{line_id}/qr/recent", summary="The line's latest code reads: known, unknown, no-read and unpaired")
async def recent_qr_reads(
    line_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(require_operator),
) -> Dict[str, Any]:
    runtime = _manager().get(line_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    with runtime._lock:
        stats = dict(runtime.qr_stats)
    capture, _ = runtime.capture_image()
    return {
        "line_id": line_id,
        "reads": runtime.recent_reads(limit),
        "stats": stats,
        # Sorting verification at the outfeeds, when a camera of the line verifies one.
        "sorting": runtime.sorting_counts() if runtime.sorting_lanes else None,
        "last_capture": _capture_view(capture),
    }


def _capture_view(capture: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not capture:
        return None
    view = {k: v for k, v in capture.items() if k != "t"}
    view["codes"] = [{k: v for k, v in c.items() if k != "polygon"} for c in capture.get("codes") or []]
    return view


@router.get("/{line_id}/qr/capture", summary="The latest picture a triggered QR reader took, codes outlined (JPEG)")
async def last_qr_capture(line_id: str, user: User = Depends(require_operator)) -> Response:
    runtime = _manager().get(line_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    capture, jpeg = runtime.capture_image()
    if not capture or not jpeg:
        raise HTTPException(status_code=404, detail="No picture has been taken yet")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Capture-Id": str(capture["id"])},
    )


@router.get("/{line_id}/trigger/capture", summary="The latest picture a camera in trigger mode took, with the model's boxes (JPEG)")
async def last_trigger_capture(
    line_id: str,
    camera_id: str = Query(..., description="The trigger camera"),
    user: User = Depends(require_operator),
) -> Response:
    """``X-Capture-Id`` is the capture's id (it goes up with each picture); 404 when the camera has
    not taken one yet, or its last trigger got no picture."""
    runtime = _manager().get(line_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    capture, jpeg = runtime.trigger_capture(camera_id)
    if not capture or not jpeg:
        raise HTTPException(status_code=404, detail="No picture has been taken yet")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Capture-Id": str(capture["id"])},
    )


# ── PLC signals (inputs) ──────────────────────────────────────────────────────

@router.get("/{line_id}/plc-inputs/status", summary="What each of the line's PLC signals last read")
async def plc_inputs_status(line_id: str, user: User = Depends(require_operator)) -> Dict[str, Any]:
    """Per signal card: its value, when it last changed, how often it was read and its error.
    ``running`` is false when the server is not reading PLC signals at all."""
    from app.services.plc_input_service import PLCInputService

    if _svc().get_line(line_id, with_logic=False) is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    return {"line_id": line_id, "running": PLCInputService.is_running(), "inputs": PLCInputService.get_status(line_id)}


@router.get("/{line_id}/handshake", summary="The line's result handshake: its state, sequence and counters")
async def line_handshake(line_id: str, user: User = Depends(require_operator)) -> Dict[str, Any]:
    """``state``: idle, waiting_result (a capture request waits for its result) or result_ready (the
    result went out and waits for the PLC's acknowledge); ``sequence`` of the request under way;
    ``since`` (ISO time of the last change of state); ``last_result``; ``stale_acks``, ``timeouts``,
    ``ack_timeouts``, ``duplicates`` and ``late`` (results that came after their timeout);
    ``last_error``; ``settings``. ``enabled`` is false when the line has no handshake switched on."""
    runtime = _manager().get(line_id)
    if runtime is None or _svc().get_line(line_id, with_logic=False) is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    return {"line_id": line_id, **runtime.handshake.view()}


@router.post("/{line_id}/plc-inputs/{input_id}/read", summary="Read a PLC signal's address once, now (Level 2+ Supervisor)")
async def read_plc_input(
    line_id: str,
    input_id: str,
    body: Optional[Dict[str, Any]] = None,
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """The signal dialog's "Read now". A card that is not saved yet is sent as ``{"input": {...}}``.
    Answers ``{"ok", "value", "message"}``; nothing the card would do is done."""
    from app.services.line_config import normalize_plc_input
    from app.services.plc_input_service import PLCInputService

    line = _svc().get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    draft = (body or {}).get("input")
    if isinstance(draft, dict):
        try:
            card = normalize_plc_input({**draft, "id": draft.get("id") or input_id})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        card = next((c for c in line.get("plc_inputs") or [] if c.get("id") == input_id), None)
        if card is None:
            raise HTTPException(status_code=404, detail=f"PLC signal '{input_id}' is not saved on this line")
    return await PLCInputService.read_now(card)


# ── Trigger inspection ────────────────────────────────────────────────────────

@router.post("/{line_id}/trigger", summary="One picture, one product, for the line's cameras in trigger mode (Level 2+ Supervisor)")
async def trigger_line(line_id: str, body: Optional[Dict[str, Any]] = None,
                       user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    """Trigger every vision camera of the line that has Count mode "One picture per trigger",
    or only ``{"camera_id": "..."}``. Without a camera_id, only the cameras whose picture the server
    takes (not Camera trigger, not a hardware trigger) are triggered. Each makes one product from its next picture: it is
    counted, recorded and sent, and fires the line's cards. Answers what each camera's own
    picture said; 409 when the line is stopped or has no such camera.

    ``{"trigger_no": N}`` fires the line's "Take a picture" input with that trigger number
    instead, as its signal would: every camera that has the trigger ticked takes a picture,
    code readers included. 404 when the line has no such input switched on."""
    import asyncio

    from app.services.trigger_inspection import system_trigger_cameras, trigger_camera, trigger_cameras, trigger_input

    if _svc().get_line(line_id, with_logic=False) is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    runtime = _manager().get(line_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    number = (body or {}).get("trigger_no")
    if number not in (None, ""):
        try:
            return {"line_id": line_id, "trigger_no": int(number), "results": await trigger_input(runtime, int(number))}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="This line has no 'Take a picture' input with the trigger "
                                                        f"number {number} switched on") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    wanted = str((body or {}).get("camera_id") or "").strip()
    if wanted:
        cameras = [wanted]
    else:
        # Every trigger gets one answer: only the cameras whose picture the server takes (not Camera trigger, not Hardware).
        cameras = system_trigger_cameras(runtime)
        if not cameras and trigger_cameras(runtime):
            raise HTTPException(status_code=409, detail="This line has no camera whose picture the server takes")
    if not cameras:
        raise HTTPException(status_code=409, detail="This line has no vision camera with Count mode 'One picture per trigger'")
    try:
        results = await asyncio.gather(*(trigger_camera(runtime, camera_id) for camera_id in cameras))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"line_id": line_id, "results": list(results)}


# ── Change ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201, summary="Create a production line (Level 2+ Supervisor)")
async def create_line(body: Dict[str, Any], user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    raw = dict(body)
    raw.pop("id", None)  # new lines always get a fresh id
    raw.setdefault("enabled", False)  # starts stopped; Start connects its cameras
    line = await _save(raw)
    _audit(user, "CREATE_LINE", f"Created production line '{line['name']}'", line["id"])
    not_set = await _apply_trigger_modes(line)
    await apply_line(line["id"])
    view = await _view(line["id"], with_logic=True)
    view["warning"] = await _save_warning(body, view, not_set)
    return view


@router.put("/{line_id}", summary="Change a production line, its cameras, roles and Sync (Level 2+ Supervisor)")
async def update_line(line_id: str, body: Dict[str, Any], user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    existing = _svc().get_line(line_id, with_logic=True)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    # A card's camera choice must name one of the line's cameras (checked before saving).
    cameras = body.get("cameras") if isinstance(body.get("cameras"), list) else existing.get("cameras", [])
    own = {str(c.get("camera_id")) for c in cameras if isinstance(c, dict)}
    cards = body.get("plc_actions") if isinstance(body.get("plc_actions"), list) else existing.get("plc_actions", [])
    bad = [c for c in cards if isinstance(c, dict) and str(c.get("camera_id") or "").strip() not in ("", *own)]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Card '{bad[0].get('name') or bad[0].get('id')}' names a camera that is not on this line",
        )
    # The same for the send cards, and each one's channel must be one a message can go to.
    from app.routes.v1.send_actions import check_send_cards
    send_cards = body.get("send_actions") if isinstance(body.get("send_actions"), list) else existing.get("send_actions", [])
    check_send_cards({"cameras": [c for c in cameras if isinstance(c, dict)]}, send_cards)
    line = await _save({**body, "id": line_id})
    _audit(user, "UPDATE_LINE", f"Updated production line '{line['name']}'", line_id)
    not_set = await _apply_trigger_modes(line)
    await apply_line(line_id)
    await _fire_line_state(line, existing.get("enabled"))
    view = await _view(line_id, with_logic=True)
    view["warning"] = await _save_warning(body, view, not_set)
    return view


@router.delete("/{line_id}", summary="Delete a production line (Level 2+ Supervisor)")
async def delete_line(line_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    line = _svc().get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    try:
        _svc().delete_line(line_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(user, "DELETE_LINE", f"Deleted production line '{line['name']}'", line_id)
    await apply_line(line_id)
    return {"status": "deleted", "id": line_id}


async def set_line_running(line_id: str, running: bool, actor: Any) -> Dict[str, Any]:
    """Start or stop one line for whoever asked: a signed-in user, or a Sparkplug host's command.

    ``actor`` has a username, role and clearance_level, for the audit entry.
    Raises 404 for a line that does not exist.
    """
    line = _svc().get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    saved = await _save({"id": line_id, "enabled": running})
    if running:
        _audit(actor, "START_LINE", f"Started production line '{line['name']}'", line_id)
    else:
        _audit(actor, "STOP_LINE", f"Stopped production line '{line['name']}'", line_id)
    await apply_line()
    await _fire_line_state(saved, line.get("enabled"))
    if not running:
        await _manager().disconnect_line(line_id)
        return await _view(line_id, with_logic=False)
    results = await _manager().connect_line(line_id)
    view = await _view(line_id, with_logic=False)
    view["camera_errors"] = {cid: err for cid, err in results.items() if err}
    return view


def set_line_batch(line_id: str, batch: Any, actor: Any) -> str:
    """Set a line's batch (work order) number for whoever asked: a signed-in user, or a PLC signal.

    Returns the batch as it was saved ("" clears it). Every product, code read,
    message and record of the line carries it from now on. Raises 404 for a
    line that does not exist and ValueError for a batch that cannot be one.
    ``actor`` has a username, role and clearance_level, for the audit entry.
    """
    from app.services.line_config import clean_batch

    svc = _svc()
    line = svc.get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    batch = clean_batch(batch)
    if batch == (line.get("batch") or ""):
        return batch        # nothing changes, nothing to write down
    svc.record_audit(
        username=actor.username,
        role=actor.role,
        clearance_level=actor.clearance_level,
        action="SET_BATCH",
        category="PRODUCTION",
        details=f"Batch of line '{line['name']}' set to '{batch}'" if batch else f"Batch of line '{line['name']}' cleared",
        line_id=line_id,
    )
    svc.set_line_batch(line_id, batch)
    runtime = _manager().get(line_id)
    if runtime is not None:
        runtime.set_batch(batch)
    return batch


@router.put("/{line_id}/batch", summary="Set the line's batch (work order) number (Level 2+ Supervisor)")
async def put_line_batch(line_id: str, body: Dict[str, Any], user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    """``{"batch": "WO-2026-118"}``; an empty text clears it. At most 64 characters."""
    if "batch" not in body:
        raise HTTPException(status_code=422, detail="Send the batch number as {\"batch\": \"...\"}")
    try:
        return {"line_id": line_id, "batch": set_line_batch(line_id, body["batch"], user)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{line_id}/formula/check", summary="Check a formula of an output action (Level 2+ Supervisor)")
async def check_formula(line_id: str, body: Dict[str, Any], user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    """``{"formula": "..."}``. Answers ``{"ok": true, "names", "warnings"}``, or ``{"ok": false, "error", "position"}``.

    The warnings name product fields that no product list of the line's code readers has, and
    input values the line has none of: allowed when saving, because lists and inputs change.
    """
    from app.services.formula import FormulaError, parse
    from app.services.product_service import product_catalog
    from app.services.settings_persistence_service import SettingsPersistenceService

    line = SettingsPersistenceService.get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail="Line not found")
    text = body.get("formula")
    try:
        formula = parse(text if isinstance(text, str) else "")
    except FormulaError as exc:
        return {"ok": False, "error": str(exc), "position": exc.position}
    fields = {"name", "code", "list"}
    for camera in line.get("cameras") or []:
        if reads_codes(camera):
            fields.update(field["key"] for field in product_catalog.fields(camera.get("product_list_id")))
    inputs = {"value", "previous"} | {card.get("value_name") for card in line.get("plc_inputs") or [] if card.get("value_name")}
    warnings = []
    for name in sorted(formula.names):
        root, _, key = name.partition(".")
        if root == "product" and key not in fields:
            warnings.append(f"No product list of this line's code readers has the field '{key}'.")
        elif root == "input" and key not in inputs:
            warnings.append(f"No input of this line has the value name '{key}'.")
    return {"ok": True, "names": sorted(formula.names), "warnings": warnings}


@router.post("/{line_id}/start", summary="Run one line without touching the others (Level 2+ Supervisor)")
async def start_line(line_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    return await set_line_running(line_id, True, user)


@router.post("/{line_id}/stop", summary="Stop one line and release its cameras (Level 2+ Supervisor)")
async def stop_line(line_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    return await set_line_running(line_id, False, user)


@router.post("/{line_id}/clone", status_code=201, summary="Copy a line's logic into a new line (Level 2+ Supervisor)")
async def clone_line(line_id: str, body: Optional[Dict[str, Any]] = None, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    source = _svc().get_line(line_id, with_logic=True)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    name = str((body or {}).get("name") or f"{source['name']} copy").strip()
    cards = []
    for card in source.get("plc_actions", []):
        card = copy.deepcopy(card)
        card["id"] = f"{card.get('id') or 'card'}-{uuid.uuid4().hex[:6]}"
        card.pop("camera_id", None)  # cameras are not copied, so camera choices cannot be either
        # Nor are the PLC signal cards, so the copy cannot wait for one to confirm it.
        card.pop("confirm_input_id", None)
        card.pop("confirm_timeout_ms", None)
        cards.append(card)
    send_cards = []
    for card in source.get("send_actions", []):
        card = copy.deepcopy(card)
        card["id"] = f"send_{uuid.uuid4().hex[:8]}"
        card.pop("camera_id", None)
        send_cards.append(card)
    raw = {
        "name": name,
        "enabled": False,
        # A camera belongs to one line only, so the copy starts without cameras
        # (and so without their models and class lists).
        "cameras": [],
        "sync": {"enabled": False, "window_ms": (source.get("sync") or {}).get("window_ms")},
        "min_fps": source.get("min_fps"),
        "yield_target": source.get("yield_target"),
        "action_trigger": source.get("action_trigger", {}),
        "plc_actions": cards,
        "send_actions": send_cards,
    }
    line = await _save(raw)
    _audit(user, "CLONE_LINE", f"Copied line '{source['name']}' into '{line['name']}'", line["id"])
    await apply_line(line["id"])
    return await _view(line["id"], with_logic=True)

