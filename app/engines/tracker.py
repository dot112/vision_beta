from __future__ import annotations

import collections
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.schemas.counting import TrackInfo
from app.schemas.vision import DetectionItem
from app.utils.logger import get_logger

logger = get_logger(__name__)





def bbox_iou(box1: Tuple[float, float, float, float], box2: Tuple[float, float, float, float]) -> float:
    """Calculates Intersection-over-Union (IoU) between two bounding boxes [x1, y1, x2, y2]."""
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])

    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if intersection <= 0.0:
        return 0.0

    area1 = max(0.0, box1[2] - box1[0]) * max(0.0, box1[3] - box1[1])
    area2 = max(0.0, box2[2] - box2[0]) * max(0.0, box2[3] - box2[1])
    union = area1 + area2 - intersection
    if union <= 0.0:
        return 0.0
    return float(intersection / union)


def is_vertical_wirelines(orientation: Optional[str]) -> bool:
    """
    Returns True if wirelines are VERTICAL (spanning top-to-bottom on X coordinates),
    which intercepts and counts HORIZONTALLY moving items.
    Returns False if wirelines are HORIZONTAL (spanning left-to-right on Y coordinates),
    which intercepts and counts VERTICALLY moving items.
    """
    s = str(orientation or "").strip().lower()
    return s in ("vertical", "vertical_lines", "vertical_wirelines", "horizontal_movement", "horizontal_mode")

# Alias for backwards compatibility
is_horizontal_movement = is_vertical_wirelines


def get_exit_line_zone(
    w: int,
    h: int,
    rotation: Optional[int] = None,
    flip_h: bool = False,
    flip_v: bool = False,
    thickness: int = 20,
) -> Tuple[str, Tuple[int, int, int, int]]:
    """
    Computes the exit line edge and bounding rectangle (x1, y1, x2, y2)
    based on camera rotation and flipping settings.
    By default:
      - rotation 90 deg -> "bottom" (matches portrait video rotated 90 CW)
      - rotation 0 deg  -> "right"  (matches unrotated video conveyor flow)
      - rotation 180 deg -> "left"
      - rotation 270 deg -> "top"
    """
    rot = int(rotation if rotation is not None else 90)
    diff = (rot - 90) % 360
    steps = (diff // 90) % 4
    EDGES = ["bottom", "left", "top", "right"]
    edge = EDGES[steps]

    if flip_h:
        if edge == "left":
            edge = "right"
        elif edge == "right":
            edge = "left"
    if flip_v:
        if edge == "top":
            edge = "bottom"
        elif edge == "bottom":
            edge = "top"

    if edge == "bottom":
        rect = (0, max(0, h - thickness), w, h)
    elif edge == "top":
        rect = (0, 0, w, min(h, thickness))
    elif edge == "left":
        rect = (0, 0, min(w, thickness), h)
    else:  # right
        rect = (max(0, w - thickness), 0, w, h)

    return edge, rect


def is_in_exit_zone(cx: float, cy: float, edge: str, rect: Tuple[int, int, int, int]) -> bool:
    """Checks if a point (cx, cy) is inside or beyond the exit line zone."""
    if edge == "bottom":
        return cy >= rect[1]
    elif edge == "top":
        return cy <= rect[3]
    elif edge == "left":
        return cx <= rect[2]
    elif edge == "right":
        return cx >= rect[0]
    return False


def compute_association_cost(
    track: TrackedObject,
    det_box: Tuple[float, float, float, float],
    det_cname: str,
    orientation: str,
    direction: str,
    max_speed_pixels: float,
    position_tolerance: float,
) -> float:
    """
    Computes association cost between a predicted track and a candidate detection,
    enforcing conveyor motion constraints (spatial distance, speed gate, direction).
    Returns cost in [0.0, 1.0+] or float('inf') if invalid.
    """
    det_cx = (det_box[0] + det_box[2]) / 2.0
    det_cy = (det_box[1] + det_box[3]) / 2.0

    # 1. Spatial distance gate
    dist = math.hypot(det_cx - track.smooth_center_x, det_cy - track.smooth_center_y)
    if dist > position_tolerance:
        return float("inf")

    # 2. Maximum allowed speed / jump gate
    if dist > max_speed_pixels:
        return float("inf")

    # 3. Conveyor movement direction validation
    dx = det_cx - track.smooth_center_x
    dy = det_cy - track.smooth_center_y
    dir_mode = str(direction).lower()

    if is_horizontal_movement(orientation):
        # Conveyor flows along X axis (horizontal moving items)
        if dir_mode == "forward":
            # Flowing left-to-right (increasing X). Reject detections moving left significantly
            if dx < -15.0:
                return float("inf")
        elif dir_mode == "backward":
            # Flowing right-to-left (decreasing X). Reject detections moving right significantly
            if dx > 15.0:
                return float("inf")
    else:
        # Conveyor flows along Y axis (vertical moving items)
        if dir_mode == "forward":
            # Flowing downwards (increasing Y). Reject detections moving backwards significantly
            if dy < -15.0:
                return float("inf")
        elif dir_mode == "backward":
            # Flowing upwards (decreasing Y). Reject detections moving downwards significantly
            if dy > 15.0:
                return float("inf")

    # 4. Class compatibility
    class_penalty = 0.0
    if det_cname and track.class_name:
        if det_cname.lower() != track.class_name.lower():
            class_penalty = 0.35  # Discourage cross-class switching unless strong spatial match

    # 5. Combined Distance-IoU Cost (enables robust matching for cramped / fast objects)
    norm_dist = min(1.0, dist / max(1.0, position_tolerance))
    iou = bbox_iou(track.last_bbox, det_box)
    if iou > 0.0:
        cost = (1.0 - iou) * 0.70 + norm_dist * 0.30 + class_penalty
    else:
        # Fallback to normalized center distance when no box overlap (within tolerance)
        cost = 0.35 + 0.50 * norm_dist + class_penalty

    return float(cost)


def linear_assignment(
    track_ids: List[int],
    tracks: Dict[int, TrackedObject],
    detections: List[Tuple[Tuple[float, float, float, float], str, float, int, Optional[List[List[int]]]]],
    cost_threshold: float,
    orientation: str,
    direction: str,
    max_speed_pixels: float,
    position_tolerance: float,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """
    Greedy assignment for ByteTrack matching.
    Returns:
        matches: List of (track_id, det_idx)
        unmatched_track_ids: List of track_id
        unmatched_det_indices: List of det_idx
    """
    if not track_ids or not detections:
        return [], list(track_ids), list(range(len(detections)))

    candidates: List[Tuple[float, int, int]] = []
    for tid in track_ids:
        t = tracks[tid]
        for d_idx, (box, cname, conf, cid, poly) in enumerate(detections):
            cost = compute_association_cost(
                track=t,
                det_box=box,
                det_cname=cname,
                orientation=orientation,
                direction=direction,
                max_speed_pixels=max_speed_pixels,
                position_tolerance=position_tolerance,
            )
            if cost <= cost_threshold:
                candidates.append((cost, tid, d_idx))

    # Sort candidate pairs by ascending cost
    candidates.sort(key=lambda x: x[0])

    matches: List[Tuple[int, int]] = []
    matched_tracks = set()
    matched_dets = set()

    for cost, tid, d_idx in candidates:
        if tid in matched_tracks or d_idx in matched_dets:
            continue
        matches.append((tid, d_idx))
        matched_tracks.add(tid)
        matched_dets.add(d_idx)

    unmatched_tracks = [tid for tid in track_ids if tid not in matched_tracks]
    unmatched_dets = [i for i in range(len(detections)) if i not in matched_dets]

    return matches, unmatched_tracks, unmatched_dets


class TrackedObject:
    """
    Pure ByteTrack persistent object tracker.
    - No Kalman, BoT-SORT, Re-ID, or custom filters.
    - Raw bounding box and raw center point maintained separately.
    - Lightweight Exponential Moving Average (EMA) smoothing for tracking point.
    - Velocity estimation via smoothed center displacement.
    - Exposes: track_id, class_id, confidence, bbox, center, smooth_center, age, missed_frames.
    """

    def __init__(
        self,
        track_id: int,
        bbox: Tuple[float, float, float, float],
        class_id: int,
        class_name: str,
        confidence: float,
        polygon: Optional[List[List[int]]] = None,
        min_hits: int = 2,
        ema_alpha: float = 0.45,
    ):
        self.track_id = track_id
        self.class_id = class_id
        self.class_name = class_name
        self.confidence = float(confidence)
        self.polygon = polygon
        self.min_hits = min_hits
        self.ema_alpha = ema_alpha

        # Raw bounding box: (x1, y1, x2, y2)
        self.raw_bbox: Tuple[float, float, float, float] = tuple(float(v) for v in bbox)
        self.last_bbox: Tuple[float, float, float, float] = self.raw_bbox

        # Raw center: center of the tracked bounding box
        x1, y1, x2, y2 = self.raw_bbox
        raw_cx = (x1 + x2) / 2.0
        raw_cy = (y1 + y2) / 2.0
        self.raw_center: Tuple[float, float] = (raw_cx, raw_cy)

        # Lightweight smoothed center point (EMA)
        self.smooth_center_x: float = raw_cx
        self.smooth_center_y: float = raw_cy
        self.velocity_x: float = 0.0
        self.velocity_y: float = 0.0

        # History of smoothed centers (maxlen=40)
        self.smooth_history: collections.deque[Tuple[float, float]] = collections.deque(
            [(raw_cx, raw_cy)], maxlen=40
        )
        self.history: List[Tuple[int, int]] = [(int(round(raw_cx)), int(round(raw_cy)))]

        self.last_seen = time.time()
        self.age: int = 1
        self.hits: int = 1
        self.missed_frames: int = 0
        self.disappeared_frames: int = 0
        self.confirmed: bool = (min_hits <= 1)

        self.crossed_line1: bool = False
        self.crossed_line2: bool = False
        self.counted: bool = False

    @property
    def current_centroid(self) -> Tuple[int, int]:
        return (int(round(self.smooth_center_x)), int(round(self.smooth_center_y)))

    @property
    def center(self) -> Tuple[float, float]:
        return self.raw_center

    @property
    def smooth_center(self) -> Tuple[float, float]:
        return (self.smooth_center_x, self.smooth_center_y)

    @property
    def bbox(self) -> Dict[str, int]:
        return self.current_bbox_dict

    @property
    def current_bbox_dict(self) -> Dict[str, int]:
        x1, y1, x2, y2 = self.last_bbox
        return {
            "x1": int(round(x1)),
            "y1": int(round(y1)),
            "x2": int(round(x2)),
            "y2": int(round(y2)),
        }

    def predict(self) -> Tuple[float, float, float, float]:
        """
        Projects track forward by 1 frame using estimated velocity.
        Maintains tracking persistence across momentary detection loss.
        """
        x1, y1, x2, y2 = self.last_bbox
        pred_box = (
            x1 + self.velocity_x,
            y1 + self.velocity_y,
            x2 + self.velocity_x,
            y2 + self.velocity_y,
        )
        self.last_bbox = pred_box
        self.smooth_center_x += self.velocity_x
        self.smooth_center_y += self.velocity_y
        self.smooth_history.append((self.smooth_center_x, self.smooth_center_y))
        return pred_box

    def update(
        self,
        bbox: Tuple[float, float, float, float],
        class_name: str,
        confidence: float,
        class_id: Optional[int] = None,
        polygon: Optional[List[List[int]]] = None,
    ) -> None:
        """Updates track with an associated YOLO detection observation."""
        self.raw_bbox = tuple(float(v) for v in bbox)
        self.last_bbox = self.raw_bbox

        # 1. Raw center point at the center of the bounding box
        x1, y1, x2, y2 = self.raw_bbox
        raw_cx = (x1 + x2) / 2.0
        raw_cy = (y1 + y2) / 2.0
        self.raw_center = (raw_cx, raw_cy)

        # 2. Lightweight EMA smoothing on center point (minimal latency, low jitter)
        prev_scx = self.smooth_center_x
        prev_scy = self.smooth_center_y
        alpha = self.ema_alpha
        new_scx = alpha * raw_cx + (1.0 - alpha) * prev_scx
        new_scy = alpha * raw_cy + (1.0 - alpha) * prev_scy

        # Velocity estimation
        inst_vx = new_scx - prev_scx
        inst_vy = new_scy - prev_scy
        self.velocity_x = 0.6 * inst_vx + 0.4 * self.velocity_x
        self.velocity_y = 0.6 * inst_vy + 0.4 * self.velocity_y

        self.smooth_center_x = new_scx
        self.smooth_center_y = new_scy
        self.smooth_history.append((new_scx, new_scy))
        self.history.append((int(round(new_scx)), int(round(new_scy))))
        if len(self.history) > 40:
            self.history.pop(0)

        self.class_name = class_name
        if class_id is not None:
            self.class_id = class_id
        self.confidence = float(confidence)
        if polygon is not None:
            self.polygon = polygon

        self.age += 1
        self.hits += 1
        if self.hits >= self.min_hits:
            self.confirmed = True
        self.missed_frames = 0
        self.disappeared_frames = 0
        self.last_seen = time.time()

    def mark_missed(self) -> None:
        """Marks track as missed when unassociated in the current frame."""
        self.age += 1
        self.missed_frames += 1
        self.disappeared_frames = self.missed_frames
        # Continue smooth history trail and project bounding box using estimated motion
        self.smooth_center_x += self.velocity_x
        self.smooth_center_y += self.velocity_y
        x1, y1, x2, y2 = self.last_bbox
        self.last_bbox = (
            x1 + self.velocity_x,
            y1 + self.velocity_y,
            x2 + self.velocity_x,
            y2 + self.velocity_y,
        )
        self.smooth_history.append((self.smooth_center_x, self.smooth_center_y))
        self.history.append((int(round(self.smooth_center_x)), int(round(self.smooth_center_y))))
        if len(self.history) > 40:
            self.history.pop(0)

    def to_track_info(self) -> TrackInfo:
        return TrackInfo(
            track_id=self.track_id,
            class_id=self.class_id,
            class_name=self.class_name,
            confidence=round(float(self.confidence), 4),
            bbox=self.current_bbox_dict,
            center={"cx": round(self.raw_center[0], 2), "cy": round(self.raw_center[1], 2)},
            smooth_center={"cx": round(self.smooth_center_x, 2), "cy": round(self.smooth_center_y, 2)},
            smooth_center_x=round(float(self.smooth_center_x), 2),
            smooth_center_y=round(float(self.smooth_center_y), 2),
            velocity_x=round(float(self.velocity_x), 2),
            velocity_y=round(float(self.velocity_y), 2),
            age=self.age,
            missed_frames=self.missed_frames,
            confirmed=self.confirmed,
            counted=self.counted,
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.to_track_info().model_dump()


class WirelineTracker:
    """
    Multi-Object Tracker optimized for conveyor belts with ByteTrack two-stage association,
    NumPy-based Kalman velocity filtering, motion gating, and smoothed 2-wireline crossing.
    """

    def __init__(
        self,
        track_high_thresh: float = 0.50,
        track_low_thresh: float = 0.15,
        match_threshold: float = 0.70,
        max_missed_frames: int = 15,
        max_speed_pixels: float = 120.0,
        min_hits: int = 2,
        position_tolerance: float = 180.0,
        ema_alpha: float = 0.45,
        max_distance: Optional[float] = None,
        max_disappeared: Optional[int] = None,
    ):
        self.track_high_thresh = track_high_thresh
        self.track_low_thresh = track_low_thresh
        self.match_threshold = match_threshold
        self.max_missed_frames = max_disappeared if max_disappeared is not None else max_missed_frames
        self.max_speed_pixels = max_speed_pixels
        self.min_hits = min_hits
        self.position_tolerance = max_distance if max_distance is not None else position_tolerance
        self.ema_alpha = ema_alpha

        # Legacy aliases
        self.max_distance = self.position_tolerance
        self.max_disappeared = self.max_missed_frames

        self.next_track_id = 1
        self.objects: Dict[int, TrackedObject] = {}
        # Wire lines crossed during the last update(): (track_id, class_name, line 1 or 2).
        # A QR reader set to capture on a line crossing listens to these.
        self.line_crossings: List[Tuple[int, str, int]] = []

        # Anti-duplicate guard (fast, 1s/25px):
        # Stops same-object re-track that crosses the line again within 1 second
        # at almost the same pixel position.
        self._recently_counted: collections.deque = collections.deque(maxlen=200)

    def update(
        self,
        detections: List[DetectionItem],
        frame_w: int,
        frame_h: int,
        line1_rel: float,
        line2_rel: float,
        orientation: str = "horizontal",
        direction: str = "forward",
        expected_classes: Optional[List[str]] = None,
        defect_classes: Optional[List[str]] = None,
        track_high_thresh: Optional[float] = None,
        track_low_thresh: Optional[float] = None,
        match_threshold: Optional[float] = None,
        max_missed_frames: Optional[int] = None,
        max_speed_pixels: Optional[float] = None,
        min_hits: Optional[int] = None,
        position_tolerance: Optional[float] = None,
        camera_rotation: Optional[int] = None,
        camera_flip_h: Optional[bool] = None,
        camera_flip_v: Optional[bool] = None,
    ) -> List[Tuple[int, str, bool, float, Any, Optional[List[List[int]]], Tuple[float, float], Tuple[float, float]]]:
        """
        Updates tracks with new detections via ByteTrack two-stage association,
        advances Kalman filters, and checks smoothed wireline crossings.
        """
        # Dynamic parameter overrides from counting config
        high_thresh = track_high_thresh if track_high_thresh is not None else self.track_high_thresh
        low_thresh = track_low_thresh if track_low_thresh is not None else self.track_low_thresh
        match_thresh = match_threshold if match_threshold is not None else self.match_threshold
        missed_limit = max_missed_frames if max_missed_frames is not None else self.max_missed_frames
        max_spd = max_speed_pixels if max_speed_pixels is not None else self.max_speed_pixels
        min_h = min_hits if min_hits is not None else self.min_hits
        pos_tol = position_tolerance if position_tolerance is not None else self.position_tolerance

        expected_set = set(c.lower() for c in (expected_classes or []))
        defect_set = set(c.lower() for c in (defect_classes or []))
        allowed_set = expected_set | defect_set

        # Calculate absolute pixel coordinates for the two lines
        if is_horizontal_movement(orientation):
            # Counting horizontal moving items -> lines are vertical across X
            line1_pos = int(line1_rel * frame_w)
            line2_pos = int(line2_rel * frame_w)
        else:
            # Counting vertical moving items -> lines are horizontal across Y
            line1_pos = int(line1_rel * frame_h)
            line2_pos = int(line2_rel * frame_h)

        # Exit line threshold: 20px wide zone rotated dynamically according to camera settings
        # Stops detecting objects and deletes box & ID when center reaches this line,
        # preventing IDs from jumping around when objects are cropped out of the frame.
        exit_edge, exit_rect = get_exit_line_zone(
            w=frame_w,
            h=frame_h,
            rotation=camera_rotation,
            flip_h=bool(camera_flip_h),
            flip_v=bool(camera_flip_v),
            thickness=20,
        )

        # 0. Prune any existing tracks that reached the exit line
        for tid in list(self.objects.keys()):
            track = self.objects[tid]
            t_cx, t_cy = track.smooth_center_x, track.smooth_center_y
            r_cx, r_cy = getattr(track, "raw_center", (t_cx, t_cy))
            b_cx = (track.last_bbox[0] + track.last_bbox[2]) / 2.0
            b_cy = (track.last_bbox[1] + track.last_bbox[3]) / 2.0
            if (is_in_exit_zone(t_cx, t_cy, exit_edge, exit_rect) or
                is_in_exit_zone(r_cx, r_cy, exit_edge, exit_rect) or
                is_in_exit_zone(b_cx, b_cy, exit_edge, exit_rect)):
                logger.debug("Deleting track #%d [%s]: reached exit line (%s)", tid, track.class_name, exit_edge)
                del self.objects[tid]

        # 1. Split detections into High Confidence and Low Confidence groups (ByteTrack)
        high_dets: List[Tuple[Tuple[float, float, float, float], str, float, int, Optional[List[List[int]]]]] = []
        low_dets: List[Tuple[Tuple[float, float, float, float], str, float, int, Optional[List[List[int]]]]] = []

        for det in detections:
            cname_lower = det.class_name.lower()
            if allowed_set and cname_lower not in allowed_set:
                continue

            box = det.bbox
            b_coords = (float(box.x1), float(box.y1), float(box.x2), float(box.y2))
            conf = float(det.confidence)
            cid = int(det.class_id)
            poly = getattr(det, "polygon", None)

            if conf >= high_thresh:
                high_dets.append((b_coords, det.class_name, conf, cid, poly))
            elif conf >= low_thresh:
                low_dets.append((b_coords, det.class_name, conf, cid, poly))

        # 2. Advance all existing tracks with Kalman filter prediction
        for track in self.objects.values():
            track.predict()

        active_track_ids = list(self.objects.keys())

        # 3. Stage 1: Associate Active Tracks with High-Confidence Detections
        matches_1, unmatched_tracks_1, unmatched_high_dets = linear_assignment(
            track_ids=active_track_ids,
            tracks=self.objects,
            detections=high_dets,
            cost_threshold=match_thresh,
            orientation=orientation,
            direction=direction,
            max_speed_pixels=max_spd,
            position_tolerance=pos_tol,
        )

        for tid, d_idx in matches_1:
            box, cname, conf, cid, poly = high_dets[d_idx]
            self.objects[tid].update(
                bbox=box,
                class_name=cname,
                confidence=conf,
                class_id=cid,
                polygon=poly,
            )

        # 4. Stage 2: Associate Remaining Unmatched Tracks with Low-Confidence Detections (Recovery)
        matches_2, unmatched_tracks_2, _ = linear_assignment(
            track_ids=unmatched_tracks_1,
            tracks=self.objects,
            detections=low_dets,
            cost_threshold=match_thresh,
            orientation=orientation,
            direction=direction,
            max_speed_pixels=max_spd,
            position_tolerance=pos_tol,
        )

        for tid, d_idx in matches_2:
            box, cname, conf, cid, poly = low_dets[d_idx]
            self.objects[tid].update(
                bbox=box,
                class_name=cname,
                confidence=conf,
                class_id=cid,
                polygon=poly,
            )

        # 5. Handle Unmatched Tracks (advance missed frames and prune stale tracks)
        for tid in unmatched_tracks_2:
            track = self.objects[tid]
            track.mark_missed()
            if track.missed_frames > missed_limit:
                del self.objects[tid]

        # 6. Initialize New Tracks from Unmatched High-Confidence Detections
        # CRITICAL: Do NOT spawn any new tracks for detections whose center is at or past the exit line!
        for d_idx in unmatched_high_dets:
            box, cname, conf, cid, poly = high_dets[d_idx]
            det_cx = (box[0] + box[2]) / 2.0
            det_cy = (box[1] + box[3]) / 2.0
            if is_in_exit_zone(det_cx, det_cy, exit_edge, exit_rect):
                continue  # Stop detecting it / never spawn new tracks in exit zone
            self._register_object(
                bbox=box,
                class_id=cid,
                class_name=cname,
                confidence=conf,
                polygon=poly,
                min_hits=min_h,
            )

        # Prune all tracks whose center has reached the exit line
        for tid in list(self.objects.keys()):
            track = self.objects[tid]
            t_cx, t_cy = track.smooth_center_x, track.smooth_center_y
            r_cx, r_cy = getattr(track, "raw_center", (t_cx, t_cy))
            b_cx = (track.last_bbox[0] + track.last_bbox[2]) / 2.0
            b_cy = (track.last_bbox[1] + track.last_bbox[3]) / 2.0
            if (is_in_exit_zone(t_cx, t_cy, exit_edge, exit_rect) or
                is_in_exit_zone(r_cx, r_cy, exit_edge, exit_rect) or
                is_in_exit_zone(b_cx, b_cy, exit_edge, exit_rect)):
                logger.debug("Deleting track #%d [%s]: reached exit line (%s)", tid, track.class_name, exit_edge)
                del self.objects[tid]


        # 7. Check Wireline Crossings using Smoothed Center Coordinates
        counted_events: List[Tuple[int, str, bool, float, Any, Optional[List[List[int]]], Tuple[float, float], Tuple[float, float]]] = []
        self.line_crossings = []


        for oid, track in list(self.objects.items()):
            # Only count tracks once, and need at least 2 points in smooth history
            if track.counted or len(track.smooth_history) < 2:
                continue

            prev_pt = track.smooth_history[-2]
            curr_pt = track.smooth_history[-1]
            crossed_before = (track.crossed_line1, track.crossed_line2)

            if is_horizontal_movement(orientation):
                prev_coord = prev_pt[0]  # X (horizontal moving items)
                curr_coord = curr_pt[0]  # X
            else:
                prev_coord = prev_pt[1]  # Y (vertical moving items)
                curr_coord = curr_pt[1]  # Y

            lines_ascending = (line1_pos <= line2_pos)
            dir_mode = str(direction).lower()

            # Detection band: ±10px around each line = 20px total trigger zone
            # (matches the visual translucent 20px band drawn on screen)
            BAND = 10

            crossing_completed = False

            if dir_mode == "forward":
                # Entry gate: Line 1
                if not track.crossed_line1:
                    if lines_ascending:
                        crossed_l1 = ((prev_coord <= line1_pos and curr_coord >= line1_pos) or
                                      ((prev_coord <= line1_pos + BAND) and (curr_coord >= line1_pos - BAND) and (curr_coord > prev_coord)))
                    else:
                        crossed_l1 = ((prev_coord >= line1_pos and curr_coord <= line1_pos) or
                                      ((prev_coord >= line1_pos - BAND) and (curr_coord <= line1_pos + BAND) and (curr_coord < prev_coord)))
                    if crossed_l1:
                        track.crossed_line1 = True
                        logger.info("Track #%d [%s] crossed Line 1 (Entry)", oid, track.class_name)

                # Exit gate: Line 2 (only if Line 1 crossed first)
                if track.crossed_line1 and not track.crossed_line2:
                    if lines_ascending:
                        crossed_l2 = ((prev_coord <= line2_pos and curr_coord >= line2_pos) or
                                      ((prev_coord <= line2_pos + BAND) and (curr_coord >= line2_pos - BAND) and (curr_coord > prev_coord)))
                    else:
                        crossed_l2 = ((prev_coord >= line2_pos and curr_coord <= line2_pos) or
                                      ((prev_coord >= line2_pos - BAND) and (curr_coord <= line2_pos + BAND) and (curr_coord < prev_coord)))
                    if crossed_l2:
                        track.crossed_line2 = True
                        crossing_completed = True

            elif dir_mode == "backward":
                # Entry gate: Line 2
                if not track.crossed_line2:
                    if lines_ascending:
                        crossed_l2 = (prev_coord >= line2_pos - BAND) and (curr_coord <= line2_pos + BAND) and (curr_coord < prev_coord)
                    else:
                        crossed_l2 = (prev_coord <= line2_pos + BAND) and (curr_coord >= line2_pos - BAND) and (curr_coord > prev_coord)
                    if crossed_l2:
                        track.crossed_line2 = True
                        logger.info("Track #%d [%s] crossed Line 2 (Entry)", oid, track.class_name)

                # Exit gate: Line 1 (only if Line 2 crossed first)
                if track.crossed_line2 and not track.crossed_line1:
                    if lines_ascending:
                        crossed_l1 = (prev_coord >= line1_pos - BAND) and (curr_coord <= line1_pos + BAND) and (curr_coord < prev_coord)
                    else:
                        crossed_l1 = (prev_coord <= line1_pos + BAND) and (curr_coord >= line1_pos - BAND) and (curr_coord > prev_coord)
                    if crossed_l1:
                        track.crossed_line1 = True
                        crossing_completed = True

            else:  # "both"
                # Forward crossing
                if not track.crossed_line1 and not track.crossed_line2:
                    if lines_ascending and (prev_coord <= line1_pos + BAND) and (curr_coord >= line1_pos - BAND) and (curr_coord > prev_coord):
                        track.crossed_line1 = True
                    elif not lines_ascending and (prev_coord >= line1_pos - BAND) and (curr_coord <= line1_pos + BAND) and (curr_coord < prev_coord):
                        track.crossed_line1 = True
                    elif lines_ascending and (prev_coord >= line2_pos - BAND) and (curr_coord <= line2_pos + BAND) and (curr_coord < prev_coord):
                        track.crossed_line2 = True
                    elif not lines_ascending and (prev_coord <= line2_pos + BAND) and (curr_coord >= line2_pos - BAND) and (curr_coord > prev_coord):
                        track.crossed_line2 = True

                elif track.crossed_line1 and not track.crossed_line2:
                    if lines_ascending and (prev_coord <= line2_pos + BAND) and (curr_coord >= line2_pos - BAND) and (curr_coord > prev_coord):
                        track.crossed_line2 = True
                        crossing_completed = True
                    elif not lines_ascending and (prev_coord >= line2_pos - BAND) and (curr_coord <= line2_pos + BAND) and (curr_coord < prev_coord):
                        track.crossed_line2 = True
                        crossing_completed = True

                elif track.crossed_line2 and not track.crossed_line1:
                    if lines_ascending and (curr_coord <= line1_pos <= prev_coord):
                        track.crossed_line1 = True
                        crossing_completed = True
                    elif not lines_ascending and (prev_coord <= line1_pos <= curr_coord):
                        track.crossed_line1 = True
                        crossing_completed = True

            if track.crossed_line1 and not crossed_before[0]:
                self.line_crossings.append((oid, track.class_name, 1))
            if track.crossed_line2 and not crossed_before[1]:
                self.line_crossings.append((oid, track.class_name, 2))

            if crossing_completed:
                # Require confirmation (hits >= min_hits or age >= 2 or confirmed) before counting
                if not (track.confirmed or track.hits >= min_h or track.age >= 2):
                    continue

                track.counted = True
                cname_clean = track.class_name.strip().lower()
                is_defect = (
                    cname_clean in defect_set
                    or "defect" in cname_clean
                    or "scratch" in cname_clean
                    or "broken" in cname_clean
                )

                is_expected = (len(expected_set) == 0) or (cname_clean in expected_set) or is_defect

                if is_expected:
                    # Anti-duplicate guard:
                    # Only applies to short-lived unconfirmed re-tracks (hits < min_h and age <= 2).
                    # Established distinct tracks (hits >= min_h or age >= 3) represent legitimate
                    # distinct physical objects (even when tightly cramped side-by-side or touching)
                    # and must NEVER be suppressed.
                    is_duplicate = False
                    if track.hits < min_h and track.age <= 2:
                        _DEDUP_TTL_S = 0.4
                        _DEDUP_RADIUS_PX = 8.0
                        now_t = time.time()

                        while self._recently_counted and (now_t - self._recently_counted[0][0]) > _DEDUP_TTL_S:
                            self._recently_counted.popleft()

                        cx_ev, cy_ev = track.smooth_center_x, track.smooth_center_y
                        is_duplicate = any(
                            math.hypot(cx_ev - _cx, cy_ev - _cy) < _DEDUP_RADIUS_PX
                            for _ts, _cx, _cy in self._recently_counted
                        )

                    if not is_duplicate:
                        now_t = time.time()
                        self._recently_counted.append((now_t, track.smooth_center_x, track.smooth_center_y))
                        counted_events.append((
                            oid,
                            track.class_name,
                            is_defect,
                            track.confidence,
                            track.last_bbox,
                            track.polygon,
                            (track.smooth_center_x, track.smooth_center_y),
                            (track.velocity_x, track.velocity_y),
                        ))
                        logger.info(
                            "Track #%d [%s] COMPLETED 2-WIRELINE CROSSING! (Defect=%s | SmoothCenter=(%.1f, %.1f) | Vel=(%.1f, %.1f))",
                            oid, track.class_name, is_defect, track.smooth_center_x, track.smooth_center_y, track.velocity_x, track.velocity_y
                        )
                    else:
                        logger.debug("Track #%d [%s] crossing SUPPRESSED (duplicate guard)", oid, track.class_name)


        return counted_events


    def _register_object(
        self,
        bbox: Tuple[float, float, float, float],
        class_id: int,
        class_name: str,
        confidence: float,
        polygon: Optional[List[List[int]]] = None,
        min_hits: int = 2,
    ) -> None:
        self.objects[self.next_track_id] = TrackedObject(
            track_id=self.next_track_id,
            bbox=bbox,
            class_id=class_id,
            class_name=class_name,
            confidence=confidence,
            polygon=polygon,
            min_hits=min_hits,
            ema_alpha=self.ema_alpha,
        )
        self.next_track_id += 1
