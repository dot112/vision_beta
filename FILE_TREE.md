# Project File Tree

Curated source, configuration, dashboard, and test files for the Industrial Vision System. Local secrets, virtual environments, model weights, databases, camera/PLC runtime state, logs, uploads and generated caches are omitted.

```text
.
├── PROJECT_DESCRIPTION.md
├── FILE_TREE.md
├── README.md
├── handoff.md
├── .gitignore
├── .env.example
├── main.py
├── dashboard.html
├── action_engine_prototype.html
├── requirements.txt
├── alembic.ini
├── start_server.bat
├── plc_simulator.py
├── modbus port502 test.py
├── test_modbus_full.py
├── assets/
│   └── zajel_logo.png
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 0001_initial_schema.py
│       └── 0002_scoped_api_keys.py
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── dependencies.py
│   ├── core/
│   │   └── security.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models/
│   │       ├── __init__.py
│   │       ├── action.py
│   │       ├── api_key.py
│   │       ├── camera.py
│   │       ├── detection.py
│   │       ├── event.py
│   │       ├── flow.py
│   │       ├── model.py
│   │       ├── product.py
│   │       ├── rule.py
│   │       └── user.py
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── action_engine.py
│   │   ├── flow_engine.py
│   │   ├── inference_engine.py
│   │   ├── qr_engine.py
│   │   ├── rule_engine.py
│   │   └── tracker.py
│   ├── events/
│   │   ├── __init__.py
│   │   ├── alarm_events.py
│   │   ├── detection_events.py
│   │   ├── event_bus.py
│   │   └── system_events.py
│   ├── hardware/
│   │   ├── __init__.py
│   │   ├── camera/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── gige_camera.py
│   │   │   ├── ip_camera.py
│   │   │   └── usb_camera.py
│   │   ├── modbus/
│   │   │   ├── __init__.py
│   │   │   └── client.py
│   │   ├── mqtt/
│   │   │   ├── __init__.py
│   │   │   └── client.py
│   │   └── plc/
│   │       ├── __init__.py
│   │       ├── base.py
│   │       ├── ethernet_ip_driver.py
│   │       ├── factory.py
│   │       ├── generic_tcp_driver.py
│   │       ├── modbus_driver.py
│   │       ├── opcua_driver.py
│   │       └── s7_driver.py
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── error_handler.py
│   │   ├── request_body_limit.py
│   │   ├── security_headers.py
│   │   └── timing.py
│   ├── routes/
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── actions.py
│   │       ├── auth.py
│   │       ├── camera.py
│   │       ├── comms.py
│   │       ├── control.py
│   │       ├── counting.py
│   │       ├── flows.py
│   │       ├── models.py
│   │       ├── mqtt.py
│   │       ├── plc.py
│   │       ├── qr.py
│   │       ├── rules.py
│   │       ├── system.py
│   │       ├── telemetry.py
│   │       ├── vision.py
│   │       └── websocket.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── actions.py
│   │   ├── api_key.py
│   │   ├── auth.py
│   │   ├── camera.py
│   │   ├── counting.py
│   │   ├── detection.py
│   │   ├── flow.py
│   │   ├── modbus.py
│   │   ├── model.py
│   │   ├── mqtt.py
│   │   ├── plc.py
│   │   ├── qr.py
│   │   ├── rules.py
│   │   ├── system.py
│   │   └── vision.py
│   ├── security/
│   │   └── api_key_scopes.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── action_service.py
│   │   ├── auth_service.py
│   │   ├── camera_service.py
│   │   ├── counting_service.py
│   │   ├── discovery_service.py
│   │   ├── modbus_service.py
│   │   ├── model_service.py
│   │   ├── mqtt_service.py
│   │   ├── opcua_discovery_service.py
│   │   ├── ota_service.py
│   │   ├── plc_dispatcher_service.py
│   │   ├── qr_service.py
│   │   ├── rule_service.py
│   │   ├── settings_persistence_service.py
│   │   ├── telemetry_service.py
│   │   └── vision_service.py
│   ├── state/
│   │   ├── __init__.py
│   │   └── application_state.py
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── file_utils.py
│   │   ├── logger.py
│   │   ├── security.py
│   │   ├── upload_limits.py
│   │   └── validators.py
│   └── workers/
│       ├── __init__.py
│       ├── event_worker.py
│       ├── frame_grabber.py
│       ├── inference_worker.py
│       ├── ring_buffer.py
│       └── shared_memory.py
└── tests/
    ├── __init__.py
    ├── test_bytetrack_kalman.py
    ├── test_plc_dispatcher.py
    ├── test_plc_drivers.py
    └── test_session_and_continuous_vision.py
```

## Environment and runtime files not listed

- `.env` — local secrets and deployment settings; never publish or commit it.
- `.venv/` — Python virtual environment.
- `data/`, `logs/`, `uploads/`, `certificates/`, `certs/` — runtime-created data and uploaded files.
- `model_store/` — model assets/weights, which can be large and deployment-specific.
- `__pycache__/`, `.pytest_cache/`, `*.pyc` — generated Python/test caches.
