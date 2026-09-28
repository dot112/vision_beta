from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.hardware.camera.base import BaseCamera
from app.utils.logger import get_logger

logger = get_logger(__name__)


class GigECamera(BaseCamera):
    """
    Driver stub for GigE Vision (GenICam / Basler pypylon / Harvesters) industrial cameras.
    """

    def __init__(self, camera_id: str, name: str, source: str, settings: Optional[Dict[str, Any]] = None):
        super().__init__(camera_id=camera_id, name=name, source=source, settings=settings)
        self._device: Optional[Any] = None

    def connect(self) -> bool:
        self.last_error = (
            f"GigE Vision camera driver for '{self.name}' ({self.source}) requires a vendor SDK "
            "(e.g., pypylon for Basler, or harvesters for GenICam CTI). "
            "Please configure the hardware CTI driver."
        )
        logger.warning(self.last_error)
        self.is_connected = False
        return False

    def disconnect(self) -> None:
        self.is_connected = False

    def grab_frame(self) -> Tuple[bool, Optional[bytes]]:
        return False, None

    def grab_raw_frame(self) -> Tuple[bool, Optional[Any], int]:
        return False, None, 0

    def get_properties(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "connected": self.is_connected,
            "type": "gige",
            "settings": self.settings,
            "note": "GigE hardware driver awaiting SDK integration",
        }

    def set_properties(self, properties: Dict[str, Any]) -> bool:
        self.settings.update(properties)
        return True
