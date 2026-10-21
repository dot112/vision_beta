"""The Line Dashboard status bar (state, batch, model / speed / cameras, buttons) keeps its parts readable.

As one flex row, the state, the batch and the buttons kept their width and only the middle part (model,
camera speed, cameras) shrank: on a square display it became a narrow column of stacked text and the bar
grew tall. The bar is a grid now: one row where there is room, two rows below (the middle part gets the
whole second row), one column on a phone.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    found = re.search(r"(?:^|\n)\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"no rule for {selector}"
    return found.group(1)


def _media(max_width: int) -> str:
    """Every rule inside the @media (max-width: Npx) blocks, joined."""
    blocks = []
    for found in re.finditer(r"@media \(max-width: " + str(max_width) + r"px\) \{", CSS):
        depth, i = 1, found.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(CSS[i], 0)
            i += 1
        blocks.append(CSS[found.end():i - 1])
    assert blocks, f"no @media (max-width: {max_width}px)"
    return "\n".join(blocks)


def _areas(rule: str) -> list[str]:
    found = re.search(r"grid-template-areas:\s*((?:\"[^\"]*\"\s*)+);", rule)
    assert found, rule
    return re.findall(r"\"([^\"]*)\"", found.group(1))


def test_the_bar_is_one_row_where_there_is_room():
    bar = _rule(CSS, ".line-status")
    assert "display: grid" in bar
    assert _areas(bar) == ["state batch meta actions"]
    # The middle part takes what is left and may shrink; the others keep their own width.
    assert re.search(r"grid-template-columns:\s*auto auto minmax\(0, 1fr\) auto", bar)
    for part, area in ((".line-status-state", "state"), (".line-batch", "batch"),
                       (".line-status-meta", "meta"), (".line-status-actions", "actions")):
        assert f"grid-area: {area}" in _rule(CSS, part), part


def test_below_the_wide_layout_the_middle_part_gets_its_own_row():
    bar = _rule(_media(1599), ".line-status")
    assert _areas(bar) == ["state batch actions", "meta meta meta"]
    assert re.search(r"grid-template-columns:\s*auto minmax\(0, 1fr\) auto", bar)


def test_on_a_phone_each_part_has_a_row():
    bar = _rule(_media(640), ".line-status")
    assert _areas(bar) == ["state", "batch", "meta", "actions"]


def test_the_old_flex_rules_are_gone():
    # The 1100 px wrap of the flex row (buttons on a row of their own) is replaced by the grid.
    assert ".line-status {" not in _media(1100) and ".line-status-actions {" not in _media(1100)
