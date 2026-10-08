"""Send cards: what a production line reports to other systems, and when."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.models.user import User
from app.dependencies import require_operator, require_supervisor
from app.services.line_config import PRIMARY_LINE_ID, SEND_PROTOCOLS, normalize_send_card

router = APIRouter(prefix="/send", tags=["Send Results"])

LINE_QUERY = Query(default=None, description="Production line id; Line 1 when omitted")

# What a message can contain, as the dashboard lists it. The ids are the names
# a card keeps in "fields"; send_dispatcher_service.MESSAGE_FIELDS maps each
# to the keys of the message.
FIELD_LABELS = [
    ("event", "Event name"),
    ("timestamp", "Time"),
    ("line", "Line (id and name)"),
    ("line_state", "Line state (started / stopped)"),
    ("camera_id", "Camera"),
    ("track_id", "Product track number"),
    ("class_name", "Class"),
    ("result", "Result (PASSED / REJECTED)"),
    ("is_defect", "Rejected (true / false)"),
    ("reject_reason", "Reject reason (vision class, code not in list, code in reject list, no code)"),
    ("confidence", "Confidence"),
    ("bbox", "Box in the picture"),
    ("smooth_center", "Position"),
    ("velocity", "Speed"),
    ("qr", "Code (text, type, known, product name)"),
    ("counts", "Counts per class"),
    ("metrics", "Totals (inspected, good, rejected, per minute, yield)"),
    ("alarm", "Alarm (code, message, severity)"),
]


def _svc():
    from app.services.settings_persistence_service import SettingsPersistenceService
    return SettingsPersistenceService


def _dispatcher():
    from app.services.send_dispatcher_service import SendDispatcherService
    return SendDispatcherService


def _line_id(line_id: Optional[str]) -> str:
    """The named line, or Line 1; 404 for a line that does not exist."""
    resolved = (line_id if isinstance(line_id, str) else None) or PRIMARY_LINE_ID
    if _svc().get_line(resolved, with_logic=False) is None:
        raise HTTPException(status_code=404, detail=f"Production line '{resolved}' not found")
    return resolved


def check_send_cards(line: Dict[str, Any], cards: List[Dict[str, Any]]) -> None:
    """A card's channel must be an MQTT, TCP or webhook channel, a card on an MQTT channel
    needs a topic, and its camera must be one of the line's."""
    channels = {ep.get("id"): ep for ep in _svc().get_endpoints()}
    own_cameras = {c.get("camera_id") for c in line.get("cameras", [])}
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = card.get("name") or card.get("id")
        endpoint_id = str(card.get("endpoint_id") or "").strip()
        if endpoint_id:
            channel = channels.get(endpoint_id)
            if channel is None:
                raise HTTPException(status_code=422, detail=f"Send card '{name}': its channel does not exist (it may have been deleted)")
            protocol = str(channel.get("protocol", "")).lower()
            if protocol not in SEND_PROTOCOLS:
                raise HTTPException(status_code=422, detail=f"Send card '{name}': '{channel.get('name')}' is not an MQTT, TCP or webhook channel")
            if protocol == "mqtt" and card.get("enabled", True) is not False and not str(card.get("topic") or "").strip():
                raise HTTPException(status_code=422, detail=f"Send card '{name}': enter the MQTT topic it publishes to on '{channel.get('name')}'")
        camera = str(card.get("camera_id") or "").strip()
        if camera and camera not in own_cameras:
            raise HTTPException(status_code=422, detail=f"Send card '{name}' names a camera that is not on this line")


@router.get("/fields", summary="What a send card's message can contain")
async def list_message_fields(user: User = Depends(require_operator)) -> Dict[str, Any]:
    from app.services.send_dispatcher_service import MESSAGE_FIELDS
    return {"fields": [{"id": field, "label": label, "keys": list(MESSAGE_FIELDS[field])} for field, label in FIELD_LABELS]}


@router.get("/actions", summary="A line's send cards, with how often each has sent and its last outcome")
async def list_send_actions(user: User = Depends(require_operator), line_id: Optional[str] = LINE_QUERY) -> List[Dict[str, Any]]:
    lid = _line_id(line_id)
    status = _dispatcher().get_status(lid)
    cards = _svc().get_line_send_actions(lid)
    for card in cards:
        live = status.get(card.get("id")) or {}
        card["sent_count"] = live.get("sent_count", 0)
        card["last_sent_at"] = live.get("last_sent_at")
        card["last_result"] = live.get("last_result", {})
    return cards


@router.post("/actions/batch", summary="Replace a line's send cards (Level 2+ Supervisor)")
async def save_send_actions(
    cards: List[Dict[str, Any]],
    user: User = Depends(require_supervisor),
    line_id: Optional[str] = LINE_QUERY,
) -> Dict[str, Any]:
    svc = _svc()
    lid = _line_id(line_id)
    check_send_cards(svc.get_line(lid, with_logic=False) or {}, cards)
    try:
        saved = svc.replace_line_send_actions(lid, cards)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    svc.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="SAVE_SEND_ACTIONS",
        category="SEND",
        details=f"Saved {len(saved)} send card(s) on line '{lid}'",
        line_id=lid,
    )
    svc.save()
    _dispatcher().set_cards(saved, line_id=lid)
    return {"saved": len(saved), "cards": saved}


@router.post("/actions/{card_id}/test", summary="Send a card's message once now, with example values (Level 2+ Supervisor)")
async def test_send_action(
    card_id: str,
    body: Optional[Dict[str, Any]] = None,
    user: User = Depends(require_supervisor),
    line_id: Optional[str] = LINE_QUERY,
) -> Dict[str, Any]:
    """The card as saved, or the card in the request body (so a card can be tried before it is applied).
    The message carries "test": true."""
    svc = _svc()
    lid = _line_id(line_id)
    line = svc.get_line(lid, with_logic=False) or {}
    raw = (body or {}).get("card")
    if not isinstance(raw, dict):
        raw = next((c for c in svc.get_line_send_actions(lid) if c.get("id") == card_id), None)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Send card '{card_id}' not found. Apply the cards first.")
    try:
        card = normalize_send_card({**raw, "id": card_id})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    check_send_cards(line, [card])
    card["line_id"] = lid
    svc.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="TEST_SEND_ACTION",
        category="SEND",
        details=f"Test message of send card '{card['name']}' by {user.username}",
        line_id=lid,
    )
    result = await _dispatcher().send_test(card, line_name=line.get("name") or lid)
    return {"card_id": card_id, **result}
