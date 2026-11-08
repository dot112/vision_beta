# System Usage Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two rings in the dashboard's top bar (CPU and GPU usage in percent) that open a panel with CPU, GPU, RAM, storage and this server's share, read from the whole machine on Windows and Linux.

**Architecture:** A pure reading module (`app/services/system_usage.py`) builds one dict from `psutil`, NVIDIA's library (`pynvml` from `nvidia-ml-py`) and Linux system files (Jetson, AMD); a sampler thread takes it every 2 s while someone asks and stops when nobody does. `GET /api/v1/system/usage` answers the latest reading. `assets/system_usage.js` draws the rings and the panel and asks every 2 s while the page is visible.

**Tech Stack:** psutil, nvidia-ml-py (pynvml), FastAPI, plain JS in `assets/`, pytest + node.

**Spec:** `docs/superpowers/specs/2026-10-25-system-usage-monitor-design.md`

## Global Constraints

- Reading shape: exactly spec section 1 (`taken_at`, `cpu`, `memory`, `disks`, `gpus`, `server`); numbers rounded to one decimal; sizes in bytes; temperatures in °C; anything missing is `null`.
- GPU `source` values: `"nvidia"`, `"jetson"`, `"amd"`.
- Sampler: a reading every 2 s; stops after 60 s without a request; the first answer waits for the first reading.
- Route: `GET /api/v1/system/usage`, Level 1+ (`require_operator`); API key scope `{"monitor:read"}`.
- Ring colours: green below 70, amber from 70, red from 90 (whole percent shown, e.g. `23%`); no GPU: `—` in a grey ring; the GPU ring shows the busiest GPU.
- Refresh every 2 s while signed in and visible; rings grey after 10 s without an answer; no help icons.
- Sizes in the panel: `GB` with one decimal, `MB` under 1 GB (1 GB = 1024³ bytes).
- Repo files are CRLF in the worktree: edit with the Edit tool or a script that keeps CRLF. Work in `.claude/worktrees/system-usage`; run pytest there with `../../../.venv/Scripts/python.exe -m pytest ... -p no:cacheprovider` (not `pt.sh`, which tests `main`).

## Review Focus

1. A GPU that fails in the middle of a run (driver reset, NVML `GpuIsLost`): that source is skipped for the reading, the rest still read, the log does not fill up. → Task 1 test `test_a_source_failing_mid_reading_is_skipped`.
2. A network drive that hangs `disk_usage()` for seconds (Windows mapped drive offline): the API keeps answering the last reading at once instead of waiting. → Task 2 test `test_a_slow_reading_does_not_hold_the_answer`.
3. The user signs out, or the session ends (401): the dashboard stops asking instead of failing every 2 s. → Task 4 test `test_polling_stops_on_401`.
4. A Docker container or a machine with no GPU files and no NVIDIA library: no GPU, no error, the ring shows `—`. → Task 1 test `test_no_gpu_anywhere` and Task 4 test `test_no_gpu_shows_a_dash`.
5. A phone's top bar (375 px): the rings must not push the bar sideways. → Task 4 test `test_ring_labels_hide_on_phones` (CSS rule) and the Task 5 live check at 375 px.

---

### Task 1: Readings

**Files:**
- Create: `app/services/system_usage.py`
- Test: `tests/test_system_usage_readings.py`

**Interfaces:**
- Produces (all in `app/services/system_usage.py`):
  - `cpu_reading(ps) -> dict` (`percent`, `per_core`, `cores_logical`, `cores_physical`, `freq_mhz`, `freq_max_mhz`, `load_avg`, `temp_c`); `ps` is the `psutil` module or a stand-in.
  - `memory_reading(ps) -> dict`; `disk_readings(ps, data_dir: str) -> list`; `server_reading(ps, process) -> dict` (`cpu_percent` divided by `ps.cpu_count()`).
  - `nvidia_gpus(nvml=None) -> list` (imports `pynvml` when `nvml` is None; `[]` when it cannot import or `nvmlInit` fails); `jetson_gpus(root: str = "/") -> list`; `amd_gpus(root: str = "/") -> list`.
  - `gpu_readings(nvml=None, root: str = "/") -> list` (all three, a source that raises is skipped, logged through `LogThrottle(60.0)`).
  - `take_reading(ps=psutil, nvml=None, root: str = "/", data_dir: str = DATA_DIR, process=None) -> dict` (the whole reading, `taken_at` UTC ISO); `process=None` uses one module-level `psutil.Process()` (its `cpu_percent` compares with the previous call on the same object).
  - `prime(ps=psutil, process=None) -> None`: the first `cpu_percent()` calls (machine, per core, this process), which return 0 and start the measurement.
  - `DATA_DIR` imported from `app.services.settings_persistence_service`.

- [ ] **Step 1: Write the failing tests** (a `SimpleNamespace` psutil stand-in with `cpu_percent`, `cpu_count`, `cpu_freq`, `virtual_memory`, `swap_memory`, `disk_partitions`, `disk_usage`, optional `getloadavg` / `sensors_temperatures`; a fake NVML namespace; `tmp_path` as `root` holding `sys/devices/gpu.0/load`, `proc/device-tree/model`, `sys/class/drm/card0/device/{gpu_busy_percent,mem_info_vram_used,mem_info_vram_total,product_name,hwmon/hwmon0/temp1_input}`):
  - `test_reading_has_every_key`: `take_reading(...)` keys == `{"taken_at", "cpu", "memory", "disks", "gpus", "server"}`; `cpu`, `memory`, `server` keys exactly as spec section 1.
  - `test_linux_only_values_are_null_without_them`: stand-in without `getloadavg` and `sensors_temperatures` → `load_avg is None and temp_c is None`.
  - `test_cpu_temperature_prefers_coretemp`: sensors `{"acpitz": [..80..], "coretemp": [..54..]}` → `54.0`.
  - `test_a_partition_that_raises_is_left_out` (`disk_usage` raises `PermissionError` for `E:\`) and `test_snap_loops_are_left_out` (fstype `squashfs` on `/snap/x`).
  - `test_server_data_marks_the_longest_matching_mount`: mounts `/` and `/srv`, data dir `/srv/app/data` → only `/srv` true.
  - `test_nvidia_gpus_from_the_library`: fake NVML with one device → `{"name", "source": "nvidia", "percent": 12.0, "memory_used", "memory_total", "temp_c": 49.0}`.
  - `test_no_nvidia_library_or_driver`: `nvidia_gpus(nvml=<init raises>) == []`.
  - `test_jetson_load_is_tenths_of_a_percent`: `load` = `412` → `41.2`, name from the model file (NUL stripped), memory `None`.
  - `test_amd_busy_vram_and_temperature`: `37`, VRAM bytes, `temp1_input` `61000` → `61.0`.
  - Review Focus: `test_a_source_failing_mid_reading_is_skipped` (NVML `nvmlDeviceGetUtilizationRates` raises; Jetson files present → the Jetson GPU is still listed, no exception); `test_no_gpu_anywhere` (empty `tmp_path`, NVML init raises → `gpus == []`).
- [ ] **Step 2: Run** `../../../.venv/Scripts/python.exe -m pytest tests/test_system_usage_readings.py -q -p no:cacheprovider` → FAIL (module missing).
- [ ] **Step 3: Implement** the module. Round with `round(x, 1)`; NVML: `nvmlInit` once per process (a module flag), then per device `nvmlDeviceGetName`, `nvmlDeviceGetUtilizationRates(h).gpu`, `nvmlDeviceGetMemoryInfo(h)`, `nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU)` (a name that is `bytes` is decoded).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `System usage: readings from psutil, NVIDIA, Jetson and AMD`.

### Task 2: Sampler

**Files:**
- Modify: `app/services/system_usage.py`
- Test: `tests/test_system_usage_sampler.py`

**Interfaces:**
- Consumes: Task 1 `take_reading`, `prime`.
- Produces: `class UsageSampler` with `__init__(self, read=take_reading, prime=prime, interval: float = 2.0, first_wait: float = 0.5, idle_stop: float = 60.0, clock=time.monotonic)`, `latest(self, wait: float = 3.0) -> dict` (starts the thread when it is not running, notes the request time, waits up to `wait` s only while there is no reading yet), `running` (property), `stop(self) -> None`; module instance `usage_sampler = UsageSampler()`. The thread calls `prime()`, waits `first_wait`, then reads every `interval`, and exits when `clock() - last_request > idle_stop`.

- [ ] **Step 1: Write the failing tests** (a counting `read` stand-in, a fake clock moved by the test; real thread with `interval=0.05`):
  - `test_starts_on_the_first_request_and_reads_every_interval`: not `running` before `latest()`; `prime` called once before the first `read`; after `latest()` and 0.3 s (`first_wait=0`), reads ≥ 3.
  - `test_stops_after_idle`: fake clock advanced past `idle_stop` → `running` is False within 0.5 s.
  - `test_restarts_after_stopping`: `latest()` after it stopped → running again, a fresh reading.
  - Review Focus: `test_a_slow_reading_does_not_hold_the_answer`: after a first reading, `read` sleeps 2 s; `latest()` returns the previous reading in < 0.2 s.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS (and Task 1's tests).
- [ ] **Step 5: Commit** `System usage: a sampler that runs only while someone asks`.

### Task 3: Route, API key scope, requirements

**Files:**
- Modify: `app/routes/v1/system.py` (`GET /usage` with `Depends(require_operator)`, `await asyncio.to_thread(usage_sampler.latest)`), `app/security/api_key_scopes.py` (`/api/v1/system/usage` GET → `{"monitor:read"}`, before the generic `/api/v1/system/` rule), `requirements.txt` (`nvidia-ml-py>=12.535  # GPU usage on NVIDIA cards (System usage in the top bar)` after the `psutil` line)
- Test: `tests/test_system_usage_api.py`

**Interfaces:**
- Consumes: Task 2 `usage_sampler.latest()`.
- Produces: `GET /api/v1/system/usage` → the reading dict.

- [ ] **Step 1: Write the failing tests** (monkeypatch `usage_sampler.latest` to return a fixed reading):
  - `test_signed_out_is_401`; `test_level1_gets_the_reading` (Level 1 user made through `POST /api/v1/auth/users` as `tests/test_config_lock_api.py` does → 200, keys of section 1); `test_api_key_needs_monitor_read` (key with `monitor:read` → 200; with only `configuration:read` → 403); `test_scope_rule` (`required_api_key_scopes("GET", "/api/v1/system/usage") == {"monitor:read"}`); `test_requirements_list_nvidia_ml_py`.
  - `tests/test_config_lock_policy.py` still passes (a GET needs no classification).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests, `tests/test_api.py`, `tests/test_config_lock_policy.py` → PASS.
- [ ] **Step 5: Commit** `System usage: GET /system/usage`.

### Task 4: Top bar rings and details panel

**Files:**
- Create: `assets/system_usage.js` (IIFE, `window.SystemUsage`, `module.exports` for node)
- Modify: `dashboard.html` (a `<div class="user-menu usage-menu" id="usageMenu">` in `.topbar-right` before `#configLockChip`: two `<button type="button" class="usage-ring" data-usage="cpu|gpu" onclick="toggleMenu('usageMenu', event)">` each with an SVG ring and `<span class="usage-ring-val">` + `<span class="usage-ring-label">`, and `<div class="user-menu-panel usage-panel" id="usagePanel">`; `<script src="/assets/system_usage.js?v=1.0.0">` after `config_lock.js`; `SystemUsage.start()` where the dashboard's timers start, before the `_dashboardTimersStarted` return), `assets/dashboard.css` (ring, panel, bars; `@media (max-width: 640px) { .usage-ring-label { display:none; } }`)
- Test: `tests/test_system_usage_dashboard.py`

**Interfaces:**
- Consumes: Task 3 route; `toggleMenu` / `closeMenus` (the account menu's open and close, a click outside and Esc).
- Produces: `SystemUsage.level(percent) -> "ok" | "warn" | "high" | "none"` (none for null), `SystemUsage.ringText(percent) -> string` (`"23%"` or `"—"`), `SystemUsage.busiestGpu(gpus) -> object | null`, `SystemUsage.formatBytes(n) -> string`, `SystemUsage.panelHtml(reading) -> string`, `SystemUsage.isStale(lastAnswerMs, nowMs) -> boolean` (more than 10 000 ms), `SystemUsage.start()`, `SystemUsage.stop()`.
- Behaviour: `start()` asks every 2 s (`setTimeout` chain, one request at a time) while `sessionStorage.access_token` is set and `!document.hidden`; a 401 stops it (`start()` again after sign-in); the rings get `.stale` after 10 s without an answer; ring stroke from `level()` (CSS classes `usage-ok/warn/high/none`); the panel is rebuilt only while it is open.

- [ ] **Step 1: Write the failing tests** (node on the script's exports, as `tests/test_code_processing_dashboard.py` does):
  - `test_script_is_valid_and_loaded` (`node --check`; the script tag after `config_lock.js`; `SystemUsage.start()` in `dashboard.html`).
  - `test_ring_levels`: `level(69) == "ok"`, `level(70) == "warn"`, `level(89.9) == "warn"`, `level(90) == "high"`, `level(None) == "none"`; `ringText(23.4) == "23%"`, `ringText(None) == "—"`.
  - `test_busiest_gpu`: two GPUs 12 and 55 → the 55 one; `[]` → null.
  - Review Focus: `test_no_gpu_shows_a_dash`: `panelHtml` with `gpus: []` contains `No GPU reading on this machine.`; `ringText(busiestGpu([])?.percent ?? null) == "—"`.
  - `test_stale_after_10_s`: `isStale(0, 10000) is false`, `isStale(0, 10001) is true`.
  - `test_format_bytes`: `512 * 1024**2 → "512.0 MB"`, `4 * 1024**3 → "4.0 GB"`.
  - `test_panel_has_every_section`: headings CPU, GPU, RAM, Storage, This server; `server data` on the marked disk; one bar per core.
  - `test_markup_in_the_top_bar`: `id="usageMenu"` before `id="configLockChip"`, two `data-usage` buttons, no `data-help` inside it.
  - Review Focus: `test_polling_stops_on_401` (fake timers and fetch as the preview loop test: a 401 answer → no request in the next 10 s) and `test_ring_labels_hide_on_phones` (the CSS rule).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the script, markup and CSS (rings 30 px: SVG circle `r=13`, `stroke-dasharray` from the percent; colours from the dashboard's success / warning / danger variables).
- [ ] **Step 4: Run** the new tests, `tests/test_help_tips.py`, `tests/test_config_lock_dashboard.py`, `tests/test_dashboard_user_per_tab.py` → PASS.
- [ ] **Step 5: Commit** `System usage: CPU and GPU rings in the top bar with a details panel`.

### Task 5: Docs, full suite, live check

**Files:**
- Modify: `README.md` (a "System usage" paragraph near the top-bar / dashboard part: what the rings show, the GPU sources, the route; and the route in the API list if there is one)
- Scratch only (not committed): a headless Chrome check in the session scratchpad from `check_proc_live.py`'s top, with a launcher copy whose `REPO` is this worktree.

- [ ] **Step 1:** write the README paragraph.
- [ ] **Step 2:** full suite in the worktree (pt.sh's command line from the worktree). Expected: only the known timing flake, passing alone.
- [ ] **Step 3:** live check on the scratch server (8765) on this machine: `GET /system/usage` shows the GTX 1650 under `nvidia`; the rings show numbers that change; a click opens the panel with every section, Esc and an outside click close it; screenshots at 1440 and 375 px; no page errors, no sideways scroll.
- [ ] **Step 4: Commit** `System usage: docs`; stop the scratch server by its port's PID.
