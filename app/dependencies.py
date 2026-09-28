from __future__ import annotations

import hashlib
import hmac
from typing import AsyncGenerator, Callable, Optional
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.models.api_key import APIKey
from app.db.models.user import User
from app.db.session import AsyncSessionLocal
from app.security.api_key_scopes import API_KEY_SCOPES, required_api_key_scopes
from app.services.auth_service import AuthService

security_bearer = HTTPBearer(auto_error=False)


def _configured_endpoint_protocol(endpoint_id: str) -> Optional[str]:
    """Return the saved endpoint protocol; None means it could not be resolved safely."""
    try:
        from app.services.settings_persistence_service import SettingsPersistenceService

        endpoint = next(
            (item for item in SettingsPersistenceService.get_endpoints() if item.get("id") == endpoint_id),
            None,
        )
        return str(endpoint.get("protocol", "")).lower() if endpoint else None
    except Exception:
        return None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_current_user(
    request: Request,
    auth: Optional[HTTPAuthorizationCredentials] = Security(security_bearer),
    api_key_value: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Authenticates a dashboard JWT or a scoped X-API-Key and returns its owner.
    """
    if api_key_value:
        if auth and auth.credentials:
            raise HTTPException(status_code=400, detail="Send either a Bearer token or X-API-Key, not both")
        if len(api_key_value) > 128 or not api_key_value.startswith("ivk_"):
            raise HTTPException(status_code=401, detail="Invalid API key")
        key_digest = hashlib.sha256(api_key_value.encode("utf-8")).hexdigest()
        result = await db.execute(
            select(APIKey).where(APIKey.key_hash == key_digest, APIKey.revoked_at.is_(None))
        )
        api_key = result.scalar_one_or_none()
        if not api_key or not hmac.compare_digest(api_key.key_hash, key_digest):
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        now = datetime.now(timezone.utc)
        expires_at = api_key.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise HTTPException(status_code=401, detail="API key expired")

        user = await db.get(User, api_key.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="API key owner is unavailable")

        request.state.auth_method = "api_key"
        request.state.api_key_id = api_key.id
        request.state.api_key_scopes = set(api_key.scopes or [])
        required_scopes = required_api_key_scopes(request.method, request.url.path)
        if required_scopes is None:
            raise HTTPException(status_code=403, detail="API keys cannot access this endpoint")

        # Changing settings can also rewrite executable PLC action cards, so those
        # changes need their own explicit capability in addition to config access.
        if request.url.path.rstrip("/") == "/api/v1/system/settings" and request.method in {"POST", "PUT", "PATCH"}:
            try:
                body = await request.json()
            except Exception:
                body = {}
            action_trigger = body.get("action_trigger") if isinstance(body, dict) else None
            if (isinstance(body, dict) and "plc_actions" in body) or (
                isinstance(action_trigger, dict) and "plc_actions" in action_trigger
            ):
                required_scopes.add("plc:configure")

        endpoint_path = request.url.path.rstrip("/")
        endpoint_prefix = "/api/v1/system/endpoints"
        endpoint_id = None
        if endpoint_path.startswith(endpoint_prefix + "/"):
            endpoint_suffix = endpoint_path[len(endpoint_prefix) + 1:]
            if endpoint_suffix and "/" not in endpoint_suffix:
                endpoint_id = endpoint_suffix
        saved_protocol = (
            _configured_endpoint_protocol(endpoint_id)
            if endpoint_id and request.method in {"PUT", "DELETE"}
            else None
        )

        if endpoint_path.startswith(endpoint_prefix) and request.method in {"POST", "PUT"} and not endpoint_path.endswith("/test"):
            try:
                body = await request.json()
            except Exception:
                body = {}
            new_protocol = str(body.get("protocol", "")).lower() if isinstance(body, dict) else ""
            if new_protocol == "plc" or saved_protocol == "plc" or (endpoint_id and saved_protocol is None):
                required_scopes.add("plc:configure")

        if endpoint_id and request.method == "DELETE" and saved_protocol not in {"tcp", "mqtt", "ipcam", "webhook"}:
            # A missing endpoint or failed lookup stays conservative until the route returns 404.
            required_scopes.add("plc:configure")

        missing_scopes = sorted(required_scopes - request.state.api_key_scopes)
        if missing_scopes:
            raise HTTPException(
                status_code=403,
                detail="API key is missing required scope(s): " + ", ".join(missing_scopes),
            )
        if user.clearance_level < max(API_KEY_SCOPES[scope]["min_clearance"] for scope in required_scopes):
            raise HTTPException(status_code=403, detail="API key owner's clearance does not permit this operation")

        last_used_at = api_key.last_used_at
        if last_used_at is None or (now - (last_used_at.replace(tzinfo=timezone.utc) if last_used_at.tzinfo is None else last_used_at)).total_seconds() >= 60:
            api_key.last_used_at = now
        return user

    if not auth or not auth.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide Authorization: Bearer <token> or X-API-Key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(auth.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username: str = payload["sub"]
    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is deactivated")

    request.state.auth_method = "jwt"

    # Verify single active session
    token_sid = payload.get("sid")
    if token_sid and not AuthService.is_session_valid(user.username, token_sid):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SESSION_TERMINATED: This account was signed into from another location.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Touch live online heartbeat
    AuthService.touch_user(user.username)

    return user


def require_clearance(min_level: int) -> Callable:
    """
    Dependency factory to enforce 3-Level Access Clearance:
    - Level 1: Operator
    - Level 2: Supervisor
    - Level 3: Admin
    """
    async def _clearance_checker(request: Request, current_user: User = Depends(get_current_user)) -> User:
        if min_level >= 3 and getattr(request.state, "auth_method", "jwt") == "api_key":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API keys cannot administer users or API keys")
        if current_user.clearance_level < min_level:
            level_names = {1: "Operator (Level 1)", 2: "Supervisor (Level 2)", 3: "Administrator (Level 3)"}
            required_name = level_names.get(min_level, f"Level {min_level}")
            user_name = level_names.get(current_user.clearance_level, f"Level {current_user.clearance_level}")

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient clearance. Required: {required_name}. Your clearance: {user_name}.",
            )
        return current_user

    return _clearance_checker


require_operator = require_clearance(1)    # Level 1+ (Operator, Supervisor, Admin)
require_supervisor = require_clearance(2)  # Level 2+ (Supervisor, Admin)
require_admin = require_clearance(3)       # Level 3 (Admin only)
