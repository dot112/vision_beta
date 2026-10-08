"""A camera's rotation and flips turn its picture (in the driver); counting and
drawing then follow the product flow set on the camera, not the rotation."""
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


def test_process_frame_hands_the_tracker_the_cameras_counting_settings():
    from app.schemas.counting import CountingConfig
    from app.services.counting_service import CountingService

    counter = CountingService(dispatch_telemetry=False)
    counter.update_config(CountingConfig(
        orientation="vertical", direction="backward", defect_classes=["dent"],
        name_based_defects=False, min_hits=3, max_speed_pixels=300.0,
    ))
    tracker = _RecordingTracker()
    counter._trackers["cam-rot"] = tracker

    counter.process_frame([], 640, 480, camera_rotation=180, camera_id="cam-rot")

    call = tracker.calls[0]
    assert (call["orientation"], call["direction"]) == ("vertical", "backward")
    assert call["defect_classes"] == ["dent"] and call["name_based_defects"] is False
    assert (call["min_hits"], call["max_speed_pixels"]) == (3, 300.0)


def test_the_drawing_does_not_depend_on_the_camera_rotation(fake_camera):
    from app.engines.inference_engine import InferenceEngine

    # "No rotation" is saved as 0, which used to be read as 90 degrees.
    fake_camera("cam-rot").settings = {"rotation": 0, "flip_h": True}
    engine = InferenceEngine(device="cpu")
    blank = np.zeros((480, 640, 3), dtype=np.uint8)

    def draw(**orientation):
        return engine.draw_annotations_mat(blank.copy(), [], draw_wirelines=False, camera_id="cam-rot", **orientation)

    looked_up = draw()
    assert np.array_equal(looked_up, draw(camera_rotation=90))
    assert np.array_equal(looked_up, draw(camera_rotation=180, camera_flip_h=False, camera_flip_v=True))
    # The default flow is top to bottom: the exit band is along the bottom edge.
    assert np.mean(looked_up[460:480, :]) > 0 and np.mean(looked_up[:, 0:20][:440]) == 0
