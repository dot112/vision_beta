"""A trigger camera's latest picture with its boxes, its counters, and the capture endpoint.

The line keeps, per camera in count mode "One picture per trigger", the latest capture
(an increasing id, the time, the verdict and the JPEG, or none when no picture came) and
six counters. The inference worker hands the frame it inferred to the result callback;
the picture is that frame with the model's boxes drawn. The line status lists both, and
GET /lines/{id}/trigger/capture gives the JPEG.
"""
from __future__ import annotations

import threading

import cv2
import numpy as np
import pytest

from app.schemas.vision import BoundingBox, DetectionItem
from app.security.api_key_scopes import required_api_key_scopes
from app.services.trigger_inspection import render_trigger_capture
from tests.conftest import FakeInferenceEngine
from tests.test_joined_cameras import _line, lines  # noqa: F401  (a fixture)

COUNTERS = ("triggers", "pictures", "results", "missing", "late", "extra")


def _trigger(camera_id="c", **extra):
    return {"camera_id": camera_id, "count_mode": "trigger", "expected_classes": ["apple"], "defect_classes": ["dent"], **extra}


@pytest.fixture
def runtime(lines):  # noqa: F811
    return _line(lines, [_trigger("c")])


def _det(name="bottle"):
    # In the middle of a 160 x 120 picture, away from the band along its edge.
    return DetectionItem(class_id=0, class_name=name, confidence=0.9,
                         bbox=BoundingBox(x1=60, y1=40, x2=100, y2=80, width=40, height=40))


# ── The latest capture ───────────────────────────────────────────────────────

def test_capture_ids_increase_and_keep_the_latest(runtime):
    assert runtime.trigger_capture("c") == (None, None)
    a = runtime.remember_trigger_capture("c", {"status": "inspected", "class_name": "apple", "result": "good", "trigger_no": 1}, b"J1")
    info, jpeg = runtime.trigger_capture("c")
    assert info["id"] == a and jpeg == b"J1" and info["has_picture"] is True
    assert (info["class_name"], info["result"], info["reason"], info["trigger_no"]) == ("apple", "good", None, 1)

    b = runtime.remember_trigger_capture("c", {"status": "no_picture", "result": "reject", "reject_reason": "no_picture"}, None)
    info, jpeg = runtime.trigger_capture("c")
    assert b == a + 1 and info["id"] == b and info["has_picture"] is False and jpeg is None
    assert info["reason"] == "no_picture" and info["at"]       # ISO time
    assert info["result"] == "reject" and info["trigger_no"] is None


def test_the_capture_is_a_copy_and_the_time_is_iso_utc(runtime):
    from datetime import datetime

    runtime.remember_trigger_capture("c", {"status": "inspected", "result": "good"}, b"J")
    info, _ = runtime.trigger_capture("c")
    assert datetime.fromisoformat(info["at"]).utcoffset().total_seconds() == 0
    info["result"] = "changed"
    assert runtime.trigger_capture("c")[0]["result"] == "good"


def test_each_camera_keeps_its_own_capture(lines):  # noqa: F811
    runtime = _line(lines, [_trigger("c"), _trigger("d")])
    runtime.remember_trigger_capture("c", {"status": "inspected", "result": "good"}, b"C")
    runtime.remember_trigger_capture("d", {"status": "inspected", "result": "reject", "reject_reason": "vision"}, b"D")
    assert runtime.trigger_capture("c")[1] == b"C" and runtime.trigger_capture("d")[1] == b"D"
    ids = {camera: runtime.trigger_capture(camera)[0]["id"] for camera in ("c", "d")}
    assert ids["c"] != ids["d"]
    assert runtime.trigger_capture("nobody") == (None, None)


# ── The counters ─────────────────────────────────────────────────────────────

def test_counters_and_reset(runtime):
    runtime.count_trigger("c", "triggers")
    runtime.count_trigger("c", "missing")
    row = next(r for r in runtime.trigger_view() if r["camera_id"] == "c")
    assert row["counters"] == {"triggers": 1, "pictures": 0, "results": 0, "missing": 1, "late": 0, "extra": 0}
    runtime.count_trigger("c", "pictures", 3)
    assert next(iter(runtime.trigger_view()))["counters"]["pictures"] == 3

    runtime.remember_trigger_capture("c", {"status": "inspected", "result": "good"}, b"J")
    runtime.reset()
    assert next(r for r in runtime.trigger_view())["counters"]["missing"] == 0
    assert set(next(iter(runtime.trigger_view()))["counters"]) == set(COUNTERS)
    assert all(v == 0 for v in next(iter(runtime.trigger_view()))["counters"].values())
    # The latest picture stays; only the counts start again.
    assert runtime.trigger_capture("c")[1] == b"J"


def test_an_unknown_counter_is_refused(runtime):
    with pytest.raises(ValueError, match="counter"):
        runtime.count_trigger("c", "pictures_taken")


def test_counting_from_several_threads_loses_nothing(runtime):
    def work():
        for _ in range(200):
            runtime.count_trigger("c", "triggers")

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert next(iter(runtime.trigger_view()))["counters"]["triggers"] == 800


# ── The inference worker keeps the frame ─────────────────────────────────────

def test_worker_keeps_the_frame_it_inferred(lines, monkeypatch):  # noqa: F811
    from app.services.vision_service import _CameraInferenceWorker

    engine = FakeInferenceEngine()
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (engine, "fake-model", None))
    worker = _CameraInferenceWorker("cam-keeps-frame")
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    seen, done = [], threading.Event()

    def on_result(frame_id, detections, inference_ms, arrived):
        seen.append(worker.last_inferred_frame)
        done.set()

    try:
        assert worker.last_inferred_frame is None
        assert worker.submit_frame_if_idle(frame, 7, copy=False, on_result=on_result)
        assert done.wait(3)
    finally:
        worker.stop()
    # The callback finds the very frame the model got through, and it is still kept afterwards.
    assert seen[0] is frame and worker.last_inferred_frame is frame


# ── The picture with its boxes ───────────────────────────────────────────────

def test_render_trigger_capture_draws_and_never_raises(lines):  # noqa: F811
    frame = np.zeros((40, 60, 3), np.uint8)
    assert render_trigger_capture("c", frame, [])[:2] == b"\xff\xd8"
    assert render_trigger_capture("c", None, []) is None
    assert render_trigger_capture("c", "not a picture", []) is None
    assert render_trigger_capture("c", frame, [object()]) is None


def test_the_boxes_are_drawn_on_a_copy_and_the_frame_stays_as_it_was(lines):  # noqa: F811
    frame = np.zeros((120, 160, 3), np.uint8)
    plain = render_trigger_capture("c", frame, [])
    boxed = render_trigger_capture("c", frame, [_det()])
    assert not frame.any(), "the camera's frame was drawn on"
    with_boxes = cv2.imdecode(np.frombuffer(boxed, np.uint8), cv2.IMREAD_COLOR)
    without = cv2.imdecode(np.frombuffer(plain, np.uint8), cv2.IMREAD_COLOR)
    assert with_boxes.shape == frame.shape
    # The box (and its class name) is in the middle of the picture, where nothing else is drawn.
    assert with_boxes[40:80, 60:100].any() and not without[40:80, 60:100].any()


def test_a_picture_that_cannot_be_drawn_gives_none(lines, monkeypatch):  # noqa: F811
    # A model engine that fails while drawing: no picture, and no error for the trigger.
    monkeypatch.setattr(lines, "engine_for_camera", lambda camera_id: (FakeInferenceEngine(), "fake-model", None))
    assert render_trigger_capture("c", np.zeros((40, 60, 3), np.uint8), [_det()]) is None


# ── The line status ──────────────────────────────────────────────────────────

def test_line_status_lists_trigger_cameras(lines, runtime):  # noqa: F811
    rows = lines.summary(runtime)["trigger_cameras"]
    assert [set(r) for r in rows] == [{"camera_id", "picture_source", "notice_trigger", "capture", "counters"}]
    assert rows[0]["camera_id"] == "c" and rows[0]["capture"] is None
    assert rows[0]["counters"] == dict.fromkeys(COUNTERS, 0)

    runtime.remember_trigger_capture("c", {"status": "inspected", "class_name": "apple", "result": "good", "trigger_no": 2}, b"J")
    capture = lines.summary(runtime)["trigger_cameras"][0]["capture"]
    assert set(capture) == {"id", "at", "class_name", "result", "reason", "trigger_no", "has_picture"}
    assert capture["has_picture"] is True and capture["trigger_no"] == 2 and "jpeg" not in capture


def test_only_vision_cameras_in_trigger_mode_are_listed(lines):  # noqa: F811
    runtime = _line(lines, [_trigger("one"), {"camera_id": "tracks", "expected_classes": ["apple"]}, _trigger("two")])
    assert [r["camera_id"] for r in lines.summary(runtime)["trigger_cameras"]] == ["one", "two"]
    plain = _line(lines, [{"camera_id": "tracks-too", "expected_classes": ["apple"]}], name="Plain")
    assert lines.summary(plain)["trigger_cameras"] == []


def test_the_notice_shows_only_for_a_camera_that_takes_the_picture(lines, fake_camera):  # noqa: F811
    hardware = fake_camera("old")
    hardware.trigger_mode = "hardware"
    runtime = _line(lines, [
        _trigger("sys"),                                                          # nothing saved: the system
        _trigger("cam", picture_source="camera", notice_trigger=4),
        _trigger("left", picture_source="system", notice_trigger=5),              # kept from before; not used
        _trigger("old"),                                                          # nothing saved, on hardware trigger
    ])
    rows = {r["camera_id"]: r for r in lines.summary(runtime)["trigger_cameras"]}
    assert (rows["sys"]["picture_source"], rows["sys"]["notice_trigger"]) == ("system", None)
    assert (rows["cam"]["picture_source"], rows["cam"]["notice_trigger"]) == ("camera", 4)
    assert (rows["left"]["picture_source"], rows["left"]["notice_trigger"]) == ("system", None)
    assert (rows["old"]["picture_source"], rows["old"]["notice_trigger"]) == ("camera", None)


# ── The endpoint ─────────────────────────────────────────────────────────────

def test_capture_endpoint(client, admin_headers):
    from app.services.line_service import line_manager

    runtime = line_manager.get("line-1")
    url = "/api/v1/lines/line-1/trigger/capture"
    saved = (dict(runtime._trigger_captures), dict(runtime._trigger_jpegs))
    try:
        runtime._trigger_captures.clear()
        runtime._trigger_jpegs.clear()
        assert client.get(url, params={"camera_id": "cam-t"}, headers=admin_headers).status_code == 404
        assert client.get("/api/v1/lines/nope/trigger/capture", params={"camera_id": "cam-t"}, headers=admin_headers).status_code == 404
        assert client.get(url, headers=admin_headers).status_code == 422          # the camera is named

        first = runtime.remember_trigger_capture("cam-t", {"status": "inspected", "result": "good"}, b"\xff\xd8first")
        res = client.get(url, params={"camera_id": "cam-t"}, headers=admin_headers)
        assert res.status_code == 200 and res.content == b"\xff\xd8first"
        assert res.headers["content-type"] == "image/jpeg"
        assert res.headers["cache-control"] == "no-store" and res.headers["x-capture-id"] == str(first)
        assert client.get(url, params={"camera_id": "other"}, headers=admin_headers).status_code == 404

        # A product without a picture has a capture and no JPEG to give.
        runtime.remember_trigger_capture("cam-t", {"status": "no_picture", "result": "reject", "reject_reason": "no_picture"}, None)
        assert client.get(url, params={"camera_id": "cam-t"}, headers=admin_headers).status_code == 404
    finally:
        runtime._trigger_captures.clear()
        runtime._trigger_captures.update(saved[0])
        runtime._trigger_jpegs.clear()
        runtime._trigger_jpegs.update(saved[1])


def test_capture_endpoint_needs_a_sign_in(client):
    assert client.get("/api/v1/lines/line-1/trigger/capture", params={"camera_id": "cam-t"}).status_code in (401, 403)


def test_capture_endpoint_api_key_scope():
    assert required_api_key_scopes("GET", "/api/v1/lines/l1/trigger/capture") == {"monitor:read"}
    # Taking a picture stays an inspection.
    assert required_api_key_scopes("POST", "/api/v1/lines/l1/trigger") == {"inspection:trigger"}
