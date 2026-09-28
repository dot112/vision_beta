from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RuleOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"


class RuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    condition_field: str = Field(..., description="Field to test: total_detections, defect_count, max_confidence, qr_data, class_name")
    operator: RuleOperator
    threshold_value: str = Field(..., description="Comparison value (e.g. '0', '0.80', 'scratch')")
    action_id: Optional[str] = Field(default=None, description="Optional Action ID to execute if rule matches")
    is_enabled: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    condition_field: Optional[str] = None
    operator: Optional[RuleOperator] = None
    threshold_value: Optional[str] = None
    action_id: Optional[str] = None
    is_enabled: Optional[bool] = None


class RuleResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    condition_field: str
    operator: RuleOperator
    threshold_value: str
    action_id: Optional[str] = None
    is_enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class RuleEvaluationResult(BaseModel):
    rule_id: str
    rule_name: str
    matched: bool
    actual_value: Any
    threshold_value: Any
    action_triggered: Optional[str] = None
