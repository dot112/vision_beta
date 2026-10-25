"""A code-reading camera's image processing settings, saved with its line (Line setup)."""
from __future__ import annotations

import copy

import pytest

from app.engines.code_processing import DEFAULT_PROCESSING
from app.services.line_config import normalize_camera


@pytest.fixture
def new_line(client, admin_headers):
    created = []

    def make(name, **extra):
        res = client.post("/api/v1/lines", json={"name": name, **extra}, headers=admin_headers)
        assert res.status_code == 201, res.text
        created.append(res.json()["id"])
        return res.json()

    yield make
    for line_id in created:
        client.delete(f"/api/v1/lines/{line_id}", headers=admin_headers)


def _cameras(line):
    return {c["camera_id"]: c for c in line["cameras"]}


def test_saved_with_defaults_filled(new_line):
    line = new_line("Processing 1", cameras=[{"camera_id": "cam-proc-1", "role": "qr",
                                              "code_processing": {"enabled": True, "grayscale": {"on": True}}}])
    expected = copy.deepcopy(DEFAULT_PROCESSING)
    expected["enabled"] = True
    expected["grayscale"]["on"] = True
    assert _cameras(line)["cam-proc-1"]["code_processing"] == expected


def test_bad_value_is_422_with_the_camera_number(client, admin_headers, new_line):
    line = new_line("Processing 2", cameras=[{"camera_id": "cam-proc-2a", "role": "qr"}])
    res = client.put(f"/api/v1/lines/{line['id']}", headers=admin_headers, json={"cameras": [
        {"camera_id": "cam-proc-2a", "role": "qr"},
        {"camera_id": "cam-proc-2b", "role": "qr", "code_processing": {"scale": {"factor": 9}}},
    ]})
    assert res.status_code == 422, res.text
    assert res.json()["detail"].startswith("Camera 2 image processing: the scale factor must be 0.25 to 4")


def test_absent_is_not_stored(new_line):
    line = new_line("Processing 3", cameras=[{"camera_id": "cam-proc-3", "role": "qr"}])
    assert "code_processing" not in _cameras(line)["cam-proc-3"]
    assert "code_processing" not in normalize_camera({"camera_id": "c", "role": "qr"}, 0)


def test_kept_when_a_client_leaves_it_out(client, admin_headers, new_line):
    line = new_line("Processing 4", cameras=[{"camera_id": "cam-proc-4", "role": "qr",
                                              "code_processing": {"enabled": True, "sharpen": {"on": True, "strength": 7}}}])
    res = client.put(f"/api/v1/lines/{line['id']}", headers=admin_headers,
                     json={"cameras": [{"camera_id": "cam-proc-4", "role": "qr"}]})
    assert res.status_code == 200, res.text
    assert _cameras(res.json())["cam-proc-4"]["code_processing"]["sharpen"] == {"on": True, "strength": 7}


def test_dropped_when_the_camera_stops_reading_codes(client, admin_headers, new_line, vision_model):
    line = new_line("Processing 5", cameras=[{"camera_id": "cam-proc-5", "model_id": vision_model, "read_codes": True,
                                              "code_processing": {"enabled": True}}])
    assert _cameras(line)["cam-proc-5"]["code_processing"]["enabled"] is True
    res = client.put(f"/api/v1/lines/{line['id']}", headers=admin_headers,
                     json={"cameras": [{"camera_id": "cam-proc-5", "model_id": vision_model}]})
    assert res.status_code == 200, res.text
    assert "code_processing" not in _cameras(res.json())["cam-proc-5"]
