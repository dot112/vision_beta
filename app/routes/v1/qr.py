from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.qr import BarcodeDecodeResponse, LiveCameraBarcodeResponse
from app.services.qr_service import QRService
from app.config import settings
from app.utils.logger import get_logger
from app.utils.upload_limits import read_upload_limited

router = APIRouter(prefix="/qr", tags=["QR & Barcodes"])
logger = get_logger(__name__)


@router.get("/code-types", summary="Code types a reader can be limited to (all, 2D, 1D, or one type)")
async def list_code_types() -> dict:
    """What Line setup offers for a camera that reads codes. The single types
    are the ones the installed reader library supports."""
    from app.engines.qr_engine import code_type_options, reader_backend

    return {"backend": reader_backend(), "types": code_type_options()}


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

    # 2. Check the code against the product lists (the one the scan names, else
    # all of them), then send it to the saved channels through the shared
    # telemetry dispatcher.
    from app.services.product_service import product_catalog
    scan_list = payload.get("list_id") or payload.get("product_list_id")
    product = product_catalog.lookup(code, str(scan_list)) if scan_list else product_catalog.lookup_any(code)
    scan_event_payload = {
        "event": "MOBILE_BARCODE_SCANNED",
        "code": code,
        "format": fmt,
        "type": code_type,
        "device": device,
        "timestamp": ts,
        "known": product is not None,
        "product_name": product.get("name") if product else None,
    }
    try:
        from app.services.counting_service import send_to_channels
        send_to_channels(scan_event_payload, protocol=str(target_protocol), endpoint_id=str(target_endpoint))
    except Exception:
        logger.exception("Could not queue the mobile scan for dispatch")

    return {
        "status": "success",
        "message": f"Scan received: {code}",
        "code": code,
        "format": fmt,
        "type": code_type,
        "timestamp": ts,
        "known": product is not None,
        "product_name": product.get("name") if product else None,
    }

