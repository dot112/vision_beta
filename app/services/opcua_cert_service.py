"""
Certificates for secured OPC UA channels
========================================
A controller channel on OPC UA with a Security Policy other than None signs
(and encrypts) its messages with a client certificate and its private key.
They are uploaded on the Connections page, kept in ``certs/opcua`` on the
server, and a channel names the files it uses.

Every upload is checked before it is kept: a certificate must be an X.509
certificate, a private key must load without a password. The file is stored
under the extension its format needs: the OPC UA library reads ``.pem`` files
as text (PEM) and every other file as binary (DER), whatever is inside.
A private key is never sent back to a client.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List

OPCUA_CERTS_DIR = str(Path(__file__).resolve().parents[2] / "certs" / "opcua")
KINDS = ("client_cert", "private_key", "server_cert")
MAX_BYTES = 256 * 1024
_KEY_SUFFIXES = (".key.pem", ".key.der")
_KNOWN_SUFFIXES = (*_KEY_SUFFIXES, ".pem", ".der", ".crt", ".cer", ".key", ".ca")


def _is_pem(data: bytes) -> bool:
    return data.lstrip().startswith(b"-----BEGIN")


def _load_certificate(data: bytes) -> Any:
    from cryptography import x509

    try:
        return x509.load_pem_x509_certificate(data) if _is_pem(data) else x509.load_der_x509_certificate(data)
    except Exception as exc:
        raise ValueError("This file is not a certificate (X.509, as PEM or DER)") from exc


def _load_key(data: bytes) -> Any:
    from cryptography.hazmat.primitives import serialization

    loader = serialization.load_pem_private_key if _is_pem(data) else serialization.load_der_private_key
    try:
        return loader(data, password=None)
    except TypeError as exc:
        raise ValueError("This private key is protected by a password. Save it without a password and upload it again") from exc
    except Exception as exc:
        raise ValueError("This file is not a private key (PEM or DER)") from exc


def describe_certificate(data: bytes) -> Dict[str, str]:
    """A certificate's format ("pem" or "der"), subject, expiry and application URI. ValueError when it is none."""
    from cryptography import x509

    cert = _load_certificate(data)
    uri = ""
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = names.get_values_for_type(x509.UniformResourceIdentifier)
        uri = uris[0] if uris else ""
    except x509.ExtensionNotFound:
        pass
    expires = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {"format": "pem" if _is_pem(data) else "der", "subject": cert.subject.rfc4514_string(),
            "expires": expires.isoformat(), "application_uri": uri}


def _safe_stem(filename: str) -> str:
    """The upload's own name without folders, extension or odd characters."""
    stem = re.split(r"[\\/]", filename or "")[-1]
    for suffix in _KNOWN_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file")[:64]


def store(kind: str, filename: str, data: bytes) -> Dict[str, Any]:
    """Check and keep one uploaded file. Returns its stored ``filename``, ``kind``, ``format`` and,
    for a certificate, ``subject``, ``expires`` and ``application_uri``. ValueError says what is wrong."""
    if kind not in KINDS:
        raise ValueError("The kind of file must be 'client_cert', 'private_key' or 'server_cert'")
    if not data:
        raise ValueError("The file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"The file is too large (at most {MAX_BYTES // 1024} KB)")
    if kind == "private_key":
        _load_key(data)
        info: Dict[str, Any] = {"format": "pem" if _is_pem(data) else "der"}
        name = f"{_safe_stem(filename)}.key.{info['format']}"
    else:
        info = describe_certificate(data)
        name = f"{_safe_stem(filename)}.{info['format']}"
    os.makedirs(OPCUA_CERTS_DIR, exist_ok=True)
    path = os.path.join(OPCUA_CERTS_DIR, name)
    with open(path, "wb") as handle:
        handle.write(data)
    if kind == "private_key":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return {"filename": name, "kind": kind, **info}


def list_files() -> Dict[str, List[str]]:
    """The uploaded files by what they are: ``certificates``, ``private_keys`` and ``all_files``."""
    files: List[str] = []
    if os.path.isdir(OPCUA_CERTS_DIR):
        files = sorted(name for name in os.listdir(OPCUA_CERTS_DIR)
                       if name.lower().endswith((".pem", ".der")) and os.path.isfile(os.path.join(OPCUA_CERTS_DIR, name)))
    keys = [name for name in files if name.lower().endswith(_KEY_SUFFIXES)]
    return {"certificates": [name for name in files if name not in keys], "private_keys": keys, "all_files": files}


def resolve(filename: Any) -> str:
    """The path of an uploaded file by its name; "" for no file. ValueError when it is not an uploaded file."""
    name = str(filename or "").strip()
    if not name:
        return ""
    if name in (".", "..") or name != os.path.basename(name) or "/" in name or "\\" in name:
        raise ValueError(f"'{name}' is not the name of an uploaded file")
    path = os.path.join(OPCUA_CERTS_DIR, name)
    if not os.path.isfile(path):
        raise ValueError(f"The file '{name}' has not been uploaded")
    return path


def check_pair(cert_path: str, key_path: str) -> None:
    """ValueError when the private key is not the one of the certificate."""
    from cryptography.hazmat.primitives import serialization

    def public(key: Any) -> bytes:
        return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    with open(cert_path, "rb") as handle:
        cert = _load_certificate(handle.read())
    with open(key_path, "rb") as handle:
        key = _load_key(handle.read())
    if public(cert.public_key()) != public(key.public_key()):
        raise ValueError(f"The private key '{os.path.basename(key_path)}' does not belong to the certificate "
                         f"'{os.path.basename(cert_path)}'. Choose the key that was made with this certificate")
