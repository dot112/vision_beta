"""Callers that pass no rotation/flip get them from the camera's own settings."""
from __future__ import annotations

import numpy as np


class _RecordingTracker:
    """Stands in for WirelineTracker and keeps the arguments of each update."""

    def __init__(self):
        self.objects = {}
        self._recently_counted = set()
        self.calls = []

    def update(self, **kwargs):
        self.calls.append(kwargs)
        return []


def _orientation(call):
    return call["camera_rotation"], call["camera_flip_h"], call["camera_flip_v"]


def test_process_frame_uses_the_cameras_rotation_and_flips(fake_camera):
    from app.services.counting_service import CountingService

    fake_camera("cam-rot").settings = {"rotation": 180, "flip_h": True}
    counter = CountingService(dispatch_telemetry=False)
    tracker = _RecordingTracker()
    counter._trackers["cam-rot"] = tracker

    counter.process_frame([], 640, 480, camera_id="cam-rot")
    # A caller that passes a rotation is left alone.
    counter.process_frame([], 640, 480, camera_rotation=90, camera_id="cam-rot")
    # A flip the caller passes is kept.
    counter.process_frame([], 640, 480, camera_flip_h=False, camera_id="cam-rot")

    assert _orientation(tracker.calls[0]) == (180, True, False)
    assert _orientation(tracker.calls[1]) == (90, None, None)
    assert _orientation(tracker.calls[2]) == (180, False, False)


def test_annotated_frame_uses_the_cameras_rotation_and_flips(fake_camera):
    from app.engines.inference_engine import InferenceEngine

    fake_camera("cam-rot").settings = {"rotation": 180, "flip_h": True}
    engine = InferenceEngine(device="cpu")
    blank = np.zeros((480, 640, 3), dtype=np.uint8)

    def draw(**orientation):
        return engine.draw_annotations_mat(blank.copy(), [], draw_wirelines=False, camera_id="cam-rot", **orientation)

    looked_up = draw()
    assert np.array_equal(looked_up, draw(camera_rotation=180, camera_flip_h=True, camera_flip_v=False))
    assert not np.array_equal(looked_up, draw(camera_rotation=180, camera_flip_h=False, camera_flip_v=False))
    assert not np.array_equal(looked_up, draw(camera_rotation=90))


def test_orientation_falls_back_to_the_active_camera(fake_camera, monkeypatch):
    from app.services.camera_service import camera_orientation
    from app.services.settings_persistence_service import SettingsPersistenceService

    fake_camera("cam-first").settings = {"rotation": 90}
    fake_camera("cam-active").settings = {"rotation": 270, "flip_v": True}
    SettingsPersistenceService.get_state()  # loads the state the active camera is saved in
    monkeypatch.setitem(SettingsPersistenceService._state, "active_camera_id", "cam-active")

    assert camera_orientation(None) == (270, False, True)
    assert camera_orientation("cam-not-connected") == (270, False, True)
    assert camera_orientation("cam-first") == (90, False, False)
