from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

pwd_context = CryptContext(schemes=["pbkdf2_sha256", "sha256_crypt"], deprecated="auto")

# ── Dynamic Server Boot Session Identifier ────────────────────────────────────
# Generated uniquely on each server process start. When the server restarts,
# all tokens issued in prior runs will be instantly rejected as invalid,
# cleanly logging out all accounts and resetting all sessions.
SERVER_BOOT_ID: str = secrets.token_hex(16)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False


def create_access_token(
    subject: str,
    clearance_level: int,
    role: str,
    expires_delta: Optional[timedelta] = None,
    session_id: Optional[str] = None,
) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode: Dict[str, Any] = {
        "sub": subject,
        "role": role,
        "clearance_level": clearance_level,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "boot_id": SERVER_BOOT_ID,  # Binds token strictly to current server instance
        "sid": session_id,          # Unique session ID — invalidated on forced re-login
    }

    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        # Invalidate any session token issued before the current server restart
        if payload.get("boot_id") != SERVER_BOOT_ID:
            logger.debug("Token boot_id mismatch (server was restarted): rejecting stale session")
            return None
        return payload
    except JWTError as exc:
        logger.debug("JWT decode error: %s", exc)
        return None
