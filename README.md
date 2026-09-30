# fastapi_vision_server

## Industrial Vision API

A FastAPI backend for industrial machine-vision systems, defect detection, and PLC automation.

## Documentation

Swagger UI and ReDoc are disabled by default. Enable `DEBUG=true` in `.env` only during local development to expose `/docs` and `/redoc`.

For scoped API key setup and the YOLO annotated stream endpoint, see [API_KEY_QUICKSTART.md](API_KEY_QUICKSTART.md).

---

## Features

- **Camera HAL**: USB (DirectShow/UVC) and RTSP IP (low-latency TCP); GigE Vision needs a vendor SDK driver that is not included
- **AI Inference Engine**: YOLO ONNX inference through ONNX Runtime; GPU providers depend on platform-specific runtime installation
- **QR & 1D/2D Barcode Engine**: Instant decoding and SKU verification
- **Rule, Action & Flow Engines**: Asynchronous decision logic for configured PLC outputs and MQTT alerts
- **Industrial Protocols**: Modbus TCP, Siemens S7, EtherNet/IP, OPC UA, generic configured TCP frames, and MQTT IoT Telemetry; OPC UA channels can scan a private IPv4 subnet and currently connect through None-security endpoints
- **Live Stream Monitoring**: Real-time WebSockets with live annotated bounding boxes
- **Database & Auth**: SQLAlchemy Async ORM with Alembic migrations & JWT authentication
- **Production Lines**: several independent lines on one server, each with up to two cameras (vision or QR reader), its own counters and PLC actions, sharing the PLC, MQTT, TCP and webhook channels

---

## Quick Start

```bash
# 1. Activate virtual environment
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux/macOS

# 2. Copy .env.example to .env and set unique SECRET_KEY and ADMIN_INITIAL_PASSWORD values
#    SECRET_KEY must be at least 32 characters; initial admin password at least 20.

# 3. Run from the project directory
uvicorn main:app

# 4. Sign in to the dashboard using the initial admin password configured in .env
#    http://localhost:8000/dashboard
```

### Docker Compose (Linux, production)

```bash
cp .env.example .env          # set SECRET_KEY and ADMIN_INITIAL_PASSWORD; never commit it
docker compose up -d --build  # build, start, and keep running
docker compose ps             # STATUS shows (healthy) once the server answers
docker compose logs -f        # follow the server log
```

- **Recovers by itself.** `restart: unless-stopped` restarts the container after a crash. The entrypoint (`docker/supervise.py`) polls `/health` and, when the server stops answering for about a minute (`WATCHDOG_*` settings), stops it so the container restarts too; Docker alone would leave a hung container running as "unhealthy".
- **Survives reboots.** Enable the Docker service once (`sudo systemctl enable docker`); running containers start again with the host. A container stopped with `docker compose stop` stays stopped.
- **Keeps its data.** The database, saved settings, models, uploads, MQTT certificates and logs are named volumes (`vision-data`, `vision-models`, `vision-uploads`, `vision-certs`, `vision-logs`). They survive restarts, rebuilds and `docker compose down`; only `docker compose down -v` deletes them. Back up with e.g. `docker run --rm -v vision-server_vision-data:/data -v "$PWD":/backup alpine tar czf /backup/vision-data.tgz -C /data .` while the server is stopped.
- **Stops safely.** `docker compose stop` sends SIGTERM and allows 60 s: open requests and video streams close, queued PLC operations finish and PLC outputs go to their safe state before the process exits.
- **Upgrading.** `git pull && docker compose up -d --build`; the volumes carry over and database migrations run on start.

The image (`python:3.12-slim`) installs `requirements.txt`, runs as an unprivileged user with all Linux capabilities dropped, and contains no `.env` or keys. It serves ONNX models on CPU; PyTorch and Ultralytics for training are in `requirements-training.txt`. USB cameras and serial (Modbus RTU) adapters must be passed in with `devices:`, and PLCs, IP cameras and the discovery broadcast on the plant network usually need `network_mode: host`; both are commented in `docker-compose.yml`. Logs are capped at 5 × 10 MB.

### Production lines (version 2)

One server runs several production lines. Each line has up to two cameras, its own wirelines, classes, counters, dispatch settings and PLC action cards, and runs on its own; the communication channels set up on the Communications page are shared by every line. Only Supervisor and Admin accounts can change lines and product codes; every user sees every line.

**Upgrading from version 1.** On the first start the server copies `data/system_state.json` to `data/system_state.v1-backup.json` and the SQLite database to `*.v1-backup.db`, adds an empty `products` table, and turns the current setup into **Line 1** (same settings, active camera and PLC cards). Line 1 keeps its settings in the old keys, so every existing API call and both dashboards answer for Line 1 when no line is named. To go back, export the product list if you need it, put the database backup back in place and start version 1; it runs Line 1 and ignores the other lines.

**Setting up a line** (dashboard, Supervisor or Admin):

1. **Lines** → enter a name → **Add line**. A new line starts stopped and without cameras. **Clone** copies a line's logic and PLC cards, but not its cameras.
2. Pick the line in the header's **Line** selector and open **Line Logic**. Under **Line setup** choose up to two cameras and their role:
   - *Vision*: counting and inspection with the line's model. With two vision cameras, the **counting camera** gives the line totals; the other counts on its own and only fires PLC cards that name it.
   - *QR reader*: reads QR codes and 1D barcodes continuously and checks each code against **Products**. A code seen in consecutive frames counts once; it counts again after it has been out of view for the camera's hold time.
   - **Sync** (one vision camera and one QR reader looking at the same spot): each counted product goes out with the code read inside the sync window around its crossing. A product without a code goes out as *no read*; a code without a product goes out *unpaired*. Products must pass further apart than the window, and a reject card's travel delay must be longer than it.
   - **Vision model**: *Server active model* follows the Models page; lines that pick the same model share one loaded copy.
   - **Minimum processed frames/s**: the line raises an alarm when it falls below this.
3. Set the wirelines, classes, dispatch and PLC action cards below as before. Cards gain the triggers *QR code read*, *Known code*, *Unknown code* and *No code read (Sync)*, and an optional camera. When two lines write the same PLC address, the Lines page lists it as a warning.
4. **Start** the line. Its cameras connect without disconnecting other lines' cameras (at most `MAX_CONNECTED_CAMERAS`, default 8, across all lines).

**Product codes**: **Products** page, or the API. CSV files need a header row `code,name` (`description` optional); **Replace the whole list** also removes codes that are not in the file. An MES or ERP system can keep the list current with `POST /api/v1/products/import?replace=true` and an API key with `configuration:write`.

**API**: `GET /api/v1/lines` and `/lines/overview` list lines with their live figures; `POST /api/v1/lines`, `PUT`/`DELETE /api/v1/lines/{line_id}`, `POST .../start`, `.../stop` and `.../clone` change them; `GET .../qr/recent` gives the latest reads. The counting, PLC action, alarm, health and metrics endpoints take an optional `?line_id=`. Events sent to MQTT, TCP and webhooks gain `line_id`, `line_name` and `camera_id`, plus `qr_code`, `qr_status` and `product_name` for synced products; QR reads go out as `QR_CODE_READ` events when a line's *Send QR reads* setting is on. Prometheus metrics gain `vision_server_line_*` series with a `line` label. Flows can be limited to one line with a `production_line` value on the trigger node.

Nothing has run against real PLCs yet: start the first plant trial with one line on a spare PLC or a simulator.

### Tests and lint

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check .
```

Tests use a temporary database and fake cameras, ONNX inference and PLCs, so no hardware is needed. GitHub Actions runs both, plus a Docker Compose build, start-up, health and clean-stop check, on every push and pull request.

Database migrations run during startup. For access outside a trusted isolated network, terminate TLS at a trusted reverse proxy and expose only HTTPS/WSS; the built-in development server does not configure TLS certificates. The API rate limit (`RATE_LIMIT_PER_SECOND`, per client address) needs the proxy to send `X-Forwarded-For` and uvicorn to trust it: set `FORWARDED_ALLOW_IPS` to the proxy's address in the server's environment (or pass `--forwarded-allow-ips`), otherwise every user behind the proxy shares one limit. In production, query-string WebSocket tokens are disabled. Browser clients should send the token using the `Sec-WebSocket-Protocol` values `industrial-vision-v1` and `bearer.<access-token>`; native clients may use an Authorization bearer header.
