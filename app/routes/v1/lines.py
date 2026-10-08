"""Production lines: configure, start, stop and monitor each line on its own."""
from __future__ import annotations

import copy
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
        line["warnings"] = line_warnings(line, await _model_info(line))
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


async def _save(raw: Dict[str, Any]) -> Dict[str, Any]:
    _check_code_types(raw)
    _check_product_lists(raw)
    await _check_models(raw)
    try:
        return _svc().save_line(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _sync_forced(body: Dict[str, Any], saved: Dict[str, Any]) -> Optional[str]:
    """Said once, when a save switched Sync on because a camera now checks codes."""
    asked = body.get("sync") if isinstance(body.get("sync"), dict) else None
    if asked is not None and asked.get("enabled") is False and (saved.get("sync") or {}).get("enabled") \
            and any(code_check(cam) for cam in saved.get("cameras", [])):
        return "Sync was switched on: it pairs each product with its code, so the product gets one result."
    return None


async def _save_warning(body: Dict[str, Any], view: Dict[str, Any]) -> Optional[str]:
    """What the user is told after a save: the first of a model problem, Sync switched on, or a line warning."""
    about_models = model_warnings(view, await _model_info(view))
    return next(iter(about_models), None) or _sync_forced(body, view) or next(iter(view.get("warnings") or []), None)


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


@router.post("/{line_id}/qr/capture", summary="Take a test picture with the line's QR reader (sends no events; Level 2+)")
async def test_qr_capture(line_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    """Take and decode one picture now, to check the QR camera's aim and focus.
    The result shows on the Line Dashboard; no read, PLC card or message is triggered."""
    import asyncio

    from app.services.qr_service import QRReaderPipeline
    from app.state.application_state import app_state

    runtime = _manager().get(line_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    # The QR reader, else a vision camera with Read codes on.
    readers = sorted((c for c in runtime.cameras if reads_codes(c)), key=lambda c: c.get("role") != "qr")
    qr_camera = readers[0]["camera_id"] if readers else None
    if qr_camera is None:
        raise HTTPException(status_code=409, detail="No camera on this line reads codes")
    if not getattr(app_state.cameras.get(qr_camera), "is_connected", False):
        raise HTTPException(status_code=409, detail="The camera that reads codes is not connected")
    before, _ = runtime.capture_image()
    before_id = before["id"] if before else 0
    QRReaderPipeline.get_worker(qr_camera).request_capture(test=True)
    for _ in range(60):  # up to 3 s
        await asyncio.sleep(0.05)
        capture, _ = runtime.capture_image()
        if capture and capture["id"] > before_id:
            return _capture_view(capture)
    raise HTTPException(status_code=504, detail="The QR reader did not return a picture in time")


# ── Change ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201, summary="Create a production line (Level 2+ Supervisor)")
async def create_line(body: Dict[str, Any], user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    raw = dict(body)
    raw.pop("id", None)  # new lines always get a fresh id
    raw.setdefault("enabled", False)  # starts stopped; Start connects its cameras
    line = await _save(raw)
    _audit(user, "CREATE_LINE", f"Created production line '{line['name']}'", line["id"])
    await apply_line(line["id"])
    view = await _view(line["id"], with_logic=True)
    view["warning"] = await _save_warning(body, view)
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
    await apply_line(line_id)
    await _fire_line_state(line, existing.get("enabled"))
    view = await _view(line_id, with_logic=True)
    view["warning"] = await _save_warning(body, view)
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

