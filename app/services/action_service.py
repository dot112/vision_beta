from __future__ import annotations

from typing import Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.action import Action
from app.engines.action_engine import ActionEngine
from app.schemas.actions import ActionCreate, ActionExecutionResult, ActionUpdate
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ActionService:
    @staticmethod
    async def list_actions(db: AsyncSession) -> List[Action]:
        stmt = select(Action).order_by(Action.created_at.desc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_action_by_id(db: AsyncSession, action_id: str) -> Optional[Action]:
        stmt = select(Action).where(Action.id == action_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def create_action(db: AsyncSession, data: ActionCreate) -> Action:
        action = Action(
            name=data.name,
            action_type=data.action_type.value if hasattr(data.action_type, "value") else str(data.action_type),
            target=data.target,
            payload=data.payload,
            is_enabled=data.is_enabled,
        )
        db.add(action)
        await db.commit()
        await db.refresh(action)
        return action

    @staticmethod
    async def update_action(db: AsyncSession, action_id: str, data: ActionUpdate) -> Optional[Action]:
        action = await ActionService.get_action_by_id(db, action_id)
        if not action:
            return None

        if data.name is not None:
            action.name = data.name
        if data.action_type is not None:
            action.action_type = data.action_type.value if hasattr(data.action_type, "value") else str(data.action_type)
        if data.target is not None:
            action.target = data.target
        if data.payload is not None:
            action.payload = data.payload
        if data.is_enabled is not None:
            action.is_enabled = data.is_enabled

        await db.commit()
        await db.refresh(action)
        return action

    @staticmethod
    async def delete_action(db: AsyncSession, action_id: str) -> bool:
        action = await ActionService.get_action_by_id(db, action_id)
        if not action:
            return False
        await db.delete(action)
        await db.commit()
        return True

    @staticmethod
    async def execute_action(db: AsyncSession, action_id: str, context: Optional[dict] = None) -> ActionExecutionResult:
        action = await ActionService.get_action_by_id(db, action_id)
        if not action:
            raise ValueError(f"Action {action_id} not found")
        return await ActionEngine.execute(action, context or {})
