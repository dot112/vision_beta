# Industrial Vision API

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

Database migrations run during startup. For access outside a trusted isolated network, terminate TLS at a trusted reverse proxy and expose only HTTPS/WSS; the built-in development server does not configure TLS certificates. In production, query-string WebSocket tokens are disabled. Browser clients should send the token using the `Sec-WebSocket-Protocol` values `industrial-vision-v1` and `bearer.<access-token>`; native clients may use an Authorization bearer header.
