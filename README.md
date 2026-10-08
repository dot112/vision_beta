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
- **Sparkplug B**: each production line published as a device of a Sparkplug B edge node, for Ignition and other SCADA hosts, with optional start/stop and counter-reset commands
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
- Cameras, the MQTT broker and PLCs may be off when the server starts. It waits at most `STARTUP_CONNECT_WAIT_SECONDS` for them, then serves the API while they connect in the background. A camera that could not be opened is retried with growing pauses (up to `CAMERA_RECONNECT_MAX_SECONDS`); a stream that drops is reopened the same way. Each MQTT channel has its own broker connection and reconnects on its own (1 s, doubling up to 60 s). PLC connections are retried on the next operation, with the fail-safe watchdog raising alarms meanwhile.
- The Line dashboard's **Alarms** panel lists the line's active alarms and those every line shares (PLC connection, inference, flows), with severity, since when, how often it repeated and who acknowledged it; **Cleared** shows the recently cleared ones. **Acknowledge** (any signed-in user) records that someone has seen an alarm; it stays listed until its cause is gone. The alarm count next to the line state counts the same alarms and opens the panel. `GET /api/v1/alarms?line_id=…&include_all_lines=true` gives the same list.
- Alarms clear themselves once their cause is gone: flow alarms on the node's or flow's next clean run (or when the flow is changed or deleted), the overload alarm once the backlog halves, a PLC that did not disconnect cleanly on its next connect, and a PLC action's failure alarm when the action is deleted or switched off. `GET /api/v1/alarms/catalog` lists every kind of alarm with its label and whether it belongs to one line or to all.
- `/health` answers from memory and never runs inference or touches a camera. It returns 503 only when the database is down; a camera or PLC fault shows as `degraded` with status 200, so a cable fault does not restart the server.

**Stopping.** `docker compose stop` (or `down`, a host shutdown or a Docker restart) sends SIGTERM and allows 60 s. Open video streams close at once, queued PLC operations finish, every PLC output with a safe state goes to it (all PLCs at once), then telemetry, MQTT, cameras and the database are closed.

**Security.** Unprivileged user (uid 10001), read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, memory and process-count limits, no `.env` or keys in the image (`.env` is read at start). Only the HTTP port is published; `VISION_BIND_ADDRESS` limits it to one network card. Passwords in camera URLs and tokens are masked in the logs. The API is plain HTTP for the local network; its accounts and API keys are what protect it.

**Hardware.** A container only gets the hardware it is given when it is created, so the machine is checked on the host, before the container starts:

```bash
python3 docker/detect_hardware.py --up    # Windows: python docker\detect_hardware.py --up
```

The script writes `docker-compose.override.yml` for this machine (Compose reads it together with `docker-compose.yml`; it is not committed), then builds and starts. The usual `docker compose` commands use the hardware from then on. Run it again after adding or removing hardware: the container does not start while a GPU or device listed in the file is missing. `--dry-run` only prints what it found, and deleting the file goes back to the plain CPU setup.

| Host | Inference | USB cameras, serial adapters |
| --- | --- | --- |
| Linux PC (x86-64), NVIDIA GPU, NVIDIA Container Toolkit installed | GPU (CUDA) | passed in |
| Windows PC, NVIDIA GPU, Docker Desktop | GPU (CUDA) | cannot be passed in |
| Jetson, Raspberry Pi and other arm64 boards | CPU | passed in |
| Anything else | CPU | passed in on Linux only |

- *NVIDIA GPU.* The script builds the image with `ACCEL=nvidia` (ONNX Runtime's CUDA build plus CUDA and cuDNN, which makes it several GB larger) and reserves the GPU for the container. If the GPU cannot be initialised at run time, ONNX Runtime logs the error and the model runs on the CPU. Tested on Windows 11 with Docker Desktop and a GTX 1650 (a YOLO26s model at 36 ms per frame, against 244 ms on that machine's CPU); not yet tried on a Linux host.
- *Linux, USB cameras and RS-485 adapters.* The script passes in every `/dev/video*`, `/dev/ttyUSB*` and `/dev/ttyACM*` with the group that owns it; listing them by hand under `devices:` and `group_add:` in `docker-compose.yml` works too. A listed device that is missing stops the container from starting. A USB camera that stops delivering frames (unplugged and plugged back in, for example) is reopened; this has been tested with a simulated device only.
- *Host network.* The discovery beacon (UDP 8888) and some PLC or camera setups need the host's own address; remove `ports:` and uncomment `network_mode: host`.
- *Windows and macOS (Docker Desktop).* Runs as is with IP cameras and network PLCs; keep the named volumes (a bind mount of a Windows folder can refuse SQLite's WAL mode). USB cameras and serial ports cannot be passed into the container there; run the server natively (`start_server.bat`) for those.
- *NVIDIA Jetson and other arm64 boards.* The script recognises a Jetson, passes its cameras and serial adapters in and builds the CPU image. The arm64 image has been built and has run a model under emulation on a PC, not on a real board. For the GPU on a Jetson, build on NVIDIA's L4T/JetPack base image with the ONNX Runtime GPU wheel for that JetPack version, add `runtime: nvidia` to the service (with the NVIDIA container runtime installed) and set `INFERENCE_DEVICE=auto`. This is not part of this image and has not been tested. The original Jetson Nano (JetPack 4, Python 3.6, CUDA 10.2) has no ONNX Runtime GPU build for the Python and package versions this server uses, so it can only run inference on the CPU.

**MQTT broker (optional).** A site with no MQTT broker gets a Mosquitto beside the server:

```bash
# .env: set BROKER_PASSWORD (and BROKER_USERNAME, vision by default)
docker compose --profile broker up -d
```

Without `--profile broker` it is not started, and an existing deployment does not change. Every client signs in with the one login from `.env` (the password file is written from it on every start); the broker does not start while `BROKER_PASSWORD` is empty. Under Connections, add an MQTT channel with host `mqtt`, port 1883 and that login; Ignition and other devices use this machine's address and `BROKER_PORT` (1883 by default; `BROKER_BIND_ADDRESS` limits it to one network card). Its retained messages are on the `vision-broker` volume. It has no TLS: keep it on the plant network. This service has not been started under Docker yet (its configuration was run with a local Mosquitto): watch `docker compose logs mqtt` on the first start.

### Production lines (version 2)

One server runs several production lines. Each line has up to two cameras, its own wirelines, classes, counters, send cards and PLC action cards, and runs on its own; the connections set up on the Connections page are shared by every line. Only Supervisor and Admin accounts can change lines and product codes; every user sees every line.

**Upgrading from version 1.** On the first start the server copies `data/system_state.json` to `data/system_state.v1-backup.json` and the SQLite database to `*.v1-backup.db`, adds an empty `products` table, and turns the current setup into **Line 1** (same settings, active camera and PLC cards). Line 1 keeps its settings in the old keys, so every existing API call and the dashboard answer for Line 1 when no line is named. To go back, export the product list if you need it, put the database backup back in place and start version 1; it runs Line 1 and ignores the other lines.

**Later upgrades.** The settings file carries a `schema_version`; a newer server upgrades it once on its first start, so a line keeps doing what it did: the two "connect on start" switches became the one on the Cameras page, the product codes became **List 1** (every code reader stays set to it), each line's "send results" settings became send cards that send the same messages, each vision camera got the model and the class lists its line used (see **AI models** below), and each send card on an MQTT channel got the topic that channel published to.

**Setting up a line** (dashboard, Supervisor or Admin):

1. **Lines** → enter a name → **Add line**. A new line starts stopped and without cameras. **Clone** copies a line's logic and PLC cards, but not its cameras.
2. Pick the line in the header's **Line** selector and open **Line setup**. In step 1, choose up to two cameras and their role:
   - *Vision*: counting and inspection with the model chosen for the camera. Its box has a **Vision model** dropdown (the models uploaded on the AI models page), a line saying whether that model is loaded, and two lists filled from the model's own class names: **Products to count** and **Defects to reject**. A line cannot be saved with a vision camera that has no model. With nothing ticked in either list, every class the model finds counts as a product. With two vision cameras, the **counting camera** gives the line totals; the other counts on its own and only fires PLC cards that name it. **Also read QR codes and barcodes** makes a vision camera decode codes too, on its own thread beside the model, with the same **Read codes** choices as a QR reader: one camera can count products and read their codes. Its reads show under **Latest code reads** on the Line dashboard (with **Test picture**), are outlined on its AI view, and go out like a QR reader's.
   - *QR reader*: reads QR codes, Data Matrix and 1D barcodes (with zxing-cpp) and checks each code against **Products**. **Read codes** sets when:
     - *Continuously*: every frame. A code seen in consecutive frames counts once; it counts again after it has been out of view for the camera's hold time.
     - *One picture when a product crosses wire line 1* (or *2*): the counting camera triggers the QR camera, which takes one picture after the **picture delay** (the time the product needs to reach it) and decodes it. The picture shows on the Line dashboard with each code boxed, green for known products and red for unknown ones. A picture without a code is a *no read* (and fires *No code read* cards) even without Sync; before that, the next two frames are tried (within 0.4 s). **Test picture** on the dashboard takes one on demand to check aim and focus; it sends nothing.
     - A frame where nothing reads gets a second pass: code-like areas are sharpened and deblurred for straight motion blur of 3 to 15 px along or across the picture (40 ms per frame when reading continuously, 150 ms for a single picture). It reads codes smeared by a moving conveyor that the first pass misses, but a short exposure (fast shutter, more light) is still the real fix: the code should move well under the size of one of its squares while the picture is taken. While nothing reads, a continuously reading camera tries the second pass at most ten times a second.
     - A camera reads codes only in the *QR reader* role or with **Also read QR codes and barcodes** on, and only while its line is running.
   - A camera that reads codes also has an **Action**, a **Product list** and **When no code is read**:
     - *Report only* (the default): the code is shown and can fire code triggers; the product count is not changed.
     - *Accept only listed codes*: a product whose code is not in the camera's product list is rejected.
     - *Reject listed codes*: a product whose code is in the list is rejected.
     - With one of the two checks, a product gets **one result, decided once**: it is good only when the vision camera found it good and its code passed. The line holds the product until its code arrives or the Sync window ends, then counts it and fires the cards once. Sync is switched on for this. *When no code is read* says what happens to a product without a code: *Reject*, or *Ignore* (the vision camera alone decides). A reject by code counts and fires reject cards exactly like a reject by the vision camera, and a reject card's travel delay runs from the moment the product crossed the count line, so it must be longer than the Sync window (Line setup warns when it is not).
     - On a line with only a reader and no vision camera, the code alone decides: each code read is one product, good or reject. The same code counts again only after it has been out of view for the hold time, so this is reliable only when products pass further apart than that.
   - **Sync** (one vision camera and one QR reader looking at the same spot, or one vision camera that also reads codes): each counted product goes out with the code read inside the sync window around its crossing; a picture taken on a wire line crossing pairs only with the product that triggered it. A product without a code goes out as *no read*; a code without a product goes out *unpaired*. Products must pass further apart than the window, and a reject card's travel delay must be longer than it.
   - **Minimum processed frames/s**: the line raises an alarm when it falls below this.
   - **Yield target (%)**: the Line dashboard marks the yield as below target under this value; 0 means no target.
3. Set the count lines, the send cards and the PLC actions in steps 2 to 4. **Send results** works like PLC actions: add a card, pick the one MQTT, TCP or webhook channel it sends to, its trigger and condition, and what the message contains (or the whole message). An MQTT channel is the broker (address, login, TLS, MQTT 3.1.1 or 5, QoS and retain) and the card names the **topic**, so several cards can publish to different topics on one broker, and two MQTT channels can be two brokers at once. For a plant broker reached by IP address over TLS, upload its CA certificate and enter the name on the broker's certificate as the server name; **Test message** sends it once with example values, and **Apply** saves the cards. To report to two systems, add two cards. A send card has no delay: the message goes out as soon as the product's result is known. PLC cards and send cards share the same triggers and the same check on the server, and *good* and *rejected* mean the product's final result. Cards gain the triggers *QR code read*, *Known code*, *Unknown code* and *No code read (Sync)*, and an optional camera. The *Line started / stopped* trigger fires when the line is started or stopped (from the dashboard or the API), with the conditions *On* (starts), *Off* (stops) or *Toggled* (either), for example to switch a tower light. The *Alarm raised / cleared* trigger fires on the alarms ticked in the card's list (or *Any alarm*), with the conditions *Raised* (a ticked alarm goes on), *Cleared* (none of them is on any more) or either; for a lamp, use one card that sets the output on *Raised* and one that resets it on *Cleared*. Camera, frame-rate and PLC action alarms fire only their own line's cards; PLC connection, inference and flow alarms fire every line's. **Cards / List** above the PLC actions switches how they are shown. When two lines write the same PLC address, the Lines page lists it as a warning.
4. **Start** the line. Its cameras connect without disconnecting other lines' cameras (at most `MAX_CONNECTED_CAMERAS`, default 8, across all lines). **Connect cameras when the server starts** on the Cameras page is the one switch for all lines: when it is on, the cameras of every running line are connected at startup (and retried in the background while they are offline). A camera that belongs to no line is connected by hand.

The **Line dashboard** shows the line's state, yield and counts, the counting camera and, beside it at the same size, its second camera (for a QR reader: the last picture or the live view). Video is only sent while the Line dashboard (or Plant overview with live tiles) is open and the browser tab is visible; it stops when you leave and resumes when you come back. Each camera's **Max Stream Width** and **JPEG Quality %** (Cameras → **Settings**) set the size of its video. **Region of Interest** in the same window crops each camera's picture before it reaches the AI model, the QR reader and the video: drag on the camera's picture to draw it. The crop is taken after resizing, rotation and flipping, so it is drawn on the picture as the video shows it, and it keeps its own size. On the AI view, count line A (azure, *Entry*) and B (amber, *Count*) are drawn with arrows showing the flow between them, and line B flashes when a product is counted.

**AI models**: each vision camera runs the model chosen for it on Line setup, so several models run at the same time; cameras that choose the same model share one loaded copy. On the **AI models** page a model shows as *Active* while at least one camera runs it, with the line and camera under **Used by**; there is no Activate button, and a model that is in use cannot be deleted. A model that cannot be loaded (a missing or damaged file, or no memory left on the GPU) raises the alarm *Camera has no model it can run* for each camera of a running line that needs it; Line setup shows the reason in that camera's box, and nothing is detected on that camera until it loads. A class name saved before the lists were picked from the model, and which the model does not have, is shown as *not in this model* with a warning. All loaded models share the machine's one GPU: on a small GPU, check the alarms after adding a model.

Calls written for the one active model of version 1 keep working, where that server's one camera is now Line 1's counting camera: `POST /api/v1/models/{id}/activate` picks the model for Line 1's counting camera (the same as choosing it on Line setup), `GET /api/v1/models/active` returns that model, and the detect calls (`POST /api/v1/vision/detect`, `/vision/detect/camera/{id}`) take an optional `?model_id=`; without it they run the camera's own model, else the model of Line 1's counting camera, and fail with a message when there is none. `GET /api/v1/models` gives each model's `is_active`, `used_by`, `loaded` and `load_error`. A model file left in `model_store/` without being uploaded or registered is no longer loaded by itself.

**Product lists**: **Products** page, or the API. There are up to four lists; the page shows one at a time, chosen in **List shown**, and each camera that reads codes is set to one list on Line setup. CSV files need a header row `code,name` (`description` optional). **Import CSV as a new list** makes a new list named after the file (refused when there are four); **Replace contents** makes the list that is shown hold exactly the rows of a file and keeps its id and name, so the cameras set to it keep working; a list that a camera uses cannot be deleted. The same code may be in more than one list. An MES or ERP system can keep a list current with `POST /api/v1/products/lists/{list_id}/replace` and an API key with `configuration:write` (`GET /api/v1/products/lists` gives the ids).

**API**: `GET /api/v1/lines` and `/lines/overview` list lines with their live figures; `POST /api/v1/lines`, `PUT`/`DELETE /api/v1/lines/{line_id}`, `POST .../start`, `.../stop` and `.../clone` change them (each vision camera in `cameras` carries its `model_id`, `expected_classes` and `defect_classes`); `GET .../qr/recent` gives the latest reads and the last picture's details, `GET .../qr/capture` the last picture (JPEG) and `POST .../qr/capture` takes a test picture. The counting, PLC action, alarm, health and metrics endpoints take an optional `?line_id=`. Events sent to MQTT, TCP and webhooks gain `line_id`, `line_name` and `camera_id`, plus `qr_code`, `qr_status` and `product_name` for synced products; a product's event also carries `reject_reason` (`vision_class`, `code_not_in_list`, `code_in_reject_list` or `no_code`). What a line sends is on its send cards: `GET /api/v1/send/actions`, `POST /api/v1/send/actions/batch` and `POST /api/v1/send/actions/{id}/test` (with `?line_id=`), and `GET /api/v1/send/fields` for what a message can contain; code reads go out as `QR_CODE_READ` events from a card with a code trigger. The product list calls are `GET /api/v1/products/lists`, `POST /api/v1/products/import` (a new list), `POST /api/v1/products/lists/{list_id}/replace`, and `?list_id=` on `GET /api/v1/products` and `/products/export`. Prometheus metrics gain `vision_server_line_*` series with a `line` label. Flows can be limited to one line with a `production_line` value on the trigger node.

Nothing has run against real PLCs yet: start the first plant trial with one line on a spare PLC or a simulator.

### Sparkplug B (Ignition and other SCADA hosts)

Sparkplug B is the plant standard on top of MQTT: fixed topic names, binary payloads, and birth and death messages. A host such as Ignition (with the Cirrus Link MQTT Engine module) then finds the tags by itself and knows when the device behind them is offline. With it switched on for an MQTT channel, this server is an **edge node** on that channel's broker and every production line is one of its **devices**. Send cards on the same channel keep working; Sparkplug uses a connection of its own.

**Setup.** Connections, edit the MQTT channel, section **Sparkplug B**:

| Setting | Meaning |
| --- | --- |
| Publish the production lines as Sparkplug B | On or off for this channel. |
| Group ID, Edge node ID | Where the lines appear in the host, for example `PlantA` and `VisionServer1`. No `/`, `+` or `#`. Two channels to the same broker cannot use the same pair. |
| Primary host ID | Optional. The host ID set in Ignition's MQTT Engine. The lines are then published only while that host reports itself online, and born again when it comes back. |
| Allow commands | Off by default. See **Commands** below. |
| Publish interval | 100 to 60000 ms, 1000 by default. Changed values are sent at most this often. |

In Ignition, point MQTT Engine at the same broker (and set the same Primary Host ID, if one is used). The lines appear under `Edge Nodes/<group>/<edge node>/<line name>`. The channel's card on the Connections page shows the node's state (publishing, waiting for the host, or not connected), and so does `GET /api/v1/telemetry/health` under `components.mqtt.channels[].sparkplug`.

**What each line publishes.**

| Tag | Type | |
| --- | --- | --- |
| `Line/Running`, `Line/Name` | Boolean, String | The line is started; its name. |
| `Counts/Inspected`, `Counts/Good`, `Counts/Rejected` | Int64 | The line's totals. |
| `Counts/Class/<class>` | Int64 | One per class the line's cameras are set to, and any other class that was counted. |
| `Rate/Products Per Minute`, `Quality/Yield Percent`, `Quality/Defect PPM` | Double | |
| `Last Product/Result`, `Class`, `Reject Reason`, `Code`, `Confidence` | String, Double | The latest product. |
| `Last Code/Text`, `Last Code/Status` | String | The latest code read: `known`, `unknown` or `no_read`. |
| `Cameras/<camera name>/Connected` | Boolean | One per camera of the line. |
| `Alarms/Active Count`, `Alarms/Active` | Int32, String | The line's active alarms; their codes, comma separated. |
| `Commands/Reset Counters` | Boolean | Always false; a host writes true. |

The node itself publishes `Node Control/Rebirth`, `Node Info/Software Version` and `bdSeq`.

- Only changed tags are sent. `Last Product` therefore shows the latest product of an interval, not every product; a send card reports every one.
- A line that is added, renamed or deleted is born or dies as a device; a renamed line is a new device. A class counted for the first time, or a camera added to a line, gives that line a new birth, because a tag has to be in the birth before data can be sent for it.
- After an outage of the broker the node is born again with the current totals. Messages are not buffered while the broker is unreachable; the counters are totals, so nothing is lost from them. After a restart of the server the counters start again from where the server starts them, as on the dashboard.
- A rebirth request from the host (`Node Control/Rebirth`) is always answered.
- A server that is stopped or crashes is reported offline at once. One that loses power or its network is reported by the broker after about 20 seconds (the Sparkplug connection pings every 15).
- Class names are published in lower case, as the counters count them: a class set as `Bottle` is the tag `Counts/Class/bottle`.

**Commands.** With **Allow commands** on, a host can write `Line/Running` (true starts the line, false stops it, as the dashboard's buttons do) and `Commands/Reset Counters` = true. Each one is written to the audit log as user `sparkplug:<channel name>`, and the tag's real value is sent back at once, also when a write was refused. Anything else a host writes is ignored. Anyone who can publish to the broker can send these, so leave the switch off unless it is needed, and on a broker with logins per client give the host its own. The bundled broker has one login for every client: with the switch on, every device that holds it can start and stop the lines. A command that a broker kept as a retained message is not carried out.

Tried against Mosquitto, with the messages also read by Eclipse Tahu's own message definition. Not yet tried against Ignition itself: check the first connection there before relying on it.

### Tests and lint

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check .
```

Tests use a temporary database and fake cameras, ONNX inference and PLCs, so no hardware is needed. GitHub Actions runs both, plus a Docker Compose build, start-up, health and clean-stop check, on every push and pull request.

Database migrations run during startup. For access outside a trusted isolated network, terminate TLS at a trusted reverse proxy and expose only HTTPS/WSS; the built-in development server does not configure TLS certificates. The API rate limit (`RATE_LIMIT_PER_SECOND`, per client address) needs the proxy to send `X-Forwarded-For` and uvicorn to trust it: set `FORWARDED_ALLOW_IPS` to the proxy's address in the server's environment (or pass `--forwarded-allow-ips`), otherwise every user behind the proxy shares one limit. In production, query-string WebSocket tokens are disabled. Browser clients should send the token using the `Sec-WebSocket-Protocol` values `industrial-vision-v1` and `bearer.<access-token>`; native clients may use an Authorization bearer header.
