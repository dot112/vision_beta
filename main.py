from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.config import settings
from app.middleware.error_handler import unhandled_exception_handler
from app.middleware.request_body_limit import RequestBodyLimitMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.timing import TimingMiddleware
from app.state.application_state import app_state
from app.utils.logger import setup_logging
from app.dependencies import require_operator

# ── Routes ────────────────────────────────────────────────────────────────────
from app.routes.v1 import system as system_router
from app.routes.v1 import camera as camera_router
from app.routes.v1 import vision as vision_router
from app.routes.v1 import models as models_router
from app.routes.v1 import qr as qr_router
from app.routes.v1 import rules as rules_router
from app.routes.v1 import flows as flows_router
from app.routes.v1 import actions as actions_router
from app.routes.v1 import control as control_router
from app.routes.v1 import telemetry as telemetry_router
from app.routes.v1 import websocket as ws_router
from app.routes.v1 import mqtt as mqtt_router
from app.routes.v1 import counting as counting_router
from app.routes.v1 import auth as auth_router
from app.routes.v1 import comms as comms_router
from app.routes.v1 import plc as plc_router
from app.routes.v1 import health as health_router

setup_logging()
logger = logging.getLogger(__name__)


def _upgrade_schema(command_module, alembic_config, sync_connection) -> None:
    from pathlib import Path
    alembic_config.set_main_option("script_location", str(Path(__file__).resolve().parent / "alembic"))
    alembic_config.attributes["connection"] = sync_connection
    command_module.upgrade(alembic_config, "head")


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting up %s v%s [%s]", settings.APP_NAME, settings.APP_VERSION, settings.APP_ENV)

    # 0. Ensure runtime directories exist
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    os.makedirs("uploads", exist_ok=True)

    import asyncio
    from app.services.counting_service import CountingService
    CountingService.set_event_loop(asyncio.get_running_loop())
    from app.events.system_events import set_event_loop
    set_event_loop(asyncio.get_running_loop())

    # 1. Database initialisation
    try:
        from app.db.session import engine, AsyncSessionLocal
        from alembic import command
        from alembic.config import Config
        from pathlib import Path

        alembic_config = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: _upgrade_schema(command, alembic_config, sync_conn))
        app_state.db_ready = True
        logger.info("Database ready and schema verified")

        # 2. Seed Default 3-Level Access Clearance Users & Reset Active Sessions
        async with AsyncSessionLocal() as db:
            from app.services.auth_service import AuthService
            await AuthService.seed_default_users(db)
            AuthService.reset_all_sessions()

        # 3. Automatically load active Vision Model into memory
        async with AsyncSessionLocal() as db:
            from app.db.models.model import VisionModel
            from app.engines.inference_engine import InferenceEngine
            from app.services.vision_service import COCO_CLASSES, DEFAULT_ONNX_PATH

            stmt = select(VisionModel).where(VisionModel.is_active == True)
            res = await db.execute(stmt)
            active_m = res.scalar_one_or_none()

            if active_m and os.path.exists(active_m.file_path):
                engine_inst = InferenceEngine(
                    model_path=active_m.file_path,
                    classes=active_m.classes,
                    input_size=(active_m.input_width, active_m.input_height),
                    confidence_threshold=active_m.confidence_threshold,
                    nms_threshold=active_m.nms_threshold,
                    device=settings.INFERENCE_DEVICE,
                )
                if active_m.metadata_json and active_m.metadata_json.get("task"):
                    engine_inst.task = str(active_m.metadata_json["task"]).strip().lower()
                app_state.active_model = {
                    "id": active_m.id,
                    "name": active_m.name,
                    "version": active_m.version,
                    "file_path": active_m.file_path,
                    "classes": active_m.classes,
                    "engine": engine_inst,
                }
                logger.info("Auto-loaded active model from DB: '%s' (%s) on device '%s'", active_m.name, active_m.version, settings.INFERENCE_DEVICE)
            elif os.path.exists(DEFAULT_ONNX_PATH):
                engine_inst = InferenceEngine(
                    model_path=DEFAULT_ONNX_PATH,
                    classes=COCO_CLASSES,
                    confidence_threshold=0.30,
                    nms_threshold=0.45,
                    device=settings.INFERENCE_DEVICE,
                )
                app_state.active_model = {
                    "name": "YOLOv8n_COCO",
                    "version": "v1.0",
                    "file_path": DEFAULT_ONNX_PATH,
                    "classes": COCO_CLASSES,
                    "engine": engine_inst,
                }
                logger.info("Auto-loaded default YOLOv8n ONNX model (%s)", DEFAULT_ONNX_PATH)

        # 4. Restore Persistent Server Settings & Auto-Connections
        try:
            from app.services.settings_persistence_service import SettingsPersistenceService
            await SettingsPersistenceService.apply_on_startup()
        except Exception as st_err:
            logger.warning("Settings persistence restore error: %s", st_err)

        # 5. Start UDP Auto-Discovery Beacon
        try:
            from app.services.discovery_service import ServerDiscoveryService
            await ServerDiscoveryService.start(http_port=settings.PORT)
        except Exception as disc_err:
            logger.warning("Discovery beacon skipped: %s", disc_err)

        # 6. Start Continuous 24/7 Server Vision Runner (Independent of web clients)
        try:
            from app.services.vision_service import ContinuousVisionRunner
            ContinuousVisionRunner.start()
        except Exception as vr_err:
            logger.warning("Continuous vision runner start error: %s", vr_err)

        from app.engines.flow_engine import bootstrap_flow_engine
        await bootstrap_flow_engine()

        from app.services.health_service import HealthMonitor
        HealthMonitor.start()

    except Exception as exc:
        app_state.db_ready = False
        logger.exception("Startup initialization failed; refusing to serve an unhealthy instance")
        raise RuntimeError("Application startup failed") from exc

    yield  # ← application runs here

    logger.info("Shutting down…")
    try:
        from app.services.health_service import HealthMonitor
        await HealthMonitor.stop()
    except Exception:
        logger.exception("Failed to stop health monitor cleanly")
    try:
        from app.services.vision_service import ContinuousVisionRunner
        ContinuousVisionRunner.stop()
    except Exception:
        logger.exception("Failed to stop continuous vision runner cleanly")
    try:
        from app.engines.flow_engine import FlowEngine
        await FlowEngine.get().shutdown()
    except Exception:
        logger.exception("Failed to stop flow engine cleanly")
    try:
        from app.services.discovery_service import ServerDiscoveryService
        ServerDiscoveryService.stop()
    except Exception:
        logger.exception("Failed to stop discovery service cleanly")
    try:
        from app.services.mqtt_service import MQTTService
        await MQTTService.disconnect()
    except Exception:
        logger.exception("Failed to disconnect MQTT cleanly")

    try:
        from app.services.vision_service import CameraStreamPipeline
        CameraStreamPipeline.stop_all()
    except Exception:
        logger.exception("Failed to stop camera stream workers cleanly")
    try:
        from app.services.counting_service import counting_service
        await counting_service.shutdown()
    except Exception:
        logger.exception("Failed to stop telemetry dispatcher cleanly")
    try:
        from app.services.plc_dispatcher_service import PLCDispatcherService
        await PLCDispatcherService.shutdown()
    except Exception:
        logger.exception("Failed to finish queued PLC operations cleanly")
    try:
        from app.services.plc_failsafe_service import PLCFailsafeService
        await PLCFailsafeService.shutdown()
    except Exception:
        logger.exception("Failed to drive PLC outputs to their safe state")
    try:
        from app.hardware.plc.factory import PLCDriverFactory
        await PLCDriverFactory.close_all()
    except Exception:
        logger.exception("Failed to close PLC drivers cleanly")
    try:
        from app.engines.action_engine import ActionEngine
        await ActionEngine.close()
    except Exception:
        logger.exception("Failed to close action engine HTTP client cleanly")

    from app.db.session import engine
    await engine.dispose()
    from app.events.system_events import set_event_loop
    set_event_loop(None)
    logger.info("Shutdown complete")


# ── App factory ───────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "REST + WebSocket API for industrial machine-vision systems. "
            "Supports 3-Level Access Clearance (Operator, Supervisor, Admin), "
            "YOLO inference, 2-Wireline Counting, Modbus/MQTT, and live video streaming."
        ),
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
        openapi_url="/openapi.json" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    request_overhead_bytes = 1024 * 1024
    app.add_middleware(
        RequestBodyLimitMiddleware,
        limits={
            "/api/v1/models/upload": settings.MAX_MODEL_UPLOAD_BYTES + request_overhead_bytes,
            "/api/v1/vision/detect": settings.MAX_IMAGE_UPLOAD_BYTES + request_overhead_bytes,
            "/api/v1/qr/decode": settings.MAX_IMAGE_UPLOAD_BYTES + request_overhead_bytes,
            "/api/v1/mqtt/certs/upload": 4 * 1024 * 1024 + request_overhead_bytes,
        },
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TimingMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    if os.path.isdir("assets"):
        app.mount("/assets", StaticFiles(directory="assets"), name="assets")

    API_PREFIX = "/api/v1"
    app.include_router(auth_router.router, prefix=API_PREFIX)
    app.include_router(health_router.public_router)

    protected_api = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_operator)])
    for router_mod in (
        system_router,
        camera_router,
        vision_router,
        models_router,
        qr_router,
        rules_router,
        flows_router,
        actions_router,
        control_router,
        telemetry_router,
        mqtt_router,
        counting_router,
        comms_router,
        plc_router,
        health_router,
    ):
        protected_api.include_router(router_mod.router)
    app.include_router(protected_api)
    # WebSockets authenticate with a token in their handshake; browsers cannot
    # set an Authorization header through the native WebSocket constructor.
    app.include_router(ws_router.router, prefix=API_PREFIX)

    @app.get("/api/v1/discovery", summary="Server Discovery & Handshake")
    async def get_discovery_info(request: Request) -> JSONResponse:
        from app.services.discovery_service import get_local_ip
        req_host = request.url.hostname or ""
        host = req_host if req_host in ("127.0.0.1", "localhost", "0.0.0.0") else get_local_ip()
        return JSONResponse({
            "status": "online",
            "server_name": "Industrial Vision Server",
            "version": settings.APP_VERSION,
            "host_ip": host,
            "http_port": settings.PORT,
            "dashboard_url": f"http://{host}:{settings.PORT}/dashboard",
        })

    RESTRICTED_ACCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Access Restricted — Industrial Vision</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
        body { background-color: #191B26; color: #FFFFFF; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
        .lock-card {
            background: #212332; border: 1px solid #2e3247; border-radius: 16px;
            padding: 40px 36px; max-width: 480px; width: 100%; text-align: center;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
        }
        .lock-icon { font-size: 56px; margin-bottom: 16px; }
        .lock-title { font-size: 22px; font-weight: 700; color: #F87171; margin-bottom: 10px; }
        .lock-desc { font-size: 13px; color: #8E92A4; line-height: 1.6; margin-bottom: 24px; }
        .app-badge {
            display: inline-flex; align-items: center; gap: 8px;
            background: rgba(38, 151, 255, 0.12); border: 1px solid rgba(38, 151, 255, 0.3);
            color: #2697FF; padding: 10px 20px; border-radius: 30px; font-size: 13px; font-weight: 600;
        }
        .security-note { margin-top: 20px; font-size: 11px; color: #64748b; }
    </style>
</head>
<body>
    <div class="lock-card">
        <div class="lock-icon">🔒</div>
        <div class="lock-title">Access Restricted</div>
        <div class="lock-desc">
            The Industrial Vision Control Dashboard is available only from the server's local or private network.<br><br>
            Connect from an authorized factory network, then sign in with an assigned account.
        </div>
        <div class="app-badge">
            🔐 Network restricted
        </div>
        <div class="security-note">
            API operations require an authenticated account.
        </div>
    </div>
</body>
</html>"""

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def get_dashboard(request: Request) -> HTMLResponse:
        # Verify if request is from local host, private network (Wi-Fi / LAN), or verified app
        import ipaddress
        client_host = request.client.host if request.client else ""
        req_host = request.url.hostname or ""
        is_private = False
        try:
            is_private = ipaddress.ip_address(client_host).is_private
        except ValueError:
            pass

        is_local = (
            client_host in ("127.0.0.1", "::1", "localhost", "testclient")
            or req_host in ("127.0.0.1", "localhost")
            or is_private
        )
        # This only limits dashboard delivery to the local network. API access
        # is separately authenticated with user tokens and role checks.
        is_app = is_local
        
        if not is_app:
            return HTMLResponse(content=RESTRICTED_ACCESS_HTML, status_code=403)

        with open("dashboard.html", "r", encoding="utf-8") as f:
            return HTMLResponse(
                content=f.read(),
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

    @app.get("/terms-of-use", include_in_schema=False)
    async def get_terms_of_use() -> Response:
        terms_path = Path(__file__).resolve().with_name("terms_of_use.html")
        if not terms_path.is_file():
            return HTMLResponse(content="Terms of Use are unavailable.", status_code=404)
        return FileResponse(
            terms_path,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate, max-age=0"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({
            "message": f"Welcome to {settings.APP_NAME}",
            "docs": "/docs" if settings.DEBUG else None,
            "dashboard": "/dashboard",
        })

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=1 if settings.DEBUG else settings.WORKERS,
        log_level=settings.LOG_LEVEL.lower(),
    )
