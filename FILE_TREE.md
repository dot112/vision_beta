# Project File Tree

Curated source, configuration, dashboard, and test files for the Industrial Vision System. Local secrets, virtual environments, model weights, databases, camera/PLC runtime state, logs, uploads and generated caches are omitted.

```text
.
├── .dockerignore
├── .env.example
├── .gitattributes
├── .gitignore
├── action_engine_prototype.html
├── alembic.ini
├── API_KEY_QUICKSTART.md
├── dashboard.html
├── docker-compose.yml
├── Dockerfile
├── FILE_TREE.md
├── FUTURE_FIXES.md
├── main.py
├── PROJECT_DESCRIPTION.md
├── pyproject.toml
├── README.md
├── requirements-dev.txt
├── requirements-industrial.txt   # optional: GigE Vision / USB3 Vision cameras (harvesters)
├── requirements-runtime.txt
├── requirements-training.txt
├── requirements.txt
├── start_server.bat
├── THIRD_PARTY_NOTICES.md
├── terms_of_use.html
├── .github/
│   └── workflows/
│       └── ci.yml
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 0001_initial_schema.py
│       ├── 0002_scoped_api_keys.py
│       ├── 0003_encrypted_api_key_secrets.py
│       ├── 0004_products.py
│       ├── 0005_product_lists.py
│       └── 0006_product_records.py
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── dependencies.py
│   ├── core/
│   │   ├── api_key_crypto.py
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
│   │       ├── production_record.py   # product_records: every product and code read
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
│   │   │   ├── genicam_backend.py   # the calls into the GenICam library (the only file that imports harvesters)
│   │   │   ├── genicam_sim.py       # a stand-in industrial camera (tests, and GENTL_CTI_PATH=simulator)
│   │   │   ├── gige_camera.py       # GigE Vision / USB3 Vision driver: trigger modes, the picture queue
│   │   │   ├── http_capture.py
│   │   │   ├── ip_camera.py
│   │   │   └── usb_camera.py
│   │   ├── modbus/
│   │   │   ├── __init__.py
│   │   │   ├── client.py
│   │   │   └── rtu_client.py
│   │   ├── mqtt/
│   │   │   ├── __init__.py
│   │   │   ├── client.py
│   │   │   └── sparkplug_codec.py
│   │   └── plc/
│   │       ├── __init__.py
│   │       ├── _common.py
│   │       ├── base.py
│   │       ├── ethernet_ip_driver.py
│   │       ├── factory.py
│   │       ├── fins_driver.py
│   │       ├── generic_tcp_driver.py
│   │       ├── melsec_driver.py
│   │       ├── modbus_driver.py
│   │       ├── opcua_driver.py
│   │       └── s7_driver.py
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── error_handler.py
│   │   ├── rate_limit.py
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
│   │       ├── health.py
│   │       ├── lines.py
│   │       ├── models.py
│   │       ├── mqtt.py
│   │       ├── plc.py
│   │       ├── products.py
│   │       ├── qr.py
│   │       ├── records.py             # production records: list, summary, CSV / Excel export
│   │       ├── rules.py
│   │       ├── send_actions.py
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
│   │   ├── card_triggers.py
│   │   ├── counting_service.py
│   │   ├── discovery_service.py
│   │   ├── health_service.py
│   │   ├── line_config.py
│   │   ├── line_service.py
│   │   ├── modbus_service.py
│   │   ├── model_service.py
│   │   ├── mqtt_service.py
│   │   ├── opcua_cert_service.py    # uploaded client certificates and keys of secured OPC UA channels
│   │   ├── opcua_discovery_service.py
│   │   ├── ota_service.py
│   │   ├── plc_dispatcher_service.py
│   │   ├── plc_failsafe_service.py
│   │   ├── plc_input_service.py   # PLC signals: reads each channel's signal cards and acts on the line
│   │   ├── product_service.py
│   │   ├── production_records_service.py   # recorder (queue + batch writer, retention), filters, summary, restored counters
│   │   ├── qr_service.py
│   │   ├── records_export.py   # CSV and .xlsx (zipfile + XML) files of the records
│   │   ├── rule_service.py
│   │   ├── send_dispatcher_service.py
│   │   ├── settings_persistence_service.py
│   │   ├── sparkplug_metrics.py
│   │   ├── sparkplug_service.py
│   │   ├── tcp_channels.py
│   │   ├── trigger_inspection.py   # one picture, one product, for a vision camera in trigger mode
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
│   │   ├── threads.py
│   │   ├── upload_limits.py
│   │   └── validators.py
│   └── workers/
│       ├── __init__.py
│       ├── event_worker.py
│       ├── frame_grabber.py
│       ├── inference_worker.py
│       ├── ring_buffer.py
│       └── shared_memory.py
├── assets/
│   ├── camera_settings.js     # a camera's image/video settings form (Line setup cards, Cameras dialog)
│   ├── dashboard.css
│   ├── plc_signals.js         # the PLC signals (inputs) step of Line setup: cards, dialog, live values
│   ├── production_lines.js    # lines, Line setup camera cards, Line dashboard camera strip
│   ├── zajel_logo.png
│   ├── zajel_logo_small.png
│   └── fonts/
│       ├── inter-latin.woff2
│       └── Inter-LICENSE.txt
├── docs/
│   ├── progress/          # handoff files: the work split into 5 sections (README.md first)
│   │   ├── README.md
│   │   ├── section-1-logic-fixes.md
│   │   ├── section-2-camera-cards.md
│   │   ├── section-3-joined-cameras.md
│   │   ├── section-4-product-records.md
│   │   ├── section-5-plc-inputs.md
│   │   ├── session-prompts.md   # the first message for each section's session
│   │   └── screenshots/         # dashboard screenshots a section took (section-2/, section-3/: Line setup, Line dashboard; section-4/: Production records; section-5/: PLC signals, their dialog, the batch field)
│   ├── real-life-tests/
│   │   └── triggered-cameras.md # what to check on the first real industrial camera, step by step
│   └── superpowers/
│       ├── plans/           # the step-by-step plan a piece of work was built from
│       │   ├── 2026-10-06-sparkplug-b.md
│       │   ├── 2026-10-12-section-5-plc-inputs.md
│       │   └── 2026-10-13-triggered-industrial-cameras.md
│       └── specs/
│           └── 2026-10-06-sparkplug-b-design.md
├── docker/
│   ├── mosquitto/
│   │   ├── mosquitto.conf
│   │   └── start.sh
│   ├── detect_hardware.py
│   └── supervise.py
├── test codes/
│   ├── modbus port502 test.py
│   ├── opc au tag control.py
│   ├── opc ua server scan detection and DB control.py
│   ├── industrial_camera_check.py   # lists the industrial cameras found and prints what one offers
│   ├── plc_simulator.py
│   └── test_modbus_full.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── mqtt_broker.py
    ├── sparkplug_helpers.py
    ├── test_alarm_events.py
    ├── test_api.py
    ├── test_auto_connect.py
    ├── test_batch.py
    ├── test_bundled_broker.py
    ├── test_bytetrack_kalman.py
    ├── test_camera_cards.py
    ├── test_camera_models.py
    ├── test_camera_orientation.py
    ├── test_camera_roi_and_qr.py
    ├── test_camera_trigger_modes.py # triggered cameras on a line: products and code reads from each picture
    ├── test_code_types.py
    ├── test_detect_hardware.py
    ├── test_error_alarms.py
    ├── test_flow_engine.py
    ├── test_health.py
    ├── test_ip_camera_http.py
    ├── test_industrial_camera.py    # the GigE / USB3 Vision driver and its API, on a stand-in camera
    ├── test_inputs_see_output_pulses.py # an input reads what an output action's pulse switches on
    ├── test_joined_cameras.py
    ├── test_lines.py
    ├── test_lines_api.py
    ├── test_logic_fixes.py
    ├── test_messages_and_plc_values.py
    ├── test_mqtt_channels.py
    ├── test_network_camera_channel.py # the network camera card: IP camera or GigE Vision
    ├── test_opcua_certs.py          # uploading and choosing OPC UA certificates and keys
    ├── test_opcua_tag_names.py      # an OPC UA address as a tag's name: finding its full NodeId
    ├── test_mqtt_client.py
    ├── test_plc_action_persistence.py
    ├── test_plc_dispatcher.py
    ├── test_plc_drivers.py
    ├── test_plc_failsafe.py
    ├── test_plc_inputs.py
    ├── test_plc_inputs_e2e.py   # a real Modbus TCP driver against test codes/plc_simulator.py
    ├── test_plc_protocols.py
    ├── test_plc_read.py
    ├── test_product_lists.py
    ├── test_production_records.py
    ├── test_qr_trigger.py
    ├── test_reader_actions.py
    ├── test_reject_confirmation.py
    ├── test_request_load_protection.py
    ├── test_resilience.py
    ├── test_rule_engine.py
    ├── test_send_cards.py
    ├── test_session_and_continuous_vision.py
    ├── test_snapshot_width.py
    ├── test_sparkplug_api.py
    ├── test_sparkplug_codec.py
    ├── test_sparkplug_commands.py
    ├── test_sparkplug_metrics.py
    ├── test_sparkplug_node.py
    ├── test_sparkplug_service.py
    ├── test_trigger_inspection.py
    ├── test_trigger_numbers.py      # a Take a picture input's trigger number; the triggers a camera ticks
    └── test_vision_pipeline.py
```

## Environment and runtime files not listed

- `.env` — local secrets and deployment settings; never publish or commit it.
- `.venv/` — Python virtual environment.
- `data/`, `logs/`, `uploads/`, `certificates/`, `certs/` — runtime-created data and uploaded files.
- `model_store/` — model assets/weights, which can be large and deployment-specific.
- `backup/`, `docker-compose.override.yml` — local backups and scratch test tools, and this machine's hardware settings written by `docker/detect_hardware.py`.
- `__pycache__/`, `.pytest_cache/`, `*.pyc` — generated Python/test caches.
