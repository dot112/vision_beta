"""The dashboard keeps the signed-in user per browser tab.

The token was kept in sessionStorage (one per tab) but the user's name, level and role in
localStorage, which every tab of the browser shares: with an Admin on one tab and a Level 2 user
on another, the last sign-in decided what both tabs showed. The page's Auth section and the level
helpers of the other scripts are cut out and run with node, each tab with its own sessionStorage
and one localStorage for the browser.
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
USER_KEYS = ("access_token", "username", "clearance_level", "role", "full_name")
ADMIN = {"username": "admin", "full_name": "Admin", "role": "admin", "clearance_level": 3}
SUPERVISOR = {"username": "sup", "full_name": "Sam Super", "role": "supervisor", "clearance_level": 2}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cut(text: str, pattern: str, what: str) -> str:
    found = re.search(pattern, text, re.S)
    assert found, f"{what} is not in the code"
    return found.group(0)


def _pieces() -> dict:
    page = _text(PAGE)
    return {
        "flutter": _cut(page, r"\nfunction notifyFlutterAuthState\(.*?\n\}\n", "notifyFlutterAuthState"),
        # Sign-in, sign-out, resuming a tab's sign-in, the user bar and the level helpers.
        "auth": _cut(page, r"\n/\* ─── Auth ─+ \*/\n.*?(?=\n/\* ─── Tabs Navigation)", "the Auth section"),
        "lines": _cut(_text(ROOT / "assets" / "production_lines.js"), r"\n    const level = [^\n]*\n", "level() of production_lines.js"),
        "lock": _cut(_text(ROOT / "assets" / "config_lock.js"), r"\n    function level\(\) \{.*?\n    \}\n", "level() of config_lock.js"),
    }


HARNESS = r"""
const vm = require("vm");
const PIECES = __PIECES__;
function store() {
    const items = new Map();
    return { getItem: (k) => (items.has(k) ? items.get(k) : null), setItem: (k, v) => { items.set(k, String(v)); },
             removeItem: (k) => { items.delete(k); }, dump: () => Object.fromEntries(items) };
}
const shared = store();   // localStorage: one for the whole browser
const run = (tab, code) => vm.runInContext(code, tab);
const settle = () => new Promise((resolve) => setImmediate(resolve));

function openTab() {
    const els = {};
    const button = { disabled: false, title: "", removeAttribute(name) { this[name] = ""; } };   // a .restricted-l2 control
    const tab = {
        els, button, answers: [], seen: [], console, window: {}, setTimeout: () => 0,
        sessionStorage: store(), localStorage: shared,
        document: {
            getElementById: (id) => (els[id] = els[id] || { id, value: "", innerText: "", className: "", style: {} }),
            querySelectorAll: () => [button],
        },
    };
    // Each request answers with the next queued body.
    tab.fetch = async () => { const body = tab.answers.shift(); return { ok: true, status: 200, json: async () => body }; };
    vm.createContext(tab);
    run(tab, `
        let currentAuthToken = "";
        const showModal = () => {};
        const stopLiveFeedStream = () => {};
        function initDashboard() { seen.push(clearanceLevel()); }   // the level the pages are drawn with
        ${PIECES.flutter}
        ${PIECES.auth}
        const linesLevel = (() => { ${PIECES.lines} return level; })();
        const lockLevel = (() => { const state = { level: null }; ${PIECES.lock} return level; })();
    `);
    return tab;
}

async function signIn(tab, user) {
    tab.els.loginUser = { value: user.username };
    tab.els.loginPass = { value: "pw" };
    tab.answers.push({ access_token: `token-${user.username}`, ...user });
    await run(tab, "handleLogin()");
}

function view(tab) {
    run(tab, "updateUserUI()");
    return {
        name: tab.els.barUsername.innerText, level: tab.els.barClearance.innerText,
        usersPage: tab.els.adminMenuTab.style.display !== "none", restricted: tab.button.disabled,
        page: run(tab, "clearanceLevel()"), configure: run(tab, "canConfigure()"),
        lines: run(tab, "linesLevel()"), lock: run(tab, "lockLevel()"),
    };
}

(async () => {
    const out = {};
    __SCENARIO__
    console.log(JSON.stringify(out));
})().catch((err) => { console.error(String((err && err.stack) || err)); process.exit(1); });
"""


def _node(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    code = HARNESS.replace("__PIECES__", json.dumps(_pieces())).replace("__SCENARIO__", scenario)
    code = code.replace("__ADMIN__", json.dumps(ADMIN)).replace("__SUPERVISOR__", json.dumps(SUPERVISOR))
    run = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr[:1500]
    return json.loads(run.stdout)


def _shows(view: dict, user: dict) -> bool:
    level = user["clearance_level"]
    return (view["name"] == user["full_name"] and view["level"].startswith(f"Level {level} ")
            and view["usersPage"] == (level == 3) and view["restricted"] == (level < 2)
            and view["page"] == view["lines"] == view["lock"] == level and view["configure"] == (level >= 2))


def test_two_tabs_signed_in_to_two_accounts_each_show_their_own():
    out = _node("""
        const admin = openTab(), supervisor = openTab();
        await signIn(admin, __ADMIN__);
        await signIn(supervisor, __SUPERVISOR__);
        out.admin = view(admin);
        out.supervisor = view(supervisor);
        await run(supervisor, "handleLogout()");
        out.adminAfter = view(admin);
        out.shared = shared.dump();
    """)
    assert _shows(out["admin"], ADMIN), out["admin"]
    assert _shows(out["supervisor"], SUPERVISOR), out["supervisor"]
    # Signing out on one tab leaves the other signed in as it was.
    assert _shows(out["adminAfter"], ADMIN), out["adminAfter"]
    assert not set(USER_KEYS) & set(out["shared"]), out["shared"]


def test_a_tab_signed_in_by_the_older_page_asks_the_server_who_it_is():
    # The older page left the user in the shared storage, here another account's.
    out = _node("""
        Object.entries(__ADMIN__).forEach(([k, v]) => shared.setItem(k, v));
        const tab = openTab();
        tab.sessionStorage.setItem("access_token", "token-sup");
        tab.answers.push(__SUPERVISOR__);   // the answer to /auth/me
        run(tab, "resumeSession()");
        await settle();
        out.view = view(tab);
        out.seen = tab.seen;
        out.shared = shared.dump();
    """)
    assert _shows(out["view"], SUPERVISOR), out["view"]
    # The pages were drawn once, after the server answered, at this tab's level.
    assert out["seen"] == [2]
    assert not set(USER_KEYS) & set(out["shared"]), out["shared"]


def test_a_tab_that_knows_its_user_shows_the_pages_at_once():
    out = _node("""
        const tab = openTab();
        await signIn(tab, __ADMIN__);
        tab.seen.length = 0;
        tab.answers.push(__ADMIN__);   // the answer to /auth/me, after a reload
        run(tab, "resumeSession()");
        out.before = tab.seen.slice();
        await settle();
        out.after = tab.seen;
        out.view = view(tab);
    """)
    assert out["before"] == [3] and out["after"] == [3]
    assert _shows(out["view"], ADMIN), out["view"]


def test_no_script_reads_the_user_from_shared_storage():
    reads = []
    for path in [PAGE, *sorted((ROOT / "assets").glob("*.js"))]:
        for found in re.finditer(r"localStorage\.getItem\(\s*[\"'](\w+)[\"']", _text(path)):
            if found.group(1) in USER_KEYS:
                reads.append((path.name, found.group(1)))
    assert not reads, reads


def test_the_browser_wide_choices_stay_shared():
    assert 'localStorage.getItem("theme")' in _text(PAGE)
    lines = _text(ROOT / "assets" / "production_lines.js")
    assert 'const STORAGE_KEY = "selected_line_id"' in lines and "localStorage.getItem(STORAGE_KEY)" in lines
    assert "localStorage.getItem(SEND_VIEW_KEY)" in _text(PAGE) and "localStorage.getItem(PLC_VIEW_KEY)" in _text(PAGE)
