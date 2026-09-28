from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_key import APIKey
from app.db.models.user import User
from app.core.api_key_crypto import encrypt_api_key
from app.dependencies import get_current_user, get_db, require_admin
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    TokenResponse,
    UserCreate,
    UserResponse,
    UserUpdateClearance,
)
from app.schemas.api_key import APIKeyCreated, APIKeyCreateRequest, APIKeyView
from app.security.api_key_scopes import API_KEY_SCOPES
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["3-Level Access Clearance & User Management"])
_login_attempts: Dict[str, tuple[int, float]] = {}
_login_attempt_lock = threading.Lock()
_session_login_lock = asyncio.Lock()
_LOGIN_WINDOW_SECONDS = 300
_MAX_LOGIN_FAILURES = 8


@router.post("/login", response_model=TokenResponse, summary="Login and obtain JWT token with Clearance Level")
async def login(
    req: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Authenticate with username & password to receive an Access Token and mark account ONLINE.
    """
    client_ip = request.client.host if request.client else "unknown"
    attempt_key = f"{client_ip}:{req.username.strip().lower()}"
    now = time.monotonic()
    with _login_attempt_lock:
        failures, started = _login_attempts.get(attempt_key, (0, now))
        if now - started >= _LOGIN_WINDOW_SECONDS:
            failures, started = 0, now
        if failures >= _MAX_LOGIN_FAILURES:
            raise HTTPException(status_code=429, detail="Too many failed login attempts; try again later")
        if len(_login_attempts) >= 10_000:
            expired_keys = [
                key for key, (_, window_start) in _login_attempts.items()
                if now - window_start >= _LOGIN_WINDOW_SECONDS
            ]
            for key in expired_keys:
                _login_attempts.pop(key, None)
            if len(_login_attempts) >= 10_000:
                oldest_key = min(_login_attempts, key=lambda key: _login_attempts[key][1])
                _login_attempts.pop(oldest_key, None)

    user = await AuthService.authenticate_user(db, req.username, req.password)
    if not user:
        with _login_attempt_lock:
            failures, started = _login_attempts.get(attempt_key, (0, now))
            if now - started >= _LOGIN_WINDOW_SECONDS:
                failures, started = 0, now
            _login_attempts[attempt_key] = (failures + 1, started)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with _login_attempt_lock:
        _login_attempts.pop(attempt_key, None)
    # Serialize the active-session check and creation so simultaneous logins
    # cannot both pass before either heartbeat is recorded.
    async with _session_login_lock:
        if not req.force and AuthService.is_user_online(user.username):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ACCOUNT_ALREADY_LOGGED_IN")
        await AuthService.record_login(db, user)
        return AuthService.generate_token(user)


@router.get("/me", response_model=UserResponse, summary="Get current user clearance profile")
async def get_current_user_profile(user: User = Depends(get_current_user)) -> UserResponse:
    """Returns the authenticated user's details, role, and clearance level."""
    return UserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        clearance_level=user.clearance_level,
        is_active=user.is_active,
        is_online=True,
        last_seen_at=user.last_seen_at,
        last_seen_relative="Just now",
        created_at=user.created_at,
    )


@router.post("/change-password", summary="Change password for current authenticated user (including Admin)")
async def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Allows any logged-in user (including Admin) to update their password.
    Requires verifying the current password first.
    """
    try:
        await AuthService.change_password(db, user.username, req.current_password, req.new_password)
        return {"status": "success", "message": f"Password updated successfully for user '{user.username}'"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/heartbeat", summary="Keep-alive heartbeat ping to maintain ONLINE state")
async def send_heartbeat(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Sent periodically by active dashboards to indicate the user is currently online."""
    AuthService.touch_user(user.username)
    return {"status": "ok", "user": user.username, "online": True}


@router.post("/logout", summary="Logout and set user state to OFFLINE")
async def logout(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Marks user offline and invalidates local session."""
    AuthService.mark_offline(user.username)
    try:
        from app.services.settings_persistence_service import SettingsPersistenceService
        SettingsPersistenceService.record_audit(
            username=user.username,
            role=user.role,
            clearance_level=user.clearance_level,
            action="USER_LOGOUT",
            category="AUTH",
            details=f"User '{user.username}' logged out of dashboard",
        )
    except Exception:
        pass
    return {"status": "logged_out", "user": user.username, "online": False}


# ── Level 3 Admin User Management ─────────────────────────────────────────────

@router.get("/users", response_model=List[UserResponse], summary="List all users with Real-time Online/Offline Status (Level 3 Admin only)")
async def list_users_with_status(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> List[UserResponse]:
    """
    [Level 3 Admin Clearance Required]
    Returns all registered accounts in the system with real-time Online/Offline status,
    last seen timestamps, role, and clearance tier.
    """
    return await AuthService.get_all_users_with_status(db)


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED, summary="Provision new Operator or Supervisor account (Level 3 Admin only)")
async def create_user(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    """
    [Level 3 Admin Clearance Required]
    Allows administrators to provision new accounts with username, password,
    and assigned clearance level (Level 1: Operator or Level 2: Supervisor).
    Note: Level 3 Admin accounts cannot be created via API.
    """
    try:
        new_u = await AuthService.create_user(db, user_in, allow_admin_creation=False)
        return UserResponse(
            id=new_u.id,
            username=new_u.username,
            full_name=new_u.full_name,
            role=new_u.role,
            clearance_level=new_u.clearance_level,
            is_active=new_u.is_active,
            is_online=False,
            last_seen_at=None,
            last_seen_relative="Never",
            created_at=new_u.created_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/users/{user_id}", summary="Delete an account (Level 3 Admin only)")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Dict[str, Any]:
    """
    [Level 3 Admin Clearance Required]
    Permanently removes a user account from the system.
    """
    try:
        await db.execute(delete(APIKey).where(APIKey.user_id == user_id))
        success = await AuthService.delete_user(db, user_id)
        if not success:
            raise HTTPException(status_code=404, detail="User not found")
        return {"status": "deleted", "user_id": user_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _api_key_view(api_key: APIKey, username: str) -> APIKeyView:
    return APIKeyView(
        id=api_key.id,
        user_id=api_key.user_id,
        username=username,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        scopes=list(api_key.scopes or []),
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
    )


@router.get("/api-keys", response_model=List[APIKeyView], summary="List integration API keys (Level 3 Admin only)")
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> List[APIKeyView]:
    result = await db.execute(
        select(APIKey, User.username)
        .join(User, User.id == APIKey.user_id)
        .order_by(APIKey.created_at.desc())
    )
    return [_api_key_view(api_key, username) for api_key, username in result.all()]


@router.post("/api-keys", response_model=APIKeyCreated, status_code=status.HTTP_201_CREATED, summary="Create a scoped integration API key (Level 3 Admin only)")
async def create_api_key(
    body: APIKeyCreateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> APIKeyCreated:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="API key name cannot be blank")
    scopes = set(body.scopes)
    unknown = scopes - set(API_KEY_SCOPES)
    if unknown:
        raise HTTPException(status_code=422, detail="Unknown API key scope(s): " + ", ".join(sorted(unknown)))
    if len(scopes) != len(body.scopes):
        raise HTTPException(status_code=422, detail="Duplicate API key scopes are not allowed")

    owner = await db.get(User, body.user_id)
    if not owner or not owner.is_active:
        raise HTTPException(status_code=404, detail="Active API key owner account not found")
    required_clearance = max(API_KEY_SCOPES[scope]["min_clearance"] for scope in scopes)
    if owner.clearance_level < required_clearance:
        raise HTTPException(status_code=422, detail="Selected account does not have enough clearance for these scopes")

    raw_key = "ivk_" + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=body.expires_in_days)
    api_key = APIKey(
        user_id=owner.id,
        name=name,
        key_prefix=raw_key[:12],
        key_hash=hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
        encrypted_key=encrypt_api_key(raw_key),
        scopes=sorted(scopes),
        created_by_user_id=admin.id,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.flush()

    try:
        from app.services.settings_persistence_service import SettingsPersistenceService
        SettingsPersistenceService.record_audit(
            username=admin.username,
            role=admin.role,
            clearance_level=admin.clearance_level,
            action="CREATE_API_KEY",
            category="AUTH",
            details=f"Created API key '{name}' for '{owner.username}' with scopes: {', '.join(sorted(scopes))}",
        )
    except Exception:
        pass

    return APIKeyCreated(**_api_key_view(api_key, owner.username).model_dump(), api_key=raw_key)


@router.get("/api-keys/{api_key_id}/secret", summary="Reveal an integration API key (Level 3 Admin only)")
async def reveal_api_key(
    api_key_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> Dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    api_key = await db.get(APIKey, api_key_id)
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    if not api_key.encrypted_key:
        raise HTTPException(
            status_code=410,
            detail="This key was created before secure reveal was available. Create a replacement key to view its value.",
        )
    try:
        from app.core.api_key_crypto import decrypt_api_key
        raw_key = decrypt_api_key(api_key.encrypted_key)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Could not decrypt this API key with the current server secret") from exc
    try:
        from app.services.settings_persistence_service import SettingsPersistenceService
        SettingsPersistenceService.record_audit(
            username=_admin.username,
            role=_admin.role,
            clearance_level=_admin.clearance_level,
            action="REVEAL_API_KEY",
            category="AUTH",
            details=f"Revealed API key '{api_key.name}' ({api_key.key_prefix})",
        )
    except Exception:
        pass
    return {"api_key": raw_key}


@router.delete("/api-keys/{api_key_id}", summary="Revoke an integration API key (Level 3 Admin only)")
async def revoke_api_key(
    api_key_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Dict[str, Any]:
    api_key = await db.get(APIKey, api_key_id)
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(timezone.utc)
        await db.flush()
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.record_audit(
                username=admin.username,
                role=admin.role,
                clearance_level=admin.clearance_level,
                action="REVOKE_API_KEY",
                category="AUTH",
                details=f"Revoked API key '{api_key.name}' ({api_key.key_prefix})",
            )
        except Exception:
            pass
    return {"status": "revoked", "id": api_key_id}


@router.put("/users/{user_id}/clearance", response_model=UserResponse, summary="Modify user clearance level (Level 3 Admin only)")
async def update_user_clearance(
    user_id: str,
    data: UserUpdateClearance,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    """[Level 3 Admin Clearance Required] Promote or demote user clearance level (Level 1 or Level 2)."""
    try:
        role_str = data.role.value if data.role else None
        updated = await AuthService.update_clearance(db, user_id, data.clearance_level.value, role_str)
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse(
            id=updated.id,
            username=updated.username,
            full_name=updated.full_name,
            role=updated.role,
            clearance_level=updated.clearance_level,
            is_active=updated.is_active,
            is_online=AuthService.is_user_online(updated.username),
            last_seen_at=updated.last_seen_at,
            last_seen_relative="Active",
            created_at=updated.created_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
