from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _cipher() -> Fernet:
    # Derive a dedicated Fernet key from the server secret using domain separation.
    material = hashlib.sha256(b"api-key-encryption:v1:" + settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_api_key(raw_key: str) -> str:
    return _cipher().encrypt(raw_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(encrypted_key: str) -> str:
    try:
        return _cipher().decrypt(encrypted_key.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ValueError("API key could not be decrypted with the current server secret") from exc
