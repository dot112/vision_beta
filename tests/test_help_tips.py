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
         "assets/card_values.js", "assets/product_fields.js")

# Controls without a help icon, each named by a piece of its opening tag (id, class, data-act or handler).
EXEMPT = {
    "dashboard.html": (
        # Dialog footers and corners: Cancel, OK, Save, Upload, Close and the ✕ buttons.
        'id="modalBtnCancel"', 'id="modalBtnConfirm"',
        'onclick="closeCamSettings()"', 'onclick="saveCamSettings()"',
        'onclick="closeCommModal()"', 'id="commSaveBtn"',
        'onclick="closeOpcuaScanModal()"',
        'onclick="closeSendActionSettingsModal()"', 'onclick="saveSendActionFromModal()"',
        'onclick="closePlcActionSettingsModal()"', 'onclick="savePlcActionFromModal()"',
        'onclick="PlcSignals.closeDialog()"', 'onclick="PlcSignals.saveDialog()"',
        'onclick="closeModelUploadModal()"', 'onclick="submitModelUpload()"',
        'onclick="closeModelDeleteModal()"', 'onclick="confirmModelDelete()"',
        'onclick="hideNewApiKey()"',
        # The sign-in form, the menu's show / hide arrows and the full-screen corner of the video:
        # icons with their own label, and an icon beside them would sit in the wrong place.
        'id="toggleLoginPassBtn"', 'class="btn-login"', 'class="btn-sidebar-collapse"', 'class="btn-hamburger"',
        'id="videoFloatBtn"',
        # The ✕ that deletes a card.
        'onclick="deleteSendActionCard(', 'onclick="deletePlcActionCard(',
        # Buttons repeated on every row of a list or table.
        'onclick="selectOpcuaCandidate(', 'onclick="pickGigeCamera(this)"', 'class="send-chip"',
        'selectModelById(this.closest(\'tr\').dataset.id);deleteSelectedModel()',
        'onclick="confirmDeleteUser(', 'class="api-key-prefix-link"', 'onclick="revokeApiKey(',
        'onclick="acknowledgeAlarmAndRefresh(',
        'data-act="${connected ? "disconnect" : "connect"}"', 'class="btn-action btn-sm btn-outline" data-act="settings"',
        'data-act="delete" aria-label="Delete ${escapeHtml(cam.name)}"',
    ),
    "assets/production_lines.js": (
        # A line's row on the Lines page.
        'data-act="logic" data-id=', 'data-act="${running ? "stop" : "start"}"', 'data-act="rename" data-id=',
        'data-act="clone" data-id=', 'class="btn-action btn-red" data-act="delete" data-id=',
        # A camera card's move up / move down / ✕, and the big picture of a camera tile (it has its title).
        'class="pl-icon-btn', 'class="video-box pl-strip-pic"',
        # Rows: industrial cameras found, product checks, products.
        'data-act="industrial-add"', 'pl-check-del', 'class="btn-action btn-outline" data-act="edit"',
        'class="btn-action btn-red" data-act="delete">',
        # The footer of the Products dialogs.
        'pf-cancel', 'pf-ok',
    ),
    "assets/plc_signals.js": ('onclick="PlcSignals.remove(',),
    # The form helper adds data-cs to a <select> written elsewhere: not a control of its own.
    "assets/camera_settings.js": ('<select data-cs="${name}"',),
    # A rule's row in the Only when editor.
    "assets/card_values.js": ("ow-kind", "ow-right", "ow-left", "ow-op", "ow-del"),
    # A field's row in the fields editor.
    "assets/product_fields.js": ("pf-type", "pf-up", "pf-down", "pf-del"),
}

# Controls whose title= is set by code as the state changes (it stays beside the help icon).
TITLE_SET_BY_CODE = ('id="lineAlarmPill"',)

TAG = re.compile(r"<(button|select)(?=[\s>])")
HELP = re.compile(r'\sdata-help="([^"]*)"')


def _opening_tags(text: str):
    """The opening tag of every <button> and <select> in markup or a template string (not in comments)."""
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


@pytest.mark.parametrize("name", FILES)
def test_every_button_and_dropdown_says_what_it_does(name):
    exempt = EXEMPT.get(name, ())
    tags = list(_opening_tags(_read(name)))
    assert tags
    missing = [f"{name}:{line}: {tag[:120]}" for tag, line in tags
               if not HELP.search(tag) and not any(piece in tag for piece in exempt)]
    assert not missing, "\n".join(missing)
    # Every exemption still names a control, and a control is either helped or exempt, not both.
    for piece in exempt:
        named = [tag for tag, _ in tags if piece in tag]
        assert named, f"{name}: no control has {piece!r}"
        assert not any(HELP.search(tag) for tag in named), f"{name}: {piece!r} is exempt but has data-help"


def _help_texts():
    for name in FILES:
        for tag, line in _opening_tags(_read(name)):
            found = HELP.search(tag)
            if found:
                yield f"{name}:{line}", found.group(1)
    page = _read("dashboard.html")
    for item in re.findall(r'<li class="menu-item[^"]*"[^>]*>', page):
        found = HELP.search(item)
        yield "dashboard.html menu", found.group(1) if found else ""
    for map_name in ("MENU_HELP", "FOLD_HELP"):
        for text in re.findall(r'^\s+\w+: "([^"]*)",?\r?$', _js_map(map_name), re.M):
            yield f"production_lines.js {map_name}", text
    ranges = re.search(r"const RECORD_RANGES = \[(.*?)\n\s*\];", _read("assets/production_lines.js"), re.S).group(1)
    texts = re.findall(r'\["\w+", "[^"]+", "([^"]*)"\]', ranges)
    assert len(texts) == len(re.findall(r"\[", ranges))         # each time range has its text
    for text in texts:
        yield "production_lines.js RECORD_RANGES", text


def _js_map(name: str) -> str:
    found = re.search(rf"const {name} = \{{(.*?)\n\s*\}};", _read("assets/production_lines.js"), re.S)
    assert found, name
    return found.group(1)


def test_each_help_text_is_one_plain_sentence():
    texts = list(_help_texts())
    assert len(texts) > 200
    for where, text in texts:
        if "${" in text:                     # chosen by the template from one of the maps checked here
            continue
        assert text and text[0].isupper() and text.endswith("."), f"{where}: {text!r}"
        assert len(text) <= 150, f"{where}: {len(text)} characters"
        assert ". " not in text[:-1] and "  " not in text, f"{where}: one sentence: {text!r}"
        assert not re.search(r"[<>&`\\]|https?:", text), f"{where}: {text!r}"


def test_the_sidebar_menu_items_say_what_they_do():
    page = _read("dashboard.html")
    items = re.findall(r'<li class="menu-item[^"]*"[^>]*>', page)
    assert len(items) == 8
    assert all(HELP.search(item) for item in items), [item for item in items if not HELP.search(item)]
    # The items assets/production_lines.js adds: each tab it passes to menuItem() has a text in MENU_HELP.
    script = _read("assets/production_lines.js")
    tabs = set(re.findall(r'menuItem\("(\w+)"', script))
    assert tabs == {"tabOverview", "tabRecords", "tabLines", "tabProducts"}
    helped = set(re.findall(r"^\s+(\w+): \"", _js_map("MENU_HELP"), re.M))
    assert tabs <= helped, tabs - helped
    body = re.search(r"function menuItem\(tab, label\) \{(.*?)\n    \}", script, re.S).group(1)
    assert "li.dataset.help = MENU_HELP[tab]" in body
    # Each fold of a camera card says what is in it.
    folds = set(re.findall(r'fold\("(\w+)", "', script))
    assert folds and folds <= set(re.findall(r"^\s+(\w+): \"", _js_map("FOLD_HELP"), re.M))


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
