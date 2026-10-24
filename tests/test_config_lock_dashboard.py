"""The configuration lock on the dashboard: the chip, the dialog, the banner, the code panel,
and the script that keeps the locked pages read-only.

The script needs a page to run; its pieces that decide something are cut out and run with node
on small stand-ins (as tests/test_trigger_card_markup.py does).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "dashboard.html"
SCRIPT = ROOT / "assets" / "config_lock.js"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(script: str, name: str) -> str:
    found = re.search(rf"\n    (?:async )?function {name}\(.*?\n    \}}\n", script, re.S)
    assert found, f"function {name} is not in the lock script"
    return found.group(0)


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def test_the_page_has_the_chip_dialog_banner_and_code_panel():
    page = _page()
    for element_id in ("configLockChip", "configLockDialog", "configLockState", "configLockCode", "configLockAction",
                       "configLockError", "configLockBanner", "configLockBannerText", "configLockCodePanel",
                       "lockCodeNew", "lockCodeConfirm", "lockCodeState"):
        assert f'id="{element_id}"' in page, element_id
    assert '<script src="/assets/config_lock.js' in page
    # The page follows the lock: every poll, and any 423 answer.
    assert "ConfigLock.apply(d.config_lock)" in page
    assert "res.status === 423" in page


def test_the_banner_shows_on_the_locked_pages_only():
    css = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")
    rule = re.search(r"body\.config-locked:is\(([^)]*)\) \.config-lock-banner", css)
    assert rule, "no banner rule"
    tabs = set(re.findall(r'data-tab="(\w+)"', rule.group(1)))
    assert tabs == {"tabLines", "tabActionTrigger", "tabCameras", "tabModels", "tabComms", "tabProducts"}


def test_a_disabled_select_does_not_open():
    assert "if (select.disabled && !isOpen) return;" in _page()


def test_the_script_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "--check", str(SCRIPT)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def _tags_with(text: str, needle: str):
    """Every opening tag (or template tag) that contains the needle."""
    tags = []
    start = text.find(needle)
    while start != -1:
        tags.append(text[text.rfind("<", 0, start + 1):text.find(">", start) + 1])
        start = text.find(needle, start + 1)
    assert tags, f"{needle!r} not found"
    return tags


def _marked(tag: str) -> bool:
    return re.search(r"\bdata-lock(?![-\w])", tag) is not None


# Buttons that save, add, remove or apply settings of the locked pages, by file.
SAVE_BUTTONS = {
    "dashboard.html": [
        'id="commBtnDelete"', 'id="commSaveBtn"', 'onclick="addSendActionCard()"', 'onclick="applySendActionCards()"',
        'onclick="addPlcActionCard()"', 'onclick="applyPlcActionCards()"', 'onclick="PlcSignals.add()"',
        'onclick="PlcSignals.apply()"', 'onclick="openModelUploadModal()"', 'onclick="submitModelUpload()"',
        'onclick="confirmModelDelete()"', "selectModelById(this.closest('tr').dataset.id);deleteSelectedModel()",
        'onclick="openAddCommModal(', 'onclick="deleteCommEndpointById(',
        'onclick="deleteSendActionCard(', 'onclick="deletePlcActionCard(', 'aria-label="Delete ${escapeHtml(cam.name)}"',
    ],
    "assets/production_lines.js": [
        'id="plAddLine"', 'data-act="rename"', 'data-act="clone"', 'data-act="delete" data-id', 'id="plSaveSetup"',
        'data-act="up"', 'data-act="down"', 'data-act="remove"', 'data-act="add">Add camera',
        'class="btn-action btn-outline pl-check-add', 'class="btn-action btn-red pl-check-del',
        'class="btn-action btn-outline pl-review-keep"', 'id="plImportLabel"', 'id="plReplaceLabel"', 'id="plListNew"', 'id="plListRename"',
        'id="plListDelete"', 'id="plProdAdd"', 'data-act="delete">Delete</button>', 'class="btn-action btn-blue pf-ok"',
    ],
    "assets/plc_signals.js": ['onclick="PlcSignals.add()"', 'onclick="PlcSignals.remove('],
    "assets/product_fields.js": ['class="btn-action btn-outline pf-up"', 'class="btn-action btn-outline pf-down"',
                                 'class="btn-action btn-red pf-del"', 'class="btn-action btn-outline pf-add"'],
    "assets/camera_settings.js": ['class="btn-action btn-sm btn-outline cs-roi-reset"'],
}

# Buttons that run, test or read: usable while locked.
RUNNING_BUTTONS = {
    "assets/production_lines.js": ['id="plStartLine"', 'id="plStopLine"', 'id="plResetCounts"', 'data-act="${running ? "stop" : "start"}"'],
    "assets/plc_signals.js": ['onclick="PlcSignals.readNow('],
    "assets/camera_settings.js": ['restricted-l2 cs-trigger-btn"'],
    "dashboard.html": ['onclick="testSendActionCard(', 'onclick="testPlcActionCard(', 'onclick="testEndpointById('],
}


def test_save_buttons_are_marked():
    missing = []
    for name, needles in SAVE_BUTTONS.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        missing += [(name, needle, tag[:120]) for needle in needles for tag in _tags_with(text, needle) if not _marked(tag)]
    assert not missing, missing


def test_running_buttons_are_not_marked():
    marked = []
    for name, needles in RUNNING_BUTTONS.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        marked += [(name, needle) for needle in needles for tag in _tags_with(text, needle) if _marked(tag)]
    assert not marked, marked


def test_view_only_fields_stay_free_and_product_dialogs_are_locked():
    script = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    for needle in ('id="plProdList"', 'id="plProdSearch"', 'data-cf="try"'):
        assert all("data-lock-free" in tag for tag in _tags_with(script, needle)), needle
    assert all("data-lock-root" in tag for tag in _tags_with(script, 'class="modal-backdrop" id="${id}"'))


def _locked_answer_run(times_ms):
    script = _script()
    code = f"""
        const LOCKED_MESSAGE = "locked";
        const WARN_EVERY_MS = 3000;
        const state = {{ status: {{ locked: false }}, warnedAt: 0 }};
        const toasts = []; let refreshed = 0; let clock = 0;
        Date.now = () => clock;
        const showToast = (msg, kind) => toasts.push([msg, kind]);
        const apply = (status) => {{ state.status = status; }};
        const refresh = () => {{ refreshed += 1; return Promise.resolve(); }};
        const locked = () => Boolean(state.status && state.status.locked);
        {_function(script, "warnLocked")}
        {_function(script, "onLockedAnswer")}
        const seen = [];
        for (const t of {json.dumps(times_ms)}) {{ clock = t; onLockedAnswer(); seen.push(locked()); }}
        console.log(JSON.stringify({{ toasts: toasts.length, kinds: toasts.map(t => t[1]), refreshed, seen }}));
    """
    return _node(code)


def test_423_turns_the_page_locked():
    result = _locked_answer_run([10_000])
    assert result["seen"] == [True] and result["refreshed"] == 1 and result["kinds"] == ["warning"]


def test_no_lock_toast_storm():
    assert _locked_answer_run([10_000, 11_000, 12_500])["toasts"] == 1
    assert _locked_answer_run([10_000, 14_000])["toasts"] == 2
