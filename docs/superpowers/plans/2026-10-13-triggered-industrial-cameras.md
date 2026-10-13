# Triggered industrial cameras (hardware and software trigger) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An industrial camera (GigE Vision or USB3 Vision) can be connected, and it takes one picture per trigger: from a wire on its own trigger input (hardware trigger) or from a command the server sends it (software trigger). Each picture is inspected and becomes one product.

**Architecture:** The stub `GigECamera` becomes a real driver on top of GenICam (the `harvesters` library plus the camera maker's GenTL producer file). In a trigger mode the driver keeps every picture in a small queue, the frame runner hands them to the model in order, and `trigger_inspection.py` (Section 5) makes one product of each. Nothing is added to a line's saved settings: the camera's trigger mode is a setting of the camera, and the line reads it from the connected driver.

**Tech Stack:** Python 3.12+, FastAPI, `harvesters` (GenICam / GenTL, optional install), OpenCV for pixel conversion; one HTML dashboard with plain JavaScript.

**Spec:** the owner's request of 2026-10-13, written out under "What it must do" below. There is no separate spec file. Rules and the project map: [docs/progress/README.md](../../progress/README.md). It builds on Section 5: [docs/progress/section-5-plc-inputs.md](../../progress/section-5-plc-inputs.md) ("Trigger inspection").

## What it must do

Of the three trigger families the owner listed, this plan builds **1 and 2**. Family 3 (CoaXPress, PTP) is not built.

1. **Hardware trigger (external).** A sensor, an encoder output or a PLC output is wired to the camera's own input. The camera exposes on the edge (rising or falling), after an optional delay in microseconds. The server does not start the picture; it receives it. Every picture is one product.
2. **Software trigger (internal).** The server sends the camera its trigger command over the camera's cable (`TriggerSoftware`). What asks the server: a PLC signal card with the action *Take a picture*, `POST /api/v1/lines/{id}/trigger`, or `/api/v1/control/trigger/{camera_id}`. The picture that command produces is the product's picture.
3. From the "common modes" list: **Frame Start** (one picture per pulse) and **trigger delay** are built. Trigger overlap stays at the camera's default. Level triggers (high level, low level), pulse-width (bulb) exposure and the trigger divider are not built.
4. The camera's **exposure**, **gain** and an optional **strobe output** (on while the sensor is exposed, for a light) are set from the camera's card.
5. A camera that is not triggered (free running) works as a normal vision camera.

### How it is wired (goes into the README in Task 8)

```
Hardware trigger                                    Software trigger
sensor or PLC output ──wire──> camera trigger input  sensor ──> PLC input ──> PLC bit ──network──> server
camera strobe output ──wire──> light (optional)      server ──camera cable (TriggerSoftware)──> camera
camera ──GigE or USB3 cable──> server                camera ──GigE or USB3 cable──> server
server ──network──> PLC (reject pulse, as today)     server ──network──> PLC (reject pulse, as today)
```

- The camera's input is usually opto-isolated: the signal goes to the input pin and the signal's 0 V to the camera's I/O ground. Check the voltage range in the camera's manual (many take 5 to 24 V; some need a resistor at 24 V).
- Hardware trigger is exact to microseconds and is the one for **moving** parts. Software trigger is late by the PLC read time (50 ms or more) plus the network and suits parts that **stand still**.
- The result is known one picture transfer and one model run after the trigger. A reject gate's travel delay must be longer than that.

## The owner's answers (2026-10-13), and what they change

1. **Which camera?** No single maker: GigE Vision cameras and a wide range of compatible Ethernet cameras. So the driver stays on the GenICam standard and loads **every** GenTL producer file (`.cti`) it finds, so cameras of several makers work at the same time (D13).
2. **A camera for testing?** None. Everything is built and tested against a stand-in camera, and **nothing in this plan is proven on a real camera**. Task 8's checks on the desk become a document the owner works through when a camera is there (`docs/real-life-tests/triggered-cameras.md`), with a script that prints what a camera offers (D15). A stand-in camera can also be switched on in a running server to try the whole path without hardware (D14).
3. **New: "the triggers for the other camera modes too, also the code read, so I have full control".** The plan first allowed a triggered camera only with *One picture per trigger*. That rule is gone: D4 is replaced, and D11, D12 and D16 are added.

### Task 1 result (2026-10-13)

- `harvesters` 1.4.3 is plain Python; it needs `genicam` and `numpy`. `genicam` 1.6.0 has ready-built files for Python 3.10 to 3.14 on Windows, Linux (x86-64, ARM64, ARMv7) and macOS, so the Docker image (3.12), a Jetson and the development machine (3.14) can all install it.
- Installing and importing it in a throwaway Python 3.14 environment on the development machine: see the ledger line of Task 1.
- Not checked, because there is no camera and no maker's SDK here: that a `.cti` file loads and lists a camera. That is step 1 of the real-life tests.

### Changes to the tasks

| Task | Change |
|---|---|
| 1 | Step 2 (list the real camera) moves to the real-life tests. The ten-line script becomes `test codes/industrial_camera_check.py`. |
| 2 | `default_backend()` loads every `.cti` file of `GENTL_CTI_PATH`, else of `GENICAM_GENTL64_PATH` (folders or files, several allowed); one shared library handle for all cameras (D13). `GENTL_CTI_PATH=simulator` gives the stand-in cameras of `app/hardware/camera/genicam_sim.py` (D14); the tests' `FakeBackend` builds on it. |
| 3 | Adds `take_picture(timeout)`: clear the queue, send the trigger, wait for that picture (D12). |
| 4 | The runner pushes a hardware camera's pictures only to the uses that count every picture (D11); the other uses still get the newest picture. |
| 5 | Software trigger is `take_picture` then the model on that picture. The code reader gets pushed hardware pictures, and takes its own picture from a software camera. The Count mode is not forced; the warnings of D4 and D16 are added. |
| 7 | Count mode is not locked. Notes under Trigger, Count mode and Read codes say what the chosen combination does. |
| 8 | Steps 1 to 3 become `docs/real-life-tests/triggered-cameras.md`. |

## Global Constraints

- Branch `claude/triggered-industrial-cameras`, started from `main` once Section 5 is merged there (else from `claude/section-5-plc-inputs`).
- **Never break an existing line.** USB and network cameras, and every line that has none of the new cameras, behave exactly as before. `SCHEMA_VERSION` stays **8**; no key is added to a line's settings; the camera type stays `gige` in the database (no migration).
- The new library is **optional**: the server starts, and every test passes, without `harvesters` installed. It is imported only inside `app/hardware/camera/genicam_backend.py`, when a `gige` camera connects or a scan is asked for.
- Tests need no hardware and no `harvesters`: they use `FakeBackend` (Task 2).
- Never start the server from the repository folder (use the scratch copy, port 8765).
- Run tests with `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` and `.venv/Scripts/python.exe -m ruff check .`. On the Windows development machine 11 tests fail before this work too (8 need Linux, 3 need `tzdata`): compare with that, not with zero.
- Keep each file's line endings and UTF-8 BOM. Dashboard changes are checked at 1440 px and 375 px.
- Docs and UI text use the plain wording of the existing docs. Never put a model name in the repository.
- Commit after each task. Push only when the owner says so.

## Decisions this plan makes

| # | Decision | Why |
|---|---|---|
| D1 | One driver for GigE Vision and USB3 Vision, through GenICam with `harvesters` and the maker's `.cti` file. The UI calls the type **Industrial camera (GigE / USB3 Vision)**. | One standard covers both cables and most makers; a driver per maker's SDK is several drivers. |
| D2 | `harvesters` goes into a new `requirements-industrial.txt`, not `requirements.txt`. Without it a `gige` camera does not connect and its card says what to install. | Sites without such cameras install nothing new. |
| D3 | The trigger settings are the **camera's** (`Camera.settings`): `trigger_mode` (`off`, `hardware`, `software`), `trigger_line`, `trigger_edge` (`rising`, `falling`), `trigger_delay_us`, `exposure_us`, `gain_db`, `strobe_line`. The line reads the mode from the connected driver (`driver.trigger_mode`). | One place for one fact. A copy in the line's settings could disagree with the camera. |
| D4 | **The camera's trigger decides when a picture is taken. The line decides what a picture means.** Every combination is accepted (table below). The two "one picture" choices of a line, Count mode *One picture per trigger* and Read codes *When a PLC signal asks*, make **every triggered picture one product / one read**. The other choices (count moving products, read continuously, read on a wire line) treat the camera as a stream and take the newest picture. | The owner wants full control. A hardware trigger from an encoder or a clock is a steady stream and tracks well; forcing one mode would rule that out. |
| D5 | In a trigger mode **no picture is skipped**: the driver queues up to **64** pictures and the runner gives them to the model oldest first. A full queue drops its oldest picture, counts it and raises `camera.trigger_overrun` (warning, line scope), cleared after 60 s without a drop. | Today's runner takes only the newest picture, which is right for video and loses products here. |
| D6 | A product's time is when its picture **arrived** at the server, not when the model finished. | A reject gate's travel delay runs from the product's time. |
| D7 | No picture while waiting for a trigger is **not** a lost camera. The link is checked by reading one camera feature every 5 s; two failures in a row mark the camera disconnected. | A triggered camera can be silent for hours. |
| D8 | A trigger from the server for a camera in `hardware` mode is refused: "this camera is triggered by its own input". | It would count a product no part stands for. |
| D9 | After a software trigger that got no picture in time, the driver's queue is emptied before the next trigger. | A late picture of trigger N must not be taken as the picture of trigger N+1. |
| D10 | Docker: not built into the image. The README says what a Linux host needs (host networking, the maker's GenTL files mounted in). On Windows the server runs natively for these cameras. | The makers' SDKs cannot be shipped in the image, and GigE discovery does not cross Docker Desktop's network. |

| D11 | **Hardware trigger pushes.** Each picture is taken from the queue once and given to every use of that camera that counts every picture: the model (Count mode *One picture per trigger*) and the code reader (Read codes *When a PLC signal asks*). One picture serves both. The next picture waits until those are free. A use that treats the camera as a stream gets the newest picture as today, and when no use counts every picture the queue is emptied without an alarm. | A queue behind a tracker would make it run late for good; a tracker wants the newest picture. |
| D12 | **Software trigger pulls.** Whoever needs a picture calls the driver's `take_picture()`: empty the queue, send `TriggerSoftware`, wait for that picture. Askers: a *Take a picture* signal card, the trigger API, a wire-line crossing (Read codes on a wire line) and the reader's test capture. One asker at a time per camera. A camera that both counts per trigger and reads codes on a signal takes one picture for each. Nothing is fed on its own to a use that counts every picture. | The reader's retry on an unread code and the product's id travel with the request; a pushed picture could not carry them. |
| D13 | All `.cti` files found are loaded into **one** library handle shared by every industrial camera, behind one lock. | Makers' producers are one-per-process; a handle per camera can fail on the second camera. |
| D14 | `GENTL_CTI_PATH=simulator` gives two stand-in cameras (no hardware, no library): a picture every 2 s on hardware trigger, one per software trigger, 5 a second when free running. | The owner has no camera; the whole path can be tried, and the dashboard checked, without one. |
| D15 | No camera was available: the driver's calls into the library are written from its documentation and source and are **not run against hardware**. The real-life tests say what to check first and what to send back when a step fails. | Saying so plainly is better than a claim nobody tested. |
| D16 | A *Take a picture* signal card that names a hardware-triggered camera is not obeyed: the card shows "triggered by its own input", and saving the line gives a warning. A software-triggered camera in a stream use that nothing triggers gets a warning too. | A second read of the last picture would count a code twice. Warnings, not refusals: the line's settings cannot see the camera's. |

### What each combination does (D4)

| Camera trigger | Count moving products | One picture per trigger | Read codes: continuously or on a wire line | Read codes: when a PLC signal asks |
|---|---|---|---|---|
| **Off** (free running) | as today | a signal card or the API takes the stream's next picture (Section 5) | as today | a signal card takes a fresh picture (Section 5) |
| **Hardware** | newest picture, like any camera (for a camera triggered by an encoder or a clock) | **every picture is one product**; no signal card | newest picture, like any camera | **every picture is read once**; no signal card |
| **Software** | warning: nothing takes pictures on its own | a signal card or the API triggers the camera; **that picture is the product** | on a wire line: the crossing triggers the camera. Continuously: warning | a signal card triggers the camera; **that picture is read** (again, up to 3 pictures, while no code reads) |

## File Structure

| File | Responsibility |
|---|---|
| **`requirements-industrial.txt`** (new) | `harvesters` |
| **`app/hardware/camera/genicam_backend.py`** (new) | `GenICamBackend` (the few calls the driver needs) and `HarvestersBackend`; the only file that imports `harvesters` |
| `app/hardware/camera/gige_camera.py` | `GigECamera`: connect, read pictures on a thread, convert to BGR, apply settings, trigger modes, the picture queue, the link check |
| `app/hardware/camera/base.py` | `trigger_mode = "off"` for every camera |
| `app/services/vision_service.py` | the runner feeds a triggered camera's queue in order; the inference worker reports each result |
| `app/services/trigger_inspection.py` | `product_from_result`; hardware pictures become products; software trigger |
| `app/services/line_config.py`, `app/routes/v1/lines.py` | a warning for a triggered camera that is not in trigger count mode |
| `app/events/alarm_events.py` | `camera.trigger_overrun` |
| `app/routes/v1/camera.py`, `app/schemas/camera.py`, `app/services/camera_service.py` | `GET /cameras/discover/gige`; the new settings checked when a camera is saved |
| `assets/camera_settings.js`, `assets/production_lines.js`, `dashboard.html` | scan and add an industrial camera; the Trigger fields on its card; Count mode locked for a triggered camera |
| `tests/test_industrial_camera.py`, `tests/test_camera_trigger_modes.py` (new) | see each task |

## Review Focus

1. **Two triggers closer together than one model run**: both pictures become products, in the order taken (Task 4).
2. **A burst that fills the queue** (65 pictures while the model is busy): the oldest is dropped and counted, the overrun alarm is raised once, the server stays responsive (Tasks 3 and 4).
3. **A camera that waits an hour for a trigger** stays connected with no alarm and no reconnect loop; an unplugged cable is seen within about 10 s (Task 3).
4. **A software trigger the camera does not answer**: no product, the reason is shown, and the next trigger gets its own picture (Task 5).
5. **The library or the `.cti` file is missing**: the server starts, other cameras work, the card says what to install (Task 2).

---

### Task 1: Check that the library works here (go or no-go)

**Files:** Create `requirements-industrial.txt` (one line: `harvesters`). Nothing else changes.

- [ ] **Step 1:** In a throwaway virtual environment of **Python 3.12** (the Docker image's version), run `pip install -r requirements-industrial.txt`, then `python -c "from harvesters.core import Harvester; print('ok')"`. Expected: `ok`. Repeat with the development machine's `.venv`.
- [ ] **Step 2:** Install the camera maker's SDK. Find its `.cti` file (the installer sets `GENICAM_GENTL64_PATH`). With the camera plugged in, run a ten-line script: `Harvester()`, `add_file(cti)`, `update()`, print `device_info_list`. Expected: the camera is listed with its serial number.
- [ ] **Step 3:** Write the result at the top of this plan under "Task 1 result": the Python versions that installed, the `.cti` path, the camera's model and serial, and the names of its trigger input lines (`LineSelector` values).
- [ ] **Step 4:** **Stop and ask the owner** if Step 1 fails on Python 3.12, or Step 2 cannot list the camera. The fallback is a backend for that maker's own Python SDK behind the same `GenICamBackend` (Task 2 only changes).
- [ ] **Step 5:** Commit `Add the optional requirements for industrial cameras`.

### Task 2: Connect an industrial camera and read its pictures (free running)

**Files:** Create `app/hardware/camera/genicam_backend.py`, `tests/test_industrial_camera.py`. Modify `app/hardware/camera/gige_camera.py`, `app/hardware/camera/base.py`.

**Interfaces — Produces:**

```python
# genicam_backend.py
class GenICamUnavailable(RuntimeError): ...        # the library or a .cti file is missing; str() says what to install
class GenICamBackend(Protocol):
    def list_devices(self) -> List[Dict[str, str]]: ...   # [{"serial", "model", "vendor", "interface"}]
    def open(self, serial: str) -> None: ...               # raises LookupError when the serial is not found
    def close(self) -> None: ...
    def has(self, feature: str) -> bool: ...
    def get(self, feature: str) -> Any: ...
    def set(self, feature: str, value: Any) -> None: ...   # raises ValueError naming the feature when the camera refuses
    def execute(self, feature: str) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def fetch(self, timeout: float) -> Optional[Tuple[np.ndarray, str]]: ...   # (pixels, pixel format) or None on timeout
def default_backend() -> GenICamBackend     # HarvestersBackend; .cti files from GENTL_CTI_PATH, else GENICAM_GENTL64_PATH

# gige_camera.py
class GigECamera(BaseCamera):
    def __init__(self, camera_id, name, source, settings=None, backend: Optional[GenICamBackend] = None)
# base.py
class BaseCamera: trigger_mode: str = "off"
```

`source` is the camera's serial number. `tests/test_industrial_camera.py` defines `FakeBackend(GenICamBackend)` with `features: dict`, `frames: deque`, `calls: list`, and `push(pixels, pixel_format="Mono8")`; later tasks import it.

- [ ] **Step 1: Write the failing tests.**

```python
def test_a_camera_connects_by_serial_and_gives_bgr_pictures():
    backend = FakeBackend(devices=[{"serial": "A1", "model": "X", "vendor": "V", "interface": "GigE"}])
    cam = GigECamera("c1", "Top", "A1", backend=backend)
    assert cam.connect() is True and cam.is_connected
    backend.push(np.full((4, 6), 200, np.uint8), "Mono8")
    assert cam.wait_for_new_frame(timeout=1.0)
    ok, mat, frame_id = cam.get_latest_raw_mat()
    assert ok and mat.shape == (4, 6, 3) and frame_id == 1

@pytest.mark.parametrize("pixel_format", ["Mono8", "BayerRG8", "BayerGB8", "RGB8", "BGR8"])
def test_every_common_pixel_format_becomes_bgr(pixel_format): ...       # shape (h, w, 3), dtype uint8

def test_an_unknown_serial_does_not_connect():
    cam = GigECamera("c1", "Top", "NOPE", backend=FakeBackend(devices=[]))
    assert cam.connect() is False and "NOPE" in cam.last_error

def test_a_missing_library_says_what_to_install(monkeypatch):           # Review Focus 5
    monkeypatch.setattr(genicam_backend, "default_backend", lambda: (_ for _ in ()).throw(GenICamUnavailable("Install requirements-industrial.txt")))
    cam = GigECamera("c1", "Top", "A1")
    assert cam.connect() is False and "requirements-industrial.txt" in cam.last_error

def test_exposure_and_gain_are_set_on_connect():
    cam = GigECamera("c1", "Top", "A1", settings={"exposure_us": 800, "gain_db": 6}, backend=backend)
    cam.connect()
    assert ("set", "ExposureTime", 800.0) in backend.calls and ("set", "Gain", 6.0) in backend.calls
    assert ("set", "ExposureAuto", "Off") in backend.calls

def test_disconnect_stops_the_thread_and_closes_the_camera(): ...        # ("stop",) then ("close",) in backend.calls; no thread left
```

- [ ] **Step 2:** Run `pytest tests/test_industrial_camera.py -q`. Expected: FAIL (the stub refuses to connect).
- [ ] **Step 3: Implement.** `HarvestersBackend` wraps `Harvester` and one `ImageAcquirer`; it raises `GenICamUnavailable` with the install text when the import fails or no `.cti` file is found. `GigECamera.connect` opens by serial, applies the settings, starts a daemon thread that calls `backend.fetch(0.2)` and stores each picture through the same `_process_frame` and latest-picture fields `IPCamera` uses (so resize, rotation, flips and the ROI work unchanged). A feature the camera does not have (`backend.has` false) is skipped, not an error.
- [ ] **Step 4:** Run the file, then the whole suite and `ruff check .`. Expected: pass; no new failure outside the Windows baseline.
- [ ] **Step 5:** Commit `Connect GigE and USB3 Vision cameras through GenICam`.

### Task 3: Trigger modes in the driver

**Files:** Modify `app/hardware/camera/gige_camera.py`, `app/events/alarm_events.py`. Test: `tests/test_industrial_camera.py`.

**Interfaces — Consumes:** Task 2. **Produces:**

```python
class GigECamera:
    trigger_mode: str                                   # "off" | "hardware" | "software", from settings["trigger_mode"]
    dropped_frames: int                                  # pictures dropped from a full queue since connect
    QUEUE_SIZE = 64
    LINK_CHECK_S = 5.0
    def trigger_software(self) -> bool                   # sends TriggerSoftware; False (and last_error) when refused
    def next_triggered_frame(self) -> Optional[Tuple[np.ndarray, int, float]]   # (BGR picture, frame id, arrived at) oldest first
    def clear_triggered_frames(self) -> int              # empties the queue; returns how many were waiting
AlarmCode.CAMERA_TRIGGER_OVERRUN = "camera.trigger_overrun"   # label "Camera pictures arrive faster than they are inspected", warning, scope line
```

Settings and the camera features they set, in this order: `TriggerSelector=FrameStart`; `TriggerMode=On` (`Off` for `off`); `TriggerSource=<trigger_line>` (default `Line0`) for `hardware`, `Software` for `software`; `TriggerActivation=RisingEdge|FallingEdge`; `TriggerDelay=<trigger_delay_us>` (0 to 10,000,000). Strobe, when `strobe_line` is set: `LineSelector=<strobe_line>`, `LineMode=Output`, `LineSource=ExposureActive`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_hardware_trigger_sets_the_cameras_features_in_order():
    cam = _connected(settings={"trigger_mode": "hardware", "trigger_line": "Line1", "trigger_edge": "falling", "trigger_delay_us": 250})
    sets = [c for c in backend.calls if c[0] == "set" and c[1].startswith("Trigger")]
    assert sets == [("set", "TriggerSelector", "FrameStart"), ("set", "TriggerMode", "On"), ("set", "TriggerSource", "Line1"),
                    ("set", "TriggerActivation", "FallingEdge"), ("set", "TriggerDelay", 250.0)]

def test_free_running_switches_the_trigger_off():           # ("set", "TriggerMode", "Off")
def test_a_strobe_line_follows_the_exposure():              # LineSelector, LineMode Output, LineSource ExposureActive

def test_every_triggered_picture_is_kept_in_order():        # Review Focus 1
    cam = _connected(settings={"trigger_mode": "hardware"})
    for value in (10, 20, 30): backend.push(np.full((2, 2), value, np.uint8))
    _wait_until(lambda: cam.get_latest_frame_id() == 3)
    got = [cam.next_triggered_frame() for _ in range(3)]
    assert [g[1] for g in got] == [1, 2, 3] and [int(g[0][0, 0, 0]) for g in got] == [10, 20, 30]
    assert got[0][2] <= got[1][2] <= got[2][2] and cam.next_triggered_frame() is None

def test_a_full_queue_drops_the_oldest_and_counts_it():      # Review Focus 2: push 65 -> ids 2..65 remain, dropped_frames == 1

def test_software_trigger_sends_the_command():
    cam = _connected(settings={"trigger_mode": "software"})
    assert cam.trigger_software() is True and ("execute", "TriggerSoftware") in backend.calls
    assert ("set", "TriggerSource", "Software") in backend.calls

def test_a_refused_software_trigger_says_why():              # backend.execute raises -> False, reason in last_error
def test_a_free_running_camera_has_no_software_trigger():    # trigger_mode "off" -> trigger_software() False

def test_no_picture_for_a_long_time_is_not_a_lost_camera(monkeypatch):     # Review Focus 3
    monkeypatch.setattr(GigECamera, "LINK_CHECK_S", 0.05)
    cam = _connected(settings={"trigger_mode": "hardware"})
    time.sleep(0.3)
    assert cam.is_connected and cam.last_error is None

def test_a_camera_that_stops_answering_is_disconnected(monkeypatch):       # backend.get raises twice -> is_connected False
def test_clear_triggered_frames_empties_the_queue():                       # returns the number that waited
```

- [ ] **Step 2:** Run them. Expected: FAIL (`trigger_mode` is always "off").
- [ ] **Step 3: Implement.** In a trigger mode each fetched picture also goes into a `deque(maxlen=QUEUE_SIZE)` with its id and `time.time()`; before appending to a full deque add 1 to `dropped_frames`. The link check reads `DeviceTemperature` when the camera has it, else `TriggerMode`. Add the alarm to `AlarmCode` and `ALARM_CATALOG`.
- [ ] **Step 4:** Run the file and the suite. Expected: pass.
- [ ] **Step 5:** Commit `Add hardware and software trigger modes to the industrial camera driver`.

### Task 4: Every triggered picture reaches the model, in order

**Files:** Modify `app/services/vision_service.py`. Test: create `tests/test_camera_trigger_modes.py`.

**Interfaces — Consumes:** `driver.trigger_mode`, `next_triggered_frame()`, `dropped_frames` (Task 3). **Produces:**

```python
# on the inference worker class that CameraStreamPipeline.get_worker() returns
worker.on_result: Optional[Callable[[int, list, float, Optional[float]], None]]   # (frame id, detections, inference ms, arrived at); called on the worker's thread after each picture
# submit_frame_if_idle gains a keyword: arrived_at: Optional[float] = None, passed through to on_result
```

- [ ] **Step 1: Write the failing tests** (with a `GigECamera` on `FakeBackend`, the stand-in engine and line helpers of `tests/test_trigger_inspection.py`).

```python
def test_the_runner_feeds_a_triggered_camera_oldest_first():     # Review Focus 1
    # three pictures pushed while the worker is busy with the first: on_result sees frame ids [1, 2, 3], none skipped

def test_a_free_running_camera_still_gets_only_its_newest_picture():   # a USB/IP camera: unchanged (latest id wins)

def test_dropped_pictures_raise_the_overrun_alarm_and_it_clears(monkeypatch):   # Review Focus 2
    # dropped_frames goes 0 -> 1: camera.trigger_overrun is active with details.line_id and details.dropped == 1;
    # with OVERRUN_CLEAR_S patched to 0.05 and no new drop, it is cleared

def test_on_result_carries_the_time_the_picture_arrived():      # the fourth argument equals the queue entry's time
```

- [ ] **Step 2:** Run them. Expected: FAIL (the runner submits only the newest picture; `on_result` does not exist).
- [ ] **Step 3: Implement.** In `ContinuousVisionRunner`'s loop, for a driver whose `trigger_mode` is not `"off"`: when the worker is idle, take `driver.next_triggered_frame()` and submit it with its `arrived_at`; do not use the latest-picture path for that camera. Compare `driver.dropped_frames` with the last value seen to raise or clear the alarm (`OVERRUN_CLEAR_S = 60.0`). The worker calls `on_result` inside a `try`, so a callback that throws never stops inference.
- [ ] **Step 4:** Run the file and the suite. Expected: pass.
- [ ] **Step 5:** Commit `Feed every triggered picture to the model in order`.

### Task 5: Products from hardware and software triggers

**Files:** Modify `app/services/trigger_inspection.py`, `app/services/counting_service.py` (`make_crossing`), `app/services/line_service.py`, `app/services/line_config.py`, `app/routes/v1/lines.py`. Test: `tests/test_camera_trigger_modes.py`.

**Interfaces — Consumes:** `worker.on_result` (Task 4), `trigger_software`, `clear_triggered_frames` (Task 3). **Produces:**

```python
# trigger_inspection.py
def product_from_result(runtime, camera_id: str, detections: list, arrived_at: Optional[float] = None) -> Dict[str, Any]
    # the part of trigger_camera after the wait, moved out unchanged: verdict -> make_crossing -> emit_crossing.
    # Returns {"status": "inspected" | "nothing", "result", "class_name", "reject_reason", "track_id", "detections"}
def watch_hardware_triggers(runtime) -> None      # sets worker.on_result for each of the line's cameras whose driver is in "hardware" mode; clears it for the others
# counting_service.py
CountingService.make_crossing(..., arrived_at: Optional[float] = None)   # when given, it is the crossing's time
```

Read `make_crossing` and `_crossing` first: `arrived_at` fills the same time field they fill today.

- [ ] **Step 1: Write the failing tests.**

```python
def test_each_hardware_triggered_picture_is_one_product():
    # line with one GigE camera (hardware), count_mode "trigger"; three pictures: defect, product, empty with no_product "reject"
    # -> total 3, rejected 2; reasons ["vision_class", None, "nothing_detected"]; no signal card on the line

def test_a_hardware_product_has_the_time_its_picture_arrived():     # the event's timestamp is the queue entry's time, not "now"

def test_the_server_cannot_trigger_a_hardware_triggered_camera():   # D8
    with pytest.raises(ValueError, match="triggered by its own input"):
        _run(lambda: trigger_camera(runtime, "cam"))

def test_a_software_trigger_takes_the_picture_it_asked_for():
    # trigger_camera(): ("execute", "TriggerSoftware") was sent once, then the pushed picture became the product

def test_an_unanswered_software_trigger_does_not_shift_the_next_one(monkeypatch):   # Review Focus 4, D9
    # TRIGGER_TIMEOUT_S patched to 0.1. Trigger 1: no picture -> status "no_frame". Its picture arrives late (a defect).
    # Trigger 2: a good picture -> its result is "good", and the late defect made no product.

def test_a_refused_software_trigger_is_an_error_not_a_product():    # trigger_software() False -> ValueError with the camera's reason

def test_a_free_running_camera_in_trigger_count_mode_works_as_before():   # the Section 5 behaviour, unchanged

def test_a_stopped_line_makes_no_product_from_a_hardware_picture():

def test_a_triggered_camera_that_counts_moving_products_gets_a_warning():
    # camera settings trigger_mode "hardware", line camera without count_mode -> the line's warnings name the camera
    # and say to set Count mode to "One picture per trigger"
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.** `trigger_camera`: for `driver.trigger_mode == "hardware"` raise the D8 error; for `"software"` call `clear_triggered_frames()`, then `trigger_software()`, then wait for `worker.result_seq` to pass its value from before the command; for `"off"` keep today's code. Both paths end in `product_from_result`. `watch_hardware_triggers` is called where a line's cameras are applied and connected (`LineManager`); its callback hops to the main loop with `call_soon_threadsafe` and skips a stopped line. The warning is added in `routes/v1/lines.py` beside the other save warnings, because only the route can read the cameras' own settings.
- [ ] **Step 4:** Run the file, `tests/test_trigger_inspection.py` and the suite. Expected: pass.
- [ ] **Step 5:** Commit `Make products from hardware and software triggered pictures`.

### Task 6: Find, add and set up an industrial camera through the API

**Files:** Modify `app/routes/v1/camera.py`, `app/schemas/camera.py`, `app/services/camera_service.py`. Test: `tests/test_industrial_camera.py`.

**Interfaces — Produces:** `GET /api/v1/cameras/discover/gige` → `[{"serial", "model", "vendor", "interface", "in_use"}]` (Supervisor; the scope of `/cameras/discover/usb`); 503 with the install text when the library or `.cti` file is missing. Camera `settings` accept and check: `trigger_mode` (`off`, `hardware`, `software`), `trigger_line` (text, 32 at most), `trigger_edge` (`rising`, `falling`), `trigger_delay_us` (0 to 10000000), `exposure_us` (10 to 10000000), `gain_db` (0 to 48), `strobe_line` (text, 32 at most; empty = none).

- [ ] **Step 1: Write the failing tests:** the scan lists the fake backend's cameras and marks one whose serial is a saved camera's `source` as `in_use`; the scan answers 503 with the install text when `default_backend` raises `GenICamUnavailable`; a camera of type `gige` is created with `source` = serial; `PATCH /cameras/{id}` with `{"settings": {"trigger_mode": "laser"}}` is 422 and names the field; a trigger setting sent for a `usb` camera is 422; a saved setting is applied to the connected camera without a reconnect (`("set", "TriggerDelay", 500.0)` in `backend.calls`).
- [ ] **Step 2:** Run them. Expected: FAIL (404 on the scan).
- [ ] **Step 3: Implement**, following the USB scan route and the place the camera settings of USB and IP cameras are checked today.
- [ ] **Step 4:** Run the file and the suite. Expected: pass.
- [ ] **Step 5:** Commit `Add the industrial camera scan and its trigger settings to the API`.

### Task 7: Dashboard

**Files:** Modify `assets/camera_settings.js`, `assets/production_lines.js`, `dashboard.html`; `assets/dashboard.css` only if a class is missing. Test: by hand in the scratch copy, plus the text checks of `tests/test_lines_api.py`.

- [ ] **Step 1: Write the failing test** in `tests/test_lines_api.py`: `assets/camera_settings.js` holds `"off"`, `"hardware"`, `"software"`, `"rising"`, `"falling"`, and `/cameras/discover/gige` is in `assets/production_lines.js`. Run it. Expected: FAIL.
- [ ] **Step 2: Add camera.** The **Add camera** card gets **Scan industrial cameras** beside **Scan USB**: it lists model, serial and cable (cameras in use greyed out); picking one adds a camera of type `gige`. A 503 shows its install text.
- [ ] **Step 3: Camera card, Image fold**, for an industrial camera only: **Trigger** (*Off: the camera runs freely* / *Hardware: a signal on the camera's own input* / *Software: the server triggers the camera*), and below it, only when they apply: **Trigger input** (text, `Line0` by default), **Edge** (*Rising* / *Falling*), **Trigger delay (µs)**, then always **Exposure (µs)**, **Gain (dB)**, **Strobe output** (empty = none). A note under Trigger says in one sentence how each is wired (the text of "How it is wired").
- [ ] **Step 4: Count mode follows the trigger.** With Trigger on *Hardware* or *Software*, the card's **Count mode** is set to *One picture per trigger* and cannot be changed; its note says why. With *Hardware*, the note adds that no PLC signal is needed, and the PLC signals dialog does not offer the camera under **Take a picture**.
- [ ] **Step 5: A triggered camera's picture.** Its card and its Line dashboard tile show the last picture taken with its time, not a live video, and "Waiting for a trigger" before the first.
- [ ] **Step 6: Check in the scratch copy** with a stand-in backend (set `GENTL_CTI_PATH` to nothing and patch `default_backend` in the copy only): scan, add, set Hardware trigger, save, reload (every field kept); a line with only USB and network cameras looks and saves as before. Screenshots at 1440 px and 375 px into `docs/progress/screenshots/triggered-cameras/`.
- [ ] **Step 7:** Run the suite and `ruff check .`. Commit `Add industrial cameras and their trigger settings to the dashboard`.

### Task 8: The real camera, and the docs

**Files:** Modify `README.md`, `FILE_TREE.md`, `PROJECT_DESCRIPTION.md`, `FUTURE_FIXES.md`, `.env.example` (`GENTL_CTI_PATH`).

- [ ] **Step 1: Hardware trigger on the desk.** Camera on *Hardware*, a push button or the PLC's output on its input. Ten pulses: ten products, each with its picture. Then 20 pulses in one second with a model that takes 100 ms: 20 products, in order, no overrun alarm.
- [ ] **Step 2: Software trigger.** Camera on *Software*, a *Take a picture* signal card on the PLC simulator's coil: each rising edge is one product. Unplug the camera, trigger: no product, the card shows the reason; plug it in, trigger: one product.
- [ ] **Step 3: Idle and unplug.** Leave the camera 30 minutes without a trigger: still connected, no alarm. Pull its cable: disconnected within about 10 s, and it reconnects with its trigger settings applied again.
- [ ] **Step 4: Docs.** README: a part **Triggered industrial cameras** with "How it is wired", the settings, the limits (one picture per trigger; the rate the model can keep up with; what is not built: bulb exposure, trigger divider, CoaXPress, PTP), installing `requirements-industrial.txt` and the maker's SDK, and Docker on Linux (D10). Update the Features line that says the GigE driver is not included. `FUTURE_FIXES.md`: what was run on the real camera and what was not.
- [ ] **Step 5:** Commit `Triggered industrial cameras: docs and the checks on a real camera`. Ask the owner before pushing.
