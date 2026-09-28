"""PLC Action REST API routes."""
from __future__ import annotations

from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException

from app.db.models.user import User
from app.dependencies import require_operator, require_supervisor
from app.schemas.plc import OPCUAScanRequest, PLCActionCard, PLCActionStatus, PLCActionTestRequest, PLCActionTestResult

router = APIRouter(prefix="/plc", tags=["PLC Action Triggers"])


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_svc():
    from app.services.settings_persistence_service import SettingsPersistenceService
    return SettingsPersistenceService


def _get_dispatcher():
    from app.services.plc_dispatcher_service import PLCDispatcherService
    return PLCDispatcherService


@router.post("/opcua/scan", summary="Scan a private IPv4 subnet for OPC UA server endpoints")
async def scan_opcua_endpoints(
    request: OPCUAScanRequest,
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """Discover OPC UA endpoints in the requested private factory subnet."""
    from app.services.opcua_discovery_service import scan_opcua_servers

    try:
        return await scan_opcua_servers(request.target, request.port)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/actions", summary="List all PLC action cards with live execution status")
async def list_plc_actions(
    user: User = Depends(require_operator),
) -> List[Dict[str, Any]]:
    """Returns saved PLC action cards merged with their current live status."""
    svc = _get_svc()
    dispatcher = _get_dispatcher()
    cards = svc.get_plc_actions()

    # Merge live status from dispatcher
    statuses = {s["card_id"]: s for s in dispatcher.get_status()}
    for card in cards:
        cid = card.get("id", "")
        live = statuses.get(cid, {})
        card["runtime_status"] = live.get("status", "idle")
        card["last_result"]    = live.get("last_result", {})
        card["last_fired_at"]  = live.get("last_fired_at")
    return cards


@router.post("/actions", summary="Save (upsert) a PLC action card (Level 2+ Supervisor)")
async def save_plc_action(
    card_data: Dict[str, Any],
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """Create or update a PLC action card. The card ID must be provided."""
    if not card_data.get("id"):
        raise HTTPException(status_code=400, detail="Card 'id' is required")

    svc = _get_svc()
    cards = svc.get_plc_actions()

    existing = next((c for c in cards if c.get("id") == card_data["id"]), None)
    if existing:
        existing.update(card_data)
        saved = existing
    else:
        cards.append(card_data)
        saved = card_data

    try:
        saved_cards = svc.replace_plc_actions(cards)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved = next(card for card in saved_cards if card.get("id") == card_data["id"])

    svc.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="UPSERT_PLC_ACTION",
        category="PLC",
        details=f"Saved PLC action card '{card_data.get('name', card_data['id'])}'",
    )
    svc.save()

    # Hot-reload dispatcher
    _get_dispatcher().set_cards(saved_cards)
    return saved


@router.post("/actions/batch", summary="Replace entire PLC actions list")
async def save_plc_actions_batch(
    cards: List[Dict[str, Any]],
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """
    Replace all PLC action cards at once (used when the dashboard saves all cards together).
    """
    svc = _get_svc()
    try:
        saved_cards = svc.replace_plc_actions(cards)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    svc.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="SAVE_PLC_ACTIONS_BATCH",
        category="PLC",
        details=f"Saved {len(saved_cards)} PLC action card(s)",
    )
    svc.save()
    _get_dispatcher().set_cards(saved_cards)
    return {"saved": len(saved_cards)}


@router.delete("/actions/{card_id}", summary="Delete a PLC action card (Level 2+ Supervisor)")
async def delete_plc_action(
    card_id: str,
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    svc = _get_svc()
    cards = svc.get_plc_actions()
    deleted_card = next((c for c in cards if c.get("id") == card_id), None)
    if not deleted_card:
        raise HTTPException(status_code=404, detail=f"PLC action card '{card_id}' not found")

    saved_cards = svc.replace_plc_actions([c for c in cards if c.get("id") != card_id])

    svc.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="DELETE_PLC_ACTION",
        category="PLC",
        details=f"Deleted PLC action card '{card_id}'",
    )
    svc.save()

    # Invalidate the actual endpoint's pooled driver, not the card ID.
    from app.hardware.plc.factory import PLCDriverFactory
    if deleted_card and deleted_card.get("plc_endpoint_id"):
        PLCDriverFactory.invalidate(str(deleted_card["plc_endpoint_id"]))
    _get_dispatcher().set_cards(saved_cards)
    return {"status": "deleted", "card_id": card_id}


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/actions/{card_id}/status", summary="Live execution status for a PLC action card")
async def get_plc_action_status(
    card_id: str,
    user: User = Depends(require_operator),
) -> PLCActionStatus:
    dispatcher = _get_dispatcher()
    statuses = dispatcher.get_status(card_id=card_id)
    if not statuses:
        raise HTTPException(status_code=404, detail=f"PLC action card '{card_id}' not found")
    s = statuses[0]
    return PLCActionStatus(
        card_id=s["card_id"],
        name=s["name"],
        status=s["status"],
        last_result=s["last_result"],
        last_fired_at=s["last_fired_at"],
    )


# ── Manual Test ───────────────────────────────────────────────────────────────

@router.post("/actions/{card_id}/test", summary="Manually execute a PLC output (Test button)")
async def test_plc_action(
    card_id: str,
    req: PLCActionTestRequest,
    user: User = Depends(require_supervisor),
) -> PLCActionTestResult:
    """
    Manually fires the configured PLC operation for a card.
    Requires `confirm: true` in the request body to prevent accidental executions.
    This is logged as a manual test in the audit trail.
    """
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="Set 'confirm': true in the request body to execute the physical PLC output.",
        )

    svc = _get_svc()
    cards = svc.get_plc_actions()
    card = next((c for c in cards if c.get("id") == card_id), None)
    if not card and req.card:
        card = dict(req.card)
    if not card:
        card = next((c for c in _get_dispatcher()._cards if c.get("id") == card_id), None)
    if not card:
        raise HTTPException(status_code=404, detail=f"PLC action card '{card_id}' not found. Please save cards first.")

    # A card without a channel may only borrow one when exactly one enabled PLC
    # channel exists. Picking "the first" of several could fire an output on
    # the wrong machine.
    if not card.get("plc_endpoint_id"):
        enabled = [ep for ep in svc.get_endpoints(protocol="plc") if ep.get("enabled", True) is True]
        if len(enabled) != 1:
            raise HTTPException(
                status_code=400,
                detail="This card has no PLC channel. Select the PLC channel on the card before testing it.",
            )
        card["plc_endpoint_id"] = enabled[0]["id"]

    svc.record_audit(
        username=user.username,
        role=user.role,
        clearance_level=user.clearance_level,
        action="MANUAL_TEST_PLC_ACTION",
        category="PLC",
        details=f"Manual test of PLC card '{card.get('name', card_id)}' by {user.username}",
    )

    dispatcher = _get_dispatcher()
    result = await dispatcher.dispatch_manual(card)

    # Resolve endpoint name for the response
    ep_id = card.get("plc_endpoint_id", "")
    ep_name = None
    ep_proto = None
    if ep_id:
        endpoints = svc.get_endpoints(protocol="plc")
        ep = next((e for e in endpoints if e.get("id") == ep_id), None)
        if ep:
            ep_name = ep.get("name")
            ep_proto = ep.get("plc_sub_protocol")

    return PLCActionTestResult(
        card_id=card_id,
        success=result.get("success", False),
        message=result.get("message", ""),
        status=result.get("status") or ("sent" if result.get("success") else "failed"),
        endpoint_name=ep_name,
        protocol=ep_proto,
    )


# ── Hot-Reload ────────────────────────────────────────────────────────────────

@router.post("/actions/reload", summary="Hot-reload PLC action cards from persistence (Level 2+ Supervisor)")
async def reload_plc_actions(
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    """Re-reads plc_actions from disk and reloads the dispatcher — no server restart needed."""
    dispatcher = _get_dispatcher()
    dispatcher.load_cards()
    return {"status": "reloaded", "card_count": len(dispatcher._cards)}


# ── Driver Pool Diagnostics ───────────────────────────────────────────────────

@router.get("/drivers", summary="Show active PLC driver connection pool (Admin debug)")
async def list_plc_drivers(
    user: User = Depends(require_supervisor),
) -> Dict[str, Any]:
    from app.hardware.plc.factory import PLCDriverFactory
    return {
        "pooled_endpoint_ids": PLCDriverFactory.pool_ids(),
        "count": len(PLCDriverFactory.pool_ids()),
    }


# ── Fail-Safe ─────────────────────────────────────────────────────────────────

@router.get("/failsafe", summary="Fail-safe watchdog status per PLC endpoint")
async def get_plc_failsafe_status(
    user: User = Depends(require_operator),
) -> List[Dict[str, Any]]:
    """Safe-state outputs, pending safe-state writes and heartbeat health for opted-in endpoints."""
    from app.services.plc_failsafe_service import PLCFailsafeService
    return PLCFailsafeService.get_status()
