"""The text reader's OCR models (PP-OCRv6, through RapidOCR) and where they are on disk.

*small* comes inside the rapidocr package. *tiny* (6 MB, about three times faster on a CPU) and
*medium* (130 MB, for a GPU) are fetched once from ModelScope into OCR_MODEL_DIR, so a server never
downloads at run time:

    python -m app.engines.ocr_models --fetch

The Docker build runs it into the image.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
    try:
        spec = importlib.util.find_spec("rapidocr")
    except (ImportError, ValueError):     # a module without a spec stands in for it
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "models"


def _expected_sha256() -> Dict[str, str]:
    """The SHA-256 of each PP-OCRv6 model file, from the rapidocr package's model list ({file name: hash};
    empty when it cannot be read)."""
    folder = _package_models()
    try:
        text = (folder.parent / "default_models.yaml").read_text(encoding="utf-8") if folder else ""
    except OSError:
        return {}
    found = re.findall(r"model_dir:\s*\S*/(PP-OCRv6_(?:det|rec)_\w+\.onnx)\s*\n\s*SHA256:\s*([0-9a-fA-F]{64})", text)
    return {name: digest.lower() for name, digest in found}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _drop_broken(name: str, hashes: Dict[str, str]) -> None:
    """Remove a file of the model whose SHA-256 is not the package's: a download cut short."""
    for kind in ("det", "rec"):
        path = model_dir() / f"PP-OCRv6_{kind}_{name}.onnx"
        expected = hashes.get(path.name)
        if expected and path.is_file() and _sha256(path) != expected:
            path.unlink()


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
    import cv2
    import numpy as np
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import ModelType, OCRVersion

    folder = model_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # A printed line: RapidOCR fetches a recognition model the first time it recognises a line, so a blank
    # picture would fetch only the detection model.
    picture = np.full((64, 320, 3), 255, np.uint8)
    cv2.putText(picture, "LOT A12345", (10, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 3)
    hashes = _expected_sha256()
    done: List[str] = []
    failed: List[str] = []
    for name in names:
        _drop_broken(name, hashes)
        try:
            if not model_files(name):
                engine = RapidOCR(params={
                    "Global.log_level": "error",
                    "Global.model_root_dir": str(folder.resolve()),
                    "Det.model_type": ModelType(name), "Rec.model_type": ModelType(name),
                    "Det.ocr_version": OCRVersion.PPOCRV6, "Rec.ocr_version": OCRVersion.PPOCRV6,
                })
                engine(picture, use_det=True, use_cls=False, use_rec=True)
        except Exception as exc:  # noqa: BLE001 - the next model is still fetched; the error is raised below
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
        finally:
            _drop_broken(name, hashes)
        if model_files(name):
            done.append(name)
    if failed:
        raise RuntimeError("; ".join(failed))
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
            print(f"Fetching failed: {exc}")
            missing = {name for name in FETCHED if not model_files(name)}
        failed = bool(missing)
    for name in MODEL_NAMES:
        files = model_files(name)
        print(f"{name:7} {'on disk: ' + str(files[0].parent) if files else 'not on disk'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
