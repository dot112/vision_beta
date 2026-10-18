"""The Line setup form's markup: its grid of line settings closes every box it opens.

An unclosed box there pulls the handshake section, the warnings and the Save / Start / Reset
buttons into the grid, where each squeezes into one narrow column.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_line_settings_grid_closes_every_box_it_opens():
    script = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    start = script.index('<div class="pl-form-grid">', script.index("Line setup"))
    assert "Line name</label>" in script[start:start + 400]
    end = script.index("${handshakeSetupHtml(d)}", start)
    grid = script[start:end]
    opened, closed = len(re.findall(r"<div[\s>]", grid)), grid.count("</div>")
    assert opened == closed, f"the grid opens {opened} <div> and closes {closed}"
    # The handshake, the warnings and the buttons come after the grid, not in it.
    after = script[end:script.index('<div class="pl-cam-grid"', end)]
    assert 'id="plSetupWarnings"' in after and 'class="pl-setup-actions"' in after
