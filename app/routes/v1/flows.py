from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_db, require_supervisor
from app.db.models.flow import FlowDefinition
from app.schemas.flow import FlowCreate, FlowUpdate, FlowResponse, FlowTestInjectRequest
from app.engines.flow_engine import FlowEngine
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/flows", tags=["Visual Flow Engine"])


def _restore_flow_secrets(value, previous):
    from app.services.settings_persistence_service import _restore_redacted_url

    if isinstance(value, dict):
        old = previous if isinstance(previous, dict) else {}
        restored = {}
        for key, item in value.items():
            prior = old.get(key)
            if isinstance(item, str) and isinstance(prior, str) and (key in {"url", "source"} or key.endswith("_url")):
                restored[key] = _restore_redacted_url(item, prior)
            else:
                restored[key] = _restore_flow_secrets(item, prior)
        for key, item in old.items():
            if key.lower() in {
                "password", "headers", "header", "authorization", "token", "secret", "private_key",
                "api_key", "client_secret", "access_token", "refresh_token", "credentials",
            } and key not in restored:
                restored[key] = item
        return restored
    if isinstance(value, list):
        old_items = previous if isinstance(previous, list) else []
        old_by_id = {
            item.get("id"): item
            for item in old_items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        return [
            _restore_flow_secrets(
                item,
                old_by_id.get(item.get("id")) if isinstance(item, dict) and isinstance(item.get("id"), str)
                else old_items[index] if index < len(old_items) else None,
            )
            for index, item in enumerate(value)
        ]
    return value


def _public_flow(flow):
    from app.services.settings_persistence_service import SettingsPersistenceService

    return SettingsPersistenceService.redact_secrets(FlowResponse.model_validate(flow).model_dump())

@router.get("/endpoints-catalog", summary="List communication channels for flow node dropdowns")
async def get_endpoints_catalog() -> Dict[str, Any]:
    from app.services.settings_persistence_service import SettingsPersistenceService
    state = SettingsPersistenceService.get_state()
    return SettingsPersistenceService.redact_secrets({
        "endpoints": [
            {
                "id": ep.get("id"),
                "name": ep.get("name"),
                "protocol": str(ep.get("protocol", "")).lower(),
                "host": ep.get("host"),
                "port": ep.get("port"),
                "url": ep.get("url"),
                "topic": ep.get("topic"),
                "description": ep.get("description", ""),
                "enabled": ep.get("enabled", True),
            }
            for ep in state.get("communication_endpoints", [])
        ]
    })

@router.get("", response_model=List[FlowResponse], summary="List all flows")
async def list_flows(db: AsyncSession = Depends(get_db)) -> List[FlowDefinition]:
    res = await db.execute(select(FlowDefinition).order_by(FlowDefinition.created_at.desc()))
    return [_public_flow(flow) for flow in res.scalars().all()]

@router.get("/{flow_id}", response_model=FlowResponse, summary="Get flow by ID")
async def get_flow(flow_id: str, db: AsyncSession = Depends(get_db)) -> FlowDefinition:
    flow = await db.get(FlowDefinition, flow_id)
    if not flow: raise HTTPException(status_code=404, detail="Flow not found")
    return _public_flow(flow)

@router.post("", response_model=FlowResponse, summary="Create and deploy a new visual flow")
async def create_flow(body: FlowCreate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> FlowDefinition:
    flow = FlowDefinition(
        id=str(uuid.uuid4()), name=body.name, description=body.description or "",
        is_active=body.is_active,
        nodes=[n.model_dump() for n in body.nodes],
        links=[lnk.model_dump() for lnk in body.links],
    )
    db.add(flow); await db.commit(); await db.refresh(flow)
    if flow.is_active:
        await FlowEngine.get().load_flow({"id": flow.id, "name": flow.name, "is_active": True,
                                          "nodes": flow.nodes or [], "links": flow.links or []})
    logger.info("Flow created: '%s' (%s)", flow.name, flow.id)
    return _public_flow(flow)

@router.put("/{flow_id}", response_model=FlowResponse, summary="Update and redeploy a flow")
async def update_flow(flow_id: str, body: FlowUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> FlowDefinition:
    flow = await db.get(FlowDefinition, flow_id)
    if not flow: raise HTTPException(status_code=404, detail="Flow not found")
    if body.name is not None: flow.name = body.name
    if body.description is not None: flow.description = body.description
    if body.is_active is not None: flow.is_active = body.is_active
    if body.nodes is not None:
        incoming_nodes = [n.model_dump() for n in body.nodes]
        flow.nodes = _restore_flow_secrets(incoming_nodes, flow.nodes or [])
    if body.links is not None: flow.links = [lnk.model_dump() for lnk in body.links]
    flow.updated_at = datetime.now(timezone.utc)
    await db.commit(); await db.refresh(flow)
    engine = FlowEngine.get()
    await engine.unload_flow(flow_id)
    if flow.is_active:
        await engine.load_flow({"id": flow.id, "name": flow.name, "is_active": True,
                                 "nodes": flow.nodes or [], "links": flow.links or []})
    return _public_flow(flow)

@router.delete("/{flow_id}", summary="Delete a flow")
async def delete_flow(flow_id: str, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> Dict[str, str]:
    flow = await db.get(FlowDefinition, flow_id)
    if not flow: raise HTTPException(status_code=404, detail="Flow not found")
    await db.delete(flow); await db.commit()
    await FlowEngine.get().unload_flow(flow_id)
    return {"status": "deleted", "flow_id": flow_id}

@router.post("/{flow_id}/deploy", summary="Toggle active/inactive for a flow")
async def deploy_flow(flow_id: str, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> Dict[str, Any]:
    flow = await db.get(FlowDefinition, flow_id)
    if not flow: raise HTTPException(status_code=404, detail="Flow not found")
    flow.is_active = not flow.is_active
    await db.commit()
    engine = FlowEngine.get()
    await engine.unload_flow(flow_id)
    if flow.is_active:
        await engine.load_flow({"id": flow.id, "name": flow.name, "is_active": True,
                                 "nodes": flow.nodes or [], "links": flow.links or []})
    return {"flow_id": flow_id, "is_active": flow.is_active}

@router.post("/{flow_id}/test", summary="Inject a simulated test event through a specific flow")
async def test_inject(flow_id: str, body: FlowTestInjectRequest, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> Dict[str, Any]:
    flow = await db.get(FlowDefinition, flow_id)
    if not flow: raise HTTPException(status_code=404, detail="Flow not found")
    engine = FlowEngine.get()
    await engine.load_flow({"id": flow.id, "name": flow.name, "is_active": True,
                             "nodes": flow.nodes or [], "links": flow.links or []})
    payload = dict(body.payload)
    payload.setdefault("class_name", "test_class")
    payload.setdefault("confidence", 0.95)
    payload.setdefault("is_defect", True)
    payload.setdefault("line_index", 1)
    payload.setdefault("track_id", "test-001")
    accepted = await engine.dispatch_test({
        "id": flow.id,
        "name": flow.name,
        "is_active": True,
        "nodes": flow.nodes or [],
        "links": flow.links or [],
    }, body.event_type, payload)
    if not accepted:
        raise HTTPException(status_code=429, detail="Flow execution capacity is full")
    return {"status": "injected", "event_type": body.event_type, "flow_id": flow_id, "payload": payload}
