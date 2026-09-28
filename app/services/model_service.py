from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional
from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.model import VisionModel
from app.engines.inference_engine import InferenceEngine
from app.schemas.model import ModelCreate, ModelUpdate
from app.state.application_state import app_state
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _read_onnx_metadata(path: Path) -> tuple[dict, Optional[List[str]]]:
    """Read optional ONNX labels/task without blocking the ASGI event loop."""
    try:
        import ast
        import onnxruntime as ort

        metadata = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]).get_modelmeta().custom_metadata_map
        extra = {"task": metadata["task"]} if metadata.get("task") else {}
        names = None
        if metadata.get("names"):
            names_dict = ast.literal_eval(metadata["names"])
            if names_dict:
                names = [names_dict[key] for key in sorted(names_dict.keys())]
        return extra, names
    except Exception:
        return {}, None


class ModelService:
    @staticmethod
    async def list_models(db: AsyncSession) -> List[VisionModel]:
        stmt = select(VisionModel).order_by(VisionModel.created_at.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_model_by_id(db: AsyncSession, model_id: str) -> Optional[VisionModel]:
        stmt = select(VisionModel).where(VisionModel.id == model_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_active_model(db: AsyncSession) -> Optional[VisionModel]:
        stmt = select(VisionModel).where(VisionModel.is_active == True)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def create_model(db: AsyncSession, data: ModelCreate) -> VisionModel:
        store_root = Path(settings.MODEL_STORE_PATH).resolve()
        model_path = Path(data.file_path).resolve(strict=True)
        if not model_path.is_file() or model_path.suffix.lower() != ".onnx" or not model_path.is_relative_to(store_root):
            raise ValueError("Model path must be an existing ONNX file inside MODEL_STORE_PATH")
        model = VisionModel(
            name=data.name,
            version=data.version,
            framework=data.framework,
            file_path=data.file_path,
            classes=data.classes,
            input_width=data.input_width,
            input_height=data.input_height,
            confidence_threshold=data.confidence_threshold,
            nms_threshold=data.nms_threshold,
            is_active=False,
            metadata_json=data.metadata_json or {},
        )
        db.add(model)
        await db.commit()
        await db.refresh(model)
        logger.info("Registered vision model '%s' version %s", model.name, model.version)
        return model

    @staticmethod
    async def upload_model_file(
        db: AsyncSession,
        name: str,
        version: str,
        classes: List[str],
        file: UploadFile,
    ) -> VisionModel:
        """Stores uploaded .onnx model into model_store and registers DB entry."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name or ""):
            raise ValueError("Model name contains invalid characters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", version or ""):
            raise ValueError("Model version contains invalid characters")
        filename = file.filename or ""
        if not filename or filename != Path(filename).name or "/" in filename or "\\" in filename or Path(filename).suffix.lower() != ".onnx":
            raise ValueError("Only a plain .onnx filename is accepted")

        store_root = Path(settings.MODEL_STORE_PATH).resolve()
        store_dir = (store_root / name / version).resolve()
        if not store_dir.is_relative_to(store_root):
            raise ValueError("Invalid model storage path")
        store_dir.mkdir(parents=True, exist_ok=True)
        dest_path = store_dir / filename
        if dest_path.exists():
            raise ValueError("A model with this name and version already exists")
        size_bytes = 0
        try:
            with open(dest_path, "xb") as buffer:
                while chunk := await file.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > settings.MAX_MODEL_UPLOAD_BYTES:
                        raise ValueError("Model file exceeds configured upload limit")
                    buffer.write(chunk)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise

        # Try to read ONNX metadata (task type, class names) for better registry info
        meta_extra, model_classes = await asyncio.to_thread(_read_onnx_metadata, dest_path)
        if model_classes:
            classes = model_classes

        model = VisionModel(
            name=name,
            version=version,
            framework="onnx",
            file_path=str(dest_path),
            classes=classes,
            input_width=640,
            input_height=640,
            confidence_threshold=0.5,
            nms_threshold=0.45,
            is_active=False,
            metadata_json={"original_filename": filename, "size_bytes": size_bytes, **meta_extra},
        )
        db.add(model)
        await db.commit()
        await db.refresh(model)
        return model

    @staticmethod
    async def activate_model(db: AsyncSession, model_id: str) -> Optional[VisionModel]:
        """Swaps the active model in DB and in-memory InferenceEngine."""
        model = await ModelService.get_model_by_id(db, model_id)
        if not model:
            return None

        if not model.file_path or not os.path.exists(model.file_path):
            logger.error("Cannot activate model '%s': file not found at %s", model.name, model.file_path)
            return None

        # Update runtime app_state and reload engine
        engine = await asyncio.to_thread(
            InferenceEngine,
            model_path=model.file_path,
            classes=model.classes,
            input_size=(model.input_width, model.input_height),
            confidence_threshold=model.confidence_threshold,
            nms_threshold=model.nms_threshold,
            device=settings.INFERENCE_DEVICE,
        )

        if not engine.is_loaded:
            logger.error("Failed to load engine for model '%s' from %s", model.name, model.file_path)
            return None

        # Restore saved task mode (detect or segment)
        meta = model.metadata_json or {}
        if meta.get("task"):
            engine.task = str(meta["task"]).strip().lower()

        # Deactivate all models and mark this one active
        await db.execute(update(VisionModel).values(is_active=False))
        model.is_active = True
        if engine.classes and engine.classes != model.classes:
            model.classes = engine.classes
        await db.commit()
        await db.refresh(model)

        app_state.active_model = {
            "id": model.id,
            "name": model.name,
            "version": model.version,
            "file_path": model.file_path,
            "classes": engine.classes or model.classes,
            "engine": engine,
        }

        # Persist active model in system state
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            SettingsPersistenceService.update_settings(
                {"active_model_id": model.id},
                username="system",
                role="ADMIN",
                clearance_level=3,
            )
        except Exception as exc:
            logger.debug("Failed to persist active_model_id: %s", exc)

        logger.info("Model '%s' (%s) successfully activated in memory", model.name, model.version)
        return model

    @staticmethod
    async def delete_model(db: AsyncSession, model_id: str) -> bool:
        """Delete a model record from DB and remove its file from model_store."""
        model = await ModelService.get_model_by_id(db, model_id)
        if not model:
            return False

        # Do not leave inference pointing at a removed file or silently
        # deactivate a model that is still selected in the database.
        if model.is_active or (app_state.active_model and app_state.active_model.get("id") == model_id):
            raise ValueError("Deactivate the model before deleting it")

        # Remove only files contained by the configured store, and retain the
        # database record if the file cannot be removed.
        store_root = Path(settings.MODEL_STORE_PATH).resolve()
        try:
            fp = Path(model.file_path).resolve()
            if not fp.is_relative_to(store_root):
                raise ValueError("Refusing to delete a model file outside MODEL_STORE_PATH")
            if fp.exists():
                fp.unlink()
            # Remove empty parent dirs up to model_store root
            parent = fp.parent
            for _ in range(3):
                if parent != store_root and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
                else:
                    break
        except Exception as exc:
            logger.exception("Could not safely remove model file for model %s", model_id)
            raise ValueError("Could not safely remove model file; database record was retained") from exc

        await db.delete(model)
        await db.commit()
        logger.info("Model '%s' (%s) deleted from registry", model.name, model.version)
        return True
