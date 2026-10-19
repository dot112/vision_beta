"""Add opens the card's settings first: the card is placed only on OK.

Output action cards, inputs and send cards: Add builds a draft with the defaults and opens its settings
dialog; OK adds the card, Cancel adds nothing. The dashboard's functions are cut out of the page and run
with node on stand-ins; the inputs' script is checked as text. The Cameras page rows have no Connect.
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
SIGNALS = ROOT / "assets" / "plc_signals.js"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _top_function(source: str, name: str) -> str:
    """A function of the page's main script (declared at the start of a line)."""
    found = re.search(rf"\n(?:async )?function {name}\(.*?\n\}}\n", source, re.S)
    assert found, f"function {name} is not in the page"
    return found.group(0)


def _signals_function(name: str) -> str:
    """A function of the inputs' script (an IIFE: its functions are indented four spaces)."""
    found = re.search(rf"\n    (?:async )?function {name}\(.*?\n    \}}\n", SIGNALS.read_text(encoding="utf-8"), re.S)
    assert found, f"function {name} is not in plc_signals.js"
    return found.group(0)


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr[:800]
    return json.loads(run.stdout)


def _no_push_or_render(body: str) -> None:
    assert ".push(" not in body
    assert not re.search(r"\brender\w*\(", body), "Add must not draw the card before OK"


# ── The Add functions only open the dialog ────────────────────────────────────

def test_add_builds_a_draft_and_places_nothing():
    page = _page()
    _no_push_or_render(_top_function(page, "addSendActionCard"))
    _no_push_or_render(_top_function(page, "addPlcActionCard"))
    add = _signals_function("add")
    _no_push_or_render(add)
    assert "MAX_SIGNALS" in add.split("const card")[0]           # the limit is checked before the dialog opens


def test_each_save_pushes_only_a_new_draft_and_each_close_forgets_it():
    page = _page()
    for save, flag in (("saveSendActionFromModal", "_sendActionDraftIsNew"),
                       ("savePlcActionFromModal", "_plcActionDraftIsNew")):
        body = _top_function(page, save)
        assert body.count(".push(") == 1 and re.search(rf"if \({flag}\)[^\n]*\n?[^\n]*\.push\(", body), save
    for close, flag in (("closeSendActionSettingsModal", "_sendActionDraftIsNew"),
                        ("closePlcActionSettingsModal", "_plcActionDraftIsNew")):
        assert f"{flag} = false" in _top_function(page, close), close
    save = _signals_function("saveDialog")
    assert save.count(".push(") == 1 and "state.draftIsNew" in save
    assert "state.draftIsNew = false" in _signals_function("closeDialog")


# ── Send cards, run ───────────────────────────────────────────────────────────

SEND_STUBS = """
var _sendActionCards = [], _sendActionEditingDraft = null, _sendActionDraftIsNew = false;
var marked = 0, rendered = 0, modalOpen = false;
const modal = { classList: { add: () => { modalOpen = true; }, remove: () => { modalOpen = false; } } };
const document = { getElementById: (id) => (id === "sendActionSettingsModal" ? modal : null) };
const sendChannels = () => [{ id: "ep-1" }];
const markSendActionsChanged = () => { marked += 1; };
const renderSendActionCards = () => { rendered += 1; };
const loadSendMessageFields = () => {};
const refreshSendModalBody = () => {};
const sendCardIsMqtt = () => false;
const sendTopicProblem = () => "";
const sendTemplateProblem = () => "";
const updateSendTopicHint = () => {};
const updateSendPreview = () => {};
"""


def test_a_new_send_card_is_placed_on_ok_and_cancel_places_nothing():
    page = _page()
    code = SEND_STUBS + "".join(_top_function(page, n) for n in (
        "addSendActionCard", "openSendActionSettingsModal", "closeSendActionSettingsModal", "saveSendActionFromModal",
    )) + """
        const snap = () => ({ cards: _sendActionCards.length, marked, open: modalOpen, draft: Boolean(_sendActionEditingDraft) });
        const out = {};
        addSendActionCard(); out.added = snap();
        closeSendActionSettingsModal(); out.cancelled = snap();
        addSendActionCard(); _sendActionEditingDraft.name = "Robot";
        saveSendActionFromModal(); out.ok = snap(); out.name = _sendActionCards[0].name;
        openSendActionSettingsModal(_sendActionCards[0].id); _sendActionEditingDraft.name = "Robot 2";
        saveSendActionFromModal(); out.edited = snap(); out.editedName = _sendActionCards[0].name;
        openSendActionSettingsModal(_sendActionCards[0].id); closeSendActionSettingsModal();
        saveSendActionFromModal(); out.afterClose = snap();
        console.log(JSON.stringify(out));
    """
    out = _node(code)
    assert out["added"] == {"cards": 0, "marked": 0, "open": True, "draft": True}
    assert out["cancelled"] == {"cards": 0, "marked": 0, "open": False, "draft": False}
    assert out["ok"]["cards"] == 1 and out["ok"]["marked"] == 1 and out["ok"]["open"] is False
    assert out["name"] == "Robot"
    assert out["edited"]["cards"] == 1 and out["editedName"] == "Robot 2"     # editing an existing card: unchanged
    assert out["afterClose"]["cards"] == 1


# ── Output cards, run ─────────────────────────────────────────────────────────

PLC_STUBS = """
var _plcActionCards = [], _activeEditingPlcCardId = null, _plcActionEditingDraft = null, _plcActionDraftIsNew = false;
var saved = 0, rendered = 0, modalOpen = false;
const modal = { classList: { add: () => { modalOpen = true; }, remove: () => { modalOpen = false; }, contains: () => modalOpen } };
const document = { getElementById: (id) => (id === "plcActionSettingsModal" ? modal : null) };
const allCommsEndpoints = [{ id: "plc-1", protocol: "plc" }];
const getProtocolDefForEndpoint = () => ({ types: [{ id: "coil", defaultAddr: "Q0.0" }] });
const savePlcActionCards = () => { saved += 1; };
const renderPlcActionCards = () => { rendered += 1; };
const loadCardProductLists = () => Promise.resolve();
const refreshPlcModalBody = () => {};
"""


def test_a_new_output_card_is_placed_and_saved_on_ok_and_cancel_places_nothing():
    page = _page()
    code = PLC_STUBS + "".join(_top_function(page, n) for n in (
        "getEditablePlcActionCard", "addPlcActionCard", "openPlcActionSettingsModal",
        "closePlcActionSettingsModal", "savePlcActionFromModal",
    )) + """
        const snap = () => ({ cards: _plcActionCards.length, saved, open: modalOpen });
        const out = {};
        addPlcActionCard(); out.added = snap();
        out.editable = getEditablePlcActionCard(_activeEditingPlcCardId) === _plcActionEditingDraft;
        closePlcActionSettingsModal(); out.cancelled = snap();
        addPlcActionCard(); _plcActionEditingDraft.target_address = "Q0.5";
        savePlcActionFromModal(); out.ok = snap(); out.address = _plcActionCards[0].target_address;
        console.log(JSON.stringify(out));
    """
    out = _node(code)
    assert out["added"] == {"cards": 0, "saved": 0, "open": True}
    assert out["editable"] is True                   # the dialog's fields edit the new draft
    assert out["cancelled"] == {"cards": 0, "saved": 0, "open": False}
    assert out["ok"] == {"cards": 1, "saved": 1, "open": False} and out["address"] == "Q0.5"


# ── Cameras page ──────────────────────────────────────────────────────────────

def test_the_cameras_page_rows_have_no_connect():
    page = _page()
    start = page.index("function addCameraRowActions()")
    rows = page[start:page.index("</script>", start)]
    assert 'data-act="connect"' not in rows and 'data-act="disconnect"' not in rows
    assert '"connect" : "disconnect"' not in rows and 'data-act="settings"' in rows and 'data-act="delete"' in rows
    for gone in ("function connectCam(", "function disconnectCam(", "camBtnConnect", "camBtnDisconnect"):
        assert gone not in page, gone
