"""Line setup's "Image processing" fold: its fields, the settings it saves, and its live preview.

assets/code_processing.js runs in node (module.exports) on small stand-ins, as card_values.js does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.engines.code_processing import DEFAULT_PROCESSING

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "code_processing.js"
CARDS = ROOT / "assets" / "production_lines.js"
PAGE = ROOT / "dashboard.html"

FIELDS = ("proc_enabled", "proc_persp", "proc_corners", "proc_rotate", "proc_angle", "proc_scale", "proc_factor",
          "proc_gray", "proc_denoise", "proc_denoise_n", "proc_contrast", "proc_contrast_n", "proc_sharpen",
          "proc_sharpen_n", "proc_bin", "proc_bin_method", "proc_bin_level", "proc_bin_block", "proc_invert", "proc_margin")


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _collect(fields: dict):
    """CodeProcessing.collect on a card whose fields have these values ({name: value or bool})."""
    return _node(f"""
        const api = require("./assets/code_processing.js");
        const values = {json.dumps(fields)};
        const card = {{ querySelector(sel) {{
            const name = (sel.match(/data-f="([^"]+)"/) || [])[1];
            if (!(name in values)) return null;
            const v = values[name];
            return typeof v === "boolean" ? {{ checked: v, value: "on" }} : {{ checked: false, value: v }};
        }} }};
        process.stdout.write(JSON.stringify(api.collect(card)));
    """)


def test_script_is_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    assert subprocess.run([node, "--check", str(SCRIPT)], capture_output=True).returncode == 0
    page = PAGE.read_text(encoding="utf-8")
    assert '<script src="/assets/code_processing.js?v=1.0.0"></script>' in page
    assert page.index("/assets/code_processing.js") < page.index("/assets/production_lines.js")
    assert "/assets/production_lines.js?v=3.11.0" in page


def test_fold_only_for_reader_jobs():
    cards = CARDS.read_text(encoding="utf-8")
    shown = re.search(r"const foldShown = .*", cards).group(0)
    assert 'fold === "processing"' in shown and "JOBS[job].codes" in shown
    assert 'fold("processing", "Image processing", CodeProcessing.foldBody(cam))' in cards
    assert 'fold.dataset.fold === "processing") CodeProcessing.open(card)' in cards


def test_entry_carries_code_processing():
    cards = CARDS.read_text(encoding="utf-8")
    entry = re.search(r"function cardEntry\(card\) \{.*?\n    \}\n", cards, re.S).group(0)
    codes = entry[entry.index("if (spec.codes) {\n            Object.assign"):]
    assert "entry.code_processing = CodeProcessing.collect(card);" in codes


def test_fold_body_has_the_fields():
    html = _node("""
        const api = require("./assets/code_processing.js");
        process.stdout.write(JSON.stringify(api.foldBody({})));
    """)
    for name in FIELDS + ("proc-pic", "proc-status"):
        assert f'data-f="{name}"' in html, name
    assert 'data-act="proc-reset-corners"' in html and html.count('data-act="proc-view"') == 2
    assert "Process the picture before reading" in html
    assert "When the processed picture reads nothing, the original is read too." in html
    helps = re.findall(r'data-help="([^"]*)"', html)
    assert helps == [
        "Processes a copy of the picture before codes are read: the video keeps showing the camera's own picture.",
        "Drag the four corners around the label on the Original view: that area is made flat and straight before reading.",
    ]


def test_fold_body_shows_saved_values():
    saved = json.loads(json.dumps(DEFAULT_PROCESSING))
    saved["enabled"] = True
    saved["scale"] = {"on": True, "factor": 2.5}
    html = _node(f"""
        const api = require("./assets/code_processing.js");
        process.stdout.write(JSON.stringify(api.foldBody({{ code_processing: {json.dumps(saved)} }})));
    """)
    assert re.search(r'<input type="checkbox" data-f="proc_enabled" checked', html)
    assert re.search(r'data-f="proc_factor"[^>]*value="2.5"', html)
    assert not re.search(r'<input type="checkbox" data-f="proc_rotate" checked', html)


def test_collect_reads_the_fields():
    got = _collect({
        "proc_enabled": True, "proc_rotate": True, "proc_angle": "12.5", "proc_bin": True, "proc_bin_method": "adaptive",
        "proc_bin_block": "41", "proc_bin_level": "128", "proc_invert": True, "proc_margin": "20",
        "proc_persp": True, "proc_corners": json.dumps([[0.2, 0.1], [0.8, 0.1], [0.9, 0.9], [0.1, 0.9]]),
        "proc_denoise": False, "proc_denoise_n": "3",
    })
    assert got["enabled"] is True
    assert got["rotate"] == {"on": True, "angle": 12.5}
    assert got["binarize"] == {"on": True, "method": "adaptive", "level": 128, "block": 41, "invert": True}
    assert got["crop_margin_px"] == 20
    assert got["perspective"] == {"on": True, "corners": [[0.2, 0.1], [0.8, 0.1], [0.9, 0.9], [0.1, 0.9]]}
    assert got["denoise"] == {"on": False, "strength": 3}
    assert set(got) == set(DEFAULT_PROCESSING)


def test_collect_fills_empty_fields_with_defaults():
    got = _collect({"proc_enabled": True, "proc_angle": "", "proc_factor": "-", "proc_margin": "", "proc_corners": "nonsense",
                    "proc_contrast_n": "", "proc_bin_method": ""})
    assert got["rotate"]["angle"] == DEFAULT_PROCESSING["rotate"]["angle"]
    assert got["scale"]["factor"] == DEFAULT_PROCESSING["scale"]["factor"]
    assert got["crop_margin_px"] == DEFAULT_PROCESSING["crop_margin_px"]
    assert got["perspective"]["corners"] == DEFAULT_PROCESSING["perspective"]["corners"]
    assert got["contrast"]["strength"] == DEFAULT_PROCESSING["contrast"]["strength"]
    assert got["binarize"]["method"] == "auto"


def test_status_line():
    got = _node("""
        const api = require("./assets/code_processing.js");
        process.stdout.write(JSON.stringify([api.statusText(2, "processed", 23.4), api.statusText(1, "original", 8),
                                              api.statusText(0, "none", 31.2)]));
    """)
    assert got == ["2 codes · read in the processed picture · 23 ms", "1 code · read in the original picture · 8 ms",
                   "No code read · 31 ms"]


def test_defaults_match_the_server():
    got = _node("""
        const api = require("./assets/code_processing.js");
        process.stdout.write(JSON.stringify(api.DEFAULTS));
    """)
    assert got == json.loads(json.dumps(DEFAULT_PROCESSING))
