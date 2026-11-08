"""The pattern check's files, under the data folder.

data/match-references/<id>/            a reference set, written once (into <id>.partial, then renamed) and
                                       never changed: set.json, mask.png, check-<n>-mean.png,
                                       check-<n>-spread.npy (Lab, float16), samples/NN.jpg (JPEG 95)
data/match-references/drafts/<camera>/ what a camera's Pattern and checks page is working on: draft.json
                                       (boxes in pixels, the search area in fractions, the samples, the
                                       last trained set), picture.png (the training picture), mask.png,
                                       samples/NN.jpg (sample 1 is the training picture)

Training again always makes a new set, so a saved line setup that names an older set still finds it.
The clean-up removes a set only when no line's camera, no saved line setup and no draft names it and it
was made more than 24 hours ago; a draft unused for 30 days goes too.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import cv2
import numpy as np

from app.engines.pattern_check import CheckModel, ReferenceSet, is_area
from app.engines.pattern_training import MAX_SAMPLES, DraftData

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^ref-[0-9a-f]{8}$")
FORMAT = 1
DRAFTS = "drafts"
SET_KEEP_S = 24 * 3600
DRAFT_KEEP_S = 30 * 24 * 3600
PARTIAL_KEEP_S = 3600
JPEG_QUALITY = 95
# A camera id as a folder name: no dots (Windows drops trailing ones, so "..." would be the drafts folder
# itself and "cam1." would be "cam1") and none of Windows' device names.
_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEVICE_NAMES = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)
_cache: Dict[str, ReferenceSet] = {}
_lock = threading.Lock()
_draft_lock = threading.RLock()


class ReferenceMissing(LookupError):
    """A reference set that is not there, or cannot be read."""


def root() -> Path:
    """The folder of the sets, under the settings' data folder as it is now (tests have their own)."""
    from app.services import settings_persistence_service

    return Path(settings_persistence_service.DATA_DIR) / "match-references"


def new_id() -> str:
    return f"ref-{uuid.uuid4().hex[:8]}"


def _set_dir(set_id: str) -> Path:
    if not isinstance(set_id, str) or not ID_RE.fullmatch(set_id):
        raise ReferenceMissing(f"the reference set {set_id} is missing")
    return root() / set_id


def _write_image(path: Path, image: np.ndarray, *params: int) -> None:
    ok, data = cv2.imencode(path.suffix, image, list(params))
    if not ok:
        raise OSError(f"{path.name} could not be encoded")
    path.write_bytes(data.tobytes())


def _read_image(path: Path, flags: int = cv2.IMREAD_UNCHANGED) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), flags)
    if image is None:
        raise ValueError(f"{path.name} is not a picture")
    return image


def _mask_png(ignore: np.ndarray) -> np.ndarray:
    return ignore.astype(np.uint8) * 255


def _absolute(reference: List[int], box: Iterable[float]) -> List[int]:
    rx, ry = (reference[0] + reference[2]) / 2, (reference[1] + reference[3]) / 2
    x1, y1, x2, y2 = box
    return [int(round(rx + x1)), int(round(ry + y1)), int(round(rx + x2)), int(round(ry + y2))]


# ── Reference sets ────────────────────────────────────────────────────────────

def write_set(ref: ReferenceSet, draft: DraftData, made_by: str, camera_id: str) -> None:
    """Write a set made from ``draft`` once. Raises FileExistsError when the id is taken."""
    final = _set_dir(ref.id)
    if final.exists():
        raise FileExistsError(str(final))
    partial = final.with_name(final.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    (partial / "samples").mkdir(parents=True)
    meta = {
        "format": FORMAT, "id": ref.id, "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "made_by": made_by, "camera_id": camera_id, "picture_size": list(ref.picture_size),
        "reference": [int(v) for v in draft.reference], "samples": ref.samples,
        "checks": [{"box": [float(v) for v in check.box], "typical": float(check.typical), "area": bool(check.area)}
                   for check in ref.checks],
    }
    _write_image(partial / "mask.png", _mask_png(draft.ignore))
    for number, check in enumerate(ref.checks, start=1):
        _write_image(partial / f"check-{number}-mean.png", check.mean)
        np.save(partial / f"check-{number}-spread.npy", check.spread.astype(np.float16))
    for number, sample in enumerate(draft.samples, start=1):
        _write_image(partial / "samples" / f"{number:02d}.jpg", sample, cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY)
    (partial / "set.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(partial, final)


def _meta(set_id: str) -> Dict[str, Any]:
    folder = _set_dir(set_id)
    path = folder / "set.json"
    if not path.is_file():
        raise ReferenceMissing(f"the reference set {set_id} is missing")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("id") != set_id or not meta.get("checks"):
            raise ValueError("set.json is not a reference set")
        return meta
    except (OSError, ValueError) as exc:
        raise ReferenceMissing(f"the reference set {set_id} cannot be read: {exc}") from exc


def load_set(set_id: str) -> ReferenceSet:
    """The set, read once and kept in memory. Raises ReferenceMissing."""
    with _lock:
        cached = _cache.get(set_id)
    if cached is not None:
        return cached
    meta = _meta(set_id)
    folder = _set_dir(set_id)
    try:
        ignore = _read_image(folder / "mask.png", cv2.IMREAD_GRAYSCALE) > 0
        checks = []
        for number, entry in enumerate(meta["checks"], start=1):
            x1, y1, x2, y2 = _absolute(meta["reference"], entry["box"])
            mean = _read_image(folder / f"check-{number}-mean.png", cv2.IMREAD_COLOR)
            spread = np.load(folder / f"check-{number}-spread.npy").astype(np.float32)
            check = CheckModel(tuple(float(v) for v in entry["box"]), mean, spread, ignore[y1:y2, x1:x2].copy(),
                               float(entry["typical"]))
            # A set trained before area checks: a check (not the reference) without edges is one.
            area = entry.get("area")
            check.area = bool(area) if area is not None else (number > 1 and is_area(check.mean, check.ignore))
            checks.append(check)
        ref = ReferenceSet(set_id, tuple(int(v) for v in meta["picture_size"]), checks, int(meta["samples"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReferenceMissing(f"the reference set {set_id} cannot be read: {exc}") from exc
    with _lock:
        return _cache.setdefault(set_id, ref)


def set_info(set_id: str) -> Optional[Dict[str, Any]]:
    """What a line save checks: how many checks, the reference box's size, the picture's size."""
    try:
        meta = _meta(set_id)
        x1, y1, x2, y2 = meta["reference"]
        return {"checks": len(meta["checks"]), "reference_size": [x2 - x1, y2 - y1],
                "picture_size": [int(v) for v in meta["picture_size"]]}
    except (ReferenceMissing, KeyError, TypeError, ValueError):
        return None


def room_ok() -> bool:
    """Whether the data folder's disk has more room than the data safety floor."""
    from app.services import settings_persistence_service
    from app.services.storage_guard import StorageGuard

    folder = root() if root().exists() else Path(settings_persistence_service.DATA_DIR)
    return shutil.disk_usage(folder).free >= StorageGuard.floor()


# ── Drafts ────────────────────────────────────────────────────────────────────

def _draft_dir(camera_id: str) -> Path:
    if not isinstance(camera_id, str) or not _CAMERA_ID.fullmatch(camera_id) or _DEVICE_NAMES.fullmatch(camera_id):
        raise KeyError(camera_id)
    return root() / DRAFTS / camera_id


def _read_draft(camera_id: str) -> Optional[Dict[str, Any]]:
    path = _draft_dir(camera_id) / "draft.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_draft(camera_id: str, data: Dict[str, Any]) -> None:
    path = _draft_dir(camera_id) / "draft.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _need(camera_id: str) -> Dict[str, Any]:
    data = _read_draft(camera_id)
    if data is None:
        raise LookupError("No draft for this camera")
    return data


def _box_px(box: Any, size: List[int], what: str) -> List[int]:
    if not isinstance(box, (list, tuple)) or len(box) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in box):
        raise ValueError(f"{what} must be [x1, y1, x2, y2] inside the picture (0 to 1)")
    w, h = size
    x1, y1, x2, y2 = (int(round(box[0] * w)), int(round(box[1] * h)), int(round(box[2] * w)), int(round(box[3] * h)))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"{what} must have x1 < x2 and y1 < y2")
    return [x1, y1, x2, y2]


def _fractions(box: Optional[List[int]], size: List[int]) -> Optional[List[float]]:
    if not box:
        return None
    w, h = size
    return [round(box[0] / w, 4), round(box[1] / h, 4), round(box[2] / w, 4), round(box[3] / h, 4)]


def start_draft(camera_id: str, picture: np.ndarray) -> None:
    """A new draft with this picture as the training picture and sample 1 (any old draft goes)."""
    folder = _draft_dir(camera_id)
    with _draft_lock:
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "samples").mkdir(parents=True)
        _write_image(folder / "picture.png", picture)
        _write_image(folder / "samples" / "01.jpg", picture, cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY)
        _write_draft(camera_id, {"reference": None, "checks": [], "search_area": None, "trained": None,
                                 "picture_size": [int(picture.shape[1]), int(picture.shape[0])],
                                 "samples": [{"file": "01.jpg", "score": 100}]})


def save_boxes(camera_id: str, reference: Any, checks: Any, search_area: Any) -> None:
    """The boxes as fractions of the training picture (kept in its pixels) and the search area."""
    with _draft_lock:
        data = _need(camera_id)
        size = data["picture_size"]
        if not isinstance(checks, list) or len(checks) > 3:
            raise ValueError("A pattern check has at most 3 checks besides the reference")
        data["reference"] = None if reference is None else _box_px(reference, size, "The reference box")
        data["checks"] = [_box_px(box, size, f"Check {n}'s box") for n, box in enumerate(checks, start=2)]
        if search_area is None:
            data["search_area"] = None
        else:
            _box_px(search_area, size, "The search area")
            data["search_area"] = [round(float(v), 4) for v in search_area]
        _write_draft(camera_id, data)


def save_mask(camera_id: str, png: bytes) -> None:
    """The mask, a PNG of the training picture's size (white or any light value = ignore)."""
    with _draft_lock:
        data = _need(camera_id)
        w, h = data["picture_size"]
        mask = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE) if png else None
        if mask is None:
            raise ValueError("The mask is not a PNG picture")
        if mask.shape != (h, w):
            raise ValueError(f"The mask must be {w} × {h} pixels, like the training picture")
        _write_image(_draft_dir(camera_id) / "mask.png", _mask_png(mask > 0))
        _write_draft(camera_id, data)


def add_sample(camera_id: str, picture: np.ndarray, score: int) -> int:
    """Add a good sample; gives its number."""
    with _draft_lock:
        data = _need(camera_id)
        if len(data["samples"]) >= MAX_SAMPLES:
            raise ValueError(f"At most {MAX_SAMPLES} good samples")
        if [int(picture.shape[1]), int(picture.shape[0])] != data["picture_size"]:
            raise ValueError("The picture is not the size of the training picture: the camera's size changed")
        number = max(int(s["file"][:2]) for s in data["samples"]) + 1
        name = f"{number:02d}.jpg"
        _write_image(_draft_dir(camera_id) / "samples" / name, picture, cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY)
        data["samples"].append({"file": name, "score": int(score)})
        _write_draft(camera_id, data)
        return len(data["samples"])


def remove_sample(camera_id: str, n: int) -> None:
    with _draft_lock:
        data = _need(camera_id)
        if n == 1:
            raise ValueError("The training picture is sample 1: use Start over to take another")
        if not 1 < n <= len(data["samples"]):
            raise ValueError(f"There is no sample {n}")
        gone = data["samples"].pop(n - 1)
        (_draft_dir(camera_id) / "samples" / gone["file"]).unlink(missing_ok=True)
        _write_draft(camera_id, data)


def set_trained(camera_id: str, set_id: str) -> None:
    with _draft_lock:
        data = _need(camera_id)
        data["trained"] = set_id
        _write_draft(camera_id, data)


def draft(camera_id: str) -> Optional[Dict[str, Any]]:
    """The draft as the page sees it: boxes and search area as fractions, the samples, the last set."""
    data = _read_draft(camera_id)
    if data is None:
        return None
    size = data["picture_size"]
    return {
        "reference": _fractions(data.get("reference"), size),
        "checks": [_fractions(box, size) for box in data.get("checks") or []],
        "search_area": data.get("search_area"),
        "samples": [{"n": n, "score": s.get("score")} for n, s in enumerate(data["samples"], start=1)],
        "trained": data.get("trained"),
        "picture_size": size,
    }


def draft_data(camera_id: str) -> Optional[DraftData]:
    """The draft in pixels, with its pictures, for training."""
    data = _read_draft(camera_id)
    if data is None:
        return None
    folder = _draft_dir(camera_id)
    picture = _read_image(folder / "picture.png")
    w, h = data["picture_size"]
    mask_path = folder / "mask.png"
    ignore = _read_image(mask_path, cv2.IMREAD_GRAYSCALE) > 0 if mask_path.is_file() else np.zeros((h, w), bool)
    samples = [picture] + [_read_image(folder / "samples" / s["file"]) for s in data["samples"][1:]]
    return DraftData(picture, list(data.get("reference") or []), [list(b) for b in data.get("checks") or []],
                     ignore, samples)


def draft_from_set(camera_id: str, set_id: str) -> None:
    """A draft made from a set: its training picture, boxes, mask and samples (Edit training)."""
    meta = _meta(set_id)
    source = _set_dir(set_id)
    names = sorted(p.name for p in (source / "samples").glob("*.jpg"))
    if not names:
        raise ReferenceMissing(f"the reference set {set_id} has no samples")
    with _draft_lock:
        start_draft(camera_id, _read_image(source / "samples" / names[0]))
        folder = _draft_dir(camera_id)
        shutil.copyfile(source / "mask.png", folder / "mask.png")
        data = _need(camera_id)
        for name in names[1:]:
            shutil.copyfile(source / "samples" / name, folder / "samples" / name)
            data["samples"].append({"file": name, "score": 100})
        data["reference"] = [int(v) for v in meta["reference"]]
        data["checks"] = [_absolute(meta["reference"], entry["box"]) for entry in meta["checks"][1:]]
        data["trained"] = set_id
        _write_draft(camera_id, data)


def delete_draft(camera_id: str) -> None:
    folder = _draft_dir(camera_id)
    with _draft_lock:
        shutil.rmtree(folder, ignore_errors=True)


def draft_file(camera_id: str, name: Union[str, int]) -> Optional[Path]:
    """The training picture ("picture"), the mask ("mask") or sample ``n``'s file, when there."""
    folder = _draft_dir(camera_id)
    if name in ("picture", "mask"):
        path = folder / f"{name}.png"
        return path if path.is_file() else None
    data = _read_draft(camera_id)
    if data is None or not isinstance(name, int) or not 1 <= name <= len(data["samples"]):
        return None
    path = folder / "samples" / data["samples"][name - 1]["file"]
    return path if path.is_file() else None


# ── Clean-up ──────────────────────────────────────────────────────────────────

def _named_by(cameras: Any) -> Iterable[str]:
    for cam in cameras or []:
        if isinstance(cam, dict) and cam.get("role") == "match":
            set_id = (cam.get("match") or {}).get("reference_id")
            if isinstance(set_id, str):
                yield set_id


def _saved_setups() -> Iterable[Dict[str, Any]]:
    from app.services import settings_persistence_service

    for path in (Path(settings_persistence_service.DATA_DIR) / "line-setups").glob("*/*.json"):
        try:
            save = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(save, dict) and isinstance(save.get("line"), dict):
            yield save["line"]


def _age(path: Path, now: float) -> float:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0.0


def clean_up(lines: List[Dict[str, Any]], now: Optional[float] = None) -> int:
    """Remove the sets nothing names (made more than 24 hours ago) and drafts unused for 30 days.
    Gives how many were removed."""
    now = time.time() if now is None else now
    top = root()
    if not top.is_dir():
        return 0
    removed = 0
    named = set()
    for line in lines:
        named.update(_named_by(line.get("cameras")))
    for line in _saved_setups():
        named.update(_named_by(line.get("cameras")))
    with _draft_lock:
        drafts = top / DRAFTS
        for folder in (drafts.iterdir() if drafts.is_dir() else []):
            if _age(folder / "draft.json", now) > DRAFT_KEEP_S:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
                continue
            data = _read_draft(folder.name) if _CAMERA_ID.fullmatch(folder.name) and not _DEVICE_NAMES.fullmatch(folder.name) else None
            if data and data.get("trained"):
                named.add(data["trained"])
    for folder in top.iterdir():
        if folder.name.endswith(".partial") and _age(folder, now) > PARTIAL_KEEP_S:
            shutil.rmtree(folder, ignore_errors=True)
        elif ID_RE.fullmatch(folder.name) and folder.name not in named \
                and _age(folder / "set.json" if (folder / "set.json").exists() else folder, now) > SET_KEEP_S:
            shutil.rmtree(folder, ignore_errors=True)
            with _lock:
                _cache.pop(folder.name, None)
            removed += 1
            logger.info("Reference set %s removed: no camera, saved line setup or draft names it", folder.name)
    return removed
