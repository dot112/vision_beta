from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.camera import Camera
from app.hardware.camera.base import BaseCamera
from app.hardware.camera.gige_camera import GigECamera
from app.hardware.camera.ip_camera import IPCamera
from app.hardware.camera.usb_camera import USBCamera
from app.schemas.camera import (
    CameraCreate,
    CameraStatusResponse,
    CameraType,
    CameraUpdate,
    DiscoveredUSBCamera,
)
from app.state.application_state import app_state
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _instantiate_driver(camera: Camera) -> BaseCamera:
    raw_type = getattr(camera.type, "value", camera.type)
    cam_type = str(raw_type).lower() if raw_type else "usb"
    settings = dict(camera.settings or {})

    if cam_type == "usb":
        return USBCamera(
            camera_id=camera.id,
            name=camera.name,
            source=camera.source,
            settings=settings,
        )
    elif cam_type == "ip":
        return IPCamera(
            camera_id=camera.id,
            name=camera.name,
            source=camera.source,
            settings=settings,
        )
    elif cam_type == "gige":
        return GigECamera(
            camera_id=camera.id,
            name=camera.name,
            source=camera.source,
            settings=settings,
        )
    else:
        logger.warning("Unknown camera type '%s', falling back to USBCamera driver", cam_type)
        return USBCamera(
            camera_id=camera.id,
            name=camera.name,
            source=camera.source,
            settings=settings,
        )


class CameraService:
    @staticmethod
    async def list_cameras(db: AsyncSession) -> List[Camera]:
        # Sync configured IP cameras from persistent settings into database
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            state = SettingsPersistenceService.get_state()
            ip_cams = state.get("ip_cameras", [])
            
            dirty_db = False
            for ip_c in ip_cams:
                c_id = ip_c.get("id")
                c_source = ip_c.get("source")
                if c_id and c_source:
                    stmt_c = select(Camera).where(Camera.id == c_id)
                    res_c = await db.execute(stmt_c)
                    existing = res_c.scalar_one_or_none()
                    
                    if not existing:
                        new_cam = Camera(
                            id=c_id,
                            name=ip_c.get("name", "IP Camera"),
                            type="ip",
                            source=c_source,
                            settings={"auto_connect": ip_c.get("auto_connect", True)},
                            is_active=False,
                        )
                        db.add(new_cam)
                        dirty_db = True
                    elif existing.source != c_source or existing.name != ip_c.get("name"):
                        existing.source = c_source
                        existing.name = ip_c.get("name", "IP Camera")
                        dirty_db = True

            if dirty_db:
                await db.commit()

        except Exception as sync_e:
            logger.debug("IP camera sync error: %s", sync_e)
            await db.rollback()

        stmt = select(Camera)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_camera_by_id(db: AsyncSession, camera_id: str) -> Optional[Camera]:
        stmt = select(Camera).where(Camera.id == camera_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def create_camera(db: AsyncSession, data: CameraCreate) -> Camera:
        cam_type_val = data.type.value if hasattr(data.type, "value") else str(data.type)
        camera = Camera(
            name=data.name,
            type=cam_type_val,
            source=data.source,
            settings=data.settings,
            is_active=False,
        )
        db.add(camera)
        await db.commit()
        await db.refresh(camera)
        logger.info("Registered new camera '%s' [%s] with id %s", camera.name, camera.type, camera.id)
        return camera

    @staticmethod
    async def update_camera(db: AsyncSession, camera_id: str, data: CameraUpdate) -> Optional[Camera]:
        camera = await CameraService.get_camera_by_id(db, camera_id)
        if not camera:
            return None

        if data.name is not None:
            camera.name = data.name
        if data.source is not None:
            source = data.source
            if isinstance(source, str) and isinstance(camera.source, str):
                from app.services.settings_persistence_service import _restore_redacted_url
                source = _restore_redacted_url(source, camera.source)
            camera.source = source
        if data.settings is not None:
            merged = dict(camera.settings or {})
            merged.update(data.settings)
            camera.settings = merged

            if camera_id in app_state.cameras:
                driver: BaseCamera = app_state.cameras[camera_id]
                try:
                    driver.set_properties(data.settings)
                except Exception as prop_err:
                    logger.error("Failed to update dynamic properties on active driver %s: %s", camera_id, prop_err)

        await db.commit()
        await db.refresh(camera)
        logger.info("Updated camera settings for id %s", camera_id)
        return camera

    @staticmethod
    async def delete_camera(db: AsyncSession, camera_id: str) -> bool:
        from app.services.vision_service import CameraStreamPipeline
        # 1. Remove from active hardware/driver memory first so the vision runner stops
        # using it, then stop its workers and release the device. Both join threads,
        # so they run off the event loop.
        driver: Optional[BaseCamera] = app_state.cameras.pop(camera_id, None)
        await asyncio.to_thread(CameraStreamPipeline.remove_camera, camera_id)
        if driver is not None:
            try:
                await asyncio.to_thread(driver.disconnect)
            except Exception as exc:
                logger.warning("Error disconnecting camera %s on delete: %s", camera_id, exc)

        # 2. Get camera from DB to check source and name before deleting
        camera = await CameraService.get_camera_by_id(db, camera_id)
        cam_source = (camera.source or "").strip() if camera else ""
        cam_name = (camera.name or "").strip() if camera else ""

        # 3. Delete from persistent settings (both ip_cameras and communication_endpoints)
        deleted_from_persistence = False
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            deleted_from_persistence = SettingsPersistenceService.delete_ip_camera(
                camera_id,
                source=cam_source,
                name=cam_name,
            )
        except Exception as e:
            logger.warning("Error deleting IP camera from persistent settings: %s", e)

        # 4. Delete from SQLite DB if present
        if camera:
            await db.delete(camera)
            await db.commit()
            logger.info("Deleted camera id %s from DB", camera_id)
            return True

        if deleted_from_persistence:
            logger.info("Deleted camera id %s from persistent settings", camera_id)
            return True

        return False

    @staticmethod
    async def discover_hardware_cameras(db: AsyncSession) -> List[DiscoveredUSBCamera]:
        """
        Scans all physical camera ports (USB / V4L2 / Hardware cameras)
        and automatically persists any newly discovered devices into the database.
        """
        raw_list = USBCamera.discover_cameras(max_devices=8)
        discovered: List[DiscoveredUSBCamera] = []

        dirty = False
        for d in raw_list:
            src = str(d["suggested_source"])
            name = d["name"]

            # Check if this hardware camera source is already registered
            stmt = select(Camera).where(Camera.source == src)
            res = await db.execute(stmt)
            existing = res.scalar_one_or_none()

            if not existing:
                new_cam = Camera(
                    name=name,
                    type="usb",
                    source=src,
                    settings={"auto_connect": False},
                    is_active=False,
                )
                db.add(new_cam)
                dirty = True

            discovered.append(
                DiscoveredUSBCamera(
                    device_index=d["device_index"],
                    name=name,
                    is_available=d["is_available"],
                    suggested_source=src,
                )
            )

        if dirty:
            await db.commit()

        return discovered

    @staticmethod
    async def connect_camera(db: AsyncSession, camera_id: str) -> Tuple[bool, Optional[str]]:
        camera = await CameraService.get_camera_by_id(db, camera_id)
        if not camera:
            return False, "Camera not found in database"

        # Auto-disconnect all other active cameras (exclusive single camera streaming)
        from app.services.vision_service import CameraStreamPipeline
        for other_id, other_driver in list(app_state.cameras.items()):
            if other_id != camera_id:
                try:
                    await asyncio.to_thread(other_driver.disconnect)
                    # Also stop its stream workers so a later reconnect never serves a stale cached frame.
                    await asyncio.to_thread(CameraStreamPipeline.remove_camera, other_id)
                    logger.info("Auto-disconnected previous camera %s", other_id)
                except Exception as exc:
                    logger.warning("Failed to auto-disconnect camera %s: %s", other_id, exc)
        app_state.cameras.clear()

        # Update other DB records to is_active=False and clear last_error so they return to Standby
        try:
            await db.execute(update(Camera).where(Camera.id != camera_id).values(is_active=False, last_error=None))
        except Exception as db_e:
            logger.debug("Failed to reset other cameras active state: %s", db_e)

        try:
            driver = _instantiate_driver(camera)
            loop = asyncio.get_running_loop()
            # Run blocking OpenCV connect() in thread pool so async loop isn't blocked
            success = await loop.run_in_executor(None, driver.connect)
        except Exception as exc:
            logger.error("Exception occurred while instantiating/connecting camera %s: %s", camera_id, exc)
            success = False
            last_err = str(exc)
        else:
            last_err = driver.last_error

        if success:
            app_state.cameras[camera_id] = driver
            camera.is_active = True
            camera.last_error = None
            try:
                from app.services.settings_persistence_service import SettingsPersistenceService
                SettingsPersistenceService.update_settings({"active_camera_id": camera_id})
            except Exception as pe:
                logger.debug("Could not persist active_camera_id: %s", pe)
        else:
            camera.is_active = False
            camera.last_error = last_err or "Unknown connection error"

        await db.commit()
        await db.refresh(camera)
        return success, camera.last_error

    @staticmethod
    async def disconnect_camera(db: AsyncSession, camera_id: str) -> bool:
        from app.services.vision_service import CameraStreamPipeline
        # 1. Immediately remove from live memory, stop stream workers and release the
        # hardware handle (off the event loop: both join threads)
        driver: Optional[BaseCamera] = app_state.cameras.pop(camera_id, None)
        await asyncio.to_thread(CameraStreamPipeline.remove_camera, camera_id)
        if driver is not None:
            try:
                await asyncio.to_thread(driver.disconnect)
                logger.info("Hardware released for camera %s", camera_id)
            except Exception as exc:
                logger.warning("Error releasing camera %s: %s", camera_id, exc)

        # 2. Update DB record
        camera = await CameraService.get_camera_by_id(db, camera_id)
        if camera:
            camera.is_active = False
            camera.last_error = None
            await db.commit()
            return True

        return False

    @staticmethod
    async def get_camera_status(db: AsyncSession, camera_id: str) -> Optional[CameraStatusResponse]:
        camera = await CameraService.get_camera_by_id(db, camera_id)
        if not camera:
            return None

        driver: Optional[BaseCamera] = app_state.cameras.get(camera_id)
        is_streaming = driver.is_connected if driver else False
        properties = driver.get_properties() if driver else {}

        return CameraStatusResponse(
            id=camera.id,
            name=camera.name,
            type=camera.type,
            is_active=camera.is_active,
            is_streaming=is_streaming,
            source=camera.source,
            properties=properties,
            last_error=camera.last_error,
        )

    @staticmethod
    async def grab_frame(db: AsyncSession, camera_id: str) -> Tuple[bool, Optional[bytes], Optional[str]]:
        # Fast path: in-memory driver check (zero database latency)
        driver: Optional[BaseCamera] = app_state.cameras.get(camera_id)
        if driver and driver.is_connected:
            # Full-resolution JPEG encoding takes milliseconds; run it off the event loop.
            success, jpeg = await asyncio.to_thread(driver.grab_frame)
            if success:
                return True, jpeg, None
            return False, None, driver.last_error or "Failed to grab frame"

        # Slow path: check if camera is active in DB
        camera = await CameraService.get_camera_by_id(db, camera_id)
        if not camera:
            return False, None, "Camera not found"

        if not camera.is_active:
            return False, None, "Camera is currently disconnected. Call POST /connect first."

        # Auto-connect only if camera was marked active but lost driver reference
        connected, err = await CameraService.connect_camera(db, camera_id)
        if not connected:
            return False, None, f"Camera connection failed: {err}"

        driver = app_state.cameras.get(camera_id)
        if not driver:
            return False, None, "Driver failed to initialize"

        success, jpeg = await asyncio.to_thread(driver.grab_frame)
        if not success:
            return False, None, driver.last_error or "Failed to grab frame"

        return True, jpeg, None

    @staticmethod
    async def grab_raw_frame(db: AsyncSession, camera_id: str) -> Tuple[bool, Optional[Any], int, Optional[str]]:
        """Fastest path: grab direct uncompressed BGR numpy array and monotonic frame_id."""
        driver: Optional[BaseCamera] = app_state.cameras.get(camera_id)
        if driver and driver.is_connected:
            success, mat, fid = driver.grab_raw_frame()
            if success and mat is not None:
                return True, mat, fid, None
            return False, None, 0, driver.last_error or "Failed to grab raw frame"

        camera = await CameraService.get_camera_by_id(db, camera_id)
        if not camera or not camera.is_active:
            return False, None, 0, "Camera is offline or not found"

        connected, err = await CameraService.connect_camera(db, camera_id)
        if not connected:
            return False, None, 0, err or "Camera connection failed"

        driver = app_state.cameras.get(camera_id)
        if not driver:
            return False, None, 0, "Driver failed to initialize"

        success, mat, fid = driver.grab_raw_frame()
        if not success or mat is None:
            return False, None, 0, driver.last_error or "Failed to grab raw frame"

        return True, mat, fid, None
