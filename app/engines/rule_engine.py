from __future__ import annotations

from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.action import Action
from app.db.models.rule import InspectionRule
from app.engines.action_engine import ActionEngine
from app.schemas.rules import RuleEvaluationResult, RuleOperator
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _compare(actual: Any, op: str, threshold: str) -> bool:
    """Evaluates comparison operators with automatic type casting."""
    if actual is None:
        return False

    op_str = op.lower()

    # Try numeric comparison if possible
    try:
        act_num = float(actual)
        thresh_num = float(threshold)

        if op_str in (RuleOperator.EQ.value, "eq"):
            return act_num == thresh_num
        elif op_str in (RuleOperator.NEQ.value, "neq"):
            return act_num != thresh_num
        elif op_str in (RuleOperator.GT.value, "gt"):
            return act_num > thresh_num
        elif op_str in (RuleOperator.GTE.value, "gte"):
            return act_num >= thresh_num
        elif op_str in (RuleOperator.LT.value, "lt"):
            return act_num < thresh_num
        elif op_str in (RuleOperator.LTE.value, "lte"):
            return act_num <= thresh_num
    except (ValueError, TypeError):
        pass

    # String comparison
    act_s = str(actual).lower()
    thresh_s = str(threshold).lower()

    if op_str in (RuleOperator.EQ.value, "eq"):
        return act_s == thresh_s
    elif op_str in (RuleOperator.NEQ.value, "neq"):
        return act_s != thresh_s
    elif op_str in (RuleOperator.CONTAINS.value, "contains"):
        return thresh_s in act_s

    return False


class RuleEngine:
    """
    Sub-millisecond Rule Evaluation Engine mapping inspection outcomes to physical triggers.
    """

    @staticmethod
    async def evaluate_rules(
        rules: List[InspectionRule],
        actions_map: Dict[str, Action],
        context: Dict[str, Any],
    ) -> List[RuleEvaluationResult]:
        results: List[RuleEvaluationResult] = []

        for rule in rules:
            if not rule.is_enabled:
                continue

            actual_val = context.get(rule.condition_field)
            matched = _compare(actual_val, rule.operator, rule.threshold_value)
            action_msg = None

            if matched and rule.action_id and rule.action_id in actions_map:
                action = actions_map[rule.action_id]
                if action.is_enabled:
                    exec_res = await ActionEngine.execute(action, context)
                    action_msg = f"Executed {action.name}: {exec_res.message}"

            results.append(
                RuleEvaluationResult(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    matched=matched,
                    actual_value=actual_val,
                    threshold_value=rule.threshold_value,
                    action_triggered=action_msg,
                )
            )

        return results
