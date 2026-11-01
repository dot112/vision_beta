"""Line setup's camera pages (assets/camera_pages.js): which pages a camera's job shows, the small card's
summary line, which page opens, and the menu. The module runs in node (module.exports), as text_reader.js does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

V = {"vision": True, "codes": False, "text": False}
VQ = {"vision": True, "codes": True, "text": False}
Q = {"vision": False, "codes": True, "text": False}
T = {"vision": False, "codes": False, "text": True}


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _run(name: str, *args):
    """CameraPages.<name>(*args), the arguments and the answer as JSON."""
    return _node(f"""
        const api = require("./assets/camera_pages.js");
        const answer = api[{json.dumps(name)}](...{json.dumps(list(args))});
        process.stdout.write(JSON.stringify(answer === undefined ? null : answer));
    """)


def test_the_pages_each_job_shows():
    assert _run("pagesFor", V) == ["camera", "job", "model", "counting", "image"]
    assert _run("pagesFor", VQ) == ["camera", "job", "model", "counting", "codes", "image"]
    assert _run("pagesFor", Q) == ["camera", "job", "codes", "image"]
    assert _run("pagesFor", T) == ["camera", "job", "counting", "text", "image"]


def test_titles():
    assert _run("pageTitle", "counting", T) == "Reading" and _run("pageTitle", "counting", V) == "Counting"
    assert _run("pageTitle", "image", V) == "Image and video"


def test_which_page_shows():
    assert _run("pageToShow", {"asked": "codes", "remembered": "model"}, V) == "model"
    assert _run("pageToShow", {"asked": None, "remembered": None}, V) == "camera"
    assert _run("pageToShow", {"asked": "text", "remembered": "model"}, T) == "text"
    assert _run("pageAfterJobChange", "model", Q) == "job" and _run("pageAfterJobChange", "image", Q) == "image"
    assert [_run("startPage", k) for k in (V, VQ, Q, T)] == ["model", "model", "codes", "text"]
    assert [_run("pageForProblem", p) for p in ("no_model", "no_list", "no_yolo", "no_format")] == ["model", "codes", "text", "text"]


def test_the_summary_line():
    def s(**kw):
        return _run("summary", {"index": 1, "type": "IP", "countMode": "track", "joined": False, "codeTrigger": "continuous", **kw})

    assert s(kind=T, countMode="trigger") == "Camera 2 · IP · One picture per trigger"
    assert s(kind=T) == "Camera 2 · IP · Shows text"
    assert s(kind=V) == "Camera 2 · IP · Count lines"
    assert s(kind=VQ, countMode="trigger", joined=True) == "Camera 2 · IP · One picture per trigger · joined"
    assert s(kind=Q, codeTrigger="signal") == "Camera 2 · IP · Reads when an input asks"
    assert s(kind=Q, codeTrigger="line2", type="") == "Camera 2 · Reads on wire line 2"
    assert s(kind=Q) == "Camera 2 · IP · Shows codes"


def test_the_menu():
    html = _run("menuHtml", T, "counting")
    assert html.count('data-act="page"') == 7 and 'data-page="model" role="tab" aria-selected="false" hidden' in html
    assert re.search(r'class="pl-page-link on" data-act="page" data-page="counting"[^>]*>Reading<', html)
    assert "data-lock" not in html


def test_reopen_needs_the_same_line_and_camera():
    o = {"id": "c1", "page": "codes", "lineId": "line-1"}
    assert _run("reopen", o, "line-1", ["c0", "c1"]) == o
    assert _run("reopen", o, "line-2", ["c1"]) is None and _run("reopen", o, "line-1", ["c0"]) is None
    assert _run("reopen", None, "line-1", ["c1"]) is None


def test_the_page_loads_the_module_before_the_line_setup():
    page = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert page.index("/assets/camera_pages.js?v=1.0.0") < page.index("/assets/production_lines.js?v=")
