"""Segmentation masks: worked out inside each object's box only, with the same result as on the whole picture.

The model's 160 x 160 mask used to be scaled to the whole picture once per object (1280 x 720
floats each), which cost as much time as the model itself on the GPU (owner's PC, 2026-10-14).
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.engines.inference_engine import InferenceEngine


def _reference(coeff, protos, box, orig_w, orig_h):
    """The mask, area and outline as worked out before: on the whole picture."""
    bx1, by1, bx2, by2 = box
    proto_h, proto_w = protos.shape[1], protos.shape[2]
    mask_raw = np.matmul(coeff, protos.reshape(32, -1)).reshape(proto_h, proto_w)
    mask_sigmoid = 1.0 / (1.0 + np.exp(-mask_raw))
    mask_resized = cv2.resize(mask_sigmoid, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)
    cropped = np.zeros((orig_h, orig_w), dtype=np.uint8)
    cropped[by1:by2, bx1:bx2] = (mask_resized[by1:by2, bx1:bx2] > 0.5).astype(np.uint8)
    area = int(np.sum(cropped))
    contours, _ = cv2.findContours(cropped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polygon = None
    if contours:
        largest = max(contours, key=cv2.contourArea)
        if len(largest) >= 3:
            polygon = [[int(pt[0][0]), int(pt[0][1])] for pt in cv2.approxPolyDP(largest, 1.5, True)]
    return polygon, area


def _scene(seed, objects=6):
    """Protos with one round blob per channel, and objects that each pick one blob, as a YOLO-seg model gives."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:160, 0:160].astype(np.float32)
    protos = np.zeros((32, 160, 160), np.float32)
    centres = rng.uniform(25, 135, (32, 2))
    radii = rng.uniform(8, 24, 32)
    for k in range(31):
        protos[k] = np.exp(-(((xx - centres[k, 0]) ** 2 + (yy - centres[k, 1]) ** 2) / (2 * radii[k] ** 2)))
    protos[31] = 1.0                                 # a constant channel: the bias that puts the mask's edge
    picks = rng.choice(31, objects, replace=False)   # where the blob is at half its height
    coeffs = np.zeros((objects, 32), np.float32)
    coeffs[np.arange(objects), picks] = 9.0
    coeffs[:, 31] = -4.5
    return protos, coeffs, centres[picks], radii[picks]


def _box(centre, radius, orig_w, orig_h, margin=1.25):
    sx, sy = orig_w / 160.0, orig_h / 160.0
    x1 = int(max(0, (centre[0] - margin * radius) * sx))
    y1 = int(max(0, (centre[1] - margin * radius) * sy))
    x2 = int(min(orig_w, (centre[0] + margin * radius) * sx))
    y2 = int(min(orig_h, (centre[1] + margin * radius) * sy))
    return x1, y1, x2, y2


@pytest.mark.parametrize("orig_w, orig_h, seed", [(1280, 720, 0), (720, 1280, 1), (615, 720, 2), (640, 640, 3)])
def test_masks_inside_the_box_match_the_whole_picture(orig_w, orig_h, seed):
    protos, coeffs, centres, radii = _scene(seed)
    boxes = [_box(c, r, orig_w, orig_h, margin) for (c, r), margin in zip(zip(centres, radii), (1.25, 0.8, 1.25, 0.8, 1.5, 1.0))]
    got = InferenceEngine(device="cpu")._instance_masks(coeffs, protos, boxes, orig_w, orig_h)
    assert len(got) == len(boxes)
    for (polygon, area), coeff, box in zip(got, coeffs, boxes):
        ref_polygon, ref_area = _reference(coeff, protos, box, orig_w, orig_h)
        assert area == pytest.approx(ref_area, rel=0.01, abs=4), box
        assert (polygon is None) == (ref_polygon is None)
        if polygon:
            got_box = cv2.boundingRect(np.array(polygon, np.int32))
            ref_box = cv2.boundingRect(np.array(ref_polygon, np.int32))
            assert np.allclose(got_box, ref_box, atol=2), (got_box, ref_box)


def test_an_empty_or_flat_box_has_no_mask():
    protos, coeffs, _, _ = _scene(4, objects=2)
    got = InferenceEngine(device="cpu")._instance_masks(coeffs, protos, [(10, 10, 10, 50), (5, 5, 60, 5)], 640, 480)
    assert got == [(None, None), (None, None)]


def test_a_model_with_masks_gives_each_object_its_outline():
    # End to end through the post-processing of an end-to-end model (YOLO26-seg: rows of box, score, class, 32 coefficients).
    protos, coeffs, centres, radii = _scene(5, objects=3)
    orig_w, orig_h = 1280, 720
    rows = []
    for coeff, centre, radius in zip(coeffs, centres, radii):
        x1, y1, x2, y2 = _box(centre, radius, 640, 640)            # in the model's 640 x 640 input
        rows.append([x1, y1, x2, y2, 0.9, 0, *coeff])
    output = np.array([rows + [[0] * 38] * 297], np.float32)          # 300 rows, the rest empty
    engine = InferenceEngine(device="cpu")
    engine.classes = ["apple"]
    detections = engine._postprocess_fast(output, orig_w, orig_h, 0.3, 0.65, protos=protos[None])
    assert len(detections) == 3
    for det in detections:
        assert det.polygon and len(det.polygon) >= 3 and det.mask_area > 0
        x, y, w, h = cv2.boundingRect(np.array(det.polygon, np.int32))
        assert det.bbox.x1 - 1 <= x and x + w <= det.bbox.x2 + 1
