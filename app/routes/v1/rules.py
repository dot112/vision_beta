from __future__ import annotations

from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_supervisor
from app.schemas.rules import RuleCreate, RuleEvaluationResult, RuleResponse, RuleUpdate
from app.services.rule_service import RuleService

router = APIRouter(prefix="/rules", tags=["Inspection Rules"])


@router.get("", response_model=List[RuleResponse], summary="List all inspection rules")
async def list_rules(db: AsyncSession = Depends(get_db)) -> List[RuleResponse]:
    return await RuleService.list_rules(db)


@router.post("", response_model=RuleResponse, status_code=status.HTTP_201_CREATED, summary="Create inspection rule")
async def create_rule(rule_in: RuleCreate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> RuleResponse:
    """
    Create rule linking inspection parameters to actuator actions:
    - `condition_field`: `total_detections`, `defect_count`, `max_confidence`, `qr_data`, `class_name`
    - `operator`: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `contains`
    - `threshold_value`: `0`, `0.75`, `defect`, `BATCH_A`
    - `action_id`: Optional action UUID to trigger on match
    """
    return await RuleService.create_rule(db, rule_in)


@router.get("/{rule_id}", response_model=RuleResponse, summary="Get rule details")
async def get_rule(rule_id: str, db: AsyncSession = Depends(get_db)) -> RuleResponse:
    rule = await RuleService.get_rule_by_id(db, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.put("/{rule_id}", response_model=RuleResponse, summary="Update rule")
async def update_rule(rule_id: str, rule_in: RuleUpdate, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> RuleResponse:
    rule = await RuleService.update_rule(db, rule_id, rule_in)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.delete("/{rule_id}", summary="Delete rule")
async def delete_rule(rule_id: str, db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> dict:
    success = await RuleService.delete_rule(db, rule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"detail": f"Rule {rule_id} deleted"}


@router.post("/evaluate", response_model=List[RuleEvaluationResult], summary="Test-evaluate rules against sample context")
async def evaluate_rules(context: Dict[str, Any], db: AsyncSession = Depends(get_db), _user=Depends(require_supervisor)) -> List[RuleEvaluationResult]:
    """Test what rules would trigger with a sample payload like `{"defect_count": 2, "max_confidence": 0.85}`."""
    return await RuleService.evaluate_all(db, context)
