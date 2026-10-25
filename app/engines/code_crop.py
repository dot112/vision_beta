"""A code cut out of a picture, straight, with a margin around it: the picture of the last code read
that the code reader's video shows in its top-left corner."""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

import cv2
import numpy as np


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.hypot(*(np.asarray(b, np.float64) - np.asarray(a, np.float64))))


def upright(img: np.ndarray, polygon: Sequence[Sequence[float]], margin_px: int, *,
            max_long: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray]:
    """A code cut out of ``img`` from its four corners (TL, TR, BR, BL as printed), warped upright,
    with ``margin_px`` around it, and the 3 x 3 matrix from ``img`` to the crop.

    A polygon thinner than a third of its length (a 1D code's scan line) is first
    made half its length tall, centred, so the bars show. ``max_long`` shrinks the
    crop until its long side fits. Outside the picture the edge's pixels are repeated."""
    tl, tr, br, bl = np.asarray(polygon, np.float64).reshape(4, 2)
    w = (_distance(tl, tr) + _distance(bl, br)) / 2
    h = (_distance(tl, bl) + _distance(tr, br)) / 2
    if w < 2:
        # No length to go by: a small square around the points.
        centre, half = (tl + tr + br + bl) / 4, max(w, h, 8.0) / 2
        tl, tr, br, bl = (centre + np.array(d) * half for d in ((-1, -1), (1, -1), (1, 1), (-1, 1)))
        w = h = 2 * half
    elif h < w / 3:
        along = (tr - tl) + (br - bl)
        along /= max(np.hypot(*along), 1e-9)
        across = np.array([-along[1], along[0]])
        left, right = (tl + bl) / 2, (tr + br) / 2
        h = w / 2
        tl, tr, br, bl = left - across * h / 2, right - across * h / 2, right + across * h / 2, left + across * h / 2
    m = float(margin_px)
    full_w, full_h = w + 2 * m, h + 2 * m
    scale = 1.0
    if max_long and max(full_w, full_h) > max_long:
        scale = max_long / max(full_w, full_h)
    dst = np.float32([[m, m], [m + w, m], [m + w, m + h], [m, m + h]]) * scale
    matrix = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), dst).astype(np.float64)
    size = (max(1, round(full_w * scale)), max(1, round(full_h * scale)))
    crop = cv2.warpPerspective(img, matrix, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return crop, matrix
