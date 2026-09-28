from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class FlowNode(BaseModel):
    id: str = Field(...)
    type: str = Field(...)
    label: str = Field(default="")
    x: float = Field(default=100.0)
    y: float = Field(default=100.0)
    config: Dict[str, Any] = Field(default_factory=dict)

class FlowLink(BaseModel):
    id: Optional[str] = Field(default=None)
    from_node: str = Field(...)
    from_port: str = Field(default="out")
    to_node: str = Field(...)
    to_port: str = Field(default="in")

class FlowCreate(BaseModel):
    name: str = Field(...)
    description: Optional[str] = Field(default="")
    is_active: bool = Field(default=True)
    nodes: List[FlowNode] = Field(default_factory=list)
    links: List[FlowLink] = Field(default_factory=list)

class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    nodes: Optional[List[FlowNode]] = None
    links: Optional[List[FlowLink]] = None

class FlowResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""
    is_active: bool = True
    nodes: List[FlowNode] = Field(default_factory=list)
    links: List[FlowLink] = Field(default_factory=list)
    execution_count: int = 0
    last_executed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    class Config:
        from_attributes = True

class FlowTestInjectRequest(BaseModel):
    event_type: str = Field(default="wireline_cross")
    payload: Dict[str, Any] = Field(default_factory=dict)

class FlowExecutionLog(BaseModel):
    flow_id: str
    node_id: str
    node_type: str
    status: Literal["passed", "blocked", "executed", "error"]
    message: str
    timestamp: datetime
