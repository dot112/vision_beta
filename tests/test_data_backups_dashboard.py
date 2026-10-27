"""The Data and backups page (assets/data_backups.js) and the archived days on the Records page.

The page script runs in node (module.exports) on the status the API gives, as system_usage.js does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "data_backups.js"
PAGE = ROOT / "dashboard.html"
CSS = ROOT / "assets" / "dashboard.css"
MB = 1024 ** 2
GB = 1024 ** 3

STATUS = {
    "disk": {"total": 32 * GB, "free": 3 * GB, "room": 0, "used_percent": 90.6, "floor": 2 * GB, "warn_percent": 85, "state": "low"},
    "database": {"size": 4 * GB, "oldest": "2026-07-28T00:00:00Z", "newest": "2026-10-26T11:59:00Z"},
    "archive": {"enabled": True, "days": 120, "size": 300 * MB, "oldest": "2026-06-28", "newest": "2026-10-25", "retention_days": 365},
    "backups": [{"name": "20261026T001000Z-nightly", "kind": "nightly", "made_at": "2026-10-26T00:10:00Z", "size": 2 * MB}],
    "copy": {"dir": "", "ok": None, "last": None},
    "clock": {"paused": True, "reason": "clock_jumped", "since": "2026-10-26T06:00:00Z"},
    "notices": [{"code": "storage.records_lost", "at": "2026-10-26T05:00:00Z", "message": "<b>3 days</b> deleted", "details": {}}],
    "last": {"cleanup": None, "archive": None, "backup": None, "copy": None},
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
        const api = require("./assets/data_backups.js");
        process.stdout.write(JSON.stringify({expression}));
    """)


def _page(status) -> str:
    return _api(f"api.pageHtml({json.dumps(status)})")


def test_script_is_valid_and_loaded():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    assert subprocess.run([node, "--check", str(SCRIPT)], capture_output=True).returncode == 0
    page = PAGE.read_text(encoding="utf-8")
    assert '<script src="/assets/data_backups.js?v=1.0.0"></script>' in page


def test_the_page_shows_every_part():
    html = _page(STATUS)
    for text in ("Back up now", "The clock is right", "20261026T001000Z-nightly", "Download", "No second folder", "OK",
                 "The clock jumped forward while the server was running.", "120 day(s)", "300.0 MB", "4.0 GB"):
        assert text in html, text
    assert "&lt;b&gt;3 days&lt;/b&gt;" in html and "<b>3 days</b>" not in html


def test_the_disk_bar_marks_the_floor():
    html = _page(STATUS)
    assert 'data-floor="6.3"' in html            # 2 GB of 32 GB, one decimal
    assert 'data-used="90.6"' in html


def test_a_clock_that_is_fine_has_no_button():
    html = _page({**STATUS, "clock": {"paused": False, "reason": None, "since": None}})
    assert "The clock is right" not in html and "The clock is fine" in html


def test_the_other_clock_reason_and_a_second_folder():
    html = _page({**STATUS, "clock": {"paused": True, "reason": "would_delete_everything", "since": None},
                  "copy": {"dir": "E:\\vision", "ok": False, "last": None}})
    assert "The newest record is older than the records are kept, so deleting would remove them all." in html
    assert "E:\\vision" in html and "not written" in html


def test_an_empty_server_says_so():
    empty = {**STATUS, "backups": [], "notices": [], "archive": {**STATUS["archive"], "days": 0, "size": 0, "oldest": None, "newest": None},
             "database": {"size": 0, "oldest": None, "newest": None}}
    html = _page(empty)
    assert "No backups yet" in html and "No archive files yet" in html


def test_format_bytes():
    assert _api('[api.formatBytes(0), api.formatBytes(1536), api.formatBytes(3 * 1024 ** 3)].join("|")') == "0 B|1.5 KB|3.0 GB"


def test_the_clock_reasons():
    assert _api("api.CLOCK_REASONS") == {
        "would_delete_everything": "The newest record is older than the records are kept, so deleting would remove them all.",
        "clock_jumped": "The clock jumped forward while the server was running.",
    }


def test_help_only_on_the_floor_and_the_clock():
    assert _page(STATUS).count("data-help=") == 2
    assert _page({**STATUS, "clock": {"paused": False, "reason": None, "since": None}}).count("data-help=") == 1


def test_the_sidebar_item_is_admin_only_and_the_page_is_wired():
    html = PAGE.read_text(encoding="utf-8")
    assert "switchTab('tabData')" in html and 'tabData: "Data and backups"' in html
    assert 'getElementById("dataMenuTab").style.display = lvl === 3' in html
    assert '<div id="tabData" class="tab-page">' in html and 'id="dataBackupsPage"' in html
    assert 'if (id === "tabData" && window.DataBackups) DataBackups.load();' in html
    # In the Admin section, after the Audit log.
    assert html.index("switchTab('tabLogs')") < html.index("switchTab('tabData')") < html.index("switchTab('tabAccount')")


def test_the_records_page_downloads_an_archived_day():
    js = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    assert 'id="recArchiveDay"' in js and "Download archived day" in js and "/api/v1/records/archive" in js
    # .pl-inline is display:flex, which would show the control with no days: hidden must win.
    assert "#recArchive[hidden] { display: none; }" in CSS.read_text(encoding="utf-8")


def test_the_page_stacks_on_a_phone():
    css = CSS.read_text(encoding="utf-8")
    assert ".dbk-grid" in css and ".dbk-bar" in css
    phone = css[css.index("@media (max-width: 640px)"):]
    assert ".dbk-grid" in phone
