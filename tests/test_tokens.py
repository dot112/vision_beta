"""Which sign-in tokens the server accepts.

The tokens here are built by hand (standard JWT: base64url JSON header and
claims, HMAC signature), so the checks hold whichever JWT library signs them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import settings
from app.core import security


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token(claims: dict, *, alg: str = "HS256", key: str | None = None) -> str:
    head = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    signing_input = f"{head}.{body}".encode()
    if alg == "none":
        return f"{head}.{body}."
    digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[alg]
    sig = hmac.new((key or settings.SECRET_KEY).encode(), signing_input, digest).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _claims(**changes) -> dict:
    now = int(time.time())
    return {"sub": "operator_test", "role": "operator", "clearance_level": 1, "iat": now, "exp": now + 600,
            "boot_id": security.SERVER_BOOT_ID, "sid": "s1", **changes}


def test_a_standard_hs256_token_signed_with_the_server_secret_is_accepted():
    payload = security.decode_access_token(_token(_claims()))
    assert payload is not None and payload["sub"] == "operator_test" and payload["sid"] == "s1"


def test_a_token_signed_with_another_secret_is_refused():
    assert security.decode_access_token(_token(_claims(), key="another-secret-of-at-least-32-characters")) is None


def test_an_unsigned_token_is_refused():
    assert security.decode_access_token(_token(_claims(), alg="none")) is None


def test_a_token_signed_with_another_algorithm_than_the_configured_one_is_refused():
    assert settings.JWT_ALGORITHM == "HS256"
    assert security.decode_access_token(_token(_claims(), alg="HS512")) is None


def test_an_expired_token_is_refused():
    assert security.decode_access_token(_token(_claims(exp=int(time.time()) - 5))) is None


def test_a_token_of_an_earlier_server_start_is_refused():
    assert security.decode_access_token(_token(_claims(boot_id="an-earlier-start"))) is None


def test_a_token_the_server_makes_is_a_standard_hs256_jwt():
    token = security.create_access_token("operator_test", 1, "operator", session_id="s2")
    head, body, sig = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(head + "==")) ["alg"] == "HS256"
    expected = hmac.new(settings.SECRET_KEY.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert sig == _b64(expected)
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    assert claims["sub"] == "operator_test" and claims["sid"] == "s2" and claims["exp"] > time.time()
