"""The top bar's CPU and GPU rings and their details panel (assets/system_usage.js).

The script runs in node (module.exports) on small stand-ins, as code_processing.js does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "system_usage.js"
PAGE = ROOT / "dashboard.html"
CSS = ROOT / "assets" / "dashboard.css"
GB = 1024 ** 3

READING = {
    "taken_at": "2026-10-25T13:02:44+00:00",
    "cpu": {"percent": 23.5, "per_core": [20.0, 27.0, 30.0, 17.0], "cores_logical": 4, "cores_physical": 2,
            "freq_mhz": 2400.0, "freq_max_mhz": 3000.0, "load_avg": None, "temp_c": None},
    "memory": {"percent": 81.2, "used": 13 * GB, "total": 16 * GB, "available": 3 * GB,
               "swap_percent": 25.0, "swap_used": 1 * GB, "swap_total": 4 * GB},
    "disks": [{"mount": "C:\\", "device": "C:\\", "fstype": "NTFS", "percent": 84.3, "used": 84 * GB, "free": 16 * GB,
               "total": 100 * GB, "server_data": False},
              {"mount": "F:\\", "device": "F:\\", "fstype": "NTFS", "percent": 74.4, "used": 74 * GB, "free": 26 * GB,
               "total": 100 * GB, "server_data": True}],
    "gpus": [{"name": "NVIDIA GeForce GTX 1650", "source": "nvidia", "percent": 12.0, "memory_used": 127 * 1024 ** 2,
              "memory_total": 4 * GB, "temp_c": 49.0}],
    "server": {"cpu_percent": 4.1, "memory_rss": 900 * 1024 ** 2, "memory_percent": 5.6},
}


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _api(expression: str):
    return _node(f"""
        const api = require("./assets/system_usage.js");
        process.stdout.write(JSON.stringify({expression}));
    """)


def test_script_is_valid_and_loaded():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    assert subprocess.run([node, "--check", str(SCRIPT)], capture_output=True).returncode == 0
    page = PAGE.read_text(encoding="utf-8")
    assert '<script src="/assets/system_usage.js?v=1.0.0"></script>' in page
    assert page.index("/assets/config_lock.js") < page.index("/assets/system_usage.js")
    assert "SystemUsage.start()" in page


def test_ring_levels():
    assert _api("[69, 70, 89.9, 90, 100, null].map(api.level)") == ["ok", "warn", "warn", "high", "high", "none"]
    assert _api("[api.ringText(23.4), api.ringText(0), api.ringText(null)]") == ["23%", "0%", "—"]


def test_busiest_gpu():
    got = _api('[api.busiestGpu([{name: "a", percent: 12}, {name: "b", percent: 55}]), api.busiestGpu([])]')
    assert got == [{"name": "b", "percent": 55}, None]


def test_stale_after_10_s():
    assert _api("[api.isStale(0, 10000), api.isStale(0, 10001)]") == [False, True]


def test_format_bytes():
    assert _api(f"[api.formatBytes({512 * 1024 ** 2}), api.formatBytes({4 * GB}), api.formatBytes(null)]") == \
        ["512.0 MB", "4.0 GB", "—"]


def test_panel_has_every_section():
    html = _api(f"api.panelHtml({json.dumps(READING)})")
    for heading in ("CPU", "GPU", "RAM", "Storage", "This server"):
        assert f">{heading}<" in html, heading
    assert html.count('class="usage-core"') == 4
    assert html.count("server data") == 1 and html.index("F:\\") < html.index("server data")
    assert "NVIDIA GeForce GTX 1650" in html and "13.0 GB of 16.0 GB" in html


def test_markup_in_the_top_bar():
    page = PAGE.read_text(encoding="utf-8")
    assert page.index('id="usageMenu"') < page.index('id="configLockChip"')
    menu = page[page.index('id="usageMenu"'):page.index('id="configLockChip"')]
    assert menu.count("data-usage=") == 2 and 'data-usage="cpu"' in menu and 'data-usage="gpu"' in menu
    assert "data-help" not in menu


# ── Review Focus ──────────────────────────────────────────────────────────────

def test_no_gpu_shows_a_dash():
    no_gpu = {**READING, "gpus": []}
    html = _api(f"api.panelHtml({json.dumps(no_gpu)})")
    assert "No GPU reading on this machine." in html
    assert _api("api.ringText((api.busiestGpu([]) || {percent: null}).percent)") == "—"


POLL = r"""
// The polling loop on a fake clock: the first answer is 401 (signed out).
let now = 0, requests = 0;
const timers = [];
let ids = 0;
global.setTimeout = (f, ms) => { const t = { id: ++ids, at: now + (ms || 0), f }; timers.push(t); return t.id; };
global.clearTimeout = (h) => { const i = timers.findIndex((t) => t.id === h); if (i >= 0) timers.splice(i, 1); };
global.window = { document: { hidden: false, getElementById: () => null, querySelectorAll: () => [] },
  sessionStorage: { getItem: () => "token" },
  fetch: () => { requests += 1; return Promise.resolve({ ok: false, status: 401, json: async () => ({}) }); } };
async function run(until) {
  for (;;) {
    await new Promise((r) => setImmediate(r));
    timers.sort((a, b) => a.at - b.at);
    const t = timers[0];
    if (!t || t.at > until) break;
    timers.shift(); now = t.at; t.f();
  }
  now = until;
}
const api = require("./assets/system_usage.js");
(async () => {
  api.start();
  await run(10000);
  process.stdout.write(JSON.stringify({ requests }));
})();
"""


def test_polling_stops_on_401():
    assert _node(POLL) == {"requests": 1}


def test_ring_labels_hide_on_phones():
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"@media \(max-width: 640px\) \{[^}]*\.usage-ring-label \{ display: none; \}", css)
