from __future__ import annotations

from typing import Any, Dict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.camera import Camera
from app.db.models.detection import DetectionLog
from app.db.models.model import VisionModel
from app.state.application_state import app_state


class TelemetryService:
    @staticmethod
    async def get_stats(db: AsyncSession) -> Dict[str, Any]:
        total_cams = await db.scalar(select(func.count(Camera.id))) or 0
        total_models = await db.scalar(select(func.count(VisionModel.id))) or 0
        
        from app.services.line_service import line_manager

        det_row = (await db.execute(select(func.count(DetectionLog.id), func.avg(DetectionLog.inference_time_ms)))).one_or_none()
        total_inspections = det_row[0] if det_row else 0
        avg_latency = float(det_row[1]) if (det_row and det_row[1] is not None) else 0.0

        return {
            "total_cameras": total_cams,
            "active_cameras": len(app_state.cameras),
            "total_models": total_models,
            # The model of Line 1's counting camera (version 1's active model), and every model in memory.
            "active_model": line_manager.model_name(line_manager.default_model_id()),
            "loaded_models": [model["name"] for model in line_manager.loaded_models()],
            "total_inspections": total_inspections,
            "total_detections": app_state.detection_count,
            "avg_inference_latency_ms": round(avg_latency, 2),
            "processed_frames": app_state.processed_frames,
            "db_ready": app_state.db_ready,
        }
