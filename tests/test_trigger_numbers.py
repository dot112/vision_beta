"""Trigger numbers: a "Take a picture" input has a number, and a camera chooses its triggers.

The input only names the trigger. On its card a camera that takes one picture
per trigger (Count mode "One picture per trigger", or Read codes "When an input
asks") ticks the trigger numbers that take its picture: one or more. Several
cameras can tick the same trigger.
"""
from __future__ import annotations

import pytest

import app.services.trigger_inspection as trigger_inspection
from app.services.line_config import (
    line_warnings,
    normalize_camera,
    normalize_plc_input,
    normalize_plc_inputs,
    triggered_cameras,
)
from app.services.plc_input_service import PLCInputService
from app.services.qr_service import QRReaderPipeline
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.trigger_inspection import product_from_result, trigger_input
from tests.test_joined_cameras import _line, _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_inputs import _reader, _signal, _vision
from tests.test_trigger_inspection import _det, _trigger_cam, workers  # noqa: F401  (a fixture)


def _take(number=None, **extra):
    card = {**_signal("capture"), "id": f"plcin_take_{number or extra.get('name') or 0}", "name": f"take {number or ''}".strip(), **extra}
    if number is not None:
        card["trigger_no"] = number
    return card


def _vis(camera_id="vis", **extra):
    return {"model_id": "model-test", **_trigger_cam(camera_id), **extra}


# ── The input: a trigger number ───────────────────────────────────────────────


def test_a_take_a_picture_input_has_a_trigger_number_and_names_no_camera():
    card = normalize_plc_input(_take(3))
    assert card["trigger_no"] == 3 and "camera_id" not in card and "camera_ids" not in card
    assert "trigger_no" not in normalize_plc_input(_signal("start_line", trigger_no=3))


def test_inputs_without_a_number_get_the_next_free_one():
    cards = normalize_plc_inputs([_take(2), _take(name="b"), _take(name="c"), _signal("start_line")])
    assert [card.get("trigger_no") for card in cards] == [2, 1, 3, None]


@pytest.mark.parametrize("number", [0, 100, -1, "two", 1.5, True])
def test_a_wrong_trigger_number_is_refused(number):
    with pytest.raises(ValueError, match="trigger number must be a whole number from 1 to 99"):
        normalize_plc_input(_take(number))


def test_two_inputs_cannot_have_the_same_trigger_number():
    with pytest.raises(ValueError, match="trigger number 4"):
        normalize_plc_inputs([_take(4), _take(4, id="other")])


# ── The camera: which triggers take its picture ──────────────────────────────


def test_a_camera_keeps_the_triggers_it_ticked():
    camera = normalize_camera(_vis(triggers=[3, 1, 3, "2"]), 0)
    assert camera["triggers"] == [1, 2, 3]
    reader = normalize_camera(_reader("qr", qr_trigger="signal", triggers=[7]), 1)
    assert reader["triggers"] == [7]


def test_a_camera_that_takes_no_picture_on_a_trigger_keeps_none():
    assert "triggers" not in normalize_camera(_vision("vis", triggers=[1]), 0)                    # counts moving products
    assert "triggers" not in normalize_camera(_reader("qr", triggers=[1]), 0)                    # reads continuously
    assert "triggers" not in normalize_camera(_vis(), 0)


@pytest.mark.parametrize("bad", [[0], [100], ["x"], "1", [1.5]])
def test_a_wrong_trigger_on_a_camera_is_refused(bad):
    with pytest.raises(ValueError, match="Camera 1.*trigger"):
        normalize_camera(_vis(triggers=bad), 0)


def test_a_client_that_leaves_the_triggers_out_keeps_them(lines):  # noqa: F811
    line = SettingsPersistenceService.save_line({"name": "L", "cameras": [_vis(triggers=[1, 2]), _reader("qr", qr_trigger="signal", triggers=[2])],
                                                 "plc_inputs": [_take(1), _take(2)]})
    assert [c.get("triggers") for c in line["cameras"]] == [[1, 2], [2]]
    again = SettingsPersistenceService.save_line({"id": line["id"], "cameras": [_vis(), _reader("qr", qr_trigger="signal")]})
    assert [c.get("triggers") for c in again["cameras"]] == [[1, 2], [2]]
    cleared = SettingsPersistenceService.save_line({"id": line["id"], "cameras": [_vis(triggers=[]), _reader("qr", qr_trigger="signal", triggers=[1])]})
    assert [c.get("triggers") for c in cleared["cameras"]] == [None, [1]]
    # Saving the inputs alone leaves the cameras' ticks alone.
    inputs_only = SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_take(1), _take(2), _take(5)]})
    assert [c.get("triggers") for c in inputs_only["cameras"]] == [None, [1]]


def test_which_cameras_a_trigger_takes_a_picture_with():
    cameras = [{"camera_id": "a", "triggers": [1, 2]}, {"camera_id": "b", "triggers": [2]}, {"camera_id": "c"}]
    assert triggered_cameras(cameras, {"action": "capture", "trigger_no": 2}) == ["a", "b"]
    assert triggered_cameras(cameras, {"action": "capture", "trigger_no": 1}) == ["a"]
    assert triggered_cameras(cameras, {"action": "capture", "trigger_no": 9}) == []
    # A card saved before cameras chose their triggers names its camera itself.
    assert triggered_cameras(cameras, {"action": "capture", "camera_id": "c"}) == ["c"]
    assert triggered_cameras(cameras, {"action": "capture", "trigger_no": 1, "camera_id": "c"}) == ["a", "c"]


def test_an_input_saved_with_its_camera_becomes_a_tick_on_that_camera(lines):  # noqa: F811
    # What was saved before this change, and what an older client still sends.
    line = SettingsPersistenceService.save_line({"name": "L", "cameras": [_vis(), _reader("qr", qr_trigger="signal")],
                                                 "plc_inputs": [_signal("capture", camera_id="qr"), _take(5, camera_id="vis")]})
    assert [(c["trigger_no"], c.get("camera_id")) for c in line["plc_inputs"]] == [(1, None), (5, None)]
    assert [c.get("triggers") for c in line["cameras"]] == [[5], [1]]
    with pytest.raises(ValueError, match="not on this line"):
        SettingsPersistenceService.save_line({"id": line["id"], "plc_inputs": [_take(1, camera_id="gone")]})
    with pytest.raises(ValueError, match="Camera 1"):
        SettingsPersistenceService.save_line({"name": "M", "cameras": [_vision("vis")], "plc_inputs": [_take(1, camera_id="vis")]})


# ── Warnings ──────────────────────────────────────────────────────────────────


def _warned(cameras, inputs):
    return line_warnings({"cameras": [normalize_camera(c, i) for i, c in enumerate(cameras)],
                          "plc_inputs": normalize_plc_inputs(inputs)})


def test_a_reader_with_no_trigger_ticked_is_warned_about():
    reader = _reader("qr", qr_trigger="signal")
    assert any("Camera 1" in w and "an input asks" in w for w in _warned([reader], [_take(1)]))
    assert not any("an input asks" in w for w in _warned([{**reader, "triggers": [1]}], [_take(1)]))
    assert any("an input asks" in w for w in _warned([{**reader, "triggers": [1]}], [_take(1, enabled=False)]))


def test_a_trigger_no_input_has_is_warned_about():
    warnings = _warned([_vis(triggers=[1, 4])], [_take(1)])
    assert len(warnings) == 1 and "Camera 1" in warnings[0] and "trigger 4" in warnings[0]


def test_a_trigger_no_camera_uses_is_warned_about():
    warnings = _warned([_vis(triggers=[1])], [_take(1), _take(2, name="spare")])
    assert len(warnings) == 1 and "'spare'" in warnings[0] and "trigger 2" in warnings[0]


# ── What a trigger does ───────────────────────────────────────────────────────


class _Reader:
    def __init__(self):
        self.requests = []

    def request_capture(self, **kwargs):
        self.requests.append(kwargs)


@pytest.fixture
def spies(lines, monkeypatch):  # noqa: F811
    """A line with a vision camera (triggers 1 and 2) and a code reader (triggers 1 and 7); every picture asked for is recorded."""
    line = SettingsPersistenceService.save_line({"name": "L", "enabled": True, "cameras": [
        _vis(triggers=[1, 2]), _reader("qr", qr_trigger="signal", qr_trigger_delay_ms=40, triggers=[1, 7])],
        "plc_inputs": [_take(1), _take(2), _take(7), _take(9)]})
    lines.apply_state()
    runtime = lines.get(line["id"])
    seen = {"products": [], "readers": {}}

    async def fake_trigger(rt, camera_id, timeout=None, with_detections=False, trigger_no=None):
        if camera_id in seen.get("broken", ()):
            raise ValueError(f"Camera '{camera_id}' is not connected")
        seen["products"].append((camera_id, trigger_no))
        return {"camera_id": camera_id, "status": "inspected", "result": "good", "trigger_no": trigger_no}

    monkeypatch.setattr(trigger_inspection, "trigger_camera", fake_trigger)
    monkeypatch.setattr(QRReaderPipeline, "get_worker",
                        classmethod(lambda cls, camera_id: seen["readers"].setdefault(camera_id, _Reader())))
    cards = {card["trigger_no"]: {**card, "line_id": runtime.id, "line_name": runtime.name} for card in runtime.plc_inputs}
    return runtime, cards, seen


def test_one_trigger_takes_a_picture_with_every_camera_that_ticked_it(spies):
    runtime, cards, seen = spies
    answers = {}

    async def steps():
        answers["one"] = await PLCInputService._capture(cards[1])

    _run(steps)
    assert seen["products"] == [("vis", 1)]
    assert seen["readers"]["qr"].requests == [{"delay": 0.04, "trigger_no": 1}]
    assert [(a["camera_id"], a["status"]) for a in answers["one"]] == [("vis", "inspected"), ("qr", "requested")]


def test_a_camera_can_have_several_triggers(spies):
    runtime, cards, seen = spies

    async def steps():
        await PLCInputService._capture(cards[2])
        await PLCInputService._capture(cards[1])
        await PLCInputService._capture(cards[7])

    _run(steps)
    assert seen["products"] == [("vis", 2), ("vis", 1)]
    assert [r["trigger_no"] for r in seen["readers"]["qr"].requests] == [1, 7]


def test_a_trigger_no_camera_ticked_says_so_on_its_card(spies):
    runtime, cards, seen = spies

    async def steps():
        with pytest.raises(ValueError, match="No camera of this line uses trigger 9"):
            await PLCInputService._capture(cards[9])

    _run(steps)
    assert seen["products"] == [] and seen["readers"] == {}


def test_one_camera_that_fails_does_not_stop_the_others(spies):
    runtime, cards, seen = spies
    seen["broken"] = {"vis"}

    async def steps():
        with pytest.raises(ValueError, match="'vis' is not connected"):
            await PLCInputService._capture(cards[1])

    _run(steps)
    assert seen["products"] == [] and len(seen["readers"]["qr"].requests) == 1


def test_the_api_can_fire_a_trigger_by_its_number(spies):
    runtime, cards, seen = spies
    answers = {}

    async def steps():
        answers["seven"] = await trigger_input(runtime, 7)
        with pytest.raises(LookupError):
            await trigger_input(runtime, 5)

    _run(steps)
    assert [a["camera_id"] for a in answers["seven"]] == ["qr"] and seen["readers"]["qr"].requests[0]["trigger_no"] == 7


def test_a_product_says_which_trigger_took_its_picture(lines, workers):  # noqa: F811
    runtime = _line(lines, [_trigger_cam("vis")])
    answer = product_from_result(runtime, "vis", [_det("bottle")], trigger_no=2)
    assert answer["status"] == "inspected" and answer["trigger_no"] == 2
    product_from_result(runtime, "vis", [_det("bottle")])
    first, second = (payload for payload, _ in runtime.sent)
    assert first["trigger_no"] == 2 and "trigger_no" not in second


def test_a_code_picture_says_which_trigger_took_it(lines, monkeypatch):  # noqa: F811
    from app.services.qr_service import _QRReaderWorker

    worker = _QRReaderWorker("qr-n")
    try:
        monkeypatch.setattr(worker, "_fresh_frame", lambda: None)
        record, _ = worker.capture({"trigger_no": 3})
        assert record["trigger_no"] == 3 and "trigger_no" not in worker.capture({})[0]
    finally:
        worker.stop()
