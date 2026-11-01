"""Line setup's Save and Load (assets/line_setups.js): the two text buttons, the Save window and its name check,
the Load window's rows. The module runs in node (module.exports), as camera_pages.js does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAVE = {"id": "setup-1a2b3c4d", "name": "<b>A</b>", "line_name": "Line 1", "saved_at": "2026-11-01T10:15:00+00:00",
        "saved_by": "admin"}


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _run(name: str, *args):
    """LineSetups.<name>(*args), the arguments and the answer as JSON."""
    return _node(f"""
        const api = require("./assets/line_setups.js");
        const answer = api[{json.dumps(name)}](...{json.dumps(list(args))});
        process.stdout.write(JSON.stringify(answer === undefined ? null : answer));
    """)


def test_the_buttons_are_level_2_only():
    assert _run("buttonsHtml", True) == ""
    html = _run("buttonsHtml", False)
    assert 'data-ls="save">Save</button>' in html and 'data-ls="open-load">Load</button>' in html
    assert html.count('class="ls-link restricted-l2"') == 2 and "data-lock" not in html


def test_a_name_must_be_1_to_64_characters():
    assert [_run("nameProblem", n) for n in ("", "  ", "x" * 65, "Product A", "x" * 64)] == \
        ["Give the setup a name.", "Give the setup a name.", "A name has at most 64 characters.", "", ""]


def test_save_window_waits_for_unsaved_changes():
    html = _run("saveWindowHtml", {"lineName": "Line <1>", "dirty": True})
    assert "Save line setup" in html and "Unsaved changes: save the line setup first." in html
    assert re.search(r'data-ls="do-save"[^>]*disabled', html) and 'data-ls="name"' in html and 'maxlength="64"' in html
    assert "Line &lt;1&gt;" in html
    clean = _run("saveWindowHtml", {"lineName": "Line 1", "dirty": False})
    assert "Unsaved changes" not in clean and not re.search(r'data-ls="do-save"[^>]*disabled', clean)


def test_load_window_rows():
    html = _run("loadWindowHtml", {"lineName": "Line 1", "running": False, "saves": [SAVE]})
    assert "Load line setup" in html
    assert "<b>&lt;b&gt;A&lt;/b&gt;</b>" in html and '<span class="ls-line">Line 1</span>' in html
    assert 'data-ls="load" data-id="setup-1a2b3c4d"' in html and 'data-ls="delete" data-id="setup-1a2b3c4d"' in html
    assert 'aria-label="Delete &lt;b&gt;A&lt;/b&gt;"' in html and "2026" in html
    pick = re.search(r'<button[^>]*data-ls="load"[^>]*>', html).group(0)
    assert "data-lock" in pick and "disabled" not in pick
    assert "data-lock" in re.search(r'<button[^>]*data-ls="delete"[^>]*>', html).group(0)


def test_load_window_empty_and_running():
    empty = _run("loadWindowHtml", {"lineName": "Line 1", "running": False, "saves": []})
    assert "No saved setups for this line yet. Use Save to keep one." in empty and 'data-ls="load"' not in empty
    running = _run("loadWindowHtml", {"lineName": "Line 1", "running": True, "saves": [SAVE, {**SAVE, "id": "setup-00000000"}]})
    assert "Stop the line to load a setup." in running
    picks = re.findall(r'<button[^>]*data-ls="load"[^>]*>', running)
    assert len(picks) == 2 and all("disabled" in p for p in picks)


def test_the_load_question():
    assert _run("loadQuestion", "Product A", "Line 1", False) == (
        "Load 'Product A' onto Line 1? Its setup and camera settings replace the current ones; "
        "connections it uses that no longer exist are added back.")
    assert _run("loadQuestion", "Product A", "Line 1", True).endswith("added back. Unsaved changes are lost.")


def test_the_page_uses_it():
    lines_js = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    assert "Line setup · ${esc(d.name)}${LineSetups.buttonsHtml(readOnly)}" in lines_js and "LineSetups.bind(" in lines_js
    html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert '<script src="/assets/line_setups.js?v=1.0.0"></script>' in html
    assert html.index("/assets/line_setups.js") < html.index("/assets/production_lines.js")
    css = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")
    rule = re.search(r"\.ls-link\s*\{([^}]*)\}", css).group(1)
    assert "color: var(--text-color)" in rule and "background: none" in rule and "border: 0" in rule
