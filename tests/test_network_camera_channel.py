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


def test_the_connections_page_offers_both_camera_types(client):
    page = client.get("/dashboard").text
    for field in ("ipcamKindInput", "ipcamSourceInput", "ipcamUserInput", "ipcamPassInput", "ipcamTransportInput",
                  "ipcamResolutionInput", "ipcamBufferInput", "ipcamSerialInput", "ipcamScanBtn", "ipcamIpFields", "ipcamGigeFields"):
        assert f'id="{field}"' in page, field
    assert '<option value="ip">' in page and '<option value="gige">' in page
    assert "/api/v1/cameras/discover/gige" in page and "camera_kind" in page
    assert '<option value="native" selected>' in page          # a new camera keeps its own picture size
