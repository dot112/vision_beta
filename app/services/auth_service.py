from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models.user import User
from app.schemas.auth import ClearanceLevel, TokenResponse, UserCreate, UserResponse, UserRole
from app.utils.logger import get_logger

logger = get_logger(__name__)

# In-memory heartbeat tracker: { username: (last_seen_epoch, last_ip) }
_active_heartbeats: Dict[str, Tuple[float, Optional[str]]] = {}
# In-memory active single session tracker: { username: session_id }
_active_sessions: Dict[str, str] = {}
ONLINE_THRESHOLD_SECONDS = 45  # Users seen within 45s are marked ONLINE


def _relative_time_str(last_seen_epoch: Optional[float]) -> str:
    if not last_seen_epoch:
        return "Never"
    diff = int(time.time() - last_seen_epoch)
    if diff < 15:
        return "Just now"
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


class AuthService:
    @staticmethod
    def reset_all_sessions() -> None:
        """Resets all in-memory user sessions on server startup."""
        _active_heartbeats.clear()
        _active_sessions.clear()
        logger.info("All user sessions and online states reset on server startup")

    @staticmethod
    def touch_user(username: str, ip: Optional[str] = None) -> None:
        """Records user activity / heartbeat."""
        _active_heartbeats[username] = (time.time(), ip)

    @staticmethod
    def mark_offline(username: str) -> None:
        """Explicitly sets user to offline on logout and revokes active session."""
        if username in _active_heartbeats:
            del _active_heartbeats[username]
        if username in _active_sessions:
            del _active_sessions[username]

    @staticmethod
    def is_session_valid(username: str, session_id: Optional[str]) -> bool:
        """Verifies if the given session_id matches the user's current active session."""
        if not session_id:
            return True
        active_sid = _active_sessions.get(username)
        if not active_sid:
            return False
        return active_sid == session_id

    @staticmethod
    def is_user_online(username: str) -> bool:
        if username not in _active_heartbeats:
            return False
        last_epoch, _ = _active_heartbeats[username]
        return (time.time() - last_epoch) <= ONLINE_THRESHOLD_SECONDS

    @staticmethod
    async def authenticate_user(db: AsyncSession, username: str, password: str) -> Optional[User]:
        stmt = select(User).where(User.username == username)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            return None
        password_valid = await asyncio.to_thread(verify_password, password, user.hashed_password)
        if not password_valid or not user.is_active:
            return None

        return user

    @staticmethod
    async def record_login(db: AsyncSession, user: User) -> None:
        """Persist login activity after the existing-session check succeeds."""
        user.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        AuthService.touch_user(user.username)

        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.record_audit(
                username=user.username,
                role=user.role,
                clearance_level=user.clearance_level,
                action="USER_LOGIN",
                category="AUTH",
                details=f"User '{user.username}' logged into dashboard",
            )
        except Exception:
            pass

    @staticmethod
    async def change_password(db: AsyncSession, username: str, current_pwd: str, new_pwd: str) -> bool:
        """Allows any authenticated user (including Admin) to change their password."""
        stmt = select(User).where(User.username == username)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        if not await asyncio.to_thread(verify_password, current_pwd, user.hashed_password):
            raise ValueError("Current password is incorrect")

        user.hashed_password = await asyncio.to_thread(hash_password, new_pwd)
        await db.commit()
        logger.info("Password successfully changed for user '%s'", username)

        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.record_audit(
                username=user.username,
                role=user.role,
                clearance_level=user.clearance_level,
                action="CHANGE_PASSWORD",
                category="AUTH",
                details=f"Password updated for user '{username}'",
            )
        except Exception:
            pass

        return True

    @staticmethod
    async def create_user(db: AsyncSession, data: UserCreate, allow_admin_creation: bool = False) -> User:
        stmt = select(User).where(User.username == data.username)
        res = await db.execute(stmt)
        if res.scalar_one_or_none():
            raise ValueError(f"Username '{data.username}' already exists")

        role_str = data.role.value if hasattr(data.role, "value") else str(data.role)
        clearance = data.clearance_level.value if isinstance(data.clearance_level, ClearanceLevel) else int(data.clearance_level)

        # Enforce rule: Cannot create Level 3 Admin accounts through standard API
        if not allow_admin_creation and (role_str == "admin" or clearance >= 3):
            raise ValueError(
                "Cannot create Level 3 Administrator accounts. "
                "Only Level 1 (Operator) and Level 2 (Supervisor) accounts can be provisioned."
            )

        level_map = {
            UserRole.OPERATOR: 1,
            UserRole.SUPERVISOR: 2,
            UserRole.ADMIN: 3,
        }
        if isinstance(data.role, UserRole) and data.role in level_map:
            clearance = level_map[data.role]

        new_user = User(
            username=data.username,
            hashed_password=await asyncio.to_thread(hash_password, data.password),
            full_name=data.full_name,
            role=role_str,
            clearance_level=clearance,
            is_active=True,
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
        logger.info("Provisioned user '%s' [Tier %d: %s]", new_user.username, new_user.clearance_level, new_user.role)

        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.record_audit(
                username="admin",
                role="ADMIN",
                clearance_level=3,
                action="CREATE_USER",
                category="USERS",
                details=f"Created account '{new_user.username}' (Level {new_user.clearance_level}: {new_user.role.upper()})",
            )
        except Exception:
            pass

        return new_user

    @staticmethod
    async def get_all_users_with_status(db: AsyncSession) -> List[UserResponse]:
        stmt = select(User).order_by(User.clearance_level.desc(), User.created_at.asc())
        result = await db.execute(stmt)
        users = result.scalars().all()

        responses = []
        for u in users:
            online = AuthService.is_user_online(u.username)
            last_epoch = _active_heartbeats[u.username][0] if u.username in _active_heartbeats else None
            rel_time = _relative_time_str(last_epoch)

            resp = UserResponse(
                id=u.id,
                username=u.username,
                full_name=u.full_name,
                role=u.role,
                clearance_level=u.clearance_level,
                is_active=u.is_active,
                is_online=online,
                last_seen_at=u.last_seen_at,
                last_seen_relative=rel_time if online else ("Offline" if rel_time == "Never" else f"Offline ({rel_time})"),
                created_at=u.created_at,
            )
            responses.append(resp)

        return responses

    @staticmethod
    async def delete_user(db: AsyncSession, user_id: str) -> bool:
        stmt = select(User).where(User.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            return False

        if user.username == "admin" or user.clearance_level == 3:
            raise ValueError("Root administrator account cannot be deleted")

        username = user.username
        AuthService.mark_offline(username)
        await db.delete(user)
        await db.commit()
        logger.info("Admin deleted user id %s (%s)", user_id, username)

        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.record_audit(
                username="admin",
                role="ADMIN",
                clearance_level=3,
                action="DELETE_USER",
                category="USERS",
                details=f"Deleted user account '{username}'",
            )
        except Exception:
            pass

        return True

    @staticmethod
    async def update_clearance(
        db: AsyncSession, user_id: str, new_level: int, role: Optional[str] = None
    ) -> Optional[User]:
        stmt = select(User).where(User.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            return None

        if user.username == "admin" and new_level < 3:
            raise ValueError("Cannot demote the root administrator account")

        user.clearance_level = new_level
        if role:
            user.role = role
        await db.commit()
        await db.refresh(user)
        logger.info("Updated clearance for user '%s' to Level %d", user.username, new_level)
        return user

    @staticmethod
    def generate_token(user: User, session_id: Optional[str] = None) -> TokenResponse:
        import uuid
        sid = session_id or uuid.uuid4().hex
        _active_sessions[user.username] = sid
        token = create_access_token(
            subject=user.username,
            clearance_level=user.clearance_level,
            role=user.role,
            session_id=sid,
        )
        return TokenResponse(
            access_token=token,
            token_type="bearer",
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role=user.role,
            clearance_level=user.clearance_level,
            expires_in_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        )

    @staticmethod
    async def seed_default_users(db: AsyncSession) -> None:
        """
        Seeds the initial Administrator only when ADMIN_INITIAL_PASSWORD is configured.
        Deletes any legacy default operator/supervisor accounts so only admin exists initially.
        """
        stmt = select(User).where(User.username == "admin")
        res = await db.execute(stmt)
        admin_user = res.scalar_one_or_none()

        if admin_user is None:
            if not settings.ADMIN_INITIAL_PASSWORD:
                logger.error("No administrator account exists. Set ADMIN_INITIAL_PASSWORD to bootstrap access.")
                return
            logger.info("Seeding initial Administrator account from ADMIN_INITIAL_PASSWORD")
            await AuthService.create_user(
                db,
                UserCreate(
                    username="admin",
                    password=settings.ADMIN_INITIAL_PASSWORD,
                    full_name="Plant Administrator",
                    role=UserRole.ADMIN,
                    clearance_level=ClearanceLevel.ADMIN,
                ),
                allow_admin_creation=True,
            )
        elif await asyncio.to_thread(verify_password, "admin123", admin_user.hashed_password):
            if settings.ADMIN_INITIAL_PASSWORD:
                admin_user.hashed_password = await asyncio.to_thread(hash_password, settings.ADMIN_INITIAL_PASSWORD)
                admin_user.is_active = True
                await db.commit()
                logger.warning("Replaced the known default Administrator password with ADMIN_INITIAL_PASSWORD")
            else:
                admin_user.is_active = False
                await db.commit()
                logger.error("Disabled the known default Administrator password. Set ADMIN_INITIAL_PASSWORD to rotate it.")

        # Cleanup legacy demo accounts if they exist with default passwords
        for legacy_user in ["supervisor", "operator"]:
            stmt_leg = select(User).where(User.username == legacy_user)
            res_leg = await db.execute(stmt_leg)
            user_to_clean = res_leg.scalar_one_or_none()
            if user_to_clean:
                # Check if it has default password
                default_credentials_match = await asyncio.to_thread(
                    lambda: verify_password(f"{legacy_user}123", user_to_clean.hashed_password)
                    or verify_password("super123", user_to_clean.hashed_password)
                )
                if default_credentials_match:
                    logger.info("Removing legacy demo account '%s'...", legacy_user)
                    await db.delete(user_to_clean)
                    await db.commit()
