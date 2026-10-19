"""The camera card on Line setup: Picture taken by, Picture delay, Trigger notice and Wait for the picture.

The card's script is read as text (it needs a page to run); the pieces that decide something are cut out
of it and run with node on small stand-ins, and the camera form's Trigger lock runs on a stand-in of its own.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import line_config

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "assets" / "production_lines.js"
FORM = ROOT / "assets" / "camera_settings.js"


def _script() -> str:
    return CARDS.read_text(encoding="utf-8")


def _function(script: str, name: str) -> str:
    """The source of one function of the card script (an IIFE: its functions are indented four spaces)."""
    found = re.search(rf"\n    (?:async )?function {name}\(.*?\n    \}}\n", script, re.S)
    assert found, f"function {name} is not in the card script"
    return found.group(0)


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _tag(script: str, field: str, name: str = "select") -> str:
    """The opening tag of the control with data-f="field" (to its first ">" outside a ${...})."""
    start = script.index(f'data-f="{field}"')
    start = script.rindex(f"<{name}", 0, start)
    return script[start:script.index(">", script.index(f'data-f="{field}"', start))]


# ── Picture taken by ──────────────────────────────────────────────────────────


def test_picture_taken_by_offers_system_trigger_and_camera_trigger():
    script = _script()
    start = script.index('data-f="picture_source"')
    select = script[start:script.index("</select>", start)]
    assert "System trigger" in select and "Camera trigger" in select
    assert re.search(r'value="system"[^>]*>System trigger</option>', select)
    assert re.search(r'value="camera"[^>]*>Camera trigger</option>', select)
    for value in line_config.PICTURE_SOURCES:
        assert f'"{value}"' in script, value
    assert "<label class=\"form-label\">Picture taken by</label>" in script


def test_only_an_industrial_camera_can_trigger_itself():
    script = _script()
    assert "Only an industrial camera can trigger itself" in script
    sync = _function(script, "syncCard")
    # The text shows, and the Camera trigger choice is greyed out, on a camera that is not "gige".
    assert 'row.type === "gige"' in sync
    assert "pl-picture-industrial" in sync and re.search(r"\.disabled\s*=\s*!industrial", sync)
    # A card that holds Camera trigger for a camera that cannot do it goes back to System trigger.
    assert re.search(r'!industrial && source === "camera"', sync)


def test_the_picture_fields_follow_the_source():
    sync = _function(_script(), "syncCard")
    # System trigger: the trigger ticks and the Picture delay. Camera trigger: the notice, and the wait with a notice.
    assert 'querySelector(".pl-picture-system").hidden = source !== "system"' in sync
    assert 'querySelector(".pl-picture-camera").hidden = source !== "camera"' in sync
    assert re.search(r'querySelector\(".pl-picture-wait"\)\.hidden = !', sync)
    # The ticks are only for System trigger: the notice select replaces them for a Camera trigger.
    assert re.search(r'renderTriggers\(card, triggerMode && isVisionJob\(job\) && source === "system"', sync)


def test_the_trigger_notice_lists_the_take_a_picture_inputs_and_none():
    script = _script()
    assert _tag(script, "notice_trigger").startswith("<select")
    options = _function(script, "noticeOptions")
    assert 'value=""' in options and ">None</option>" in options
    assert "PlcSignals.triggers()" in options
    # A trigger number no input has stays chosen (the server warns), as with the ticks.
    assert "no input has this number" in options


def test_the_delay_and_the_wait_have_the_servers_limits():
    script = _script()
    low, high = line_config.PICTURE_DELAY_LIMITS_MS
    delay = _tag(script, "picture_delay", "input")
    assert f'min="{low}" max="{high}"' in delay and 'type="number"' in delay
    low, high = line_config.PICTURE_WAIT_LIMITS_MS
    wait = _tag(script, "picture_wait", "input")
    assert f'min="{low}" max="{high}"' in wait and 'type="number"' in wait
    assert f"DEFAULT_PICTURE_WAIT_MS = {line_config.DEFAULT_PICTURE_WAIT_MS};" in script
    assert "Picture delay (ms)" in script and "Wait for the picture (ms)" in script


def test_the_notes_say_what_the_choices_mean():
    script = _script()
    assert "Shorter than the time between two products." in script
    assert ("A missed or double trigger cannot be seen. Use a trigger notice when a PLC or robot waits for each answer."
            in script)
    # The count-mode note is chosen by the picture source (and the notice), not by the camera's Trigger.
    notes = script[script.index("const COUNT_NOTES"):]
    notes = notes[:notes.index("};\n", notes.index("trigger:")) + 3]
    assert re.search(r"trigger: \{\s*system:", notes) and "camera:" in notes and "notice:" in notes
    assert "A missed or double trigger cannot be seen." in notes


# ── What the card saves ───────────────────────────────────────────────────────


def test_the_card_entry_sends_the_four_keys():
    entry = _function(_script(), "cardEntry")
    trigger = entry[entry.index('entry.count_mode === "trigger"'):]
    for key in ("picture_source", "picture_delay_ms", "notice_trigger", "picture_wait_ms"):
        assert key in trigger, key
    assert re.search(r"notice_trigger: camera && [^\n]*: null", trigger)    # a number for Camera trigger, else null
    # Only a trigger card sends them: a tracking card leaves them out and the server keeps its saved values.
    assert entry.index("picture_source") > entry.index('entry.count_mode === "trigger"')


def test_the_reasons_name_no_picture():
    script = _script()
    reasons = script[script.index("const REJECT_REASON = {"):]
    reasons = reasons[:reasons.index("};")]
    assert f'{line_config.REASON_NO_PICTURE}: "no picture"' in reasons
    assert line_config.REJECT_REASON_CODES[line_config.REASON_NO_PICTURE] == 10


# ── Count mode One picture per trigger: When nothing is found is Reject ────────


def test_switching_to_one_picture_per_trigger_sets_nothing_found_to_reject():
    script = _script()
    assert re.search(r'if \(name === "count_mode"\) onCountModeChange\(card, t\.value\);', script)
    out = _node(f"""
        const loaded = [];
        const setTimeout = (fn) => fn();
        const loadLinesPicture = (card) => loaded.push(card.id);
        const refreshed = [];
        const noProduct = {{ value: "ignore", _refreshCustomSelect: () => refreshed.push(noProduct.value) }};
        const cardField = (card, name) => (name === "no_product" ? noProduct : null);
        {_function(script, "onCountModeChange")}
        const card = {{ id: "k1" }};
        onCountModeChange(card, "trigger");
        const afterTrigger = noProduct.value;
        noProduct.value = "good";
        onCountModeChange(card, "track");        // back to tracking: the user's choice stays
        console.log(JSON.stringify({{ afterTrigger, afterTrack: noProduct.value, loaded, refreshed }}));
    """)
    assert out["afterTrigger"] == "reject"
    assert out["refreshed"] == ["reject"]      # the custom select shows Reject, not the old label
    assert out["afterTrack"] == "good" and out["loaded"] == ["k1"]    # the count lines are drawn on a picture again


# ── Ruling 15: the Trigger follows the card's own form ────────────────────────


def test_the_card_sets_the_cameras_trigger_from_its_own_count_mode_and_source():
    script = _script()
    out = _node(f"""
        {_function(script, "triggerSetByCard")}
        const t = (industrial, vision, countMode, source) => triggerSetByCard({{ industrial, vision, countMode, source }});
        console.log(JSON.stringify({{
            camera: t(true, true, "trigger", "camera"), system: t(true, true, "trigger", "system"),
            tracking: t(true, true, "track", "camera"), notIndustrial: t(false, true, "trigger", "system"),
            codeReader: t(true, false, "trigger", "system"),
        }}));
    """)
    assert out == {"camera": "hardware", "system": "software", "tracking": None, "notIndustrial": None, "codeReader": None}
    sync = _function(script, "syncCard")
    # The form's Trigger is locked to what the card sets (or freed) before the card reads it back.
    assert re.search(r"CameraSettingsForm\.lockTrigger\(card, triggerSetByCard\(", sync)
    assert sync.index("lockTrigger(") < sync.index("cameraTrigger(card)")
    # On the card the note says where it is set; the dialog-wide text says "Line setup".
    assert "Set by Picture taken by above" in script


def test_the_follow_up_camera_patch_never_sends_a_trigger_mode_the_card_sets():
    script = _script()
    save = _function(script, "saveLineSetup")
    assert "cardCameraSettings(card, row)" in save and "CameraSettingsForm.read(card, row.type)" not in save
    assert "cardCameraSettings(card, row)" in _function(script, "renderImageForms")      # the same on both sides of the compare
    out = _node(f"""
        let shown = {{}};
        const CameraSettingsForm = {{ read: () => ({{ ...shown }}) }};
        {_function(script, "cardCameraSettings")}
        const row = {{ type: "gige", settings: {{ trigger_mode: "software" }} }};
        const run = (settings, saved) => {{ shown = settings; return cardCameraSettings({{}}, saved || row); }};
        console.log(JSON.stringify({{
            locked: run({{ exposure_us: 100 }}),                                   // read-only: not in the form's settings
            same: run({{ trigger_mode: "software", exposure_us: 100 }}),           // the camera's own mode, untouched
            changed: run({{ trigger_mode: "hardware", exposure_us: 100 }}),        // edited by hand
            unset: run({{ trigger_mode: "off" }}, {{ type: "gige", settings: {{}} }}),
            usb: run({{ brightness: 3 }}, {{ type: "usb" }}),
        }}));
    """)
    assert out["locked"] == {"exposure_us": 100} and out["same"] == {"exposure_us": 100}
    assert out["changed"] == {"trigger_mode": "hardware", "exposure_us": 100}
    assert out["unset"] == {} and out["usb"] == {"brightness": 3}


LOCK = """
global.window = {};
global.document = { hidden: false };
require(process.argv[2]);
const form = window.CameraSettingsForm;
const element = () => ({ hidden: false, textContent: "" });
function fakeRoot(mode) {
    const classes = new Set();
    const select = {
        value: mode, disabled: false, innerHTML: "", dataset: { free: mode },
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
                     toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
    };
    const parts = { locked: element(), note: element(), hardware: element(), triggered: element() };
    const root = { querySelector: (q) => ({
        '[data-cs="trigger_mode"]': select, ".cs-trigger-locked": parts.locked, ".cs-trigger-note": parts.note,
        ".cs-hardware-only": parts.hardware, ".cs-triggered-only": parts.triggered })[q] || null };
    return { root, select, parts, classes };
}
const options = (select) => (select.innerHTML.match(/<option/g) || []).length;
const NOTE = "Set by Picture taken by above";
const r = fakeRoot("off");
const log = {};
const snap = (name) => { log[name] = {
    value: r.select.value, disabled: r.select.disabled, options: options(r.select), locked: r.classes.has("cs-locked"),
    note: r.parts.locked.textContent, noteHidden: r.parts.locked.hidden, hardwareRow: r.parts.hardware.hidden,
    triggeredRow: r.parts.triggered.hidden, read: form.read(r.root, "gige").trigger_mode ?? null,
}; };
snap("free");
form.lockTrigger(r.root, "software", NOTE); snap("software");
form.lockTrigger(r.root, "hardware", NOTE); snap("hardware");
form.lockTrigger(r.root, null); snap("freed");
r.select.value = "hardware";                       // chosen by hand while free
form.lockTrigger(r.root, "software", NOTE);
form.lockTrigger(r.root, null); snap("afterEdit");
form.lockTrigger(fakeRoot("off").root, "hardware", NOTE);   // a form without the Trigger select is left alone
form.lockTrigger({ querySelector: () => null }, "hardware", NOTE);
console.log(JSON.stringify(log));
"""


def test_the_camera_form_locks_and_frees_its_trigger_as_the_card_says(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "lock.js"
    script.write_text(LOCK, encoding="utf-8")
    run = subprocess.run([node, str(script), str(FORM)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr[:800]
    out = json.loads(run.stdout)
    note = "Set by Picture taken by above"

    assert out["free"]["disabled"] is False and out["free"]["read"] == "off"
    for mode, hardware_row in (("software", True), ("hardware", False)):
        shown = out[mode]
        assert shown["disabled"] is True and shown["locked"] is True       # read-only, greyed out
        assert shown["value"] == mode and shown["options"] == 1            # the mode the card sets, nothing else to choose
        assert shown["note"] == note and shown["noteHidden"] is False      # the card's own wording
        assert shown["read"] is None                                        # not sent: the server sets it on the line save
        assert shown["hardwareRow"] is hardware_row and shown["triggeredRow"] is False
    freed = out["freed"]
    assert freed["disabled"] is False and freed["locked"] is False and freed["options"] == 3
    assert freed["value"] == "off" and freed["read"] == "off"              # the camera's own mode is back
    assert freed["noteHidden"] is True and freed["note"] == ""
    assert out["afterEdit"]["value"] == "hardware"                          # what was chosen by hand is kept across a lock
    assert out["afterEdit"]["hardwareRow"] is False


def test_the_camera_form_still_renders_the_servers_lock_with_a_free_mode_behind_it():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = _node(f"""
        global.window = {{}};
        global.document = {{ hidden: false }};
        require({json.dumps(str(FORM))});
        const box = {{ innerHTML: "", querySelector: () => null, addEventListener() {{}} }};
        window.CameraSettingsForm.render(box, {{ id: "c1", type: "gige", settings: {{ trigger_mode: "software" }}, line_trigger_mode: "hardware" }}, ["image"]);
        console.log(JSON.stringify({{ html: box.innerHTML }}));
    """)
    html = out["html"]
    select = html[html.index('data-cs="trigger_mode"'):html.index("</select>", html.index('data-cs="trigger_mode"'))]
    assert "disabled" in select and select.count("<option") == 1 and 'value="hardware" selected' in select
    assert 'data-free="software"' in select                                 # what the camera has when the card lets go
