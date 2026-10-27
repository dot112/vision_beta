"""The text reader's OCR models (PP-OCRv6, through RapidOCR) and where they are on disk.

*small* comes inside the rapidocr package. *tiny* (6 MB, about three times faster on a CPU) and
*medium* (130 MB, for a GPU) are fetched once from ModelScope into OCR_MODEL_DIR, so a server never
downloads at run time:

    python -m app.engines.ocr_models --fetch

The Docker build runs it into the image.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MODEL_NAMES = ("tiny", "small", "medium")
FETCHED = ("tiny", "medium")
FETCH_HINT = "run: python -m app.engines.ocr_models --fetch"
DEFAULT_MODEL_DIR = "./model_store/ocr"


def model_dir() -> Path:
    """OCR_MODEL_DIR. From the server's settings; where they cannot load (the Docker build's fetch has
    no .env and no SECRET_KEY), from the environment, else the default."""
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001 - the settings refuse to load without a SECRET_KEY
        return Path(os.environ.get("OCR_MODEL_DIR") or DEFAULT_MODEL_DIR)
    return Path(settings.OCR_MODEL_DIR)


def _package_models() -> Optional[Path]:
    """The models folder inside the rapidocr package, found without importing it (None: not installed)."""
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "models"


def model_files(name: str) -> Optional[Tuple[Path, Path]]:
    """(detection, recognition) files of a model when both are on disk, else None."""
    if name not in MODEL_NAMES:
        return None
    folder = _package_models() if name == "small" else model_dir()
    if folder is None:
        return None
    files = (folder / f"PP-OCRv6_det_{name}.onnx", folder / f"PP-OCRv6_rec_{name}.onnx")
    return files if all(f.is_file() for f in files) else None


def available() -> List[str]:
    """The models on disk, in MODEL_NAMES order."""
    return [name for name in MODEL_NAMES if model_files(name)]


def new_reader_model() -> str:
    """The model a new text reader gets: *tiny* when the server has it, else *small* (always there)."""
    return "tiny" if model_files("tiny") else "small"


def fetch(names: Iterable[str] = FETCHED) -> List[str]:
    """Fetch models into OCR_MODEL_DIR (RapidOCR downloads a model the first time it runs it).

    Returns the names asked for that are on disk afterwards.
    """
    import numpy as np
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import ModelType, OCRVersion

    folder = model_dir()
    folder.mkdir(parents=True, exist_ok=True)
    blank = np.full((64, 320, 3), 255, np.uint8)
    done = []
    for name in names:
        if not model_files(name):
            engine = RapidOCR(params={
                "Global.log_level": "error",
                "Global.model_root_dir": str(folder.resolve()),
                "Det.model_type": ModelType(name), "Rec.model_type": ModelType(name),
                "Det.ocr_version": OCRVersion.PPOCRV6, "Rec.ocr_version": OCRVersion.PPOCRV6,
            })
            engine(blank, use_det=True, use_cls=False, use_rec=True)
        if model_files(name):
            done.append(name)
    return done


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="The text reader's OCR models")
    parser.add_argument("--fetch", action="store_true", help=f"fetch {', '.join(FETCHED)} into OCR_MODEL_DIR")
    args = parser.parse_args(argv)
    failed = False
    if args.fetch:
        try:
            missing = set(FETCHED) - set(fetch())
        except Exception as exc:  # noqa: BLE001 - said, and the exit code says it too
            print(f"Fetching failed: {type(exc).__name__}: {exc}")
            missing = {name for name in FETCHED if not model_files(name)}
        failed = bool(missing)
    for name in MODEL_NAMES:
        files = model_files(name)
        print(f"{name:7} {'on disk: ' + str(files[0].parent) if files else 'not on disk'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
