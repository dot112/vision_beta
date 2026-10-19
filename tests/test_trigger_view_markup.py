"""The Line Dashboard's view of a trigger camera: "Waiting for trigger", each picture for 5 s, its counters.

The pure helpers (assets/production_lines.js, loaded by node like assets/card_values.js) decide what the view
shows; the view's code is cut out of the script and run on small stand-ins (a fake page, a fake fetch), and the
markup is read as text.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "assets" / "production_lines.js"
PAGE = ROOT / "dashboard.html"
CSS = ROOT / "assets" / "dashboard.css"


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr[:1200]
    return json.loads(run.stdout)


def _helper(expression: str):
    """Evaluate a JS expression with the script's exported helpers in scope; the result comes back as JSON."""
    return _node(f"const H = require({json.dumps(str(SCRIPT))}); console.log(JSON.stringify({expression}));")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _view_block(script: str) -> str:
    """The view's code inside the script's closure: from its section comment to the next section's function."""
    start = script.index("    // ── Trigger cameras on the Line Dashboard")
    return script[start:script.index("\n    async function refreshLineExtras", start)]


ROW = "{camera_id: 'cam-1', picture_source: 'system', notice_trigger: null, counters: {}, capture: %s}"


def _capture(id_: int = 7, has_picture: bool = True) -> str:
    return "{id: %d, at: '2026-10-19T08:00:01+00:00', class_name: 'apple', result: 'good', reason: null, trigger_no: 3, has_picture: %s}" % (
        id_, "true" if has_picture else "false")


def _state(capture: str, shown: str, now: int, running: str = "true") -> dict:
    return _helper(f"H.triggerViewState({ROW % capture}, {shown}, {now}, {running})")


# ── The pure state ────────────────────────────────────────────────────────────


def test_view_waits_then_shows_then_waits_again():
    capture = _capture(7)
    # Nothing taken yet.
    assert _state("null", "null", 1000) == {"mode": "waiting"}
    # A capture the page has not shown: show it, and fetch its picture once.
    assert _state(capture, "null", 1000) == {"mode": "picture", "fetchId": 7}
    # Shown since 1000: still the picture 4999 ms later, waiting again at 5000 ms.
    shown = "{id: 7, atMs: 1000}"
    assert _state(capture, shown, 1000) == {"mode": "picture"}
    assert _state(capture, shown, 5999) == {"mode": "picture"}
    assert _state(capture, shown, 6000) == {"mode": "waiting"}
    assert _state(capture, shown, 600000) == {"mode": "waiting"}
    # The next trigger's capture is new again.
    assert _state(_capture(8), shown, 600000) == {"mode": "picture", "fetchId": 8}


def test_a_newer_capture_replaces_the_one_shown_at_once():
    assert _state(_capture(8), "{id: 7, atMs: 1000}", 2000) == {"mode": "picture", "fetchId": 8}


def test_no_picture_capture_shows_the_reject_text():
    capture = _capture(9, has_picture=False)
    # No JPEG exists for it: nothing to fetch, the panel says "No picture: Reject".
    assert _state(capture, "null", 1000) == {"mode": "no_picture"}
    shown = "{id: 9, atMs: 1000}"
    assert _state(capture, shown, 5999) == {"mode": "no_picture"}
    assert _state(capture, shown, 6000) == {"mode": "waiting"}


def test_same_capture_is_not_fetched_twice():
    capture = _capture(7)
    shown = "{id: 7, atMs: 1000}"
    for now in (1000, 1001, 3000, 5999):
        assert "fetchId" not in _state(capture, shown, now)
    # Once it is the one on screen the page keeps no further request for it, after the wait too.
    assert "fetchId" not in _state(capture, shown, 9000)


def test_line_stopped_view():
    # A stopped line shows its note whatever the capture is, and fetches nothing.
    for capture in ("null", _capture(7), _capture(8, has_picture=False)):
        assert _state(capture, "null", 1000, running="false") == {"mode": "stopped"}
        assert _state(capture, "{id: 7, atMs: 1000}", 2000, running="false") == {"mode": "stopped"}
    # A running line, or a caller that does not say, is not stopped.
    assert _state("null", "null", 1000, running="true") == {"mode": "waiting"}
    assert _helper(f"H.triggerViewState({ROW % 'null'}, null, 1000)") == {"mode": "waiting"}
    # No row at all (no trigger camera): waiting, not an error.
    assert _helper("H.triggerViewState(null, null, 1000, true)") == {"mode": "waiting"}


def test_caption_joins_class_result_trigger_and_time():
    reasons = "{vision_class: 'vision class', no_picture: 'no picture'}"
    time = "new Date('2026-10-19T08:00:01+00:00').toLocaleTimeString()"
    good = _helper(f"H.triggerCaption({_capture(1)}, {reasons})")
    assert good == _helper(f"['apple', 'Good', 'trigger 3', {time}].join(' \\u00b7 ')")
    reject = _helper("H.triggerCaption({id: 2, at: '2026-10-19T08:00:01+00:00', class_name: 'bruise', result: 'reject',"
                     f" reason: 'vision_class', trigger_no: 12, has_picture: true}}, {reasons})")
    assert reject == _helper(f"['bruise', 'Reject (vision class)', 'trigger 12', {time}].join(' \\u00b7 ')")
    no_picture = _helper("H.triggerCaption({id: 3, at: '2026-10-19T08:00:01+00:00', class_name: 'no picture', result: 'reject',"
                         f" reason: 'no_picture', trigger_no: null, has_picture: false}}, {reasons})")
    # No trigger number: that part is left out.
    assert no_picture == _helper(f"['no picture', 'Reject (no picture)', {time}].join(' \\u00b7 ')")
    # Nothing found and ignored: no class and no result.
    nothing = _helper("H.triggerCaption({id: 4, at: '2026-10-19T08:00:01+00:00', class_name: null, result: null,"
                      f" reason: null, trigger_no: 5, has_picture: true}}, {reasons})")
    assert nothing == _helper(f"['nothing found', 'No result', 'trigger 5', {time}].join(' \\u00b7 ')")


def test_counters_late_and_extra_only_for_camera_trigger_with_a_notice():
    counters = "{triggers: 5, pictures: 4, results: 4, missing: 1, late: 2, extra: 3}"

    def labels(source: str, notice: str):
        row = f"{{picture_source: '{source}', notice_trigger: {notice}, counters: {counters}}}"
        return _helper(f"H.triggerCounterFigures({row})")

    assert labels("camera", "2") == [["Triggers", 5], ["Pictures", 4], ["Results", 4], ["Missing", 1], ["Late", 2], ["Extra", 3]]
    for source, notice in (("camera", "null"), ("system", "null"), ("system", "2")):
        assert [name for name, _ in labels(source, notice)] == ["Triggers", "Pictures", "Results", "Missing"]
    # A counter the server does not send reads 0.
    assert _helper("H.triggerCounterFigures({picture_source: 'system', notice_trigger: null})") == [
        ["Triggers", 0], ["Pictures", 0], ["Results", 0], ["Missing", 0]]


# ── The view, run on a fake page ──────────────────────────────────────────────

_SETUP = r"""
const H = require(%(script)s);
const {triggerViewState, triggerCaption, triggerCounterFigures} = H;
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const els = {};
for (const id of ["trigView", "trigViewImg", "trigViewText", "trigViewCaption", "trigCounters"]) {
    els[id] = {id, hidden: true, textContent: "", innerHTML: "", dataset: {}, src: undefined,
        removeAttribute(name) { if (name === "src") this.src = undefined; }};
}
els.noVideoNote = {textContent: "Waiting for the first picture\u2026"};
els.noVideoOverlay = {active: false, classList: {add(name) { els.noVideoOverlay.active = name === "active" || els.noVideoOverlay.active; }}};
const cameraName = (id) => "Camera " + id;
const byId = (id) => els[id] || null;
const state = {lineId: "line 1", detail: null};
let cam = {camera_id: "cam-1", connected: true, role: "vision"};
const lineFeedCamera = () => cam;
let streamOpen = false, paused = 0;
const pauseMainFeed = () => { paused++; streamOpen = false; };
const isLiveFeedRunning = () => streamOpen;
const REJECT_REASON = {vision_class: "vision class", no_picture: "no picture"};
let now = 100000;
Date.now = () => now;
const requests = [];
let reply = () => ({ok: true, status: 200, headers: {get: (n) => (n === "X-Capture-Id" ? String(lastId) : null)}, blob: async () => "jpeg-bytes"});
let lastId = 0;
const fetch = async (url, options) => { requests.push([url, options]); return reply(url); };
const made = [], revoked = [];
const URL = {createObjectURL: () => { made.push("blob:" + made.length); return made[made.length - 1]; }, revokeObjectURL: (u) => revoked.push(u)};
const tick = () => new Promise((r) => setTimeout(r, 0));
const row = (capture, extra) => Object.assign({camera_id: "cam-1", picture_source: "system", notice_trigger: null,
    counters: {triggers: 1, pictures: 1, results: 1, missing: 0, late: 0, extra: 0}, capture}, extra || {});
const cap = (id, extra) => Object.assign({id, at: "2026-10-19T08:00:01+00:00", class_name: "apple", result: "good", reason: null, trigger_no: 3, has_picture: true}, extra || {});
const status = (rows, running) => ({state: running === false ? "stopped" : "running", trigger_cameras: rows});
%(block)s
"""


def _run_view(scenario: str):
    block = _view_block(_script())
    code = (_SETUP % {"script": json.dumps(str(SCRIPT)), "block": block}
            + "\n(async () => {\n" + scenario + "\n})().then(() => {}, (e) => { console.error(e); process.exit(1); });")
    return _node(code)


def test_view_fetches_the_picture_once_shows_it_for_5_s_then_waits():
    result = _run_view(r"""
    const log = {};
    lastId = 7;
    renderTriggerView(status([row(cap(7))]));
    await tick();
    log.first = {requests: requests.map((r) => r[0]), cache: requests[0][1], img: els.trigViewImg.src ?? null, imgHidden: els.trigViewImg.hidden,
        textHidden: els.trigViewText.hidden, captionHidden: els.trigViewCaption.hidden, viewHidden: els.trigView.hidden};
    log.caption = els.trigViewCaption.textContent === triggerCaption(cap(7), REJECT_REASON);
    // The next refreshes (1 s apart) show the same capture and ask for nothing more.
    for (let i = 0; i < 4; i++) { now += 1000; renderTriggerView(status([row(cap(7))])); await tick(); }
    log.during = {requests: requests.length, imgHidden: els.trigViewImg.hidden};
    now += 999;   // 4999 ms since the page showed it
    renderTriggerView(status([row(cap(7))]));
    log.at4999 = {imgHidden: els.trigViewImg.hidden};
    now += 1;     // 5000 ms
    renderTriggerView(status([row(cap(7))]));
    log.at5000 = {imgHidden: els.trigViewImg.hidden, src: els.trigViewImg.src ?? null, text: els.trigViewText.textContent,
        textHidden: els.trigViewText.hidden, captionHidden: els.trigViewCaption.hidden, revoked: revoked.slice(), requests: requests.length};
    // The same capture stays unshown; a new trigger shows again.
    now += 30000; renderTriggerView(status([row(cap(7))])); await tick();
    log.later = {requests: requests.length, imgHidden: els.trigViewImg.hidden};
    lastId = 8; renderTriggerView(status([row(cap(8))])); await tick();
    log.next = {requests: requests.length, imgHidden: els.trigViewImg.hidden, src: els.trigViewImg.src ?? null};
    console.log(JSON.stringify(log));
    """)
    first = result["first"]
    assert first["requests"] == ["/api/v1/lines/line%201/trigger/capture?camera_id=cam-1"]
    assert first["cache"] == {"cache": "no-store"}
    assert first["img"] == "blob:0" and first["imgHidden"] is False
    assert first["textHidden"] is True and first["captionHidden"] is False and first["viewHidden"] is False
    assert result["caption"] is True
    assert result["during"] == {"requests": 1, "imgHidden": False}
    assert result["at4999"] == {"imgHidden": False}
    at5000 = result["at5000"]
    assert at5000["imgHidden"] is True and at5000["src"] is None
    assert at5000["text"] == "Waiting for trigger" and at5000["textHidden"] is False and at5000["captionHidden"] is True
    assert at5000["revoked"] == ["blob:0"]      # the object URL is let go when the picture goes
    assert at5000["requests"] == 1
    assert result["later"] == {"requests": 1, "imgHidden": True}
    assert result["next"] == {"requests": 2, "imgHidden": False, "src": "blob:1"}


def test_waiting_before_any_trigger_and_the_counters_row():
    result = _run_view(r"""
    renderTriggerView(status([row(null, {counters: {triggers: 12, pictures: 11, results: 10, missing: 1, late: 4, extra: 5}})]));
    console.log(JSON.stringify({text: els.trigViewText.textContent, textHidden: els.trigViewText.hidden, imgHidden: els.trigViewImg.hidden,
        captionHidden: els.trigViewCaption.hidden, viewHidden: els.trigView.hidden, requests: requests.length,
        countersHidden: els.trigCounters.hidden, counters: els.trigCounters.innerHTML}));
    """)
    assert result["text"] == "Waiting for trigger" and result["textHidden"] is False
    assert result["imgHidden"] is True and result["captionHidden"] is True and result["viewHidden"] is False
    assert result["requests"] == 0 and result["countersHidden"] is False
    html = result["counters"]
    for label, value in (("Triggers", 12), ("Pictures", 11), ("Results", 10), ("Missing", 1)):
        assert f"<span>{label}</span><b>{value}</b>" in html
    assert "Late" not in html and "Extra" not in html   # a System trigger camera has neither


def test_late_and_extra_counters_show_for_camera_trigger_with_a_notice():
    result = _run_view(r"""
    renderTriggerView(status([row(null, {picture_source: "camera", notice_trigger: 2, counters: {triggers: 3, pictures: 3, results: 3, missing: 0, late: 4, extra: 5}})]));
    console.log(JSON.stringify({counters: els.trigCounters.innerHTML}));
    """)
    assert "<span>Late</span><b>4</b>" in result["counters"] and "<span>Extra</span><b>5</b>" in result["counters"]


def test_no_picture_capture_shows_the_panel_text_and_fetches_nothing():
    result = _run_view(r"""
    const reject = cap(9, {has_picture: false, class_name: "no picture", result: "reject", reason: "no_picture"});
    renderTriggerView(status([row(reject)]));
    await tick();
    const shown = {text: els.trigViewText.textContent, textHidden: els.trigViewText.hidden, imgHidden: els.trigViewImg.hidden,
        caption: els.trigViewCaption.textContent, captionHidden: els.trigViewCaption.hidden, requests: requests.length};
    now += 5000;
    renderTriggerView(status([row(reject)]));
    console.log(JSON.stringify({shown, after: els.trigViewText.textContent}));
    """)
    shown = result["shown"]
    assert shown["text"] == "No picture: Reject" and shown["textHidden"] is False
    assert shown["imgHidden"] is True and shown["requests"] == 0 and shown["captionHidden"] is False
    assert shown["caption"].startswith("no picture · Reject (no picture) · trigger 3 · ")
    assert result["after"] == "Waiting for trigger"


def test_stopped_line_says_so_and_counters_stay():
    result = _run_view(r"""
    renderTriggerView(status([row(cap(7))], false));
    await tick();
    cam = {camera_id: "cam-1", connected: false, role: "vision"};   // a stopped line has released its camera
    renderTriggerView(status([row(cap(7))], false));
    await tick();
    console.log(JSON.stringify({text: els.trigViewText.textContent, textHidden: els.trigViewText.hidden, viewHidden: els.trigView.hidden,
        imgHidden: els.trigViewImg.hidden, captionHidden: els.trigViewCaption.hidden, requests: requests.length, countersHidden: els.trigCounters.hidden}));
    """)
    assert result == {"text": "The line is stopped", "textHidden": False, "viewHidden": False, "imgHidden": True,
                      "captionHidden": True, "requests": 0, "countersHidden": False}


def test_camera_not_connected_keeps_the_video_boxs_own_note():
    # The page's status line writes that note only when the line view is read (every 5 s), and the 1 s poll
    # does not fire pl:detail: the view puts the note in itself when it gives way to it.
    result = _run_view(r"""
    cam = {camera_id: "cam-1", connected: false, role: "vision", name: "Fruit"};
    renderTriggerView(status([row(cap(7))]));
    await tick();
    const named = els.noVideoNote.textContent;
    cam = {camera_id: "cam-1", connected: false, role: "vision"};     // the server names a camera only while it is connected
    renderTriggerView(status([row(cap(7))]));
    console.log(JSON.stringify({viewHidden: els.trigView.hidden, requests: requests.length, countersHidden: els.trigCounters.hidden,
        named, unnamed: els.noVideoNote.textContent, overlay: els.noVideoOverlay.active}));
    """)
    assert result == {"viewHidden": True, "requests": 0, "countersHidden": False,
                      "named": "Fruit is not connected.", "unnamed": "Camera cam-1 is not connected.", "overlay": True}


def test_a_camera_that_is_not_in_trigger_mode_gets_no_view():
    result = _run_view(r"""
    lastId = 7;
    renderTriggerView(status([row(cap(7))]));
    await tick();
    const before = els.trigView.hidden;
    renderTriggerView(status([]));      // the card went back to continuous counting
    console.log(JSON.stringify({before, viewHidden: els.trigView.hidden, countersHidden: els.trigCounters.hidden,
        imgHidden: els.trigViewImg.hidden, revoked: revoked.length}));
    """)
    assert result == {"before": False, "viewHidden": True, "countersHidden": True, "imgHidden": True, "revoked": 1}


def test_a_live_stream_that_is_open_for_a_trigger_camera_is_closed():
    result = _run_view(r"""
    streamOpen = true;
    renderTriggerView(status([row(null)]));
    const afterTrigger = {paused, streamOpen};
    streamOpen = true;
    renderTriggerView(status([]));      // not a trigger camera: its stream is left alone
    console.log(JSON.stringify({afterTrigger, afterOther: {paused, streamOpen}}));
    """)
    assert result["afterTrigger"] == {"paused": 1, "streamOpen": False}
    assert result["afterOther"] == {"paused": 1, "streamOpen": True}


def test_a_picture_that_is_not_the_one_asked_for_is_dropped():
    result = _run_view(r"""
    lastId = 8;                      // a newer picture was taken between the status and the fetch
    renderTriggerView(status([row(cap(7))]));
    await tick();
    const stale = {img: els.trigViewImg.src ?? null, imgHidden: els.trigViewImg.hidden, made: made.length};
    lastId = 8;
    renderTriggerView(status([row(cap(8))]));   // the next refresh sees it and fetches it
    await tick();
    console.log(JSON.stringify({stale, next: {img: els.trigViewImg.src ?? null, requests: requests.length}}));
    """)
    assert result["stale"] == {"img": None, "imgHidden": True, "made": 0}
    assert result["next"] == {"img": "blob:0", "requests": 2}


def test_a_picture_that_comes_after_the_view_moved_on_is_dropped():
    result = _run_view(r"""
    let release;
    const slow = new Promise((r) => { release = r; });
    reply = () => slow;
    lastId = 7;
    renderTriggerView(status([row(cap(7))]));              // the fetch for 7 waits
    now += 6000;
    renderTriggerView(status([row(cap(7))]));              // back to waiting
    release({ok: true, status: 200, headers: {get: () => "7"}, blob: async () => "late"});
    await tick(); await tick();
    console.log(JSON.stringify({made: made.length, imgHidden: els.trigViewImg.hidden, src: els.trigViewImg.src ?? null}));
    """)
    assert result == {"made": 0, "imgHidden": True, "src": None}


def test_a_missing_or_failed_picture_leaves_the_caption_without_an_error():
    result = _run_view(r"""
    reply = () => ({ok: false, status: 404, headers: {get: () => null}, blob: async () => { throw new Error("no body"); }});
    renderTriggerView(status([row(cap(7))]));
    await tick();
    const notFound = {imgHidden: els.trigViewImg.hidden, captionHidden: els.trigViewCaption.hidden, requests: requests.length};
    reply = () => { throw new Error("network down"); };
    renderTriggerView(status([row(cap(8))]));
    await tick();
    console.log(JSON.stringify({notFound, failed: {imgHidden: els.trigViewImg.hidden, captionHidden: els.trigViewCaption.hidden}}));
    """)
    assert result["notFound"] == {"imgHidden": True, "captionHidden": False, "requests": 1}
    assert result["failed"] == {"imgHidden": True, "captionHidden": False}


def test_switching_to_another_camera_starts_the_view_over():
    result = _run_view(r"""
    lastId = 7;
    renderTriggerView(status([row(cap(7)), row(null, {camera_id: "cam-2"})]));
    await tick();
    cam = {camera_id: "cam-2", connected: true, role: "vision"};
    renderTriggerView(status([row(cap(7)), row(null, {camera_id: "cam-2"})]));
    const other = {text: els.trigViewText.textContent, imgHidden: els.trigViewImg.hidden, revoked: revoked.length, requests: requests.length};
    // Back to the first camera: its last capture is new to the view again and shows once more.
    cam = {camera_id: "cam-1", connected: true, role: "vision"};
    renderTriggerView(status([row(cap(7)), row(null, {camera_id: "cam-2"})]));
    await tick();
    console.log(JSON.stringify({other, back: {imgHidden: els.trigViewImg.hidden, requests: requests.length}}));
    """)
    assert result["other"] == {"text": "Waiting for trigger", "imgHidden": True, "revoked": 1, "requests": 1}
    assert result["back"] == {"imgHidden": False, "requests": 2}


# ── The main feed ─────────────────────────────────────────────────────────────


def _wrapper(name: str, script: str) -> str:
    found = re.search(rf"\n    window\.{name} = (?:async )?function \(.*?\n    \}};\n", script, re.S)
    assert found, f"window.{name} is not wrapped in the script"
    return found.group(0)


def test_live_stream_is_not_opened_for_a_trigger_camera():
    code = r"""
    const window = {};
    let activeCamId = null, opened = 0, paused = 0;
    const originalReloadStream = function () { opened++; };
    const dashboardShown = () => true;
    let main = null;
    const lineFeedCamera = () => main;
    const pauseMainFeed = () => { paused++; };
    const byId = () => ({classList: {add() {}}});
    const triggerIds = new Set(["trig"]);
    const isTriggerCamera = (id) => triggerIds.has(id);
    const state = {detail: {status: {}}};
    %s
    const out = {};
    main = {camera_id: "trig", connected: true};
    window.reloadStream();
    out.trigger = {opened, paused, activeCamId};
    main = {camera_id: "plain", connected: true};
    window.reloadStream();
    out.plain = {opened, paused, activeCamId};
    // The page has just loaded: the line's status is not read yet, so nobody knows which camera is shown
    // (checkActiveStream opens the feed once it is read).
    state.detail = null;
    main = null;
    window.reloadStream();
    out.unknown = {opened, paused};
    console.log(JSON.stringify(out));
    """ % _wrapper("reloadStream", _script())
    out = _node(code)
    assert out["trigger"] == {"opened": 0, "paused": 1, "activeCamId": "trig"}
    assert out["plain"] == {"opened": 1, "paused": 1, "activeCamId": "plain"}
    assert out["unknown"] == {"opened": 1, "paused": 1}


def test_the_periodic_check_does_not_reopen_the_stream_of_a_trigger_camera():
    code = r"""
    const window = {};
    let activeCamId = "trig", reloads = 0;
    window.checkActiveStream = async () => {};
    window.reloadStream = () => { reloads++; };
    const loadDetail = async () => ({});
    const main = {camera_id: "trig", connected: true};
    const lineFeedCamera = () => main;
    const renderLineExtras = () => {};
    const renderTriggerView = () => {};
    const isTab = () => true;
    const dashboardShown = () => true;
    const isLiveFeedRunning = () => false;
    const pauseMainFeed = () => {};
    const byId = () => ({classList: {add() {}}});
    const triggerIds = new Set(["trig"]);
    const isTriggerCamera = (id) => triggerIds.has(id);
    const state = {detail: {status: {}}};
    const originalCheckActiveStream = window.checkActiveStream;
    %s
    (async () => {
        const out = {};
        await window.checkActiveStream(true);       // the 5 s check: same camera, no stream running
        out.trigger = reloads;
        triggerIds.clear();
        await window.checkActiveStream(true);       // an ordinary camera whose stream ended is reopened
        out.plain = reloads;
        console.log(JSON.stringify(out));
    })();
    """ % _wrapper("checkActiveStream", _script())
    out = _node(code)
    assert out == {"trigger": 0, "plain": 1}


def test_the_refresh_polls_the_status_each_second_for_a_trigger_camera_only():
    source = re.search(r"\n    async function refreshLineExtras\(\) \{.*?\n    \}\n", _script(), re.S).group(0)
    # loadDetail is not defined here: a refresh that asked for the full line view would fail with a ReferenceError.
    code = r"""
    let shownTab = true, polls = 0, drawn = [], feedCam = {camera_id: "trig"};
    const dashboardShown = () => shownTab;
    const state = {detail: {status: {state: "running", n: 0}}, lineId: "line-1"};
    const ensureStripFeeds = () => {};
    const lineFeedCamera = () => feedCam;
    const triggerIds = new Set(["trig"]);
    const isTriggerCamera = (id) => triggerIds.has(id);
    const refreshTriggerStatus = async () => { polls++; state.detail.status = {state: "running", n: polls}; };
    const renderTriggerView = (status) => drawn.push(status.n);
    const byId = () => null;
    const renderCartonView = () => {};
    const api = async () => ({});
    %s
    (async () => {
        const out = {};
        await refreshLineExtras();
        out.trigger = {polls, drawn: drawn.slice()};
        feedCam = {camera_id: "plain"};
        await refreshLineExtras();
        out.plain = {polls, drawn: drawn.slice()};
        shownTab = false;                 // not on the Line Dashboard: nothing is read
        feedCam = {camera_id: "trig"};
        await refreshLineExtras();
        out.hidden = {polls, drawn: drawn.slice()};
        console.log(JSON.stringify(out));
    })();
    """ % source
    out = _node(code)
    # The view is drawn from the status just read; a camera that is not in trigger mode reuses the one it has.
    assert out["trigger"] == {"polls": 1, "drawn": [1]}
    assert out["plain"] == {"polls": 1, "drawn": [1, 1]}
    assert out["hidden"] == {"polls": 1, "drawn": [1, 1]}


def _status_code(scenario: str) -> str:
    """refreshTriggerStatus and loadDetail cut out of the script, on stubs whose replies the scenario holds back."""
    script = _script()
    poll = re.search(r"\n    async function refreshTriggerStatus\(\) \{.*?\n    \}\n", script, re.S).group(0)
    detail = re.search(r"\n    async function loadDetail\(\) \{.*?\n    \}\n", script, re.S).group(0)
    return r"""
    const events = [], paths = [], waiting = [];
    class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
    const window = {dispatchEvent: (e) => events.push(e.type)};
    const byId = () => null;
    const warningsHtml = () => "";
    const state = {lineId: "line-1", trigger: null,
        detail: {id: "line-1", name: "Line", enabled: true, status: {n: 0, state: "running", trigger_cameras: []}}};
    let statusIssued = 0, statusApplied = 0;
    // Each request waits until the scenario answers it, in the order it chooses.
    const api = (path) => new Promise((resolve, reject) => { paths.push(path); waiting.push({path, resolve, reject}); });
    const next = (path) => waiting.find((w) => w.path === path && !w.done && (w.done = true));
    const answer = (path, value) => next(path).resolve(value);
    const fail = (path, error) => next(path).reject(error);
    const trig = {loading: false};
    const line = (n, extra) => Object.assign({id: "line-1", name: "Line", enabled: true, status: {n, state: "running"}}, extra || {});
    const overview = (n, extra) => ({lines: [{id: "other", enabled: true, n: -1}, Object.assign({id: "line-1", enabled: true, state: "running", n}, extra || {})]});
    %s
    %s
    (async () => {
        const out = {};
    %s
        console.log(JSON.stringify(out));
    })();
    """ % (poll, detail, scenario)


def test_the_one_second_poll_reads_the_overview_and_replaces_only_the_status():
    out = _node(_status_code(r"""
        const detail = state.detail;
        const pending = refreshTriggerStatus();
        out.paths = paths.slice();
        answer("/api/v1/lines/overview", overview(5, {enabled: false, state: "stopped"}));
        await pending;
        out.status = state.detail.status;
        out.enabled = state.detail.enabled;
        out.keptTheRest = state.detail === detail && state.detail.name === "Line" && state.detail.id === "line-1";
        out.events = events.slice();
        out.loading = trig.loading;
    """))
    assert out["paths"] == ["/api/v1/lines/overview"]       # not /lines/{id}
    assert out["status"]["n"] == 5 and out["status"]["state"] == "stopped" and out["enabled"] is False
    assert out["keptTheRest"] is True
    assert out["events"] == []                              # no pl:detail from the 1 s poll
    assert out["loading"] is False


def test_a_poll_is_not_started_while_the_last_one_is_on_its_way():
    out = _node(_status_code(r"""
        const first = refreshTriggerStatus();
        const second = refreshTriggerStatus();
        out.paths = paths.slice();
        answer("/api/v1/lines/overview", overview(1));
        await Promise.all([first, second]);
        // Once it has landed the next second's poll is asked for.
        const third = refreshTriggerStatus();
        out.afterwards = paths.length;
        answer("/api/v1/lines/overview", overview(2));
        await third;
    """))
    assert out["paths"] == ["/api/v1/lines/overview"]
    assert out["afterwards"] == 2


def test_an_older_poll_landing_late_does_not_replay_an_older_capture():
    out = _node(_status_code(r"""
        // The poll was asked for first, the full line view after it; the line view lands first.
        const poll = refreshTriggerStatus();
        const full = loadDetail();
        answer("/api/v1/lines/line-1", line(2));
        await full;
        answer("/api/v1/lines/overview", overview(1));
        await poll;
        out.afterOldPoll = state.detail.status.n;
        // And the other way round: the line view was asked for first, a poll after it; the poll lands first.
        const olderFull = loadDetail();
        const newerPoll = refreshTriggerStatus();
        answer("/api/v1/lines/overview", overview(4));
        await newerPoll;
        answer("/api/v1/lines/line-1", line(3, {name: "Renamed"}));
        await olderFull;
        out.afterOldFull = {n: state.detail.status.n, name: state.detail.name};
        out.events = events.slice();
    """))
    assert out["afterOldPoll"] == 2                          # the older poll was not applied
    # The older full view keeps the newer status, and still brings what only it carries (a changed name).
    assert out["afterOldFull"] == {"n": 4, "name": "Renamed"}
    assert out["events"] == ["pl:detail", "pl:detail"]       # only the full views fire it


def test_a_poll_for_another_line_or_a_failed_poll_changes_nothing():
    out = _node(_status_code(r"""
        const pending = refreshTriggerStatus();
        state.lineId = "line-2";                                 // the page moved to another line meanwhile
        answer("/api/v1/lines/overview", overview(9));
        await pending;
        out.moved = state.detail.status.n;
        state.lineId = "line-1";
        const failing = refreshTriggerStatus();
        fail("/api/v1/lines/overview", new Error("network down"));
        await failing;
        out.failed = {n: state.detail.status.n, loading: trig.loading};
        const missing = refreshTriggerStatus();                  // the line is not in the overview
        answer("/api/v1/lines/overview", {lines: [{id: "other"}]});
        await missing;
        out.missing = state.detail.status.n;
    """))
    assert out["moved"] == 0
    assert out["failed"] == {"n": 0, "loading": False}
    assert out["missing"] == 0


# ── Markup ────────────────────────────────────────────────────────────────────


def test_markup():
    page = PAGE.read_text(encoding="utf-8")
    script = _script()
    css = CSS.read_text(encoding="utf-8")
    # The panel is inside the video box, hidden until the script shows it; the counters sit under the box.
    box = page[page.index('id="videoBox"'):page.index("<!-- What the line rejected and passed -->")]
    panel = box[box.index('id="trigView"'):]
    assert re.search(r'<div[^>]*class="trig-view"[^>]*id="trigView"[^>]*hidden|<div[^>]*id="trigView"[^>]*hidden', box)
    assert "Waiting for trigger" in panel
    for needle in ('id="trigViewImg"', 'id="trigViewText"', 'id="trigViewCaption"'):
        assert needle in panel
    assert box.index('id="trigCounters"') > box.index('id="noVideoOverlay"')
    assert re.search(r'<div[^>]*id="trigCounters"[^>]*hidden', box)
    # Nothing new needs a help icon.
    assert "data-help" not in box[box.index('id="trigView"'):]
    # The words of the view.
    assert "No picture: Reject" in script and "The line is stopped" in script and "Waiting for trigger" in script
    # The caption: class, Good / Reject (reason), trigger N, time, joined with a middle dot.
    caption = script[script.index("function triggerCaption"):]
    caption = caption[:caption.index("\n}\n")]
    assert '"Good"' in caption and "Reject" in caption and "`trigger ${" in caption and "toLocaleTimeString" in caption
    assert '" \\u00b7 "' in caption or '" · "' in caption
    # The 1 s refresh draws the view; a trigger camera's status is read each time.
    refresh = script[script.index("    async function refreshLineExtras"):]
    refresh = refresh[:refresh.index("\n    }\n")]
    assert "renderTriggerView(" in refresh and "refreshTriggerStatus()" in refresh and "loadDetail" not in refresh
    # The view's own style hides it with the hidden attribute and lies over the video box's note.
    assert re.search(r"\.trig-view\s*\{[^}]*position:\s*absolute", css) and ".trig-view[hidden]" in css
    # The script still loads in a browser as before (the closure runs only with a window).
    assert re.search(r"if \(typeof window === \"undefined\"[^\n]*\) return;", script)
    assert "module.exports" in script[:script.index("(function () {")]

