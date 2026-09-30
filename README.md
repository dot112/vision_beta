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

### Docker (Linux)

```bash
docker build -t fastapi-vision-server .
docker run -d --name vision -p 8000:8000 --env-file .env \
  -v vision-data:/app/data -v vision-models:/app/model_store -v vision-logs:/app/logs \
  fastapi-vision-server
```

The image serves ONNX models on CPU and leaves out PyTorch/Ultralytics. USB cameras need `--device /dev/video0`; PLCs and IP cameras on the plant network usually need `--network host`.

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
pip install -r requirements-runtime.txt -r requirements-dev.txt
pytest
ruff check .
```

Tests use a temporary database and fake cameras, ONNX inference and PLCs, so no hardware is needed. GitHub Actions runs both, plus a Docker build and start-up check, on every push and pull request.

Database migrations run during startup. For access outside a trusted isolated network, terminate TLS at a trusted reverse proxy and expose only HTTPS/WSS; the built-in development server does not configure TLS certificates. The API rate limit (`RATE_LIMIT_PER_SECOND`, per client address) needs the proxy to send `X-Forwarded-For` and uvicorn to trust it: set `FORWARDED_ALLOW_IPS` to the proxy's address in the server's environment (or pass `--forwarded-allow-ips`), otherwise every user behind the proxy shares one limit. In production, query-string WebSocket tokens are disabled. Browser clients should send the token using the `Sec-WebSocket-Protocol` values `industrial-vision-v1` and `bearer.<access-token>`; native clients may use an Authorization bearer header.
