from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
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
from app.utils.logger import LogThrottle, get_logger, redact

logger = get_logger(__name__)
_reconnect_log = LogThrottle(300.0)


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


def camera_orientation(
    camera_id: Optional[str],
    flip_h: Optional[bool] = None,
    flip_v: Optional[bool] = None,
) -> Tuple[Optional[int], Optional[bool], Optional[bool]]:
    """(rotation, flip_h, flip_v) from a camera's settings, for callers that were not given a rotation.

    The camera is camera_id's driver, else the active camera's, else the first
    registered one. A flip the caller already has is kept. Rotation is None when
    no camera is registered. Values are derived as the live stream derives them
    in vision_service.
    """
    driver = app_state.cameras.get(camera_id) if camera_id else None
    if driver is None and app_state.cameras:
        from app.services.settings_persistence_service import SettingsPersistenceService
        active_id = SettingsPersistenceService.get_active_camera_id()
        driver = app_state.cameras.get(active_id) if active_id else None
        if driver is None:
            driver = next(iter(app_state.cameras.values()), None)
    if driver is None:
        return None, flip_h, flip_v

    s = getattr(driver, "settings", None) or {}
    rotation = int(s.get("rotation") or 90)
    if flip_h is None:
        flip_h = bool(s.get("flip_h", False))
    if flip_v is None:
        flip_v = bool(s.get("flip_v", False))
    return rotation, flip_h, flip_v


class CameraService:
    # Cameras being reconnected in the background after their address changed.
    _restarting: Dict[str, "asyncio.Task[None]"] = {}

    @staticmethod
    async def sync_ip_cameras(db: Optional[AsyncSession] = None) -> List[str]:
        """Bring the IP cameras saved under Connections into the database, and restart
        every running camera whose saved address is no longer the one it reads from.

        Called after each save of a camera address, so a changed address takes
        effect at once instead of at the next manual Connect. Returns the ids of
        the cameras it restarted.
        """
        if db is None:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as own_db:
                return await CameraService.sync_ip_cameras(own_db)

        from app.services.settings_persistence_service import SettingsPersistenceService
        moved: List[str] = []
        dirty_db = False
        for saved in SettingsPersistenceService.get_state().get("ip_cameras", []):
            camera_id = saved.get("id")
            source = saved.get("source")
            if not camera_id or not source:
                continue
            name = saved.get("name") or "IP Camera"
            existing = await CameraService.get_camera_by_id(db, camera_id)
            if not existing:
                db.add(Camera(
                    id=camera_id,
                    name=name,
                    type="ip",
                    source=source,
                    settings={"auto_connect": saved.get("auto_connect", True)},
                    is_active=False,
                ))
                dirty_db = True
            elif existing.source != source or existing.name != name:
                if existing.source != source:
                    # A camera still waiting to connect is tried on the new address now.
                    CameraReconnector.retry_now(camera_id)
                existing.source = source
                existing.name = name
                dirty_db = True

            driver = app_state.cameras.get(camera_id)
            running_on = getattr(driver, "source", None)
            if running_on is not None and running_on != source:
                moved.append(camera_id)
            elif driver is not None:
                driver.name = name

        if dirty_db:
            await db.commit()
        # After the commit: the restart reads the address from the database.
        return [camera_id for camera_id in moved if CameraService.restart_camera(camera_id)]

    @staticmethod
    def restart_camera(camera_id: str) -> bool:
        """Reconnect a camera on its saved address in the background.

        Opening an address that does not answer takes many seconds, so the
        request that saved it is not kept waiting. A camera that does not come
        up is retried like one that was off when the server started. Returns
        False when a restart is already running or the server is shutting down.
        """
        if camera_id in CameraService._restarting or app_state.shutting_down:
            return False

        async def run() -> None:
            from app.db.session import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as db:
                    ok, err = await CameraService.connect_camera(db, camera_id)
                if ok:
                    logger.info("Camera %s restarted on its new address", camera_id)
                elif err != "Camera not found in database":
                    logger.warning("Camera %s did not connect on its new address, retrying in the background: %s",
                                   camera_id, err)
                    CameraReconnector.want(camera_id)
            except Exception:
                logger.exception("Could not restart camera %s on its new address", camera_id)
            finally:
                CameraService._restarting.pop(camera_id, None)

        CameraService._restarting[camera_id] = asyncio.create_task(run(), name=f"camera_restart_{camera_id}")
        return True

    @staticmethod
    def connection_state(camera: Camera) -> str:
        """What the Cameras page shows: connected, reconnecting, failed or disconnected.

        "reconnecting" is a camera nobody disconnected that the server is
        bringing back: its driver lost the stream or the device, it is being
        retried in the background, or it is restarting on a new address.
        """
        driver = app_state.cameras.get(camera.id)
        if driver is not None and getattr(driver, "is_connected", False):
            return "reconnecting" if getattr(driver, "reconnecting", False) else "connected"
        if camera.id in CameraService._restarting or camera.id in CameraReconnector.pending():
            return "reconnecting"
        return "failed" if camera.last_error else "disconnected"

    @staticmethod
    async def list_cameras(db: AsyncSession) -> List[Camera]:
        # Sync configured IP cameras from persistent settings into database
        try:
            await CameraService.sync_ip_cameras(db)
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

        previous_source = camera.source
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

        if data.name is not None or data.source is not None:
            # The saved Connections entry is copied over the database on every
            # camera listing, so it has to carry the change too.
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.set_ip_camera_address(camera_id, camera.name, camera.source)
        if camera.source != previous_source:
            CameraReconnector.retry_now(camera_id)
            if camera_id in app_state.cameras:
                CameraService.restart_camera(camera_id)
        return camera

    @staticmethod
    async def delete_camera(db: AsyncSession, camera_id: str) -> bool:
        from app.services.vision_service import CameraStreamPipeline
        CameraReconnector.forget(camera_id)
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
    async def connect_camera(db: AsyncSession, camera_id: str, _background: bool = False) -> Tuple[bool, Optional[str]]:
        if not _background:
            # Connecting by hand replaces any background retry of this camera.
            CameraReconnector.forget(camera_id)
        camera = await CameraService.get_camera_by_id(db, camera_id)
        if not camera:
            return False, "Camera not found in database"

        # Several cameras run at once (one or two per production line). A camera
        # that is already connected is reconnected in place.
        from app.config import settings as app_settings
        others = [cid for cid, drv in app_state.cameras.items() if cid != camera_id and getattr(drv, "is_connected", False)]
        if len(others) >= app_settings.MAX_CONNECTED_CAMERAS:
            return False, (
                f"{len(others)} cameras are already connected, the most this server allows "
                f"(MAX_CONNECTED_CAMERAS={app_settings.MAX_CONNECTED_CAMERAS}). Disconnect one first."
            )
        previous = app_state.cameras.pop(camera_id, None)
        if previous is not None:
            from app.services.vision_service import CameraStreamPipeline
            try:
                await asyncio.to_thread(previous.disconnect)
                await asyncio.to_thread(CameraStreamPipeline.remove_camera, camera_id)
            except Exception as exc:
                logger.warning("Failed to release camera %s before reconnecting: %s", camera_id, exc)

        try:
            driver = _instantiate_driver(camera)
            loop = asyncio.get_running_loop()
            # Run blocking OpenCV connect() in thread pool so async loop isn't blocked
            success = await loop.run_in_executor(None, driver.connect)
        except Exception as exc:
            logger.error("Exception occurred while instantiating/connecting camera %s: %s", camera_id, redact(str(exc)))
            success = False
            last_err = redact(str(exc))
        else:
            last_err = driver.last_error

        if success:
            app_state.cameras[camera_id] = driver
            camera.is_active = True
            camera.last_error = None
            try:
                # active_camera_id is Line 1's camera for version 1 and older
                # clients; a camera that belongs to another line leaves it alone.
                from app.services.line_config import PRIMARY_LINE_ID
                from app.services.settings_persistence_service import SettingsPersistenceService
                owner = SettingsPersistenceService.line_for_camera(camera_id)
                if owner in (None, PRIMARY_LINE_ID):
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
        CameraReconnector.forget(camera_id)
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
            connection_state=CameraService.connection_state(camera),
            is_streaming=is_streaming,
            source=camera.source,
            properties=properties,
            last_error=camera.last_error,
        )

    @staticmethod
    async def grab_full_view(camera_id: str, max_width: int = 1280) -> Optional[bytes]:
        """JPEG of a connected camera's picture before the ROI crop, at most max_width wide."""
        driver: Optional[BaseCamera] = app_state.cameras.get(camera_id)
        if driver is None or not driver.is_connected:
            return None

        def encode() -> Optional[bytes]:
            import cv2
            mat = driver.get_full_view_mat()
            if mat is None:
                return None
            if mat.shape[1] > max_width:
                scale = max_width / mat.shape[1]
                mat = cv2.resize(mat, (max_width, max(1, int(mat.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return buf.tobytes() if ok else None

        return await asyncio.to_thread(encode)

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


class CameraReconnector:
    """Retries cameras that should be running but could not be opened.

    A camera that is switched off or still booting when the server starts, or
    when its line is started, is tried again in the background, waiting longer
    after each failure (up to CAMERA_RECONNECT_MAX_SECONDS). Once a camera is
    open its driver handles later dropouts itself. Connecting or disconnecting
    the camera by hand, stopping its line or deleting it takes it off the list.
    """

    FIRST_DELAY_SECONDS = 5.0
    _pending: Dict[str, Dict[str, float]] = {}
    _task: Optional[asyncio.Task] = None

    @classmethod
    def want(cls, camera_id: str) -> None:
        if camera_id and camera_id not in cls._pending:
            cls._pending[camera_id] = {
                "next": time.monotonic() + cls.FIRST_DELAY_SECONDS,
                "delay": cls.FIRST_DELAY_SECONDS,
                "attempts": 0,
            }

    @classmethod
    def forget(cls, camera_id: str) -> None:
        cls._pending.pop(camera_id, None)

    @classmethod
    def retry_now(cls, camera_id: str) -> None:
        """Try a waiting camera on the next pass (its address was just changed). Others are left alone."""
        entry = cls._pending.get(camera_id)
        if entry is not None:
            entry.update({"next": time.monotonic(), "delay": cls.FIRST_DELAY_SECONDS, "attempts": 0})

    @classmethod
    def pending(cls) -> List[str]:
        return list(cls._pending)

    @classmethod
    def start(cls) -> None:
        if cls._task is None or cls._task.done():
            cls._task = asyncio.create_task(cls._run(), name="camera_reconnect")

    @classmethod
    async def stop(cls) -> None:
        task, cls._task = cls._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @classmethod
    async def _run(cls) -> None:
        while True:
            try:
                await cls.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Camera reconnect pass failed")
            await asyncio.sleep(1.0)

    @classmethod
    async def tick(cls, now: Optional[float] = None) -> None:
        from app.config import settings as app_settings
        from app.db.session import AsyncSessionLocal

        now = time.monotonic() if now is None else now
        for camera_id, entry in list(cls._pending.items()):
            if app_state.shutting_down:
                return
            if entry["next"] > now:
                continue
            driver = app_state.cameras.get(camera_id)
            if driver is not None and getattr(driver, "is_connected", False):
                cls.forget(camera_id)
                continue
            entry["attempts"] += 1
            async with AsyncSessionLocal() as db:
                ok, err = await CameraService.connect_camera(db, camera_id, _background=True)
                if camera_id not in cls._pending:
                    # Disconnected by hand or its line stopped while this attempt ran.
                    if ok:
                        await CameraService.disconnect_camera(db, camera_id)
                    continue
            if ok:
                logger.info("Camera %s connected after %d background attempt(s)", camera_id, int(entry["attempts"]))
                cls.forget(camera_id)
            elif err == "Camera not found in database":
                cls.forget(camera_id)
            else:
                entry["delay"] = min(entry["delay"] * 2, max(cls.FIRST_DELAY_SECONDS, app_settings.CAMERA_RECONNECT_MAX_SECONDS))
                entry["next"] = time.monotonic() + entry["delay"]
                _reconnect_log.log(
                    logger, logging.WARNING, camera_id,
                    "Camera %s is still not connecting (attempt %d, next in %.0f s): %s",
                    camera_id, int(entry["attempts"]), entry["delay"], err,
                )
