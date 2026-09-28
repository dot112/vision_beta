from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class ClearanceLevel(int, Enum):
    OPERATOR = 1      # Level 1: View streams, basic stats, manual inspection trigger
    SUPERVISOR = 2    # Level 2: Configure wirelines, reset counters, activate models, rules & actions
    ADMIN = 3         # Level 3: User provisioning, hardware management, model uploads, MQTT certs


class UserRole(str, Enum):
    OPERATOR = "operator"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    full_name: Optional[str] = None
    role: UserRole = Field(default=UserRole.OPERATOR)
    clearance_level: ClearanceLevel = Field(default=ClearanceLevel.OPERATOR)
    is_active: bool = True


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, max_length=256, description="Plaintext password (6–256 characters)")


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256, description="Current password")
    new_password: str = Field(..., min_length=6, max_length=256, description="New password (6–256 characters)")


class UserUpdateClearance(BaseModel):
    clearance_level: ClearanceLevel
    role: Optional[UserRole] = None


class UserResponse(UserBase):
    id: str
    is_online: bool = False
    last_seen_at: Optional[datetime] = None
    last_seen_relative: str = "Never"
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=1, max_length=256)
    force: bool = False  # Set True to forcibly disconnect any existing active session


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    full_name: Optional[str] = None
    role: str
    clearance_level: int
    expires_in_minutes: int
