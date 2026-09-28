from __future__ import annotations

from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_supervisor
from app.schemas.actions import ActionCreate, ActionExecutionResult, ActionResponse, ActionUpdate
from app.services.action_service import ActionService

router = APIRouter(prefix="/actions", tags=["Actuation Actions"])


@router.get("", response_model=List[ActionResponse], summary="List all configured actions")
async def list_actions(db: AsyncSession = Depends(get_db)) -> List[ActionResponse]:
    return await ActionService.list_actions(db)


@router.post("", response_model=ActionResponse, status_code=status.HTTP_201_CREATED, summary="Create new actuator action")
async def create_action(action_in: ActionCreate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> ActionResponse:
    """
    Configure an industrial trigger:
    - **MODBUS_COIL**: `target="0"`, `payload={"value": true}`
    - **MODBUS_REGISTER**: `target="40001"`, `payload={"value": 100}`
    - **MQTT_PUBLISH**: `target="factory/line1/alerts"`, `payload={"alert": "DEFECT"}`
    - **WEBHOOK**: `target="http://mes-server.local/api/reject"`, `payload={}`
    """
    return await ActionService.create_action(db, action_in)


@router.get("/{action_id}", response_model=ActionResponse, summary="Get action details")
async def get_action(action_id: str, db: AsyncSession = Depends(get_db)) -> ActionResponse:
    act = await ActionService.get_action_by_id(db, action_id)
    if not act:
        raise HTTPException(status_code=404, detail="Action not found")
    return act


@router.put("/{action_id}", response_model=ActionResponse, summary="Update action settings")
async def update_action(action_id: str, action_in: ActionUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> ActionResponse:
    act = await ActionService.update_action(db, action_id, action_in)
    if not act:
        raise HTTPException(status_code=404, detail="Action not found")
    return act


@router.delete("/{action_id}", summary="Delete action")
async def delete_action(action_id: str, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> dict:
    success = await ActionService.delete_action(db, action_id)
    if not success:
        raise HTTPException(status_code=404, detail="Action not found")
    return {"detail": f"Action {action_id} deleted"}


@router.post("/{action_id}/execute", response_model=ActionExecutionResult, summary="Manually trigger an action")
async def execute_action(action_id: str, payload: Optional[Dict] = None, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> ActionExecutionResult:
    try:
        return await ActionService.execute_action(db, action_id, payload or {})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
