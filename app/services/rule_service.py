from __future__ import annotations

from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.action import Action
from app.db.models.rule import InspectionRule
from app.engines.rule_engine import RuleEngine
from app.schemas.rules import RuleCreate, RuleEvaluationResult, RuleUpdate
from app.utils.logger import get_logger

logger = get_logger(__name__)


class RuleService:
    @staticmethod
    async def list_rules(db: AsyncSession) -> List[InspectionRule]:
        stmt = select(InspectionRule).order_by(InspectionRule.created_at.desc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_rule_by_id(db: AsyncSession, rule_id: str) -> Optional[InspectionRule]:
        stmt = select(InspectionRule).where(InspectionRule.id == rule_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def create_rule(db: AsyncSession, data: RuleCreate) -> InspectionRule:
        rule = InspectionRule(
            name=data.name,
            description=data.description,
            condition_field=data.condition_field,
            operator=data.operator.value if hasattr(data.operator, "value") else str(data.operator),
            threshold_value=data.threshold_value,
            action_id=data.action_id,
            is_enabled=data.is_enabled,
        )
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def update_rule(db: AsyncSession, rule_id: str, data: RuleUpdate) -> Optional[InspectionRule]:
        rule = await RuleService.get_rule_by_id(db, rule_id)
        if not rule:
            return None

        if data.name is not None:
            rule.name = data.name
        if data.description is not None:
            rule.description = data.description
        if data.condition_field is not None:
            rule.condition_field = data.condition_field
        if data.operator is not None:
            rule.operator = data.operator.value if hasattr(data.operator, "value") else str(data.operator)
        if data.threshold_value is not None:
            rule.threshold_value = data.threshold_value
        if data.action_id is not None:
            rule.action_id = data.action_id
        if data.is_enabled is not None:
            rule.is_enabled = data.is_enabled

        await db.commit()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def delete_rule(db: AsyncSession, rule_id: str) -> bool:
        rule = await RuleService.get_rule_by_id(db, rule_id)
        if not rule:
            return False
        await db.delete(rule)
        await db.commit()
        return True

    @staticmethod
    async def evaluate_all(db: AsyncSession, context: Dict[str, Any]) -> List[RuleEvaluationResult]:
        rules = await RuleService.list_rules(db)
        actions_res = await db.execute(select(Action))
        actions_map = {a.id: a for a in actions_res.scalars().all()}
        return await RuleEngine.evaluate_rules(rules, actions_map, context)

