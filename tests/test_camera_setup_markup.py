"""Camera setup screens: the Line setup camera card's Connect switch and the Add camera card.

The card script is read as text (it needs a page to run); the switch's rule is cut out of it and run with
node on stand-ins.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "assets" / "production_lines.js"
ADD_NOTE = "Add USB cameras on the Cameras page, and network and industrial cameras under Communication."


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


# ── The Connect switch ────────────────────────────────────────────────────────

def test_the_card_has_one_connect_switch_and_no_buttons():
    script = _script()
    assert 'data-act="keep-connected"' in script
    assert 'data-act="connect"' not in script and 'data-act="disconnect"' not in script
    tag = script[script.rindex("<label", 0, script.index('data-act="keep-connected"')):]
    tag = tag[:tag.index("</label>")]
    assert 'class="switch"' in tag and 'type="checkbox"' in tag
    assert "data-help" not in tag                          # its purpose is plain: no help icon


def test_the_switch_follows_the_saved_choice_the_line_and_the_connection():
    script = _script()
    out = _node(_function(script, "connectionView") + """
        const run = (row, connected, running, canEdit) => connectionView(row, connected, running, canEdit);
        console.log(JSON.stringify({
            off: run({ kept_connected: false, connection_state: "disconnected" }, false, false, true),
            kept: run({ kept_connected: true, connection_state: "connected" }, true, false, true),
            retrying: run({ kept_connected: true, connection_state: "reconnecting" }, false, false, true),
            running: run({ kept_connected: false, connection_state: "connected" }, true, true, true),
            viewer: run({ kept_connected: true, connection_state: "connected" }, true, false, false),
            noRow: run(null, false, false, true),
        }));
    """)
    assert out["off"] == {"checked": False, "disabled": False, "text": "Not connected"}
    assert out["kept"] == {"checked": True, "disabled": False, "text": "Connected"}
    assert out["retrying"] == {"checked": True, "disabled": False, "text": "Connecting (retrying)"}
    # A running line keeps its cameras connected: on and greyed out, whatever was saved.
    assert out["running"] == {"checked": True, "disabled": True, "text": "Connected · The running line keeps it connected"}
    assert out["viewer"]["disabled"] is True and out["viewer"]["checked"] is True
    assert out["noRow"] == {"checked": False, "disabled": False, "text": "Not connected"}


def test_the_switch_posts_connect_or_disconnect_and_shows_a_refusal():
    body = _function(_script(), "setKeptConnected")
    assert '/api/v1/cameras/${encodeURIComponent(id)}/${on ? "connect" : "disconnect"}' in body
    assert '"warning"' in body                               # a 409's detail is shown as a warning
    assert "retrying" in body                                # "Connecting (retrying)" is said when it did not connect


# ── The Add camera card ───────────────────────────────────────────────────────

def test_the_add_camera_card_has_no_scans_and_no_network_form():
    script = _script()
    for act in ("scan", "scan-industrial", "network", "net-add", "industrial-add"):
        assert f'data-act="{act}"' not in script, act
        assert f'act === "{act}"' not in script, act
    assert "pl-add-industrial" not in script and "pl-add-network" not in script
    assert "/api/v1/cameras/discover/" not in script


def test_the_add_camera_card_says_where_cameras_are_added():
    card = _function(_script(), "addCardHtml")
    assert ADD_NOTE in card
    assert 'data-f="add-device"' in card and 'data-f="add-job"' in card and 'data-act="add"' in card
    assert "scan" not in card.lower()
