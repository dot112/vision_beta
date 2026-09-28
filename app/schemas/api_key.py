from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel, Field


class APIKeyCreateRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=36)
    name: str = Field(..., min_length=1, max_length=100)
    scopes: List[str] = Field(..., min_length=1, max_length=20)
    expires_in_days: int = Field(default=90, ge=1, le=365)


class APIKeyView(BaseModel):
    id: str
    user_id: str
    username: str
    name: str
    key_prefix: str
    scopes: List[str]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class APIKeyCreated(APIKeyView):
    # List endpoints never return the secret; reveal uses a separate admin-only endpoint.
    api_key: str
