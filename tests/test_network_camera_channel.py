"""The network camera card of Connections: an IP camera or a GigE Vision camera.

An IP camera is given by its stream URL; a GigE Vision camera by its serial
number (the Scan button finds it). Transport, resolution and buffer size of an
IP camera reach the camera itself when the card is saved with them.
"""
from __future__ import annotations

import pytest

from app.hardware.camera import genicam_backend
from app.hardware.camera.genicam_backend import GenICamUnavailable
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_industrial_camera import FakeBackend
from tests.test_joined_cameras import _run, lines  # noqa: F401  (a fixture)

API = "/api/v1/system/endpoints"


def _save(**channel):
    made = {}

    async def steps():
        made["record"] = dict(await SettingsPersistenceService.add_or_update_endpoint(
            {"name": "Cam", "protocol": "ipcam", "enabled": False, **channel}))

    _run(steps)
    return made["record"]


# ── What is saved ─────────────────────────────────────────────────────────────


def test_a_channel_is_an_ip_camera_unless_it_says_gige(lines):  # noqa: F811
    record = _save(source="rtsp://10.0.0.9/stream1")
    assert record["camera_kind"] == "ip"
    listed = next(c for c in SettingsPersistenceService.get_state()["ip_cameras"] if c["id"] == record["id"])
    assert listed["type"] == "ip"


def test_a_gige_channel_is_given_by_its_serial_number(lines):  # noqa: F811
    record = _save(camera_kind="GigE", source=" A1-00.42 ")
    assert (record["camera_kind"], record["source"]) == ("gige", "A1-00.42")
    listed = next(c for c in SettingsPersistenceService.get_state()["ip_cameras"] if c["id"] == record["id"])
    assert (listed["type"], listed["source"]) == ("gige", "A1-00.42")


@pytest.mark.parametrize("channel, word", [
    ({"camera_kind": "gige", "source": "rtsp://10.0.0.9/stream1"}, "serial number"),
    ({"camera_kind": "gige", "source": "two words"}, "serial number"),
    ({"camera_kind": "gige", "source": "x" * 129}, "serial number"),
    ({"camera_kind": "thermal", "source": "A1"}, "ip.*gige"),
    ({"source": "A1"}, "RTSP"),                                   # an IP camera still needs a stream URL
    ({"source": "rtsp://10.0.0.9/s", "transport": "pigeon"}, "transport"),
    ({"source": "rtsp://10.0.0.9/s", "resolution": "big"}, "resolution"),
    ({"source": "rtsp://10.0.0.9/s", "resolution": "0x0"}, "resolution"),
])
def test_a_wrong_network_camera_is_refused(lines, channel, word):  # noqa: F811
    with pytest.raises(ValueError, match=word):
        _save(**channel)


# ── The camera it becomes ─────────────────────────────────────────────────────


@pytest.fixture
def channel(client, admin_headers):
    """``channel(**fields)`` saves a network camera channel through the API; all are deleted afterwards."""
    made = []

    def save(**fields):
        body = {"name": f"Net cam {len(made)}", "protocol": "ipcam", "enabled": False, **fields}
        res = client.post(API, json=body, headers=admin_headers) if "id" not in fields \
            else client.put(f"{API}/{fields['id']}", json=body, headers=admin_headers)
        assert res.status_code == 200, res.text
        if "id" not in fields:
            made.append(res.json()["id"])
        return res.json()

    yield save
    for endpoint_id in made:
        client.delete(f"{API}/{endpoint_id}", headers=admin_headers)


def _camera(client, admin_headers, camera_id):
    res = client.get(f"/api/v1/cameras/{camera_id}", headers=admin_headers)
    assert res.status_code == 200, res.text
    return res.json()


def test_transport_resolution_and_buffer_reach_the_camera(client, admin_headers, channel):
    saved = channel(source="rtsp://10.0.0.9/stream1", transport="udp", resolution="1280x720", buffer_size=3)
    camera = _camera(client, admin_headers, saved["id"])
    assert camera["type"] == "ip"
    assert {k: camera["settings"].get(k) for k in ("transport", "buffer_size", "width", "height")} == {
        "transport": "udp", "buffer_size": 3, "width": 1280, "height": 720}

    # Saved again without them (a rename): the camera keeps what it has, also what its own card set.
    client.patch(f"/api/v1/cameras/{saved['id']}", json={"settings": {"width": 800, "height": 600, "rotation": 90}}, headers=admin_headers)
    channel(id=saved["id"], name="Renamed", source="rtsp://10.0.0.9/stream1")
    settings = _camera(client, admin_headers, saved["id"])["settings"]
    assert (settings["width"], settings["height"], settings["transport"], settings["rotation"]) == (800, 600, "udp", 90)

    # "Native" takes the size away; the rest of the camera's settings stay.
    channel(id=saved["id"], source="rtsp://10.0.0.9/stream1", transport="tcp", resolution="native", buffer_size=1)
    settings = _camera(client, admin_headers, saved["id"])["settings"]
    assert "width" not in settings and "height" not in settings
    assert (settings["transport"], settings["buffer_size"], settings["rotation"]) == ("tcp", 1, 90)


def test_a_gige_channel_becomes_an_industrial_camera(client, admin_headers, channel):
    saved = channel(camera_kind="gige", source="A1")
    camera = _camera(client, admin_headers, saved["id"])
    assert (camera["type"], camera["source"]) == ("gige", "A1")
    assert "transport" not in camera["settings"] and "width" not in camera["settings"]
    # The same channel turned into an IP camera: the camera follows.
    channel(id=saved["id"], camera_kind="ip", source="http://10.0.0.9:8080/video")
    camera = _camera(client, admin_headers, saved["id"])
    assert (camera["type"], camera["source"]) == ("ip", "http://10.0.0.9:8080/video")


def test_the_test_button_looks_for_a_gige_camera_by_its_serial(client, admin_headers, channel, monkeypatch):
    saved = channel(camera_kind="gige", source="A1")
    monkeypatch.setattr(genicam_backend, "default_backend", lambda: FakeBackend())      # sees A1, model X
    res = client.post(f"{API}/{saved['id']}/test", headers=admin_headers).json()
    assert res["success"] is True and "A1" in res["message"] and "X" in res["message"]

    monkeypatch.setattr(genicam_backend, "default_backend", lambda: FakeBackend(devices=[]))
    res = client.post(f"{API}/{saved['id']}/test", headers=admin_headers).json()
    assert res["success"] is False and "A1" in res["message"]

    def missing():
        raise GenICamUnavailable("Install requirements-industrial.txt")

    monkeypatch.setattr(genicam_backend, "default_backend", missing)
    res = client.post(f"{API}/{saved['id']}/test", headers=admin_headers).json()
    assert res["success"] is False and "requirements-industrial.txt" in res["message"]


# ── One camera, one channel (2026-10-14: a double save made two) ─────────────


def _serial() -> str:
    import uuid
    return f"S{uuid.uuid4().hex[:8]}"


def _cameras_with(client, admin_headers, serial):
    return [c for c in client.get("/api/v1/cameras", headers=admin_headers).json() if c["source"] == serial]


def test_a_second_channel_for_a_cameras_serial_is_refused_and_names_the_first(client, admin_headers, channel):
    serial = _serial()
    first = channel(name="Top camera", camera_kind="gige", source=serial)

    again = client.post(API, json={"name": "Copy", "protocol": "ipcam", "enabled": False, "camera_kind": "gige",
                                   "source": serial}, headers=admin_headers)
    assert again.status_code == 422 and "Top camera" in again.json()["detail"], again.text
    other = channel(camera_kind="gige", source=_serial())
    moved = client.put(f"{API}/{other['id']}", json={"name": "Other", "protocol": "ipcam", "camera_kind": "gige",
                                                     "source": serial}, headers=admin_headers)
    assert moved.status_code == 422 and "Top camera" in moved.json()["detail"], moved.text
    assert [c["id"] for c in _cameras_with(client, admin_headers, serial)] == [first["id"]]


def test_two_saves_of_one_new_camera_at_once_make_one_channel(lines):  # noqa: F811
    import asyncio
    serial = _serial()
    results = []

    async def steps():
        save = SettingsPersistenceService.add_or_update_endpoint
        body = {"name": "Cam", "protocol": "ipcam", "enabled": False, "camera_kind": "gige", "source": serial}
        results.extend(await asyncio.gather(save(dict(body)), save(dict(body)), return_exceptions=True))

    _run(steps)
    assert sorted(type(r).__name__ for r in results) == ["ValueError", "dict"]
    channels = [ep for ep in SettingsPersistenceService.get_endpoints() if ep.get("source") == serial]
    assert len(channels) == 1


def test_a_deleted_cameras_serial_can_be_added_again(client, admin_headers, channel):
    serial = _serial()
    first = channel(camera_kind="gige", source=serial)
    assert client.delete(f"{API}/{first['id']}", headers=admin_headers).status_code == 200
    assert _cameras_with(client, admin_headers, serial) == []
    again = channel(camera_kind="gige", source=serial)
    assert [c["id"] for c in _cameras_with(client, admin_headers, serial)] == [again["id"]]


def test_a_camera_left_in_the_camera_list_is_taken_over_by_its_new_channel(client, admin_headers, channel):
    serial = _serial()
    made = client.post("/api/v1/cameras", json={"name": "Left behind", "type": "gige", "source": serial,
                                                "settings": {"trigger_mode": "software"}}, headers=admin_headers)
    assert made.status_code == 201, made.text

    saved = channel(name="Top camera", camera_kind="gige", source=serial)
    assert saved["id"] == made.json()["id"]
    [camera] = _cameras_with(client, admin_headers, serial)
    assert (camera["id"], camera["name"], camera["settings"]["trigger_mode"]) == (saved["id"], "Top camera", "software")


def test_deleting_a_channel_removes_every_camera_an_old_double_save_left(client, admin_headers, channel, monkeypatch):
    from app.services.camera_service import CameraService
    serial = _serial()
    first = channel(camera_kind="gige", source=serial)
    # What a double save before this fix left behind: a second channel and camera with the same serial.
    state = SettingsPersistenceService.get_state()
    twin = {**next(ep for ep in state["communication_endpoints"] if ep["id"] == first["id"]), "id": "ipcam-twin0001", "name": "Twin"}
    state["communication_endpoints"].append(twin)
    state["ip_cameras"].append({"id": twin["id"], "name": "Twin", "type": "gige", "source": serial,
                                "is_active": False, "auto_connect": False})
    client.portal.call(CameraService.sync_ip_cameras)
    assert len(_cameras_with(client, admin_headers, serial)) == 2

    assert client.delete(f"{API}/{first['id']}", headers=admin_headers).status_code == 200
    assert _cameras_with(client, admin_headers, serial) == []
    monkeypatch.setattr(genicam_backend, "default_backend",
                        lambda: FakeBackend(devices=[{"serial": serial, "model": "X", "vendor": "V", "interface": "GigE"}]))
    [row] = client.get("/api/v1/cameras/discover/gige", headers=admin_headers).json()
    assert row["in_use"] is False


def test_the_camera_list_refuses_a_second_camera_with_the_same_serial(client, admin_headers):
    serial = _serial()
    body = {"name": "Line cam", "type": "gige", "source": serial, "settings": {}}
    first = client.post("/api/v1/cameras", json=body, headers=admin_headers)
    assert first.status_code == 201, first.text
    try:
        second = client.post("/api/v1/cameras", json={**body, "name": "Again"}, headers=admin_headers)
        assert second.status_code == 422 and "Line cam" in second.json()["detail"], second.text
    finally:
        client.delete(f"/api/v1/cameras/{first.json()['id']}", headers=admin_headers)


def test_the_scan_names_the_channel_that_has_a_camera(client, admin_headers, channel, monkeypatch):
    used, loose, free = _serial(), _serial(), _serial()
    channel(name="Top camera", camera_kind="gige", source=used)
    made = client.post("/api/v1/cameras", json={"name": "Loose", "type": "gige", "source": loose, "settings": {}},
                       headers=admin_headers).json()
    try:
        monkeypatch.setattr(genicam_backend, "default_backend", lambda: FakeBackend(devices=[
            {"serial": s, "model": "X", "vendor": "V", "interface": "GigE"} for s in (used, loose, free)]))
        rows = {r["serial"]: r for r in client.get("/api/v1/cameras/discover/gige", headers=admin_headers).json()}
        assert (rows[used]["in_use"], rows[used]["channel"]) == (True, "Top camera")
        assert (rows[loose]["in_use"], rows[loose]["channel"]) == (True, None)     # a new channel takes it over
        assert (rows[free]["in_use"], rows[free]["channel"]) == (False, None)
    finally:
        client.delete(f"/api/v1/cameras/{made['id']}", headers=admin_headers)


def test_the_connections_page_offers_both_camera_types(client):
    page = client.get("/dashboard").text
    for field in ("ipcamKindInput", "ipcamSourceInput", "ipcamUserInput", "ipcamPassInput", "ipcamTransportInput",
                  "ipcamResolutionInput", "ipcamBufferInput", "ipcamSerialInput", "ipcamScanBtn", "ipcamIpFields", "ipcamGigeFields"):
        assert f'id="{field}"' in page, field
    assert '<option value="ip">' in page and '<option value="gige">' in page
    assert "/api/v1/cameras/discover/gige" in page and "camera_kind" in page
    assert '<option value="native" selected>' in page          # a new camera keeps its own picture size
