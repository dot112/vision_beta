"""What the line-level pattern check tests share: a pattern check camera's entry, a reference set file the line
save accepts (in the data folder the test's ``lines`` fixture set), and results made by hand."""
from __future__ import annotations

import json
from pathlib import Path

from app.engines.pattern_check import CheckResult, PatternResult, error_result
from app.engines.pattern_find import Found

SET_ID = "ref-0a1b2c3d"
MATCH = {"reference_id": SET_ID, "checks": [{"name": "Label"}, {"name": "Cap"}]}
BOX = [[100.0, 100.0], [300.0, 100.0], [300.0, 200.0], [100.0, 200.0]]
CAP_BOX = [[250.0, 150.0], [290.0, 150.0], [290.0, 190.0], [250.0, 190.0]]


def checker(camera_id: str = "m1", **extra) -> dict:
    return {"camera_id": camera_id, "role": "match", "match": MATCH, **extra}


def write_set_file(set_id: str = SET_ID, checks: int = 2) -> Path:
    """The set.json of a reference set (enough for the line save's check), in the data folder as it is now."""
    from app.services import settings_persistence_service

    folder = Path(settings_persistence_service.DATA_DIR) / "match-references" / set_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "set.json").write_text(json.dumps({
        "format": 1, "id": set_id, "reference": [460, 360, 820, 600], "picture_size": [1280, 960], "samples": 8,
        "checks": [{"box": [-180, -120, 180, 120], "typical": 0.9}] * checks}), encoding="utf-8")
    return folder


def result(label=(98, 0), cap=(95, 4), reason=None, ms=38.0) -> PatternResult:
    """A pattern check's result with checks Label and Cap (score, biggest defect); Cap left out when None."""
    checks = [CheckResult("Label", label[0], label[1], [[150, 150, 160, 160]] if label[1] else [], BOX,
                          reason not in ("pattern_not_found",) and label[1] <= 40)]
    if cap is not None:
        checks.append(CheckResult("Cap", cap[0], cap[1], [[260, 160, 270, 170]] if cap[1] else [], CAP_BOX,
                                  cap[0] >= 80 and cap[1] <= 40))
    return PatternResult(640, 480, ms, Found(200.0, 150.0, 2.0, 0.9), checks,
                         "good" if reason is None else "reject", reason)


GOOD = result()
NOT_FOUND = result(label=(60, 0), cap=None, reason="pattern_not_found")
MISMATCH = result(cap=(20, 0), reason="pattern_mismatch")
DEFECT = result(cap=(92, 48), reason="pattern_defect")
ERROR = error_result("the reference set ref-0a1b2c3d is missing", 640, 480)
