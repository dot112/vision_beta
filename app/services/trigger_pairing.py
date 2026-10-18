"""
Trigger pairing
===============
A trigger camera in Camera trigger mode with a trigger notice has two things
that arrive on their own: the notice (the PLC's "Take a picture" input saw an
edge) and the picture (the camera took it). They belong together when they are
at most the window ("Wait for the picture") apart, in either order.

``PicturePairer`` holds the notices and pictures that have no partner yet, one
pairer per camera. It pairs in arrival order: a notice takes the oldest waiting
picture with ``picture.at >= notice.at - window``, and a picture takes the
oldest waiting notice with ``notice.at >= picture.at - window``. A notice that
gets no picture within the window is *missing*. A picture that gets no notice
within the window is not a product; it is *late* when a notice went missing
just before it (the picture came after the window ended, but within a window of
it), else *extra*.

The unit has no I/O and no clock: every time is passed in, so the same times
give the same result. The PLC input (event loop) and the camera runner (its own
thread) both call it, so every method takes the lock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class Notice:
    """A trigger notice: when the input's edge was seen, and what it carries."""

    at: float
    context: Dict[str, Any]


@dataclass
class Picture:
    """A camera picture: when it arrived, the frame and the camera's frame id."""

    at: float
    frame: Any
    frame_id: int


@dataclass
class Expired:
    """What one ``expire`` removed from the waiting lists."""

    missing: List[Notice]  # notices that got no picture within the window
    late: List[Picture]  # pictures with no notice, just after a missing notice
    extra: List[Picture]  # pictures with no notice at all


def _take_oldest(items: List[Any], fits: Callable[[Any], bool]) -> Optional[Any]:
    """Remove and return the oldest item that fits (the list is in arrival order)."""
    for index, item in enumerate(items):
        if fits(item):
            return items.pop(index)
    return None


class PicturePairer:
    def __init__(self, window_s: float):
        self._window_s = float(window_s)
        self._lock = threading.Lock()
        self._notices: List[Notice] = []
        self._pictures: List[Picture] = []
        # When the window of the latest notice that went missing ended.
        self._last_missing_at: Optional[float] = None

    def add_notice(self, at: float, context: Dict[str, Any]) -> Optional[Tuple[Notice, Picture]]:
        """A notice came in. Returns (notice, picture) if a waiting picture fits, else waits."""
        notice = Notice(at, context)
        with self._lock:
            picture = _take_oldest(self._pictures, lambda p: p.at >= notice.at - self._window_s)
            if picture is None:
                self._notices.append(notice)
                return None
            return notice, picture

    def add_picture(self, at: float, frame: Any, frame_id: int) -> Optional[Tuple[Notice, Picture]]:
        """A picture came in. Returns (notice, picture) if a waiting notice fits, else waits."""
        picture = Picture(at, frame, frame_id)
        with self._lock:
            notice = _take_oldest(self._notices, lambda n: n.at >= picture.at - self._window_s)
            if notice is None:
                self._pictures.append(picture)
                return None
            return notice, picture

    def expire(self, now: float) -> Expired:
        """Remove what has waited longer than the window and say what each one was."""
        window = self._window_s
        with self._lock:
            missing: List[Notice] = []
            kept_notices: List[Notice] = []
            for notice in self._notices:
                if now - notice.at > window:
                    missing.append(notice)
                else:
                    kept_notices.append(notice)
            gone: List[Picture] = []
            kept_pictures: List[Picture] = []
            for picture in self._pictures:
                if now - picture.at > window:
                    gone.append(picture)
                else:
                    kept_pictures.append(picture)
            self._notices = kept_notices
            self._pictures = kept_pictures

            # A notice went missing when its window ended, not when expire got
            # to it, so the answer does not depend on how often expire runs.
            ended = [notice.at + window for notice in missing]
            marks = ended + ([] if self._last_missing_at is None else [self._last_missing_at])
            late: List[Picture] = []
            extra: List[Picture] = []
            for picture in gone:
                if any(picture.at - window <= t <= picture.at for t in marks):
                    late.append(picture)
                else:
                    extra.append(picture)
            if marks:
                self._last_missing_at = max(marks)
            return Expired(missing, late, extra)

    def clear(self) -> None:
        """Drop everything (the line stopped)."""
        with self._lock:
            self._notices.clear()
            self._pictures.clear()
            self._last_missing_at = None

    def waiting(self) -> Tuple[int, int]:
        """How many (notices, pictures) are waiting for a partner."""
        with self._lock:
            return len(self._notices), len(self._pictures)
