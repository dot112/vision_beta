from __future__ import annotations

from pathlib import Path
import httpx
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class OTAService:
    """
    Over-The-Air remote model update downloader and validator.
    """

    @staticmethod
    async def download_model(url: str, model_name: str, version: str) -> str:
        dest_dir = Path(settings.MODEL_STORE_PATH) / model_name / version
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / "model.onnx"

        logger.info("OTA: Downloading model from %s -> %s", url, dest_file)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(dest_file, "wb") as f:
                f.write(resp.content)

        logger.info("OTA: Model successfully downloaded to %s", dest_file)
        return str(dest_file)
