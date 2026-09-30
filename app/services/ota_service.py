from __future__ import annotations

import os
from pathlib import Path
import httpx
from app.config import settings
from app.utils.logger import get_logger, redact

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
        # Download next to the target and rename when complete, so a model
        # that is loaded or loads at the next start is never a half-written file.
        part_file = dest_dir / "model.onnx.part"

        logger.info("OTA: Downloading model from %s -> %s", redact(url), dest_file)
        size = 0
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(part_file, "wb") as f:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > settings.MAX_MODEL_UPLOAD_BYTES:
                                raise ValueError("Model download exceeds MAX_MODEL_UPLOAD_BYTES")
                            f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
            os.replace(part_file, dest_file)
        except Exception:
            part_file.unlink(missing_ok=True)
            raise

        logger.info("OTA: Model successfully downloaded to %s (%d bytes)", dest_file, size)
        return str(dest_file)
