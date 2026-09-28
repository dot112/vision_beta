from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.qr import BarcodeDecodeResponse, LiveCameraBarcodeResponse
from app.services.qr_service import QRService
from app.config import settings
from app.utils.upload_limits import read_upload_limited

router = APIRouter(prefix="/qr", tags=["QR & Barcodes"])


@router.post("/decode", response_model=BarcodeDecodeResponse, summary="Decode QR/Barcodes in uploaded image")
async def decode_image(
    file: UploadFile = File(..., description="Image file containing QR or barcodes"),
) -> BarcodeDecodeResponse:
    """Detects and decodes all 1D (EAN/UPC/Code128) and 2D (QR) barcodes in uploaded image."""
    try:
        if file.content_type and not file.content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Only image uploads are accepted")
        content = await read_upload_limited(file, settings.MAX_IMAGE_UPLOAD_BYTES)
        return await QRService.decode_image(content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/decode/camera/{camera_id}", response_model=LiveCameraBarcodeResponse, summary="Decode QR/Barcodes live from camera")
async def decode_live_camera(
    camera_id: str,
    db: AsyncSession = Depends(get_db),
) -> LiveCameraBarcodeResponse:
    """Grabs instant frame from connected camera and decodes barcodes in real-time."""
    try:
        return await QRService.decode_live_camera(db, camera_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/annotated/camera/{camera_id}", summary="Get camera snapshot with highlighted barcode polygons")
async def get_annotated_frame(
    camera_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Grabs camera frame, decodes barcodes, draws highlight polygons + content tags, and returns image/jpeg."""
    try:
        jpeg_bytes = await QRService.get_annotated_frame(db, camera_id)
        return Response(content=jpeg_bytes, media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/mobile-scan", summary="Receive 1D/2D Barcode Scan from Android Mobile Scanner")
async def receive_mobile_scan(
    payload: dict,
) -> dict:
    """
    Receives scanned barcode/QR data from the Android mobile app, records an audit log entry,
    and dispatches the payload to configured industrial protocols (MQTT, TCP, Webhooks).
    """
    code = str(payload.get("code", "")).strip()
    fmt = str(payload.get("format", "UNKNOWN")).upper()
    code_type = str(payload.get("type", "CODE")).upper()
    device = str(payload.get("device_name", "Android Scanner")).strip()
    from datetime import datetime, timezone
    ts = payload.get("timestamp") or datetime.now(timezone.utc).isoformat()

    target_endpoint = payload.get("target_endpoint_id", "all")
    target_protocol = payload.get("target_protocol", "all")

    if not code:
        raise HTTPException(status_code=400, detail="Barcode data (code) cannot be empty")

    # 1. Record in persistent audit logs & broadcast to all connected dashboards
    try:
        from app.services.settings_persistence_service import SettingsPersistenceService
        SettingsPersistenceService.record_audit(
            username=device,
            role="SCANNER",
            clearance_level=1,
            action="MOBILE_SCAN",
            category="QR_BARCODE",
            details=f"Scanned {fmt} ({code_type}): '{code}'",
        )
    except Exception:
        pass

    # 2. Protocol dispatching based on user selection
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        from app.services.settings_persistence_service import SettingsPersistenceService
        state = SettingsPersistenceService.get_state()
        comms = state.get("comms", {})

        scan_event_payload = {
            "event": "MOBILE_BARCODE_SCANNED",
            "code": code,
            "format": fmt,
            "type": code_type,
            "device": device,
            "timestamp": ts,
        }

        # MQTT
        if target_protocol in ("all", "mqtt"):
            for m in comms.get("mqtt_channels", []):
                if target_endpoint in ("all", m.get("id")):
                    try:
                        from app.services.mqtt_service import MQTTService
                        loop.create_task(MQTTService.publish(m.get("topic", "factory/scans"), scan_event_payload, qos=0))
                    except Exception:
                        pass

        # TCP
        if target_protocol in ("all", "tcp"):
            for t in comms.get("tcp_servers", []):
                if target_endpoint in ("all", t.get("id")):
                    try:
                        from app.services.counting_service import counting_service
                        loop.create_task(counting_service._dispatch_tcp(t.get("host"), t.get("port"), scan_event_payload))
                    except Exception:
                        pass

        # Webhook
        if target_protocol in ("all", "webhook"):
            for w in comms.get("webhooks", []):
                if target_endpoint in ("all", w.get("id")):
                    try:
                        from app.services.counting_service import counting_service
                        loop.create_task(counting_service._dispatch_webhook(w.get("url"), scan_event_payload))
                    except Exception:
                        pass
    except Exception:
        pass

    return {
        "status": "success",
        "message": f"Scan received: {code}",
        "code": code,
        "format": fmt,
        "type": code_type,
        "timestamp": ts,
    }

