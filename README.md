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

One container, `vision`, runs the server under a small supervisor (`docker/supervise.py`). Set up once:

```bash
cp .env.example .env          # set SECRET_KEY and ADMIN_INITIAL_PASSWORD (and TZ); never commit it
sudo systemctl enable docker  # start Docker, and with it the server, on every boot
```

| Task | Command |
| --- | --- |
| Build | `docker compose build` |
| Start | `docker compose up -d` |
| Status (shows `healthy` once the server answers) | `docker compose ps` |
| Follow the logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Stop and remove the container (data is kept) | `docker compose down` |
| Rebuild and restart after a code change | `docker compose up -d --build` |
| Health and restart count | `docker inspect --format '{{.State.Health.Status}} restarts={{.RestartCount}}' $(docker compose ps -q vision)` |
| Health report | `curl http://localhost:8000/health` |

**Updating safely.** `git pull`, then `docker compose up -d --build`. The volumes are reused and database migrations run on start; the first start of a new major version writes `*.v1-backup.*` copies of the database and settings first. Never add `-v` to `docker compose down` unless you mean to delete all data. Before an update, a backup is one command while the server is stopped (`docker compose stop`):

```bash
docker run --rm -v vision-server_vision-data:/data -v "$PWD":/backup alpine tar czf /backup/vision-data.tgz -C /data .
```

**What survives what.** Everything the server writes is on a named volume; the rest of the container is read-only.

| Volume (path in the container) | Holds | Restart | `down` / `up` | Rebuild | Host reboot | `down -v` |
| --- | --- | --- | --- | --- | --- | --- |
| `vision-data` (`/app/data`) | SQLite database, saved settings, v1 backups | kept | kept | kept | kept | deleted |
| `vision-models` (`/app/model_store`) | uploaded and OTA models | kept | kept | kept | kept | deleted |
| `vision-certs` (`/app/certs`) | MQTT TLS certificates and keys | kept | kept | kept | kept | deleted |
| `vision-uploads` (`/app/uploads`) | uploads; `.tmp` holds uploads in progress and is emptied on every start | kept | kept | kept | kept | deleted |
| `vision-logs` (`/app/logs`) | `app.log`, rotated at 10 MB, 5 old files kept | kept | kept | kept | kept | deleted |
| `/tmp` (memory, 64 MB) | scratch files | emptied | emptied | emptied | emptied | emptied |

The database runs in SQLite's WAL mode, so copy it only while the server is stopped (or copy the `-wal` file with it), and keep it on a local disk or Docker volume, not a network share.

**Recovery.**
- A crash, a fatal Python error, or the kernel ending the process at the memory limit (`VISION_MEM_LIMIT`) ends the container, and `restart: unless-stopped` starts it again. Docker waits longer between attempts while it keeps failing right after starting.
- A server that hangs (still running, no longer answering `/health`) is stopped by the supervisor after `WATCHDOG_FAILURES` failed checks, and then restarted. Docker's healthcheck alone only marks a container unhealthy; it never restarts it. While the server is still starting (its port not yet open), failed checks do not count, for up to `WATCHDOG_STARTUP_TIMEOUT`.
- Background threads (camera readers, inference, video, QR readers) that crash are logged with their traceback and restarted in place; `worker_restarts_total` in `/api/v1/telemetry/metrics` counts them.
- Cameras, the MQTT broker and PLCs may be off when the server starts. It waits at most `STARTUP_CONNECT_WAIT_SECONDS` for them, then serves the API while they connect in the background. A camera that could not be opened is retried with growing pauses (up to `CAMERA_RECONNECT_MAX_SECONDS`); a stream that drops is reopened the same way. MQTT reconnects on its own (1 s, doubling up to 60 s). PLC connections are retried on the next operation, with the fail-safe watchdog raising alarms meanwhile.
- `/health` answers from memory and never runs inference or touches a camera. It returns 503 only when the database is down; a camera or PLC fault shows as `degraded` with status 200, so a cable fault does not restart the server.

**Stopping.** `docker compose stop` (or `down`, a host shutdown or a Docker restart) sends SIGTERM and allows 60 s. Open video streams close at once, queued PLC operations finish, every PLC output with a safe state goes to it (all PLCs at once), then telemetry, MQTT, cameras and the database are closed.

**Security.** Unprivileged user (uid 10001), read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, memory and process-count limits, no `.env` or keys in the image (`.env` is read at start). Only the HTTP port is published; `VISION_BIND_ADDRESS` limits it to one network card. Passwords in camera URLs and tokens are masked in the logs. The API is plain HTTP for the local network; its accounts and API keys are what protect it.

**Hardware.**
- *Linux, USB cameras and RS-485 adapters.* Uncomment `devices:` and `group_add:` in `docker-compose.yml` and list the devices the machine has (`/dev/video0`, `/dev/ttyUSB0`). A listed device that is missing stops the container from starting. A USB camera that stops delivering frames (unplugged and plugged back in, for example) is reopened; this has been tested with a simulated device only.
- *Host network.* The discovery beacon (UDP 8888) and some PLC or camera setups need the host's own address; remove `ports:` and uncomment `network_mode: host`.
- *Windows and macOS (Docker Desktop).* Runs as is with IP cameras and network PLCs; keep the named volumes (a bind mount of a Windows folder can refuse SQLite's WAL mode). USB cameras and serial ports cannot be passed into the container there; run the server natively (`start_server.bat`) for those.
- *NVIDIA Jetson.* Every package the image installs publishes arm64 wheels, so it should build there and run inference on the CPU; this has not been tried. For the GPU, build on NVIDIA's L4T/JetPack base image with the ONNX Runtime GPU wheel for that JetPack version, add `runtime: nvidia` to the service (with the NVIDIA container runtime installed) and set `INFERENCE_DEVICE=auto`. This is not part of this image and has not been tested.

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
