"""OPC UA secured channels: uploading the client certificate and its private key.

A controller channel on OPC UA with a Security Policy other than None needs a
client certificate and private key. They are uploaded on the Connections page,
kept on the server, and chosen on the channel.
"""
from __future__ import annotations

import datetime
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.services import opcua_cert_service
from app.services.opcua_cert_service import list_files, resolve, store
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)

API = "/api/v1/plc/opcua/certs"


def _pair(name="vision-client", pem=True, password=None):
    """A self-signed client certificate and its private key, as file contents."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier("urn:vision:client")]), critical=False)
            .sign(key, hashes.SHA256()))
    encoding = serialization.Encoding.PEM if pem else serialization.Encoding.DER
    protection = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return (cert.public_bytes(encoding),
            key.private_bytes(encoding, serialization.PrivateFormat.PKCS8, protection))


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The certificates go to a throwaway folder."""
    monkeypatch.setattr(opcua_cert_service, "OPCUA_CERTS_DIR", str(tmp_path / "opcua"))
    return tmp_path / "opcua"


# ── Storing a file ────────────────────────────────────────────────────────────


def test_a_certificate_is_stored_under_the_extension_its_format_needs(folder):
    cert, _ = _pair()
    info = store("client_cert", "My Client.crt", cert)          # PEM text in a .crt file
    # The OPC UA library reads the format from the extension: .pem is text, anything else is binary.
    assert info["filename"] == "My_Client.pem" and info["format"] == "pem" and info["kind"] == "client_cert"
    assert "vision-client" in info["subject"] and info["application_uri"] == "urn:vision:client"
    assert info["expires"][:4].isdigit() and (folder / "My_Client.pem").read_bytes() == cert
    der, _ = _pair(pem=False)
    assert store("server_cert", "server.pem", der)["filename"] == "server.der"   # binary in a .pem file


def test_a_private_key_is_stored_apart_from_its_certificate(folder):
    cert, key = _pair()
    assert store("client_cert", "client.pem", cert)["filename"] == "client.pem"
    info = store("private_key", "client.pem", key)
    assert info["filename"] == "client.key.pem" and info["kind"] == "private_key" and "subject" not in info
    assert (folder / "client.key.pem").read_bytes() == key and (folder / "client.pem").read_bytes() == cert
    _, der_key = _pair(pem=False)
    assert store("private_key", "other.key", der_key)["filename"] == "other.key.der"


@pytest.mark.parametrize("kind, data, word", [
    ("client_cert", b"hello", "not a certificate"),
    ("client_cert", b"", "empty"),
    ("private_key", b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", "not a private key"),
    ("client_cert", b"x" * (opcua_cert_service.MAX_BYTES + 1), "too large"),
    ("picture", b"x", "kind"),
], ids=["junk", "empty", "certificate-as-key", "too-large", "unknown-kind"])
def test_a_file_that_is_not_what_it_should_be_is_refused(folder, kind, data, word):
    with pytest.raises(ValueError, match=word):
        store(kind, "file.pem", data)
    assert not folder.exists() or not list(folder.iterdir())


def test_a_key_protected_by_a_password_is_refused_and_says_why(folder):
    _, key = _pair(password=b"secret")
    with pytest.raises(ValueError, match="password"):
        store("private_key", "locked.pem", key)


def test_a_file_name_cannot_leave_the_folder(folder):
    cert, _ = _pair()
    info = store("client_cert", "..\\..\\evil/..\\name.pem", cert)
    assert info["filename"] == "name.pem" and [p.name for p in folder.iterdir()] == ["name.pem"]
    with pytest.raises(ValueError):
        resolve("../name.pem")
    with pytest.raises(ValueError, match="not been uploaded"):
        resolve("missing.pem")
    assert resolve("name.pem") == str(folder / "name.pem") and resolve("") == ""


def test_the_uploaded_files_are_listed_by_what_they_are(folder):
    cert, key = _pair()
    store("client_cert", "a.pem", cert)
    store("private_key", "a.pem", key)
    store("server_cert", "srv.pem", cert)
    listed = list_files()
    assert listed["certificates"] == ["a.pem", "srv.pem"] and listed["private_keys"] == ["a.key.pem"]
    assert list_files.__doc__ and list_files()["all_files"] == ["a.key.pem", "a.pem", "srv.pem"]


# ── On the channel ────────────────────────────────────────────────────────────


def _channel(**extra):
    return {"name": "Secure PLC", "protocol": "plc", "plc_protocol": "opcua", "host": "10.0.0.5", "port": 4840,
            "opcua_security": "Basic256Sha256_SignAndEncrypt", **extra}


def test_a_channel_takes_the_uploaded_files_by_name(lines, folder):  # noqa: F811
    cert, key = _pair()
    store("client_cert", "client.pem", cert)
    store("private_key", "client.pem", key)
    store("server_cert", "plc.pem", cert)
    saved = {}

    async def steps():
        # dict(): the answer is the saved record itself, which the next save changes.
        saved["first"] = dict(await SettingsPersistenceService.add_or_update_endpoint(_channel(
            opcua_cert_file="client.pem", opcua_key_file="client.key.pem", opcua_server_cert_file="plc.pem")))
        # A client that does not send them keeps them.
        saved["kept"] = dict(await SettingsPersistenceService.add_or_update_endpoint({"id": saved["first"]["id"], "protocol": "plc", "name": "Renamed"}))
        saved["cleared"] = await SettingsPersistenceService.add_or_update_endpoint({
            "id": saved["first"]["id"], "protocol": "plc", "opcua_cert_file": "", "opcua_key_file": "", "opcua_server_cert_file": ""})

    _run(steps)
    first = saved["first"]
    assert first["opcua_cert_path"] == str(folder / "client.pem") and first["opcua_key_path"] == str(folder / "client.key.pem")
    assert first["opcua_server_cert_path"] == str(folder / "plc.pem")
    assert saved["kept"]["opcua_cert_path"] == first["opcua_cert_path"] and saved["kept"]["name"] == "Renamed"
    assert saved["cleared"]["opcua_cert_path"] == "" and saved["cleared"]["opcua_key_path"] == ""


def test_a_key_that_is_not_the_certificates_is_refused(lines, folder):  # noqa: F811
    cert, _ = _pair()
    _, other_key = _pair()
    store("client_cert", "client.pem", cert)
    store("private_key", "other.pem", other_key)

    async def steps():
        with pytest.raises(ValueError, match="does not belong"):
            await SettingsPersistenceService.add_or_update_endpoint(_channel(opcua_cert_file="client.pem", opcua_key_file="other.key.pem"))
        with pytest.raises(ValueError, match="not been uploaded"):
            await SettingsPersistenceService.add_or_update_endpoint(_channel(opcua_cert_file="nope.pem"))

    _run(steps)


def test_the_driver_gets_files_it_can_read(lines, folder):  # noqa: F811
    """The OPC UA library loads a .pem file as text and any other as binary: the stored files must load."""
    import asyncio

    from asyncua.crypto import uacrypto

    for pem in (True, False):
        cert, key = _pair(pem=pem)
        cert_path = resolve(store("client_cert", f"c{int(pem)}.crt", cert)["filename"])
        key_path = resolve(store("private_key", f"c{int(pem)}.key", key)["filename"])
        loaded = asyncio.run(uacrypto.load_certificate(cert_path))
        assert loaded.subject.rfc4514_string() == "CN=vision-client"
        assert asyncio.run(uacrypto.load_private_key(key_path)).key_size == 2048


# ── The API and the page ──────────────────────────────────────────────────────


def test_a_supervisor_uploads_and_lists_the_files(client, admin_headers, folder):
    cert, key = _pair()
    res = client.post(f"{API}/upload", files={"file": ("client.crt", cert)}, data={"kind": "client_cert"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["filename"] == "client.pem" and "vision-client" in res.json()["subject"]
    res = client.post(f"{API}/upload", files={"file": ("client.key", key)}, data={"kind": "private_key"}, headers=admin_headers)
    assert res.status_code == 200 and res.json()["filename"] == "client.key.pem"
    assert key.decode()[30:60] not in res.text                      # the key is never sent back
    listed = client.get(API, headers=admin_headers).json()
    assert listed["certificates"] == ["client.pem"] and listed["private_keys"] == ["client.key.pem"]


def test_a_wrong_upload_is_refused(client, admin_headers, folder):
    res = client.post(f"{API}/upload", files={"file": ("client.crt", b"junk")}, data={"kind": "client_cert"}, headers=admin_headers)
    assert res.status_code == 422 and "not a certificate" in res.json()["detail"]
    assert client.post(f"{API}/upload", files={"file": ("client.crt", b"junk")}, data={"kind": "client_cert"}).status_code == 401
    assert client.get(API).status_code == 401


def test_the_connections_page_has_the_certificate_fields(client):
    page = client.get("/dashboard").text
    for field in ("plcOpcuaCertInput", "plcOpcuaKeyInput", "plcOpcuaServerCertInput", "plcOpcuaCertFields"):
        assert f'id="{field}"' in page, field
    assert "opcua_cert_file" in page and "opcua_key_file" in page and "/api/v1/plc/opcua/certs/upload" in page
    assert os.path.basename(opcua_cert_service.OPCUA_CERTS_DIR) == "opcua"
