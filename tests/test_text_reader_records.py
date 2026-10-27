"""What a text reader read goes with its product: the product's message and PLC event (text,
text_status), the values of output cards and messages (text.<field>, text_status), the product
record, and the export's text column."""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

from app.db.models.production_record import ProductRecord
from app.services.event_values import values_for
from app.services.production_records_service import build_row
from app.services.records_export import csv_header, csv_rows
from app.services.trigger_inspection import trigger_camera
from app.services.vision_service import CameraStreamPipeline
from tests.test_joined_cameras import _joined, _line, _run, lines  # noqa: F401  (a fixture)
from tests.test_text_reader_line import GOOD, WRONG, _reader, _trigger_text, text_workers  # noqa: F401  (a fixture)
from tests.test_trigger_inspection import _trigger_cam, workers  # noqa: F401  (a fixture)


def test_values_of_a_text_product():
    values = values_for({"result": "reject", "reject_reason": "text_wrong", "text": {"Lot": "LOT A1234"},
                         "text_status": "wrong"})
    assert values["text.Lot"] == "LOT A1234" and values["text_status"] == "wrong" and values["reject_reason"] == "text_wrong"
    assert "text.Exp" not in values_for({"text": {"Lot": "X"}})
    assert "text_status" not in values_for({"result": "good"})


def test_a_record_keeps_the_text_and_the_export_shows_it():
    row = build_row({"is_defect": False, "class_name": "text ok", "text": {"Expiry": "12/03/2027", "Lot": "A12345"},
                     "text_status": "good", "camera_id": "t1", "line_id": "line-1"}, "product", False)
    assert row["details"]["text"] == {"Expiry": "12/03/2027", "Lot": "A12345"} and row["details"]["text_status"] == "good"
    header = next(csv.reader(io.StringIO(csv_header("UTC").lstrip("﻿"))))
    assert header.index("text") == header.index("sequence") + 1

    def record(details):
        return ProductRecord(id=1, recorded_at=datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc), kind="product",
                             counted=False, details=details)

    rows = list(csv.reader(io.StringIO(csv_rows([record(row["details"]), record(None), record({"text": {}})],
                                                timezone.utc))))
    column = header.index("text")
    assert [r[column] for r in rows] == ["Expiry=12/03/2027; Lot=A12345", "", ""]


def test_an_own_product_carries_the_text(lines, text_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [{"camera_id": "vis"}, _reader("t1")])
    fake_camera("t1")

    async def steps():
        await _trigger_text(runtime, "t1", WRONG)

    _run(steps)
    payload, event = runtime.sent[0]
    assert (payload["text"], payload["text_status"]) == ({"Lot": "LOT A1234"}, "wrong")
    assert (event["text"], event["text_status"]) == ({"Lot": "LOT A1234"}, "wrong")
    assert values_for(event)["text.Lot"] == "LOT A1234"
    assert build_row(payload, "product", False)["details"]["text"] == {"Lot": "LOT A1234"}


def test_a_joined_product_carries_the_readers_text(lines, workers, text_workers, fake_camera):  # noqa: F811
    runtime = _line(lines, [_trigger_cam(), {**_reader("t1"), **_joined("t1", 0, 80, "reject")}])
    fake_camera("vis")
    fake_camera("t1")

    async def steps():
        task = asyncio.create_task(trigger_camera(runtime, "vis", timeout=1.0))
        await asyncio.sleep(0.03)
        CameraStreamPipeline.get_worker("vis").process("bottle")
        await task
        await _trigger_text(runtime, "t1", GOOD)
        await asyncio.sleep(0.05)

    _run(steps)
    payload, event = runtime.sent[0]
    assert (payload["result"], payload["text"], payload["text_status"]) == ("PASSED", {"Lot": "LOT A12345"}, "good")
    assert (event["text"], event["text_status"]) == ({"Lot": "LOT A12345"}, "good")
