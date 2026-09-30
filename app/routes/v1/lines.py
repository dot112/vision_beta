"""Production lines: configure, start, stop and monitor each line on its own."""
from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.models.user import User
from app.dependencies import require_operator, require_supervisor
from app.services.line_config import plc_address_clashes

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


def _view(line_id: str, with_logic: bool) -> Dict[str, Any]:
    line = _svc().get_line(line_id, with_logic=with_logic)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    runtime = _manager().get(line_id)
    line["status"] = _manager().summary(runtime) if runtime else None
    return line


def _audit(user: User, action: str, details: str, line_id: Optional[str]) -> None:
    _svc().record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action=action,
        category="LINES",
        details=details,
        line_id=line_id,
    )


async def _apply(line_id: Optional[str] = None) -> Dict[str, str]:
    """Push saved line settings into the running server. Returns model problems, if any."""
    from app.services.plc_dispatcher_service import PLCDispatcherService

    svc = _svc()
    svc.save()
    _manager().apply_state()
    if line_id:
        cards = svc.get_line_plc_actions(line_id) if svc.get_line(line_id, with_logic=False) else []
        PLCDispatcherService.set_cards(cards, line_id=line_id)
    return await _manager().refresh_models()


def _save(raw: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _svc().save_line(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _model_warning(problems: Dict[str, str], line: Dict[str, Any]) -> Optional[str]:
    problem = problems.get(line.get("model_id") or "")
    return f"The selected model is unavailable ({problem}); the line uses the active model." if problem else None


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
    return _view(line_id, with_logic=True)


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
    return {"line_id": line_id, "reads": runtime.recent_reads(limit), "stats": stats}


# ── Change ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201, summary="Create a production line (Level 2+ Supervisor)")
async def create_line(body: Dict[str, Any], user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    raw = dict(body)
    raw.pop("id", None)  # new lines always get a fresh id
    raw.setdefault("enabled", False)  # starts stopped; Start connects its cameras
    line = _save(raw)
    _audit(user, "CREATE_LINE", f"Created production line '{line['name']}'", line["id"])
    problems = await _apply(line["id"])
    view = _view(line["id"], with_logic=True)
    view["warning"] = _model_warning(problems, view)
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
    line = _save({**body, "id": line_id})
    _audit(user, "UPDATE_LINE", f"Updated production line '{line['name']}'", line_id)
    problems = await _apply(line_id)
    view = _view(line_id, with_logic=True)
    view["warning"] = _model_warning(problems, view)
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
    await _apply(line_id)
    return {"status": "deleted", "id": line_id}


@router.post("/{line_id}/start", summary="Run one line without touching the others (Level 2+ Supervisor)")
async def start_line(line_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    line = _svc().get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    _save({"id": line_id, "enabled": True})
    _audit(user, "START_LINE", f"Started production line '{line['name']}'", line_id)
    await _apply()
    results = await _manager().connect_line(line_id)
    view = _view(line_id, with_logic=False)
    view["camera_errors"] = {cid: err for cid, err in results.items() if err}
    return view


@router.post("/{line_id}/stop", summary="Stop one line and release its cameras (Level 2+ Supervisor)")
async def stop_line(line_id: str, user: User = Depends(require_supervisor)) -> Dict[str, Any]:
    line = _svc().get_line(line_id, with_logic=False)
    if line is None:
        raise HTTPException(status_code=404, detail=f"Production line '{line_id}' not found")
    _save({"id": line_id, "enabled": False})
    _audit(user, "STOP_LINE", f"Stopped production line '{line['name']}'", line_id)
    await _apply()
    await _manager().disconnect_line(line_id)
    return _view(line_id, with_logic=False)


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
    raw = {
        "name": name,
        "enabled": False,
        "auto_connect": source.get("auto_connect", True),
        # A camera belongs to one line only, so the copy starts without cameras.
        "cameras": [],
        "model_id": source.get("model_id"),
        "sync": {"enabled": False, "window_ms": (source.get("sync") or {}).get("window_ms")},
        "min_fps": source.get("min_fps"),
        "action_trigger": source.get("action_trigger", {}),
        "plc_actions": cards,
    }
    line = _save(raw)
    _audit(user, "CLONE_LINE", f"Copied line '{source['name']}' into '{line['name']}'", line["id"])
    await _apply(line["id"])
    return _view(line["id"], with_logic=True)

