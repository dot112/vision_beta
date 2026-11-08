"""The line's camera card sets an industrial camera's Trigger (decision A: one place).

After a line is saved, each vision camera that is an industrial camera gets the trigger mode its card
asks for: One picture per trigger from the camera is Hardware, from the system Software. That card
also locks the mode (the camera's own Settings may not change it). Any other vision card leaves the
mode alone (a tracking camera on Hardware, a code reader on a signal), except Software that nothing
triggers, which is Off; it does not lock. A QR reader keeps its own.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import app.routes.v1.lines as lines_route
from app.hardware.camera.gige_camera import GigECamera
from app.services.camera_service import CameraService
from app.state.application_state import app_state
from tests.test_industrial_camera import API, FakeBackend, _wait_until, camera_row  # noqa: F401  (camera_row is a fixture)

LINES = "/api/v1/lines"
NOTE = "Set on the line's camera card (Line setup)"
ONLY_INDUSTRIAL = "only an industrial camera can trigger itself"
ROOT = Path(__file__).resolve().parents[1]


def _device(serial):
    return {"serial": serial, "model": "X", "vendor": "V", "interface": "GigE"}


@pytest.fixture
def industrial(camera_row, monkeypatch):     # noqa: F811
    """``industrial(mode="off", source="A1", connect=True)`` saves an industrial camera and, by default,
    connects a stand-in for it. Gives (row, driver); the driver keeps what it was told in ``driver.sets``."""
    drivers = []

    def make(mode="off", source="A1", connect=True, **settings):
        row = camera_row(source=source, trigger_mode=mode, **settings)
        if not connect:
            return row, None
        driver = GigECamera(row["id"], row["name"], source, settings=dict(row["settings"]),
                            backend=FakeBackend(devices=[_device(source)]))
        assert driver.connect(), driver.last_error
        driver.sets = []
        applied = driver.set_properties

        def spy(properties):
            driver.sets.append(dict(properties))
            return applied(properties)

        driver.set_properties = spy
        drivers.append(driver)
        monkeypatch.setitem(app_state.cameras, row["id"], driver)
        return row, driver

    yield make
    for driver in drivers:
        driver.disconnect()


@pytest.fixture
def save_line(client, admin_headers, vision_model):
    """``save_line(*cards, line_id=None)`` creates a line with these camera cards (or changes the line);
    gives the response. Every line made is deleted afterwards."""
    made = []

    def save(*cards, line_id=None, **extra):
        cameras = [{"model_id": vision_model, **card} if card.get("role", "vision") == "vision" else card
                   for card in cards]
        if line_id is None:
            res = client.post(LINES, json={"name": f"Card line {len(made)}", "cameras": cameras, **extra},
                              headers=admin_headers)
            if res.status_code == 201:
                made.append(res.json()["id"])
            return res
        return client.put(f"{LINES}/{line_id}", json={"cameras": cameras, **extra}, headers=admin_headers)

    yield save
    for line_id in made:
        client.delete(f"{LINES}/{line_id}", headers=admin_headers)


def _card(camera_id, **extra):
    """A vision camera card with Count mode One picture per trigger."""
    return {"camera_id": camera_id, "count_mode": "trigger", "triggers": [1], **extra}


def _mode(client, headers, camera_id):
    res = client.get(f"{API}/{camera_id}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()["settings"].get("trigger_mode")


def _told(driver, mode):
    return [s for s in driver.sets if s.get("trigger_mode") == mode]


# ── The card sets the mode ────────────────────────────────────────────────────


def test_saving_a_camera_trigger_card_sets_hardware(client, admin_headers, industrial, save_line):
    row, driver = industrial("off")
    res = save_line(_card(row["id"], picture_source="camera"))
    assert res.status_code == 201, res.text
    assert _mode(client, admin_headers, row["id"]) == "hardware"            # the camera row
    assert _told(driver, "hardware")                                        # and the connected driver
    assert _wait_until(lambda: driver.trigger_mode == "hardware")
    assert not res.json().get("warning")


def test_system_trigger_sets_software_and_track_sets_off(client, admin_headers, industrial, save_line):
    row, driver = industrial("hardware")
    res = save_line(_card(row["id"], picture_source="system"))
    assert res.status_code == 201, res.text
    line_id = res.json()["id"]
    assert _mode(client, admin_headers, row["id"]) == "software" and _told(driver, "software")

    # Saved again with nothing changed: the camera is not touched (only a mode that differs is set).
    told = list(driver.sets)
    assert save_line(_card(row["id"], picture_source="system"), line_id=line_id).status_code == 200
    assert driver.sets == told

    # Any other Count mode lets the camera run freely.
    res = save_line({"camera_id": row["id"], "count_mode": "track"}, line_id=line_id)
    assert res.status_code == 200, res.text
    assert _mode(client, admin_headers, row["id"]) == "off" and _told(driver, "off")
    assert _wait_until(lambda: driver.trigger_mode == "off")


def test_a_text_reader_card_sets_the_trigger_as_a_vision_card_does(client, admin_headers, industrial, save_line):
    """Review C2: a text reader on One picture per trigger left an industrial camera Off, so it never read."""
    row, driver = industrial("off")
    ocr = {"model": "small", "fields": [{"name": "Lot", "find": "all", "format": "LOT A99999"}]}
    res = save_line(_card(row["id"], role="ocr", picture_source="camera", ocr=ocr))
    assert res.status_code == 201, res.text
    assert _mode(client, admin_headers, row["id"]) == "hardware" and _told(driver, "hardware")
    line_id = res.json()["id"]
    res = save_line(_card(row["id"], role="ocr", picture_source="system", ocr=ocr), line_id=line_id)
    assert res.status_code == 200, res.text
    assert _mode(client, admin_headers, row["id"]) == "software" and _told(driver, "software")


def test_a_trigger_card_without_a_picture_source_follows_the_cameras_mode(client, admin_headers, industrial, save_line):
    # A card saved before it held the picture source: a Hardware camera stays Hardware, any other is the system's.
    hardware, hardware_driver = industrial("hardware", source="A1")
    free, free_driver = industrial("off", source="B2")
    res = save_line(_card(hardware["id"]), _card(free["id"]))
    assert res.status_code == 201, res.text
    assert _mode(client, admin_headers, hardware["id"]) == "hardware" and hardware_driver.sets == []
    assert _mode(client, admin_headers, free["id"]) == "software" and _told(free_driver, "software")


def test_the_mode_is_set_before_the_line_is_applied(industrial, save_line, monkeypatch):
    row, driver = industrial("off")
    seen = []
    apply_line = lines_route.apply_line

    async def spy(line_id=None):
        seen.append(list(driver.sets))
        return await apply_line(line_id)

    monkeypatch.setattr(lines_route, "apply_line", spy)
    res = save_line(_card(row["id"], picture_source="camera"))
    assert res.status_code == 201, res.text
    assert len(seen) == 1 and [s["trigger_mode"] for s in seen[0]] == ["hardware"]    # told before the line was applied
    assert save_line(_card(row["id"], picture_source="system"), line_id=res.json()["id"]).status_code == 200
    assert len(seen) == 2 and [s["trigger_mode"] for s in seen[1]] == ["hardware", "software"]    # and when it is changed


def test_a_camera_that_is_not_connected_has_its_saved_mode_set(client, admin_headers, industrial, save_line):
    row, _ = industrial("off", connect=False)
    assert save_line(_card(row["id"], picture_source="camera")).status_code == 201
    assert _mode(client, admin_headers, row["id"]) == "hardware"


def test_qr_role_camera_keeps_its_mode(client, admin_headers, industrial, save_line):
    row, driver = industrial("software")
    res = save_line({"camera_id": row["id"], "role": "qr", "qr_trigger": "signal"})
    assert res.status_code == 201, res.text
    assert _mode(client, admin_headers, row["id"]) == "software" and driver.sets == []
    # Saved again as it is: still its own.
    assert save_line({"camera_id": row["id"], "role": "qr", "qr_trigger": "signal"},
                     line_id=res.json()["id"]).status_code == 200
    assert _mode(client, admin_headers, row["id"]) == "software" and driver.sets == []


def _reader(camera_id, **extra):
    """A vision camera that also reads codes, when an input asks (it takes its picture on a trigger)."""
    return {"camera_id": camera_id, "read_codes": True, "qr_trigger": "signal", "triggers": [1], **extra}


def test_a_tracking_camera_keeps_its_hardware_trigger_and_is_not_locked(client, admin_headers, industrial, save_line):
    # Count mode track on a Hardware camera is a supported setup: the card does not control the Trigger.
    row, driver = industrial("hardware")
    res = save_line({"camera_id": row["id"], "count_mode": "track"})
    assert res.status_code == 201, res.text
    assert _mode(client, admin_headers, row["id"]) == "hardware" and driver.sets == []
    # Saved again, and with no Count mode sent at all: still its own.
    assert save_line({"camera_id": row["id"]}, line_id=res.json()["id"]).status_code == 200
    assert _mode(client, admin_headers, row["id"]) == "hardware" and driver.sets == []
    assert client.get(f"{API}/{row['id']}", headers=admin_headers).json()["line_trigger_mode"] is None
    changed = client.patch(f"{API}/{row['id']}", json={"settings": {"trigger_mode": "off"}}, headers=admin_headers)
    assert changed.status_code == 200 and changed.json()["settings"]["trigger_mode"] == "off", changed.text


def test_a_code_reader_on_a_signal_keeps_its_software_trigger_and_is_not_locked(client, admin_headers, industrial, save_line):
    row, driver = industrial("software")
    res = save_line(_reader(row["id"]))
    assert res.status_code == 201, res.text
    assert res.json()["cameras"][0]["qr_trigger"] == "signal"
    assert _mode(client, admin_headers, row["id"]) == "software" and driver.sets == []
    assert client.get(f"{API}/{row['id']}", headers=admin_headers).json()["line_trigger_mode"] is None
    changed = client.patch(f"{API}/{row['id']}", json={"settings": {"trigger_mode": "hardware"}}, headers=admin_headers)
    assert changed.status_code == 200, changed.text


def test_a_software_camera_nothing_triggers_is_set_off_and_is_not_locked(client, admin_headers, industrial, save_line):
    row, driver = industrial("software")
    res = save_line({"camera_id": row["id"], "count_mode": "track"})
    assert res.status_code == 201, res.text
    assert _mode(client, admin_headers, row["id"]) == "off" and _told(driver, "off")
    assert client.get(f"{API}/{row['id']}", headers=admin_headers).json()["line_trigger_mode"] is None


def test_a_camera_that_reads_codes_keeps_its_software_trigger_whatever_it_reads_on(client, admin_headers, industrial, save_line):
    # A reader at a wire line is fired by the counting camera's crossing through take_picture (Ruling 16).
    for source, qr_trigger in (("B2", "continuous"), ("B3", "line1"), ("B4", "line2")):
        row, driver = industrial("software", source=source)
        res = save_line({"camera_id": row["id"], "count_mode": "track", "read_codes": True, "qr_trigger": qr_trigger})
        assert res.status_code == 201, res.text
        assert _mode(client, admin_headers, row["id"]) == "software" and driver.sets == [], qr_trigger
        assert client.get(f"{API}/{row['id']}", headers=admin_headers).json()["line_trigger_mode"] is None


def test_a_camera_off_stays_off_whatever_the_card(client, admin_headers, industrial, save_line):
    row, driver = industrial("off")
    assert save_line({"camera_id": row["id"], "count_mode": "track"}).status_code == 201
    assert _mode(client, admin_headers, row["id"]) == "off" and driver.sets == []


@pytest.mark.parametrize("card, current, sets, locks", [
    # A card with One picture per trigger controls the Trigger.
    ({"count_mode": "trigger", "picture_source": "camera"}, "off", "hardware", True),
    ({"count_mode": "trigger", "picture_source": "system"}, "hardware", "software", True),
    ({"count_mode": "trigger"}, "hardware", "hardware", True),
    # Any other vision card leaves the Trigger alone...
    ({"count_mode": "track"}, "hardware", None, False),
    ({}, "hardware", None, False),
    ({"read_codes": True, "qr_trigger": "signal"}, "software", None, False),
    ({"read_codes": True, "qr_trigger": "signal"}, "hardware", None, False),
    ({"count_mode": "track"}, "off", None, False),
    # ...also a camera that reads codes, on any trigger (a reader at a wire line is fired by the counting camera)...
    ({"count_mode": "track", "read_codes": True, "qr_trigger": "line1"}, "software", None, False),
    ({"count_mode": "track", "read_codes": True, "qr_trigger": "line2"}, "software", None, False),
    ({"read_codes": True, "qr_trigger": "continuous"}, "software", None, False),
    # ...except Software that nothing on the card triggers and that reads no codes.
    ({"count_mode": "track"}, "software", "off", False),
    ({}, "software", "off", False),
    # A QR reader keeps its own.
    ({"role": "qr", "qr_trigger": "signal"}, "software", None, False),
    ({"role": "qr", "qr_trigger": "continuous"}, "hardware", None, False),
    ({"role": "qr", "count_mode": "trigger"}, "off", None, False),
])
def test_what_a_save_sets_for_each_kind_of_card(card, current, sets, locks):
    from app.services.line_config import locks_trigger_mode, trigger_mode_to_set

    card = {"role": "vision", **card}
    assert trigger_mode_to_set(card, current) == sets
    assert locks_trigger_mode(card) is locks


def test_a_line_saved_without_cameras_leaves_the_cameras_alone(client, admin_headers, industrial, save_line):
    row, driver = industrial("off")
    res = save_line(_card(row["id"], picture_source="camera"))
    assert res.status_code == 201, res.text
    told = list(driver.sets)
    renamed = client.put(f"{LINES}/{res.json()['id']}", json={"name": "Renamed card line"}, headers=admin_headers)
    assert renamed.status_code == 200, renamed.text
    assert driver.sets == told and _mode(client, admin_headers, row["id"]) == "hardware"


# ── Camera trigger needs an industrial camera ─────────────────────────────────


@pytest.mark.parametrize("kind, source", [("ip", "rtsp://127.0.0.1:8554/stream"), ("usb", "7")])
def test_camera_trigger_on_an_ip_camera_is_refused(client, admin_headers, camera_row, save_line, kind, source):   # noqa: F811
    other = camera_row(type=kind, source=source)
    res = save_line(_card(other["id"], picture_source="camera"))
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail.startswith("Camera 1") and ONLY_INDUSTRIAL in detail, detail
    names = [line["name"] for line in client.get(LINES, headers=admin_headers).json()["lines"]]
    assert "Card line 0" not in names                                       # nothing was saved
    # A card the system triggers is fine on any camera.
    assert save_line(_card(other["id"], picture_source="system")).status_code == 201


def test_the_refusal_names_the_card_by_its_place_on_the_line(industrial, camera_row, save_line):   # noqa: F811
    row, driver = industrial("off")
    usb = camera_row(type="usb", source="7", width=640)
    res = save_line(_card(row["id"], picture_source="camera"), _card(usb["id"], picture_source="camera"))
    assert res.status_code == 422 and res.json()["detail"].startswith("Camera 2"), res.text
    assert driver.sets == []                                                # refused before anything is set


def test_camera_trigger_is_also_refused_when_the_line_is_changed(client, admin_headers, camera_row, save_line):   # noqa: F811
    usb = camera_row(type="usb", source="7", width=640)
    res = save_line(_card(usb["id"], picture_source="system"))
    assert res.status_code == 201, res.text
    refused = save_line({"camera_id": usb["id"], "picture_source": "camera"}, line_id=res.json()["id"])
    assert refused.status_code == 422 and ONLY_INDUSTRIAL in refused.json()["detail"], refused.text
    kept = client.get(f"{LINES}/{res.json()['id']}", headers=admin_headers).json()["cameras"][0]
    assert kept["picture_source"] == "system"


# ── The camera's own Settings may not change the mode ─────────────────────────


def test_camera_patch_against_the_card_is_refused(client, admin_headers, industrial, save_line):
    row, driver = industrial("off")
    assert save_line(_card(row["id"], picture_source="camera")).status_code == 201
    url = f"{API}/{row['id']}"
    told = list(driver.sets)

    for method in (client.patch, client.put):
        res = method(url, json={"settings": {"trigger_mode": "software"}}, headers=admin_headers)
        assert res.status_code == 409 and res.json()["detail"] == NOTE, (method.__name__, res.text)
        res = method(url, json={"settings": {"trigger_mode": "Off", "trigger_line": "Line1"}}, headers=admin_headers)
        assert res.status_code == 409, (method.__name__, res.text)
    assert _mode(client, admin_headers, row["id"]) == "hardware" and driver.sets == told

    # Only the mode is locked: the rest of a Hardware trigger stays in the camera's Settings,
    # and the card's own mode, sent again with the form, is not a change.
    res = client.patch(url, json={"settings": {"trigger_line": "Line1", "trigger_edge": "falling"}}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["trigger_edge"] == "falling"
    res = client.patch(url, json={"settings": {"trigger_mode": "hardware", "trigger_delay_us": 300}}, headers=admin_headers)
    assert res.status_code == 200 and res.json()["settings"]["trigger_delay_us"] == 300, res.text
    assert client.patch(url, json={"name": "Renamed"}, headers=admin_headers).status_code == 200


def test_a_camera_no_vision_card_controls_may_change_its_mode(client, admin_headers, industrial, save_line):
    free, _ = industrial("off", source="A1")
    reader, _ = industrial("off", source="B2", connect=False)
    assert save_line({"camera_id": reader["id"], "role": "qr", "qr_trigger": "signal"}).status_code == 201
    for row in (free, reader):                                              # on no line, and a QR reader
        res = client.patch(f"{API}/{row['id']}", json={"settings": {"trigger_mode": "software"}}, headers=admin_headers)
        assert res.status_code == 200, res.text
        assert res.json()["line_trigger_mode"] is None


def test_the_camera_list_and_detail_say_the_mode_the_card_sets(client, admin_headers, industrial, save_line):
    hardware, _ = industrial("off", source="A1", connect=False)
    system, _ = industrial("off", source="B2", connect=False)
    alone, _ = industrial("hardware", source="C3", connect=False)
    reader, _ = industrial("software", source="D4", connect=False)
    saved = save_line(_card(hardware["id"], picture_source="camera"), _card(system["id"], picture_source="system"),
                          {"camera_id": reader["id"], "role": "qr", "qr_trigger": "signal"})
    assert saved.status_code == 201, saved.text
    listed = {row["id"]: row for row in client.get(API, headers=admin_headers).json()}
    assert listed[hardware["id"]]["line_trigger_mode"] == "hardware"
    assert listed[system["id"]]["line_trigger_mode"] == "software"
    assert listed[alone["id"]]["line_trigger_mode"] is None                 # on no line
    assert listed[reader["id"]]["line_trigger_mode"] is None                # a QR reader keeps its own
    assert client.get(f"{API}/{system['id']}", headers=admin_headers).json()["line_trigger_mode"] == "software"
    # A card with another Count mode does not control the Trigger any more: the camera is not locked.
    other = save_line({"camera_id": system["id"], "count_mode": "track"}, _card(hardware["id"], picture_source="camera"),
                      {"camera_id": reader["id"], "role": "qr", "qr_trigger": "signal"}, line_id=saved.json()["id"])
    assert other.status_code == 200, other.text
    assert client.get(f"{API}/{system['id']}", headers=admin_headers).json()["line_trigger_mode"] is None
    assert client.get(f"{API}/{hardware['id']}", headers=admin_headers).json()["line_trigger_mode"] == "hardware"


def test_the_camera_list_has_no_line_mode_for_a_camera_that_is_not_industrial(client, admin_headers, camera_row, save_line):   # noqa: F811
    usb = camera_row(type="usb", source="7", width=640)
    assert save_line(_card(usb["id"], picture_source="system")).status_code == 201
    assert client.get(f"{API}/{usb['id']}", headers=admin_headers).json()["line_trigger_mode"] is None


# ── A failure to apply is a warning ───────────────────────────────────────────


def test_failure_to_apply_is_a_warning_not_a_refused_save(client, admin_headers, industrial, save_line):
    # The camera refuses the mode (a strobe on the trigger's own input line); the other camera is still set.
    stuck, _ = industrial("off", source="A1", connect=False, trigger_line="Line1", strobe_line="Line1")
    fine, _ = industrial("off", source="B2", connect=False)
    res = save_line(_card(stuck["id"], picture_source="camera"), _card(fine["id"], picture_source="camera"))
    assert res.status_code == 201, res.text
    warning = res.json()["warning"]
    assert warning.startswith("Camera 1's trigger mode could not be set: ") and "strobe_line" in warning, warning
    assert "Camera 2" not in warning
    assert _mode(client, admin_headers, stuck["id"]) == "off"
    assert _mode(client, admin_headers, fine["id"]) == "hardware"
    saved = client.get(f"{LINES}/{res.json()['id']}", headers=admin_headers).json()      # the line itself is saved
    assert [c["camera_id"] for c in saved["cameras"]] == [stuck["id"], fine["id"]]


def test_any_error_while_applying_is_a_warning(client, admin_headers, industrial, save_line, monkeypatch):
    row, _ = industrial("off", connect=False)

    async def broken(db, camera_id, data):
        raise RuntimeError("the database is busy")

    update_camera = CameraService.__dict__["update_camera"]
    monkeypatch.setattr(CameraService, "update_camera", staticmethod(broken))
    res = save_line(_card(row["id"], picture_source="camera"))
    assert res.status_code == 201, res.text
    assert "Camera 1's trigger mode could not be set: the database is busy" in res.json()["warning"]
    monkeypatch.setattr(CameraService, "update_camera", update_camera)
    changed = save_line(_card(row["id"], picture_source="camera"), line_id=res.json()["id"])
    assert changed.status_code == 200 and not changed.json().get("warning"), changed.text    # applied on the next save
    assert _mode(client, admin_headers, row["id"]) == "hardware"


# ── The camera form shows the mode read-only ──────────────────────────────────

FORM = ROOT / "assets" / "camera_settings.js"
CHECK = """
global.window = {};
global.document = { hidden: false };
require(process.argv[2]);
const form = window.CameraSettingsForm;
const render = (cam) => {
    const box = { innerHTML: "", querySelector: () => null, addEventListener() {} };
    form.render(box, cam, ["image"]);
    return box.innerHTML;
};
const read = (select) => form.read({ querySelector: (q) => (q === '[data-cs="trigger_mode"]' ? select : null) }, "gige");
const settings = { trigger_mode: "software" };
console.log(JSON.stringify({
    free: render({ id: "c1", type: "gige", settings }),
    nulled: render({ id: "c1", type: "gige", settings, line_trigger_mode: null }),
    locked: render({ id: "c1", type: "gige", settings, line_trigger_mode: "hardware" }),
    off: render({ id: "c1", type: "gige", settings: { trigger_mode: "hardware" }, line_trigger_mode: "off" }),
    readFree: read({ value: "software", disabled: false }),
    readLocked: read({ value: "hardware", disabled: true }),
}));
"""


def test_the_camera_form_shows_the_trigger_read_only_when_a_card_sets_it(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "check.js"
    script.write_text(CHECK, encoding="utf-8")
    run = subprocess.run([node, str(script), str(FORM)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)

    select = lambda html: html[html.index('data-cs="trigger_mode"'):html.index("</select>", html.index('data-cs="trigger_mode"'))]
    for html in (out["free"], out["nulled"]):                                # no card sets it: as before
        assert NOTE not in html and "disabled" not in select(html)
        assert select(html).count("<option") == 3 and 'value="software" selected' in select(html)
    assert NOTE in out["locked"] and "disabled" in select(out["locked"])     # a card sets it: read-only, with the note
    assert select(out["locked"]).count("<option") == 1 and 'value="hardware" selected' in select(out["locked"])
    assert 'value="off" selected' in select(out["off"])                      # shows the card's mode, not the camera's

    assert out["readFree"]["trigger_mode"] == "software"
    assert "trigger_mode" not in out["readLocked"]                           # a read-only mode is not sent back
    assert "trigger_line" in out["readLocked"]                               # the rest of the trigger still is
