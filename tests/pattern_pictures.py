"""Pictures the pattern check tests draw: a printed label (a frame, a big word, a small word, a filled
circle, a lot number) put into a 1280 x 960 camera picture, turned, shifted and lit differently, with
camera noise. The live check's fake camera draws its pictures with these too.
"""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np

LABEL_SIZE = (360, 240)
# The circle's centre and radius on the label (the "cap" of the tests), and the small word's box.
CIRCLE = (300, 190, 28)
WORD_BOX = (30, 115, 250, 170)


def label(w: int = 360, h: int = 240, text: str = "ORANGE", circle: bool = True, lot: str = "LOT 12345") -> np.ndarray:
    """A grey label: light paper, a dark frame, a big word, "JUICE 1L", a circle and a lot number."""
    lab = np.full((h, w), 225, np.uint8)
    cv2.rectangle(lab, (4, 4), (w - 5, h - 5), 40, 3)
    cv2.putText(lab, text, (30, 90), cv2.FONT_HERSHEY_SIMPLEX, 2.0, 30, 5)
    cv2.putText(lab, "JUICE 1L", (40, 160), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 60, 3)
    if circle:
        cv2.circle(lab, CIRCLE[:2], CIRCLE[2], 90, -1)
    cv2.putText(lab, lot, (30, 215), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 50, 2)
    return lab


def scene(lab: np.ndarray, angle: float, cx: float, cy: float, gain: float = 1.0, offset: float = 0,
          size: Tuple[int, int] = (1280, 960), seed: int = 0, defect: bool = False,
          colour: Optional[Tuple[int, int, int]] = None) -> np.ndarray:
    """A BGR camera picture with the label at (cx, cy), turned ``angle`` degrees (counter-clockwise, as
    cv2.getRotationMatrix2D), on a blurred noisy background; brightness x gain + offset, camera noise.
    ``defect``: a 12 px spot of missing print in "JUICE" and a scratch. ``colour``: the circle's colour."""
    rng = np.random.default_rng(seed)
    width, height = size
    bg = cv2.GaussianBlur(rng.normal(120, 18, (height, width)).clip(0, 255).astype(np.uint8), (0, 0), 3)
    lab = cv2.cvtColor(lab, cv2.COLOR_GRAY2BGR) if lab.ndim == 2 else lab.copy()
    if colour is not None:
        cv2.circle(lab, CIRCLE[:2], CIRCLE[2], colour, -1)
    if defect:
        cv2.circle(lab, (200, 120), 6, (220, 220, 220), -1)
        cv2.line(lab, (60, 30), (110, 40), (120, 120, 120), 2)
    h, w = lab.shape[:2]
    big = np.zeros((height, width, 3), np.uint8)
    inside = np.zeros((height, width), np.uint8)
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    big[y0:y0 + h, x0:x0 + w] = lab
    inside[y0:y0 + h, x0:x0 + w] = 255
    turn = cv2.getRotationMatrix2D((x0 + w / 2, y0 + h / 2), angle, 1.0)
    big = cv2.warpAffine(big, turn, (width, height), flags=cv2.INTER_LINEAR)
    inside = cv2.warpAffine(inside, turn, (width, height), flags=cv2.INTER_NEAREST)
    out = np.where(inside[..., None] > 0, big, cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)).astype(np.float32)
    out = out * gain + offset + rng.normal(0, 3, out.shape)
    return out.clip(0, 255).astype(np.uint8)


def crop(img: np.ndarray, cx: float, cy: float, w: int, h: int) -> np.ndarray:
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    return img[y0:y0 + h, x0:x0 + w].copy()


def label_point(px: float, py: float, angle: float, cx: float, cy: float,
                w: int = LABEL_SIZE[0], h: int = LABEL_SIZE[1]) -> Tuple[float, float]:
    """Where a point (px, py) of a label put at (cx, cy) turned ``angle`` lands in the picture."""
    turn = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    x, y = cx - w / 2 + px, cy - h / 2 + py
    return float(turn[0, 0] * x + turn[0, 1] * y + turn[0, 2]), float(turn[1, 0] * x + turn[1, 1] * y + turn[1, 2])
