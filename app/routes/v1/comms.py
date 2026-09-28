from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.dependencies import require_supervisor

router = APIRouter(prefix="/comms", tags=["Communications & Protocol Testing"])


class TCPSendRequest(BaseModel):
    host: str = Field(default="127.0.0.1", description="Target TCP host IP")
    port: int = Field(default=9000, ge=1, le=65535, description="Target TCP port")
    payload: Dict[str, Any] = Field(default={"test": "hello_tcp"}, description="JSON dictionary payload")


class TCPCheckRequest(BaseModel):
    host: str = Field(default="127.0.0.1", description="Target TCP host IP")
    port: int = Field(default=9000, ge=1, le=65535, description="Target TCP port")


class WebhookSendRequest(BaseModel):
    url: str = Field(..., description="Target Webhook URL")
    payload: Dict[str, Any] = Field(default={"test": "webhook_ping"}, description="JSON payload to send")
    headers: Optional[Dict[str, str]] = Field(default=None, description="Custom HTTP headers")
    method: str = Field(default="POST", pattern="^(POST|PUT)$", description="HTTP method (POST/PUT)")


def _configured_endpoint(protocols: set[str], *, host: Optional[str] = None, port: Optional[int] = None, url: Optional[str] = None) -> bool:
    from app.services.settings_persistence_service import SettingsPersistenceService
    for endpoint in SettingsPersistenceService.get_endpoints():
        protocol = str(endpoint.get("protocol", "")).lower()
        if protocol not in protocols or not endpoint.get("enabled", True):
            continue
        if host is not None and (str(endpoint.get("host", "")).lower() != host.lower() or int(endpoint.get("port", 0) or 0) != port):
            continue
        if url is not None and str(endpoint.get("url", "")).rstrip("/") != url.rstrip("/"):
            continue
        return True
    return False


@router.post("/tcp/check", summary="Check TCP socket reachability")
async def check_tcp_connection(req: TCPCheckRequest, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    if not _configured_endpoint({"tcp", "plc"}, host=req.host, port=req.port):
        raise HTTPException(status_code=403, detail="TCP checks are limited to configured endpoints")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(req.host, req.port), timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
        return {"status": "connected", "message": f"TCP Socket at {req.host}:{req.port} is reachable"}
    except Exception as exc:
        return {"status": "disconnected", "message": f"Cannot connect to {req.host}:{req.port}: {exc}"}


@router.post("/tcp/test", summary="Test dispatching a JSON message to a TCP Socket")
async def test_tcp_socket(req: TCPSendRequest, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    if not _configured_endpoint({"tcp"}, host=req.host, port=req.port):
        raise HTTPException(status_code=403, detail="TCP tests are limited to configured endpoints")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(req.host, req.port), timeout=2.5
        )
        msg = (json.dumps(req.payload) + "\n").encode("utf-8")
        writer.write(msg)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return {"status": "success", "message": f"Sent {len(msg)} bytes to {req.host}:{req.port}"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TCP connection failed to {req.host}:{req.port}: {exc}")


@router.post("/webhook/test", summary="Test dispatching a JSON message to an External Webhook API")
async def test_webhook_api(req: WebhookSendRequest, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    if not req.url.lower().startswith(("http://", "https://")) or not _configured_endpoint({"webhook"}, url=req.url):
        raise HTTPException(status_code=403, detail="Webhook tests are limited to configured endpoints")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=False) as client:
            resp = await client.request(req.method or "POST", req.url, json=req.payload, headers=req.headers or {})
            return {
                "status": "success",
                "status_code": resp.status_code,
                "response_text": resp.text[:500],
            }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Webhook request failed to {req.url}: {exc}")


@router.post("/endpoints/{endpoint_id}/test", summary="Run a live handshake test for a configured communication channel")
async def test_endpoint(endpoint_id: str, _user=Depends(require_supervisor)) -> Dict[str, Any]:
    from app.services.settings_persistence_service import SettingsPersistenceService
    res = await SettingsPersistenceService.test_endpoint(endpoint_id)
    if not res.get("success", False):
        raise HTTPException(status_code=400, detail=res.get("message", "Endpoint test failed"))
    return res
