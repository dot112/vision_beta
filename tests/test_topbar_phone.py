"""The top bar on a phone: the line picker shrinks (its name cut with "…") instead of running into the CPU ring.

Measured live (owner's report, 2026-10-25): at 320-412 px the picker kept a 140 px minimum and overlapped the
usage rings by 20-77 px on Line dashboard and Line setup."""
from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "assets" / "dashboard.css").read_text(encoding="utf-8")


def _phone_rules() -> str:
    start = CSS.index("@media (max-width: 760px) {")
    end = CSS.index("\n}", start)
    return CSS[start:end]


def test_line_picker_shrinks_on_a_phone():
    phone = _phone_rules()
    holder = re.search(r"body \.pl-line-select \{([^}]*)\}", phone)
    assert holder and "min-width: 0" in holder.group(1) and "flex: 0 1 auto" in holder.group(1)
    field = re.search(r"body \.pl-line-select \.custom-select-wrap, body \.pl-line-select select \{([^}]*)\}", phone)
    assert field and "min-width: 0" in field.group(1)
    assert "min-width: 140px" not in phone
