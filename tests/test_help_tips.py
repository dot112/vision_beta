"""Help icons: every button, drop-down and sidebar menu item of the dashboard says what it does.

A control carries one plain sentence in data-help="..."; assets/help_tips.js puts a small circled "i"
after it (inside it for menu items and segmented buttons) and shows the sentence on hover, keyboard
focus or a tap. The markup and the template strings are read here; the helper itself runs in node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "assets" / "help_tips.js"
FILES = ("dashboard.html", "assets/production_lines.js", "assets/plc_signals.js", "assets/camera_settings.js",
         "assets/card_values.js", "assets/product_fields.js", "assets/data_backups.js", "assets/text_reader.js",
         "assets/pattern_check.js")

# Only controls whose purpose is not plain from their label have a help icon: technical settings, the
# choices that decide when a card fires, and the buttons whose effect the label does not say. Each is
# named here by the start of its help sentence; a control not on this list has no help icon.
KEEP = (
    # The text reader: its format notation and joining dot-matrix dots.
    "Each character of the text: 9 a digit", "Joins the dots of inkjet dot-matrix print",
    # The pattern check: the mask brush and how sensitive the defect search is.
    "Paint over the parts the check must ignore", "How different from the good samples a pixel must be",
    # Connections: TCP, MQTT, network cameras, PLCs.
    "Whether this server connects to the device", "The characters that end each message",
    "The MQTT version the broker speaks", "The CA certificate that checks", "The certificate this server shows a broker",
    "The private key that belongs to the client certificate", "How sure the delivery of each message is",
    "The delivery level of the birth message", "The delivery level of the close message",
    "The delivery level of the last-will message", "An IP camera with a stream address", "How the RTSP stream travels",
    "The stream's own picture size", "Opens a search for OPC UA servers", "Whether the addresses you type start at 0",
    "Which half comes first in a 32-bit number", "A serial port of this computer, or a serial gateway",
    "The Siemens PLC family", "Whether messages to the OPC UA server are signed",
    "The client certificate of a secured OPC UA connection", "The server's certificate, to accept only that server",
    # Line dashboard.
    "Sets the batch number",
    # Apply: the cards only work after it.
    "Saves the send cards of this line and puts them to work", "Saves the output actions of this line and puts them to work",
    "Saves the inputs of this line and starts reading them",
    # Users and API keys.
    "What the new user may do", "The account the key belongs to",
    # Send card and output action dialogs: when a card fires and what it does.
    "The event that sends this message", "Which results send the message", "Which camera's events send this message",
    "JSON with the fields you tick", "Checks the formula", "What happens when there is no value",
    "The event that fires this action", "Which results fire the action", "Which camera's events fire this action",
    "The kind of PLC memory written", "What is done at the address", "Where the written value comes from",
    "Fire once for each tracked product", "An input that tells the action really happened",
    # Camera cards on Line setup.
    "Whether this camera counts and rejects on its own", "What a product gets when this camera sees nothing of it",
    "What a triggered picture counts as when none", "The direction the products move", "Count crossings in the flow direction only",
    "Takes a still picture to draw the count lines on", "Keeps this camera showing codes only",
    "Off, or the outfeed this camera watches",
    "Who takes the picture for each trigger", "The Take a picture input that tells the server a picture is coming",
    # Product sequence and result handshake.
    "The input read for each product's sequence number", "The Take a picture input that starts each handshake",
    "The input the PLC acknowledges each result with", "What happens when a result is not ready in time",
    # Data and backups: what confirming the clock does, and the free-space floor.
    "Lets old data be deleted by age again", "Below this much free space the oldest data is deleted",
    # Products and records.
    "Sets the fields of the list shown", "Sets how the codes of this list are built", "How a code is built",
    "Shows the products, the code reads, or both",
    # Inputs, industrial camera settings, Only when rules.
    "Reads this address once now", "What the line does with this input", "How the value at the address is read",
    "Which change of the value makes the input act", "How the camera takes pictures", "Which edge of the trigger signal takes a picture",
    "Takes one test picture by software trigger now", "How the IP stream travels", "Whether all of the rules below must pass",
)

# Buttons that show only an icon: without a help icon they keep a plain tooltip.
ICON_ONLY = ("sendViewCards", "sendViewList", "plcViewCards", "plcViewList", "plcSignalViewCards", "plcSignalViewList")

# Controls whose title= is set by code as the state changes (it stays beside the help icon).
TITLE_SET_BY_CODE = ('id="lineAlarmPill"',)

TAG = re.compile(r"<(button|select|span)(?=[\s>])")
HELP = re.compile(r'\sdata-help="([^"]*)"')


def _opening_tags(text: str):
    """The opening tag of every <button>, <select> and <span> in markup or a template string (not in comments)."""
    text = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    for match in TAG.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if re.match(r"\s*(//|\*|/\*)", text[line_start:match.start()]):
            continue
        index, depth = match.end(), 0          # the first ">" outside a ${...} ends the tag
        while index < len(text):
            if text.startswith("${", index):
                depth += 1
                index += 2
                continue
            char = text[index]
            if depth and char == "{":
                depth += 1
            elif depth and char == "}":
                depth -= 1
            elif not depth and char == ">":
                break
            index += 1
        yield text[match.start():index + 1], text.count("\n", 0, match.start()) + 1


def _outside_expressions(tag: str) -> str:
    """The tag without its ${...} expressions (a title set there depends on the state)."""
    out, depth, index = [], 0, 0
    while index < len(tag):
        if tag.startswith("${", index):
            depth += 1
            index += 2
            continue
        char = tag[index]
        if depth and char == "{":
            depth += 1
        elif depth and char == "}":
            depth -= 1
        elif not depth:
            out.append(char)
        index += 1
    return "".join(out)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _help_texts():
    for name in FILES:
        for tag, line in _opening_tags(_read(name)):
            found = HELP.search(tag)
            if found:
                yield f"{name}:{line}", found.group(1)


def test_only_controls_whose_purpose_is_not_plain_have_help():
    texts = list(_help_texts())
    extra = [f"{where}: {text}" for where, text in texts if not text.startswith(KEEP)]
    assert not extra, "a help icon on a control with a plain label:\n" + "\n".join(extra)
    gone = [piece for piece in KEEP if not any(text.startswith(piece) for _, text in texts)]
    assert not gone, f"these controls lost their help: {gone}"


def test_plain_places_have_no_help_icons():
    page = _read("dashboard.html")
    script = _read("assets/production_lines.js")
    # The sidebar's pages, the camera cards' section heads, the record time ranges and the line's own buttons.
    assert not any(HELP.search(item) for item in re.findall(r'<li class="menu-item[^"]*"[^>]*>', page))
    assert "dataset.help" not in script and "MENU_HELP" not in script and "FOLD_HELP" not in script
    assert 'data-help="${help}"' not in script
    for button in ("plSaveSetup", "plStartLine", "plStopLine", "plResetCounts", "plListNew", "plListRename", "plProdAdd"):
        tag = re.search(rf'<button[^>]*id="{button}"[^>]*>', script).group(0)
        assert not HELP.search(tag), button
    for text in ("Save", "Add", "Delete", "Cancel", "Edit", "Settings", "Test", "Refresh", "Export CSV", "Sign out"):
        for name in FILES:
            for match in re.finditer(rf"<button([^>]*)>\s*{re.escape(text)}\s*</button>", _read(name)):
                assert not HELP.search(match.group(0)), f"{name}: {text}"


def test_icon_only_buttons_keep_a_plain_tooltip():
    page = _read("dashboard.html")
    for button in ICON_ONLY:
        tag = re.search(rf'<button[^>]*id="{button}"[^>]*>', page).group(0)
        assert re.search(r'\stitle="Show as (cards|a list)"', tag), button


def test_each_help_text_is_one_plain_sentence():
    for where, text in _help_texts():
        assert text and text[0].isupper() and text.endswith("."), f"{where}: {text!r}"
        assert len(text) <= 150, f"{where}: {len(text)} characters"
        assert ". " not in text[:-1] and "  " not in text, f"{where}: one sentence: {text!r}"
        assert not re.search(r"[<>&`\\]|https?:", text), f"{where}: {text!r}"


@pytest.mark.parametrize("name", FILES)
def test_a_control_with_help_has_no_tooltip_of_its_own(name):
    """No double tooltips: the help icon replaces a fixed title=, a title the code changes stays."""
    for tag, line in _opening_tags(_read(name)):
        if not HELP.search(tag) or any(piece in tag for piece in TITLE_SET_BY_CODE):
            continue
        assert not re.search(r'\stitle="', _outside_expressions(tag)), f"{name}:{line}: {tag[:120]}"


def test_the_page_loads_the_help_icons(client):
    page = client.get("/dashboard").text
    assert '<script src="/assets/help_tips.js?v=3.4.0"></script>' in page
    script = client.get("/assets/help_tips.js")
    assert script.status_code == 200 and "root.HelpTips = api" in script.text and "MutationObserver" in script.text
    css = client.get("/assets/dashboard.css").text
    for rule in (".help-tip {", ".help-tip-bubble {", "[hidden] + .help-tip"):
        assert rule in css, rule


# ── The helper, run with node on a small stand-in for the page ────────────────────────────────

FAKE_DOM = r"""
class El {
    constructor(tag, attrs) { this.tagName = tag.toUpperCase(); this.attrs = Object.assign({}, attrs || {}); this.children = []; this.parentElement = null; this.ownerDocument = doc; }
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
    setAttribute(n, v) { this.attrs[n] = String(v); }
    hasAttribute(n) { return this.getAttribute(n) !== null; }
    get className() { return this.attrs.class || ""; }
    set className(v) { this.attrs.class = v; }
    get classList() {
        const el = this;
        return { contains: (c) => el.className.split(/\s+/).includes(c),
                 add: (c) => { if (!el.classList.contains(c)) el.className = (el.className + " " + c).trim(); } };
    }
    get nextElementSibling() { const p = this.parentElement; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
    remove() { const p = this.parentElement; if (p) { p.children.splice(p.children.indexOf(this), 1); this.parentElement = null; } }
    appendChild(c) { c.remove(); c.parentElement = this; this.children.push(c); return c; }
    insertAdjacentElement(where, n) {
        n.remove();
        if (where === "beforeend") return this.appendChild(n);
        const p = this.parentElement; n.parentElement = p; p.children.splice(p.children.indexOf(this) + 1, 0, n); return n;
    }
    querySelectorAll(sel) {
        if (sel !== "[data-help]") throw new Error("unexpected selector " + sel);
        const out = []; const walk = (e) => e.children.forEach((c) => { if (c.hasAttribute("data-help")) out.push(c); walk(c); });
        walk(this); return out;
    }
}
const doc = { createElement: (tag) => new El(tag) };
const el = (tag, attrs, ...kids) => { const e = new El(tag, attrs); kids.forEach((k) => e.appendChild(k)); return e; };
const icons = (e) => { let n = e.classList.contains("help-tip") ? 1 : 0; e.children.forEach((c) => { n += icons(c); }); return n; };
const H = require(HELPER_PATH);
"""


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = FAKE_DOM.replace("HELPER_PATH", json.dumps(str(HELPER))) + code
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr[:800]
    return json.loads(result.stdout)


def test_one_icon_per_control_however_often_it_runs():
    out = _node("""
        const save = el("button", {"data-help": "Saves the line."});
        const menu = el("li", {class: "menu-item", "data-help": "Opens the line."}, el("span", {}));
        const seg = el("div", {class: "seg"}, el("button", {"data-help": "Shows the video."}));
        const plain = el("button", {});
        const body = el("body", {}, el("div", {}, save, plain), el("ul", {}, menu), seg);
        const first = H.addIcons(body);
        const again = H.addIcons(body) + H.addIcons(body) + H.addIcons(save) + H.addIcons(menu);
        console.log(JSON.stringify({first, again, total: icons(body),
            afterSave: save.nextElementSibling.className, label: save.nextElementSibling.getAttribute("aria-label"),
            role: save.nextElementSibling.getAttribute("role"), tab: save.nextElementSibling.getAttribute("tabindex"),
            inMenu: menu.children[menu.children.length - 1].className, menuTab: menu.children[1].getAttribute("tabindex"),
            inSeg: seg.children[0].children.length, segTab: seg.children[0].children[0].getAttribute("tabindex"),
            plainNext: plain.nextElementSibling}));
    """)
    assert out["first"] == 3 and out["again"] == 0 and out["total"] == 3
    assert out["afterSave"] == "help-tip" and out["label"] == "Saves the line." and out["role"] == "img"
    assert out["tab"] == "0"
    # Inside a menu item and a segmented button: a menu item cannot take focus, so its icon can;
    # a button can, and its own focus shows the text.
    assert out["inMenu"] == "help-tip" and out["menuTab"] == "0"
    assert out["inSeg"] == 1 and out["segTab"] is None
    assert out["plainNext"] is None


def test_a_dropdown_icon_follows_its_on_screen_box_whichever_comes_first():
    out = _node("""
        // The page's custom drop-down box comes right after the <select>, before or after the icon is added.
        const early = el("select", {"data-help": "The line shown."});
        const late = el("select", {"data-help": "The camera shown."});
        const box1 = el("div", {class: "form-group"}, early, el("div", {class: "custom-select-wrap"}));
        const box2 = el("div", {class: "form-group"}, late);
        const body = el("body", {}, box1, box2);
        H.addIcons(body);
        late.insertAdjacentElement("afterend", el("div", {class: "custom-select-wrap"}));
        const added = H.addIcons(body);
        console.log(JSON.stringify({added, order1: box1.children.map((c) => c.className), order2: box2.children.map((c) => c.className)}));
    """)
    assert out["added"] == 0
    assert out["order1"] == ["", "custom-select-wrap has-help", "help-tip"]
    assert out["order2"] == ["", "custom-select-wrap has-help", "help-tip"]


def test_an_icon_keeps_up_with_its_control():
    out = _node("""
        const button = el("button", {"data-help": "Starts the line."});
        const body = el("body", {}, el("div", {}, button));
        H.addIcons(body);
        button.setAttribute("data-help", "Stops the line.");
        H.addIcons(body);
        const label = button.nextElementSibling.getAttribute("aria-label");
        // Code that rewrites a menu item's contents drops its icon: the next pass puts one back.
        const item = el("li", {class: "menu-item", "data-help": "Opens the records."}, el("span", {}));
        body.appendChild(el("ul", {}, item));
        H.addIcons(body);
        item.children.splice(0).forEach((c) => { c.parentElement = null; });
        item.appendChild(el("span", {}));
        const back = H.addIcons(body);
        // A control moved away from its icon takes the icon along instead of getting a second one.
        const other = el("div", {});
        body.appendChild(other);
        other.appendChild(button);
        const moved = H.addIcons(body);
        // An empty text gives no icon.
        const empty = el("button", {"data-help": "  "});
        body.appendChild(empty);
        console.log(JSON.stringify({label, back, inItem: icons(item), moved, total: icons(body),
            follows: button.nextElementSibling && button.nextElementSibling.className, empty: H.addIcons(empty)}));
    """)
    assert out["label"] == "Stops the line."
    assert out["back"] == 1 and out["inItem"] == 1
    assert out["moved"] == 0 and out["follows"] == "help-tip" and out["total"] == 2
    assert out["empty"] == 0


def test_the_bubble_stays_inside_the_window():
    out = _node("""
        const phone = {width: 375, height: 812};
        const size = {width: 260, height: 60};
        console.log(JSON.stringify([
            H.placeBubble({left: 150, right: 164, top: 100, bottom: 114, width: 14, height: 14}, size, phone),
            H.placeBubble({left: 355, right: 369, top: 100, bottom: 114, width: 14, height: 14}, size, phone),
            H.placeBubble({left: 2, right: 16, top: 100, bottom: 114, width: 14, height: 14}, size, phone),
            H.placeBubble({left: 150, right: 164, top: 780, bottom: 794, width: 14, height: 14}, size, phone),
        ]));
    """)
    middle, right, left, low = out
    # Centred under the icon where it fits, below it, a few pixels away.
    assert middle == {"left": 157 - 130, "top": 114 + 6, "above": False}
    # Near an edge it is pushed back inside the window (8 px from the edge) instead of running off it.
    for placed in out:
        assert 8 <= placed["left"] <= 375 - 8 - 260
    assert right["left"] == 375 - 8 - 260 and left["left"] == 8
    # Without room below it goes above the icon.
    assert low["above"] and low["top"] == 780 - 6 - 60
