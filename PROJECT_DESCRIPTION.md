# Industrial Vision System — Project Description

## Overview

This project is a FastAPI-based industrial machine-vision and automation application. It combines a browser dashboard, REST and WebSocket APIs, camera capture, YOLO/ONNX inference, inspection and counting logic, and configurable actions that can communicate with PLCs and external systems.

It is intended to give operators a single place to monitor cameras and detections, inspect production counts, manage models and rules, configure communication endpoints, and manage actions used by a production line.

## Main capabilities

- **Vision:** Run object detection with YOLO-compatible ONNX models; run a model of its own on each vision camera; configure confidence and NMS thresholds.
- **Camera input:** Support USB cameras and RTSP/IP cameras through camera drivers and services.
- **Tracking and counting:** Track detections, report line crossings and class counts, and expose telemetry through the API/dashboard. Up to 8 cameras per production line; an extra vision camera inspects on its own or joins the counting camera's product result (matched by travel time), so a product inspected by several cameras gets one result.
- **Production records:** Store every product and code read in the database, show them on a Records page with filters and totals, export any time range as CSV or Excel (.xlsx), and keep the line counters through a restart.
- **QR/barcode inspection:** Decode QR and 1D/2D barcode data from uploaded images or camera frames.
- **Rules, actions and flows:** Evaluate inspection rules and route events through action/flow engines. Configured actions can affect production equipment or integrations.
- **Industrial communications:** Provide PLC and communications drivers/services, including S7, Modbus TCP, Ethernet/IP, OPC UA (anonymous/no-security connections), and generic TCP channels (a client with a connection per message or one kept open, or a server that devices connect to, each with its own message delimiter), plus MQTT and webhook-style integrations. Messages are JSON or text from a template, and a PLC write can carry a value of the product (result, counts, class, reject reason) followed by a strobe. Confirm each driver and target-device configuration against the deployment hardware.
- **Dashboard and APIs:** Serve the HTML dashboard, versioned REST endpoints under `/api/v1`, authenticated live video WebSocket routes, and supervisor-only OPC UA endpoint discovery on private IPv4 subnets.
- **Accounts and credentials:** Use JWT authentication for dashboard accounts and scoped, expiring, revocable API keys for integrations. API keys are sent using the `X-API-Key` header.

## Architecture at a glance

1. `main.py` constructs the FastAPI app, mounts the dashboard and API routers, and configures middleware.
2. API route modules validate requests and call services. Authentication and access-clearance checks are applied to protected API routes.
3. Service modules coordinate cameras, inference, counting, persistence, MQTT, PLC dispatch and other system operations.
4. Engine and worker modules handle inference, tracking, QR/barcode processing, rules, action/flow execution and frame processing.
5. Hardware modules provide camera, PLC, Modbus and MQTT driver implementations behind service-level interfaces.
6. SQLAlchemy models and Alembic migrations manage relational records. The default database URL in `app/config.py` is asynchronous SQLite; `DATABASE_URL` can be configured through the environment.

The runtime state is partly process-local. `app/config.py` requires `WORKERS=1`, so do not scale Uvicorn to multiple worker processes without first moving shared state and coordination to suitable external services.

## Important folders and files

- `dashboard.html` — Main browser control and monitoring interface.
- `assets/production_lines.js` — The dashboard's production line pages: line selector, Plant overview, Lines, Products, Production records, Line setup (the line and one card per camera, up to 8, holding every camera setting) and the Line dashboard's camera strip.
- `assets/camera_settings.js` — A camera's image and video settings (resolution, rotation, ROI, USB/IP controls, stream size) as a form, used by the Line setup camera cards and the Cameras page Settings window.
- `main.py` — FastAPI application, lifecycle setup, router registration and static/dashboard serving.
- `app/routes/v1/` — REST and WebSocket route handlers.
- `app/engines/` — Inference, tracking, QR, rules, actions and flow engines.
- `app/services/` — Application services and integration orchestration.
- `app/hardware/` — Camera and communications/PLC driver implementations.
- `app/db/models/`, `app/schemas/`, `alembic/` — Persistence models, API schemas and migrations.
- `tests/` — Automated test suite.
- `requirements.txt` — Python packages the server needs at runtime; `requirements-dev.txt` adds the test tools and `requirements-training.txt` the model training tools.
- `.env.example` — Configuration template. Copy it to `.env` for a local installation; keep the real `.env` private.
- `handoff.md` — Ongoing project handoff notes and known operational context.

See [`FILE_TREE.md`](FILE_TREE.md) for the source and configuration tree. Runtime folders and local secrets are intentionally excluded there.

## Local startup

From the project root, prepare a Python environment, install `requirements.txt`, copy `.env.example` to `.env`, and set a unique `SECRET_KEY` and a strong initial admin password. Then run:

```bash
uvicorn main:app
```

On Windows, `start_server.bat` creates/uses `.venv` and starts Uvicorn on `0.0.0.0:8000`. Database migrations run during application startup. API docs are controlled by the `DEBUG` setting.

## Deployment and security notes

- The built-in Uvicorn launcher serves HTTP; it does not set up TLS. For deployment beyond a trusted isolated development network, terminate TLS at a trusted reverse proxy and expose HTTPS/WSS.
- Never commit or share `.env`, API-key values, passwords, certificates, model secrets, or production configuration exports.
- API keys are encrypted at rest and can be revealed by dashboard admins. Give each integration only the scopes it needs, assign it to a suitable active account, and revoke/replace keys when access changes.
- PLC and action endpoints can cause physical side effects. Validate drivers, PLC addressing, interlocks, test procedures, and hardware safety controls on the target equipment before production use.
- ONNX execution providers and GPU acceleration depend on the operating system, installed runtime package and available hardware. Confirm the selected provider on the actual deployment computer.
