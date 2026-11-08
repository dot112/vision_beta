# System usage monitor

Date: 2026-10-25. Branch `claude/system-usage` (from `main` at `e5f1e00`).

The owner wants the machine's load in the dashboard's top bar: two small rings, CPU and
GPU, each with its usage in percent inside. Clicking a ring opens the details: CPU, GPU,
RAM and storage. It is read with `psutil` and works on Linux and Windows.

## Owner's decisions (2026-10-25)

- **Whose usage:** the whole machine; the details also show this server's own share.
- **GPUs:** NVIDIA (Windows, Linux, Docker with a GPU), Jetson boards and AMD cards on Linux.
  Any other GPU shows "—".
- **Who sees it:** everyone signed in (it only shows numbers).
- **High usage:** the ring's colour only (green, amber from 70%, red from 90%); no alarm.

## 1. Readings

One reading, taken every 2 s by a background sampler, shared by every open dashboard:

```json
{
  "taken_at": "2026-10-25T13:02:44.905650+00:00",
  "cpu": {"percent": 23.5, "per_core": [20.0, 31.2], "cores_logical": 8, "cores_physical": 4,
          "freq_mhz": 2496.0, "freq_max_mhz": 2496.0, "load_avg": [0.8, 0.6, 0.5], "temp_c": 54.0},
  "memory": {"percent": 81.2, "used": 13000000000, "total": 16000000000, "available": 3000000000,
             "swap_percent": 12.0, "swap_used": 500000000, "swap_total": 4000000000},
  "disks": [{"mount": "C:\\", "device": "C:\\", "fstype": "NTFS", "percent": 71.0,
             "used": 300000000000, "free": 120000000000, "total": 420000000000, "server_data": true}],
  "gpus": [{"name": "NVIDIA GeForce GTX 1650", "source": "nvidia", "percent": 12.0,
            "memory_used": 127000000, "memory_total": 4096000000, "temp_c": 49.0}],
  "server": {"cpu_percent": 4.1, "memory_rss": 900000000, "memory_percent": 5.6}
}
```

- **CPU** (`psutil`): `cpu_percent()` (whole machine) and `cpu_percent(percpu=True)`, both
  between two samples; core counts; `cpu_freq()` when the system gives it; `getloadavg()` on
  Linux only (else `null`); the CPU temperature from `sensors_temperatures()` when present
  (Linux; first of `coretemp`, `k10temp`, `cpu_thermal`, `soc_thermal`, else the highest
  reading; `null` on Windows).
- **RAM** (`psutil`): `virtual_memory()` and `swap_memory()`.
- **Disks** (`psutil`): each partition from `disk_partitions(all=False)` that answers
  `disk_usage()`. A partition that does not answer (an empty card reader, a disconnected
  network drive) is left out, not an error. Snap and squashfs loop mounts are left out on
  Linux. `server_data` is true for the disk that holds the server's `data` folder (the
  partition with the longest mount point that contains `DATA_DIR`).
- **GPUs:** each source is tried in turn; all GPUs found are listed (`source`: `nvidia`,
  `jetson`, `amd`):
  - **NVIDIA:** NVIDIA's management library through `nvidia-ml-py` (`pynvml`): name,
    utilisation, memory used/total, temperature. Initialised once; a machine without the
    library or the driver simply has no NVIDIA GPUs.
  - **Jetson:** `/sys/devices/gpu.0/load` or `/sys/devices/platform/gpu.0/load` (tenths of a
    percent), named from `/proc/device-tree/model`; memory is shared with the CPU, so
    `memory_used`/`memory_total` are `null`; temperature from the thermal zone named `GPU-therm`
    or `gpu-thermal` when present.
  - **AMD on Linux:** each `/sys/class/drm/card*/device` with `gpu_busy_percent`; memory from
    `mem_info_vram_used`/`mem_info_vram_total`; temperature from its `hwmon*/temp1_input`
    (millidegrees); named from `product_name` when present, else `AMD GPU (cardN)`.
  - A source that fails during a reading is skipped for that reading and logged at most once a
    minute.
- **Server** (`psutil.Process()`): this server's CPU as a share of the whole machine
  (`cpu_percent()` / logical cores), its resident memory and `memory_percent()`.
- Numbers are rounded to one decimal; sizes are bytes; temperatures are °C; anything a system
  does not give is `null`.

**Sampler:** started on the first request, it takes a reading every 2 s on its own thread and
stops after 60 s without a request, so no dashboard open means no work. The first answer
after a start waits for the first reading (at most about 0.5 s: the CPU needs two samples).
A reading costs about a millisecond.

## 2. API

`GET /api/v1/system/usage` answers the latest reading (section 1).

- Any signed-in user (Level 1+). API keys need `monitor:read` (a rule placed before the
  generic `/api/v1/system/` rule).
- A `GET`: the configuration lock does not touch it.

## 3. Dependencies

`nvidia-ml-py` gets its own line in `requirements.txt` (pure Python, every platform; on the
development machine it came with `ultralytics`, which is not in `requirements.txt`; it does
nothing on a machine without an NVIDIA driver). `psutil` is already there.

## 4. Dashboard

- **Top bar:** two rings left of the lock chip, labelled CPU and GPU: about 30 px, the usage
  rounded to a whole percent inside (`23%`), the label beside the ring.
  - The GPU ring shows the busiest GPU; with no GPU reading it shows `—` in a grey ring.
  - Colour: green below 70%, amber from 70%, red from 90%.
  - At 640 px and narrower the labels hide; the rings stay.
- **Details panel:** clicking either ring opens a panel under it (the account menu's
  dropdown: a click outside or Esc closes it):
  - **CPU:** total %, a small bar per core, cores (logical / physical), clock (current / max)
    when known, load average and temperature when known.
  - **GPU:** for each GPU: name, usage bar, memory used of total, temperature; or "No GPU
    reading on this machine."
  - **RAM:** usage bar, used of total; swap used of total when there is swap.
  - **Storage:** each disk: mount, usage bar, used of total and free; the disk with the
    server's data is marked "server data".
  - **This server:** its CPU share and its memory.
  - Sizes as `GB` (or `MB` under 1 GB), one decimal.
- **Refresh:** every 2 s while the dashboard is signed in and the page is visible; no
  request while hidden. A failed request leaves the last numbers and greys the rings after
  10 s without an answer.
- New file `assets/system_usage.js` (`window.SystemUsage`), styles in `assets/dashboard.css`.
  No help icons (the labels are plain).

## 5. Tests

- **Readings** (with stand-ins for `psutil`, `pynvml` and a fake `/sys` and `/proc` in a
  temporary folder): every key of section 1 present; Linux-only values `null` on Windows;
  a partition that raises is left out; `server_data` on the right disk; NVIDIA GPUs from
  the library; no library or no driver: no NVIDIA GPU, no error; Jetson load 412 → 41.2%;
  AMD busy percent, VRAM and temperature; a failing source is skipped and the rest still
  read.
- **Sampler:** starts on the first request, one reading every 2 s, stops after 60 s idle
  (clock and sleeps injected).
- **API:** 401 without sign-in; a Level 1 user gets 200 with every key; an API key with
  `monitor:read` gets 200, one without it 403.
- **Dashboard:** `node --check`; the ring colour at 69/70/89/90%; `—` without a GPU; the
  busiest GPU on the ring; size formatting; the markup in the top bar; no help icon.
- **Live check** on the scratch server on this machine (Windows, GTX 1650, so the NVIDIA path
  is real): the rings move, the panel opens and closes, at 1440 and 375 px; no page errors,
  no sideways scroll.

## Not in this change

- Intel or AMD GPUs on Windows (Windows' own GPU counters), Apple, Raspberry Pi GPUs.
- History graphs, alarms or limits on usage, per-camera or per-model usage.
