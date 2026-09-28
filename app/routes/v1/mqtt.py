from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from app.dependencies import require_supervisor

from app.schemas.mqtt import (
    MQTTCertListResponse,
    MQTTConnectRequest,
    MQTTPublishRequest,
    MQTTStatusResponse,
    MQTTSubscribeRequest,
)
from app.services.mqtt_service import CERTS_DIR, MQTTService

router = APIRouter(prefix="/mqtt", tags=["MQTT Broker"])

ALLOWED_CERT_EXTENSIONS = {".crt", ".pem", ".key", ".cer", ".ca"}


# ── Certificate Management ────────────────────────────────────────────────────

@router.get("/certs", response_model=MQTTCertListResponse, summary="List uploaded TLS certificates")
async def list_certs() -> MQTTCertListResponse:
    """Lists all uploaded CA certs, client certs, and private keys available on the server."""
    return MQTTCertListResponse(**MQTTService.list_certs())


@router.post("/certs/upload", summary="Upload TLS Certificate, Private Key, or CA Certificate")
async def upload_cert(
    file: UploadFile = File(..., description="Certificate (.crt / .pem / .ca) or Private Key (.key) file"),
    _user=Depends(require_supervisor),
) -> Dict[str, Any]:
    """
    Upload a TLS certificate file to the server for use in MQTT mTLS authentication.

    Accepted files:
    - **CA Certificate**: `ca.crt`, `ca.pem` — the broker's CA used to verify the server
    - **Client Certificate**: `client.crt`, `client.pem` — your device identity certificate
    - **Private Key**: `client.key` — the private key matching the client certificate

    After uploading, reference the filename in `POST /mqtt/connect` as:
    - `ca_cert_filename`: `"ca.crt"`
    - `client_cert_filename`: `"client.crt"`
    - `client_key_filename`: `"client.key"`
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_CERT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {sorted(ALLOWED_CERT_EXTENSIONS)}"
        )

    os.makedirs(CERTS_DIR, exist_ok=True)
    filename = os.path.basename(file.filename or "")
    if not filename or filename in {".", ".."} or filename != (file.filename or ""):
        raise HTTPException(status_code=400, detail="Invalid certificate filename")
    dest = os.path.join(CERTS_DIR, filename)
    content = await file.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Certificate file exceeds 4 MiB limit")

    with open(dest, "wb") as f:
        f.write(content)

    size_kb = round(len(content) / 1024, 2)
    return {
        "success": True,
        "filename": file.filename,
        "saved_to": "certificates/" + filename,
        "size_kb": size_kb,
        "message": f"Certificate uploaded successfully. Reference it by filename '{file.filename}' in POST /mqtt/connect",
    }


@router.delete("/certs/{filename}", summary="Delete an uploaded certificate file")
async def delete_cert(filename: str, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    success = MQTTService.delete_cert(filename)
    if not success:
        raise HTTPException(status_code=404, detail=f"Certificate file '{filename}' not found")
    return {"success": True, "deleted": filename}


# ── Connection Management ─────────────────────────────────────────────────────

@router.get("/status", response_model=MQTTStatusResponse, summary="Get MQTT broker connection status")
async def get_mqtt_status() -> MQTTStatusResponse:
    return MQTTStatusResponse(**MQTTService.get_status())


@router.post("/connect", summary="Connect to EMQX / MQTT broker (with optional TLS/mTLS)")
async def connect_mqtt(req: MQTTConnectRequest, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    """
    Configure and connect to an MQTT broker at runtime.

    **Plain TCP (no TLS):**
    ```json
    { "host": "192.168.1.10", "port": 1883 }
    ```

    **TLS with CA verification only (verify broker identity):**
    ```json
    { "host": "192.168.1.10", "port": 8883, "tls_enabled": true, "ca_cert_filename": "ca.crt" }
    ```

    **Full mTLS (mutual auth — broker verifies client too):**
    ```json
    {
      "host": "192.168.1.10", "port": 8883, "tls_enabled": true,
      "ca_cert_filename": "ca.crt",
      "client_cert_filename": "client.crt",
      "client_key_filename": "client.key"
    }
    ```
    """
    try:
        result = await MQTTService.connect(
            host=req.host,
            port=req.port,
            username=req.username,
            password=req.password,
            client_id=req.client_id,
            tls_enabled=req.tls_enabled,
            ca_cert_filename=req.ca_cert_filename,
            client_cert_filename=req.client_cert_filename,
            client_key_filename=req.client_key_filename,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not result["success"]:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to MQTT broker at {req.host}:{req.port}. Check host, port, credentials, and certificate files."
        )
    return result


@router.post("/disconnect", summary="Disconnect from MQTT broker")
async def disconnect_mqtt(_user=Depends(require_supervisor)) -> Dict[str, Any]:
    return await MQTTService.disconnect()


# ── Messaging ─────────────────────────────────────────────────────────────────

@router.post("/publish", summary="Publish a JSON payload to an MQTT topic")
async def publish_mqtt(req: MQTTPublishRequest, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    success = await MQTTService.publish(req.topic, req.payload, qos=req.qos, retain=req.retain)
    if not success:
        raise HTTPException(
            status_code=503,
            detail="Failed to publish. MQTT broker not connected — call POST /mqtt/connect first."
        )
    return {"success": True, "topic": req.topic, "payload": req.payload, "qos": req.qos}


@router.post("/subscribe", summary="Subscribe to an MQTT topic (supports # and + wildcards)")
async def subscribe_mqtt(req: MQTTSubscribeRequest, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    success = await MQTTService.subscribe(req.topic)
    return {"success": success, "topic": req.topic}
