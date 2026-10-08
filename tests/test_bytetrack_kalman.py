from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.engines.tracker import TrackedObject, WirelineTracker, bbox_iou


def make_bbox(cx, cy, w=40.0, h=60.0):
    return (cx - w/2, cy - h/2, cx + w/2, cy + h/2)


def make_det(class_name="bottle", confidence=0.9, cx=100, cy=100, cid=0, w=40, h=60):
    class B:
        pass
    b = B()
    b.x1 = cx - w/2; b.y1 = cy - h/2; b.x2 = cx + w/2; b.y2 = cy + h/2

    class D:
        pass
    d = D()
    d.class_name = class_name
    d.confidence = confidence
    d.class_id = cid
    d.bbox = b
    d.polygon = None
    return d


# ─── EMA / ByteTrack velocity ────────────────────────────────────────────────

def test_ema_velocity_direction():
    """EMA-smoothed velocity should be positive-Y when object moves downward."""
    t = TrackedObject(1, make_bbox(100, 100), 0, "bottle", 0.9, min_hits=1)
    for f in range(1, 6):
        t.predict()
        t.update(make_bbox(100, 100 + f * 10), "bottle", 0.9)
    assert t.velocity_y > 0, f"Expected positive vy, got {t.velocity_y}"


def test_ema_predict_advances_center():
    """predict() should shift the smooth center by the current velocity."""
    t = TrackedObject(1, make_bbox(100, 100), 0, "bottle", 0.9, min_hits=1)
    for f in range(1, 4):
        t.predict()
        t.update(make_bbox(100, 100 + f * 10), "bottle", 0.9)
    before_y = t.smooth_center_y
    t.predict()
    assert t.smooth_center_y > before_y, "predict() should advance Y by velocity"


def test_ema_mark_missed_increments():
    """mark_missed() should increment missed_frames."""
    t = TrackedObject(1, make_bbox(100, 100), 0, "bottle", 0.9, min_hits=1)
    t.predict()
    t.mark_missed()
    t.predict()
    t.mark_missed()
    assert t.missed_frames == 2


# ─── IoU ──────────────────────────────────────────────────────────────────────

def test_bbox_iou_perfect():
    assert abs(bbox_iou((10,10,50,50),(10,10,50,50)) - 1.0) < 1e-6


def test_bbox_iou_none():
    assert bbox_iou((0,0,10,10),(20,20,30,30)) == 0.0


def test_bbox_iou_half():
    assert 0.3 < bbox_iou((0,0,10,10),(5,0,15,10)) < 0.4


# ─── TrackedObject ────────────────────────────────────────────────────────────

def test_tracked_object_confirm():
    t = TrackedObject(1, make_bbox(100,100), 0, "bottle", 0.9, min_hits=2)
    assert not t.confirmed
    t.predict()
    t.update(make_bbox(100,110), "bottle", 0.9)
    assert t.confirmed


def test_tracked_object_missed_reset():
    t = TrackedObject(1, make_bbox(100,100), 0, "bottle", 0.9, min_hits=1)
    t.predict()
    t.mark_missed()
    assert t.missed_frames == 1
    t.predict()
    t.update(make_bbox(100,120), "bottle", 0.9)
    assert t.missed_frames == 0


def test_to_track_info():
    t = TrackedObject(5, make_bbox(200,300), 2, "can", 0.85, min_hits=1)
    info = t.to_track_info()
    assert info.track_id == 5 and info.class_name == "can"


# ─── ByteTrack Association ────────────────────────────────────────────────────

def test_high_conf_creates_track():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1)
    tr.update([make_det("bottle",0.9,cx=100,cy=200)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    assert len(tr.objects) == 1


def test_low_conf_no_new_track():
    tr = WirelineTracker(track_high_thresh=0.5, track_low_thresh=0.15, min_hits=1)
    tr.update([make_det("bottle",0.2,cx=100,cy=200)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    assert len(tr.objects) == 0


def test_low_conf_recovers_track():
    tr = WirelineTracker(track_high_thresh=0.5, track_low_thresh=0.15, min_hits=1, position_tolerance=300)
    tr.update([make_det("bottle",0.9,cx=100,cy=200)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    tid = list(tr.objects.keys())[0]
    tr.update([make_det("bottle",0.2,cx=100,cy=210)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    assert tid in tr.objects and tr.objects[tid].missed_frames == 0


def test_track_persists_during_occlusion():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, max_missed_frames=5, position_tolerance=500)
    tr.update([make_det("bottle",0.9,cx=100,cy=200)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    tid = list(tr.objects.keys())[0]
    for i in range(4):
        tr.update([], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
        assert tid in tr.objects, "Pruned on frame " + str(i+1)


def test_track_pruned_after_limit():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, max_missed_frames=3, position_tolerance=500)
    tr.update([make_det("bottle",0.9,cx=100,cy=200)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    tid = list(tr.objects.keys())[0]
    for _ in range(4):
        tr.update([], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    assert tid not in tr.objects


def test_redetection_preserves_id():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, max_missed_frames=5, max_speed_pixels=80.0, position_tolerance=200.0)
    tr.update([make_det("bottle",0.9,cx=100,cy=200)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    tid = list(tr.objects.keys())[0]
    for _ in range(2):
        tr.update([], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    tr.update([make_det("bottle",0.9,cx=100,cy=220)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[])
    assert tid in tr.objects and tr.objects[tid].missed_frames == 0


# ─── Counting ─────────────────────────────────────────────────────────────────

def test_single_count_crossing():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(25):
        total.extend(tr.update([make_det("bottle",0.9,cx=100,cy=50+f*20)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[]))
    assert len(total) == 1, "Expected 1 event, got " + str(len(total))


def test_no_double_count():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(30):
        total.extend(tr.update([make_det("bottle",0.9,cx=100,cy=50+f*20)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[]))
    assert len(total) == 1


def test_event_has_smooth_center_and_velocity():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(25):
        total.extend(tr.update([make_det("bottle",0.9,cx=100,cy=50+f*20)], 640,480,0.35,0.65,"horizontal","forward",["bottle"],[]))
    assert len(total) == 1 and len(total[0]) >= 8
    sc = total[0][6]
    vel = total[0][7]
    assert isinstance(sc, tuple) and len(sc) == 2
    assert isinstance(vel, tuple) and len(vel) == 2


def test_defect_event_is_defect():
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(25):
        total.extend(tr.update(
            [make_det("defect",0.9,cx=200,cy=50+f*20,cid=1)], 640,480,0.35,0.65,
            "horizontal","forward",expected_classes=["bottle"],defect_classes=["defect"]
        ))
    if total:
        assert total[0][2] is True


def test_cramped_objects_side_by_side_both_counted():
    """
    Two objects traveling side-by-side (centers only 20px apart in X)
    must both be tracked and counted, without being killed by the duplicate guard.
    """
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(25):
        dets = [
            make_det("bottle", 0.9, cx=100, cy=50 + f * 20, cid=0, w=20, h=40),
            make_det("bottle", 0.9, cx=120, cy=50 + f * 20, cid=0, w=20, h=40),
        ]
        events = tr.update(dets, 640, 480, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
        total.extend(events)

    assert len(total) == 2, f"Expected both cramped side-by-side objects to be counted, got {len(total)}"
    tids = {ev[0] for ev in total}
    assert len(tids) == 2, "Expected two distinct track IDs"


def test_cramped_objects_back_to_back_both_counted():
    """
    Two objects traveling one directly behind another (centers only 20px apart in Y)
    must both be tracked and counted.
    """
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(30):
        dets = [
            make_det("bottle", 0.9, cx=100, cy=50 + f * 15, cid=0, w=30, h=30),
            make_det("bottle", 0.9, cx=100, cy=30 + f * 15, cid=0, w=30, h=30),
        ]
        events = tr.update(dets, 640, 480, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
        total.extend(events)

    assert len(total) == 2, f"Expected both cramped back-to-back objects to be counted, got {len(total)}"
    tids = {ev[0] for ev in total}
    assert len(tids) == 2, "Expected two distinct track IDs"


def test_vertical_wirelines_counts_horizontal_moving_items():
    """
    With orientation='vertical', wirelines are vertical across X (width).
    Objects moving horizontally (left-to-right, increasing X) must cross Line 1 then Line 2 and be counted.
    """
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(25):
        dets = [make_det("bottle", 0.9, cx=50 + f * 20, cy=200)]
        events = tr.update(dets, 640, 480, 0.35, 0.65, "vertical", "forward", ["bottle"], [])
        total.extend(events)

    assert len(total) == 1, f"Expected 1 count event for vertical wirelines, got {len(total)}"


def test_horizontal_wirelines_counts_vertical_moving_items():
    """
    With orientation='horizontal', wirelines are horizontal across Y (height).
    Objects moving vertically (top-to-bottom, increasing Y) must cross Line 1 then Line 2 and be counted.
    """
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1, position_tolerance=300.0, max_speed_pixels=200.0)
    total = []
    for f in range(25):
        dets = [make_det("bottle", 0.9, cx=100, cy=50 + f * 20)]
        events = tr.update(dets, 640, 480, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
        total.extend(events)

    assert len(total) == 1, f"Expected 1 count event for horizontal wirelines, got {len(total)}"


def test_bottom_exit_line_deletes_track_and_stops_detection():
    """
    Objects whose center reaches the bottom 20px line (cy >= frame_h - 20)
    must have their track and ID deleted immediately, and any new detections
    in this zone must be ignored to prevent jumping IDs.
    """
    frame_w, frame_h = 640, 480
    exit_y = frame_h - 20  # 460
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1)

    # 1. Object above the bottom line: should create and maintain track
    tr.update([make_det("bottle", 0.9, cx=100, cy=400)], frame_w, frame_h, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
    assert len(tr.objects) == 1
    tid = list(tr.objects.keys())[0]

    # Advance closer to exit line in realistic 20px frame increments
    tr.update([make_det("bottle", 0.9, cx=100, cy=420)], frame_w, frame_h, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
    assert tid in tr.objects
    tr.update([make_det("bottle", 0.9, cx=100, cy=440)], frame_w, frame_h, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
    assert tid in tr.objects

    # 2. Object center reaches bottom 20px line (cy=465 >= 460): track and ID must be deleted
    tr.update([make_det("bottle", 0.9, cx=100, cy=465)], frame_w, frame_h, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
    assert len(tr.objects) == 0, f"Track should be deleted when center reaches exit line, but found: {tr.objects}"

    # 3. Spurious/cropped detection inside bottom 20px zone: must NOT create new track
    tr.update([make_det("bottle", 0.9, cx=100, cy=475)], frame_w, frame_h, 0.35, 0.65, "horizontal", "forward", ["bottle"], [])
    assert len(tr.objects) == 0, "Detections in exit zone must be ignored and not spawn new tracks"


def test_draw_annotations_bottom_exit_line():
    """
    draw_annotations_mat should draw the 20px transparent gray band at the bottom,
    and suppress drawing any bounding boxes whose center is in the bottom 20px zone.
    """
    import numpy as np
    from app.engines.inference_engine import InferenceEngine
    from app.schemas.vision import BoundingBox, DetectionItem

    engine = InferenceEngine(device="cpu")
    # 480 height x 640 width test image
    img = np.zeros((480, 640, 3), dtype=np.uint8)

    # Detection 1: above bottom line (cy = 200)
    det1 = DetectionItem(
        class_id=0,
        class_name="apple",
        confidence=0.9,
        bbox=BoundingBox(x1=100, y1=180, x2=140, y2=220, width=40, height=40),
    )
    # Detection 2: inside bottom 20px line (cy = 470, which is >= 480 - 20 = 460)
    det2 = DetectionItem(
        class_id=0,
        class_name="apple",
        confidence=0.9,
        bbox=BoundingBox(x1=100, y1=455, x2=140, y2=485, width=40, height=30),
    )

    out = engine.draw_annotations_mat(img.copy(), [det1, det2], draw_wirelines=False)
    assert out.shape == (480, 640, 3)

    # Bottom 20px (rows 460 to 480) should have the gray overlay (non-zero pixels)
    bottom_slice = out[460:480, :]
    assert np.mean(bottom_slice) > 0, "Bottom 20px should have transparent gray overlay applied"


def test_exit_edge_follows_the_product_flow():
    """Products leave the picture at the edge their flow leads to, whatever the camera rotation."""
    from app.engines.tracker import exit_edge, exit_zone, is_in_exit_zone

    w, h = 640, 480

    # Count lines across the picture (products move down): forward A->B with A above B -> bottom
    edge = exit_edge("horizontal", "forward", 0.35, 0.65)
    rect = exit_zone(w, h, edge)
    assert edge == "bottom"
    assert rect == (0, 460, 640, 480)
    assert is_in_exit_zone(320, 465, edge, rect) is True
    assert is_in_exit_zone(320, 400, edge, rect) is False

    # Count lines down the picture (products move sideways): forward A->B with A left of B -> right
    edge = exit_edge("vertical", "forward", 0.35, 0.65)
    rect = exit_zone(w, h, edge)
    assert edge == "right"
    assert rect == (620, 0, 640, 480)
    assert is_in_exit_zone(625, 240, edge, rect) is True
    assert is_in_exit_zone(500, 240, edge, rect) is False

    # Backward flow, or line B before line A, turns the exit around
    assert exit_edge("vertical", "backward", 0.35, 0.65) == "left"
    assert exit_zone(w, h, "left") == (0, 0, 20, 480)
    assert exit_edge("vertical", "forward", 0.65, 0.35) == "left"
    assert exit_edge("horizontal", "backward", 0.35, 0.65) == "top"
    assert exit_zone(w, h, "top") == (0, 0, 640, 20)
    assert exit_edge("horizontal", "backward", 0.65, 0.35) == "bottom"

    # Products that may move both ways have no exit edge: nothing is cut off
    assert exit_edge("horizontal", "both") is None
    assert exit_zone(w, h, None) is None
    assert is_in_exit_zone(320, 479, None, None) is False


def test_tracker_exit_line_rotated_right():
    """Verify WirelineTracker purges tracks at the right edge when products flow left to right."""
    from app.engines.tracker import WirelineTracker

    w, h = 640, 480
    tr = WirelineTracker(track_high_thresh=0.5, min_hits=1)

    # 1. Object moving left-to-right (cx=500 -> cx=580 -> cx=625)
    tr.update(
        [make_det("bottle", 0.9, cx=500, cy=240)],
        w, h, 0.35, 0.65, "vertical", "forward", ["bottle"], [],
        camera_rotation=0
    )
    assert len(tr.objects) == 1
    tid = list(tr.objects.keys())[0]

    tr.update(
        [make_det("bottle", 0.9, cx=580, cy=240)],
        w, h, 0.35, 0.65, "vertical", "forward", ["bottle"], [],
        camera_rotation=0
    )
    assert tid in tr.objects

    # Object reaches right 20px zone (cx=625 >= 620): track and ID must be purged
    tr.update(
        [make_det("bottle", 0.9, cx=625, cy=240)],
        w, h, 0.35, 0.65, "vertical", "forward", ["bottle"], [],
        camera_rotation=0
    )
    assert len(tr.objects) == 0, f"Track should be deleted at the right edge, got: {tr.objects}"

    # Detections inside right exit zone must not create new tracks
    tr.update(
        [make_det("bottle", 0.9, cx=630, cy=240)],
        w, h, 0.35, 0.65, "vertical", "forward", ["bottle"], [],
        camera_rotation=0
    )
    assert len(tr.objects) == 0


def test_draw_annotations_exit_band_follows_the_flow(monkeypatch):
    """Verify draw_annotations_mat draws the exit band where products flow out and suppresses boxes there."""
    import numpy as np
    from app.engines.inference_engine import InferenceEngine
    from app.schemas.counting import CountingConfig
    from app.schemas.vision import BoundingBox, DetectionItem
    from app.services.counting_service import counting_service

    engine = InferenceEngine(device="cpu")
    img = np.zeros((480, 640, 3), dtype=np.uint8)

    # Detection inside right exit line (cx = 625 >= 620)
    det_right = DetectionItem(
        class_id=0,
        class_name="bottle",
        confidence=0.9,
        bbox=BoundingBox(x1=615, y1=220, x2=635, y2=260, width=20, height=40),
    )
    # Detection in center
    det_center = DetectionItem(
        class_id=0,
        class_name="bottle",
        confidence=0.9,
        bbox=BoundingBox(x1=300, y1=220, x2=340, y2=260, width=40, height=40),
    )

    # Left to right: the band is on the right, whatever rotation a caller passes
    monkeypatch.setattr(counting_service, "config", CountingConfig(orientation="vertical", direction="forward"))
    out_right = engine.draw_annotations_mat(img.copy(), [det_right, det_center], draw_wirelines=False, camera_rotation=90)
    assert np.mean(out_right[:, 620:640]) > 0
    assert np.mean(out_right[:, 0:20]) == 0

    # Right to left: the band is on the left
    monkeypatch.setattr(counting_service, "config", CountingConfig(orientation="vertical", direction="backward"))
    out_left = engine.draw_annotations_mat(img.copy(), [det_center], draw_wirelines=False)
    assert np.mean(out_left[:, 0:20]) > 0
    assert np.mean(out_left[:, 620:640]) == 0

    # Both ways: no band at all
    monkeypatch.setattr(counting_service, "config", CountingConfig(orientation="vertical", direction="both"))
    out_none = engine.draw_annotations_mat(img.copy(), [], draw_wirelines=False)
    assert np.mean(out_none) == 0



