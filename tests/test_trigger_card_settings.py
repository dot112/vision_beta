"""A trigger camera's card: where its picture comes from, the delay, the notice and the wait.

Count mode "One picture per trigger" takes its picture from the system (the server
triggers the camera, after a delay) or from the camera itself (an industrial camera
that triggers on its own input, with an optional trigger notice). A picture that never
comes is a reject with the reason "no picture" (PLC code 10). The line warns about
setups where a PLC or robot would wait for an answer that cannot come.
"""
from __future__ import annotations

import pytest

from app.services.line_config import (
    CAMERA_KEPT_KEYS,
    DEFAULT_PICTURE_WAIT_MS,
    PICTURE_DELAY_LIMITS_MS,
    PICTURE_SOURCES,
    PICTURE_WAIT_LIMITS_MS,
    REASON_NO_PICTURE,
    REJECT_REASON_CODES,
    REJECT_REASONS,
    line_warnings,
    normalize_camera,
    picture_source,
    trigger_mode_for_card,
)

BASE = {"camera_id": "c", "role": "vision", "model_id": "m"}


def _cam(**extra):
    return normalize_camera({**BASE, **extra}, 0)


def _capture_input(number):
    return {"id": f"take-{number}", "name": f"Take {number}", "action": "capture", "trigger_no": number,
            "enabled": True, "plc_endpoint_id": "ep1", "address": "10001"}


# ── The card's settings ──────────────────────────────────────────────────────


def test_the_values_the_spec_names():
    assert PICTURE_SOURCES == ("system", "camera")
    assert PICTURE_DELAY_LIMITS_MS == (0, 5000) and PICTURE_WAIT_LIMITS_MS == (50, 10000)
    assert DEFAULT_PICTURE_WAIT_MS == 1000


def test_trigger_camera_keeps_its_picture_settings():
    cam = normalize_camera({**BASE, "count_mode": "trigger", "picture_source": "camera", "notice_trigger": 3,
                            "picture_wait_ms": 800}, 0)
    assert cam["picture_source"] == "camera" and cam["notice_trigger"] == 3 and cam["picture_wait_ms"] == 800
    assert cam["triggers"] == [3]            # the notice is the camera's trigger number


def test_camera_trigger_without_notice_has_no_triggers():
    cam = normalize_camera({**BASE, "count_mode": "trigger", "picture_source": "camera", "notice_trigger": None,
                            "triggers": [1]}, 0)
    assert cam["notice_trigger"] is None and cam["triggers"] == []
    blank = normalize_camera({**BASE, "count_mode": "trigger", "picture_source": "camera", "notice_trigger": "",
                              "triggers": [1]}, 0)
    assert blank["notice_trigger"] is None and blank["triggers"] == []


def test_system_trigger_delay_and_limits():
    cam = normalize_camera({**BASE, "count_mode": "trigger", "picture_source": "system", "picture_delay_ms": 250,
                            "triggers": [1]}, 0)
    assert cam["picture_delay_ms"] == 250 and cam["triggers"] == [1]
    for bad in ({"picture_delay_ms": 5001}, {"picture_wait_ms": 49}, {"picture_source": "plc"},
                {"notice_trigger": 100}):
        with pytest.raises(ValueError):
            normalize_camera({**BASE, "count_mode": "trigger", "picture_source": "camera", **bad}, 0)


@pytest.mark.parametrize("bad", [{"picture_delay_ms": -1}, {"picture_wait_ms": 10001}, {"picture_wait_ms": "slow"},
                                 {"notice_trigger": 0}, {"notice_trigger": "x"}, {"notice_trigger": True}])
def test_a_wrong_setting_is_refused(bad):
    with pytest.raises(ValueError):
        _cam(count_mode="trigger", picture_source="camera", **bad)


def test_the_limits_themselves_are_allowed_and_numbers_are_whole():
    cam = _cam(count_mode="trigger", picture_delay_ms=5000, picture_wait_ms=50.4)
    assert (cam["picture_delay_ms"], cam["picture_wait_ms"]) == (5000, 50)
    cam = _cam(count_mode="trigger", picture_delay_ms=0, picture_wait_ms=10000, notice_trigger="99")
    assert (cam["picture_delay_ms"], cam["picture_wait_ms"], cam["notice_trigger"]) == (0, 10000, 99)


def test_the_defaults_and_a_source_that_was_not_sent():
    cam = _cam(count_mode="trigger")
    assert "picture_source" not in cam                       # follows the camera's trigger mode (picture_source())
    assert (cam["picture_delay_ms"], cam["picture_wait_ms"], cam["notice_trigger"]) == (0, DEFAULT_PICTURE_WAIT_MS, None)
    assert _cam(count_mode="trigger", picture_source=" Camera ")["picture_source"] == "camera"


def test_a_camera_that_does_not_take_one_picture_per_trigger_keeps_none_of_them():
    cam = _cam(count_mode="track", picture_source="camera", picture_delay_ms=10, picture_wait_ms=100, notice_trigger=2)
    assert not {"picture_source", "picture_delay_ms", "picture_wait_ms", "notice_trigger"} & set(cam)
    assert "triggers" not in cam


def test_the_four_keys_are_kept_when_a_client_leaves_them_out():
    for key in ("picture_source", "picture_delay_ms", "notice_trigger", "picture_wait_ms"):
        assert key in CAMERA_KEPT_KEYS


# ── Where the picture comes from ─────────────────────────────────────────────


def test_saved_camera_without_source_follows_the_trigger_mode():
    assert picture_source({"count_mode": "trigger"}, "hardware") == "camera"
    assert picture_source({"count_mode": "trigger"}, "off") == "system"
    assert picture_source({"count_mode": "trigger"}) == "system"
    assert picture_source({"count_mode": "trigger"}, "software") == "system"
    assert picture_source({"count_mode": "trigger", "picture_source": "system"}, "hardware") == "system"
    assert picture_source({"count_mode": "trigger", "picture_source": "camera"}, "off") == "camera"


def test_trigger_mode_for_card():
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger", "picture_source": "camera"}) == "hardware"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger", "picture_source": "system"}) == "software"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "track"}) == "off"
    assert trigger_mode_for_card({"role": "vision"}) == "off"
    assert trigger_mode_for_card({"role": "qr"}) == "off"
    # no saved source: the card follows the camera's current mode
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger"}, "hardware") == "hardware"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger"}, "off") == "software"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger"}) == "software"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger"}, "software") == "software"
    # a saved source wins over the camera's current mode; other count modes are "off" whatever the mode
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger", "picture_source": "system"}, "hardware") == "software"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "trigger", "picture_source": "camera"}, "off") == "hardware"
    assert trigger_mode_for_card({"role": "vision", "count_mode": "track"}, "hardware") == "off"


def test_no_picture_reason_code():
    assert REASON_NO_PICTURE == "no_picture" and REASON_NO_PICTURE in REJECT_REASONS
    assert REJECT_REASON_CODES["no_picture"] == 10
    assert len(set(REJECT_REASON_CODES.values())) == len(REJECT_REASON_CODES)


# ── Warnings ─────────────────────────────────────────────────────────────────

NO_NOTICE = "takes its own pictures without a trigger notice: a missed or double trigger cannot be seen"
NO_ANSWER = "A trigger with nothing found gets no answer"
FPS = "Minimum frames/s"


def _camera_no_notice(camera_id="own", **extra):
    return _cam(camera_id=camera_id, count_mode="trigger", picture_source="camera", notice_trigger=None,
                no_product="reject", **extra)


def _system_ignore(camera_id="sys", **extra):
    return _cam(camera_id=camera_id, count_mode="trigger", picture_source="system", no_product="ignore",
                triggers=[1], **extra)


def _answering_line(**extra):
    return {"cameras": [_camera_no_notice(), _system_ignore()], "plc_inputs": [_capture_input(1)],
            "send_actions": [{"id": "s", "enabled": True, "endpoint_id": "tcp-1"}], "min_fps": 5, **extra}


def test_warnings_for_lines_that_answer_someone():
    line = _answering_line()
    warnings = line_warnings(line, send_protocols={"tcp-1": "tcp"})
    text = " ".join(warnings)
    assert "missed or double trigger cannot be seen" in text
    assert "A trigger with nothing found gets no answer" in text
    assert "Minimum frames/s" in text          # only trigger cameras: frames/s is not measured
    # nobody waits: neither of the two "answer" warnings
    quiet = " ".join(line_warnings({**line, "send_actions": []}, send_protocols={}))
    assert "missed or double trigger cannot be seen" not in quiet
    assert "A trigger with nothing found gets no answer" not in quiet
    assert "Minimum frames/s" in quiet         # that one does not depend on anyone waiting


NO_TICK = ("Camera 1 takes one picture per trigger, but no trigger is ticked on its card: only the API can take its "
           "picture. Tick a trigger under Counting, or add an input with the action 'Take a picture'.")


def test_a_system_trigger_card_with_no_trigger_ticked_is_warned_about():
    system = _cam(camera_id="sys", count_mode="trigger", picture_source="system", no_product="reject", triggers=[])
    assert NO_TICK in line_warnings({"cameras": [system]})
    assert NO_TICK in line_warnings({"cameras": [{k: v for k, v in system.items() if k != "triggers"}]})
    # An input that names the camera itself takes its picture too.
    named = {"action": "capture", "camera_id": "sys", "enabled": True, "name": "Take", "trigger_no": 1}
    assert not any("no trigger is ticked" in w for w in line_warnings({"cameras": [system], "plc_inputs": [named]}))
    # A ticked trigger, a camera that takes its own pictures, and a tracking camera have nothing to warn about.
    for fine in ({**system, "triggers": [1]},
                 {**system, "picture_source": "camera", "notice_trigger": 1},
                 {**system, "count_mode": "track"}):
        assert not any("no trigger is ticked" in w for w in line_warnings({"cameras": [fine]})), fine


def test_each_warning_names_its_camera_and_says_what_to_do():
    warnings = line_warnings(_answering_line(), send_protocols={"tcp-1": "tcp"})
    assert (
        "Camera 1 takes its own pictures without a trigger notice: a missed or double trigger cannot be seen. "
        "Use a trigger notice when a PLC or robot waits for each answer."
    ) in warnings
    assert (
        "Camera 2 ignores a picture with nothing in it: A trigger with nothing found gets no answer. "
        "Set When nothing is found to Reject."
    ) in warnings
    assert (
        "This line's cameras take one picture per trigger, so frames per second are not measured. "
        "Set Minimum frames/s to 0."
    ) in warnings
    # Camera 1 rejects when nothing is found, camera 2 has a system trigger: each gets only its own
    assert not any(w.startswith("Camera 1 ignores") or w.startswith("Camera 2 takes its own") for w in warnings)


@pytest.mark.parametrize("line_part, protocols", [
    ({"handshake": {"enabled": True}}, {}),
    ({"sequence_input_id": "seq"}, {}),
    ({"send_actions": [{"id": "s", "enabled": True, "endpoint_id": "tcp-1"}]}, {"tcp-1": "tcp"}),
])
def test_what_makes_a_line_answer_someone(line_part, protocols):
    line = {**_answering_line(), "send_actions": [], **line_part}
    text = " ".join(line_warnings(line, send_protocols=protocols))
    assert NO_NOTICE in text and NO_ANSWER in text


@pytest.mark.parametrize("line_part, protocols", [
    ({}, {}),
    ({"handshake": {"enabled": False}}, {}),
    ({"handshake": None}, {}),
    ({"send_actions": [{"id": "s", "enabled": True, "endpoint_id": "mqtt-1"}]}, {"mqtt-1": "mqtt"}),
    ({"send_actions": [{"id": "s", "enabled": False, "endpoint_id": "tcp-1"}]}, {"tcp-1": "tcp"}),
    ({"send_actions": [{"id": "s", "enabled": True, "endpoint_id": "tcp-1"}]}, {}),             # channel not known
])
def test_what_does_not_make_a_line_answer_someone(line_part, protocols):
    line = {**_answering_line(), "send_actions": [], **line_part}
    text = " ".join(line_warnings(line, send_protocols=protocols))
    assert NO_NOTICE not in text and NO_ANSWER not in text


def test_a_tcp_send_card_is_not_known_without_the_channels():
    line = _answering_line()
    text = " ".join(line_warnings(line))                       # send_protocols left out
    assert NO_NOTICE not in text and NO_ANSWER not in text


def test_a_camera_trigger_with_a_notice_or_a_system_trigger_needs_no_warning():
    answering = {"handshake": {"enabled": True}}
    noticed = _cam(count_mode="trigger", picture_source="camera", notice_trigger=2, no_product="reject")
    line = {"cameras": [noticed, _system_ignore()], "plc_inputs": [_capture_input(1), _capture_input(2)], **answering}
    assert NO_NOTICE not in " ".join(line_warnings(line))


def test_no_picture_source_follows_the_cameras_trigger_mode():
    saved = _cam(count_mode="trigger", no_product="reject")            # saved before there were pictures settings
    line = {"cameras": [saved], "plc_inputs": [], "handshake": {"enabled": True}}
    assert NO_NOTICE in " ".join(line_warnings(line, trigger_modes={"c": "hardware"}))
    assert NO_NOTICE not in " ".join(line_warnings(line, trigger_modes={"c": "software"}))
    assert NO_NOTICE not in " ".join(line_warnings(line))


def test_only_nothing_found_set_to_ignore_gets_the_no_answer_warning():
    line = {"plc_inputs": [_capture_input(1)], "handshake": {"enabled": True}}
    for no_product, warned in (("ignore", True), ("reject", False), ("good", False)):
        cam = _cam(count_mode="trigger", picture_source="system", no_product=no_product, triggers=[1])
        assert (NO_ANSWER in " ".join(line_warnings({**line, "cameras": [cam]}))) is warned
    tracking = _cam(count_mode="track")
    assert NO_ANSWER not in " ".join(line_warnings({**line, "cameras": [tracking]}))


def test_the_frames_per_second_warning():
    system = _system_ignore()
    base = {"plc_inputs": [_capture_input(1)]}
    assert FPS in " ".join(line_warnings({**base, "cameras": [system], "min_fps": 5}))
    assert FPS not in " ".join(line_warnings({**base, "cameras": [system], "min_fps": 0}))
    assert FPS not in " ".join(line_warnings({**base, "cameras": [system]}))
    # a camera that tracks does take pictures all the time
    assert FPS not in " ".join(line_warnings({**base, "cameras": [system, _cam(camera_id="t")], "min_fps": 5}))
    # no vision camera at all: nothing to say about it
    reader = {"camera_id": "r", "role": "qr"}
    assert FPS not in " ".join(line_warnings({**base, "cameras": [reader], "min_fps": 5}))
    # a reader beside the trigger cameras does not change that
    assert FPS in " ".join(line_warnings({**base, "cameras": [system, reader], "min_fps": 5}))


# ── "Not obeyed": an input asks a camera that triggers itself ────────────────

NOT_OBEYED = "so it is not obeyed"


def _not_obeyed(line, modes):
    return [w for w in line_warnings(line, trigger_modes=modes) if NOT_OBEYED in w]


def test_a_trigger_notice_is_not_an_input_that_is_not_obeyed():
    cam = _cam(count_mode="trigger", picture_source="camera", notice_trigger=3)
    assert cam["triggers"] == [3]
    line = {"cameras": [cam], "plc_inputs": [_capture_input(3)]}
    assert _not_obeyed(line, {"c": "hardware"}) == []
    assert _not_obeyed(line, {}) == []


def test_a_hardware_camera_without_a_notice_still_ignores_the_input_that_asks_it():
    # a camera saved before the card held a picture source, on a hardware trigger, with the input ticked
    saved = _cam(count_mode="trigger", triggers=[1])
    assert "picture_source" not in saved and saved["triggers"] == [1]
    line = {"cameras": [saved], "plc_inputs": [_capture_input(1)]}
    (warning,) = _not_obeyed(line, {"c": "hardware"})
    assert "Input 'Take 1' asks Camera 1 for a picture" in warning
    assert _not_obeyed(line, {"c": "software"}) == []
    # a system trigger on a camera whose own Trigger is still Hardware (not applied yet) warns too
    system = _cam(count_mode="trigger", picture_source="system", triggers=[1])
    assert len(_not_obeyed({"cameras": [system], "plc_inputs": [_capture_input(1)]}, {"c": "hardware"})) == 1


def test_an_input_that_is_not_the_cameras_notice_is_still_not_obeyed():
    cam = _cam(count_mode="trigger", picture_source="camera", notice_trigger=3)
    named = {**_capture_input(4), "camera_id": "c"}          # an older input that names the camera itself
    line = {"cameras": [cam], "plc_inputs": [_capture_input(3), named]}
    (warning,) = _not_obeyed(line, {"c": "hardware"})
    assert "Take 4" in warning


# ── The line's detail answers with these warnings ────────────────────────────


def test_a_lines_detail_knows_which_channels_are_tcp(client, admin_headers, vision_model, monkeypatch):
    from app.services.settings_persistence_service import SettingsPersistenceService

    channels = [{"id": "tcp-wait", "protocol": "TCP"}, {"id": "mqtt-wait", "protocol": "mqtt"}]
    monkeypatch.setattr(SettingsPersistenceService, "get_endpoints", classmethod(lambda cls, protocol=None: channels))
    card = {"id": "to-mes", "name": "To MES", "endpoint_id": "tcp-wait", "trigger": "cross_line", "condition": "any"}
    camera = {"camera_id": "cam-own-pictures", "model_id": vision_model, "count_mode": "trigger",
              "picture_source": "camera", "no_product": "reject"}
    made = client.post("/api/v1/lines", json={"name": "Waiting line", "cameras": [camera], "send_actions": [card]},
                       headers=admin_headers)
    assert made.status_code == 201, made.text
    line_id = made.json()["id"]
    try:
        detail = lambda: client.get(f"/api/v1/lines/{line_id}", headers=admin_headers).json()["warnings"]  # noqa: E731
        assert any("takes its own pictures without a trigger notice" in w for w in detail()), detail()
        channels[0]["protocol"] = "mqtt"          # the card now goes to an MQTT broker: nobody waits
        assert not any("takes its own pictures without a trigger notice" in w for w in detail()), detail()
    finally:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)
