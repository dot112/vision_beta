from __future__ import annotations

import functools
import re
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
import cv2
import numpy as np

from app.schemas.qr import BarcodeDecodeResponse, BarcodeItem
from app.schemas.vision import BoundingBox
from app.utils.logger import get_logger

logger = get_logger(__name__)

try:
    # zxing-cpp reads QR, Data Matrix, Aztec, PDF417 and the 1D retail and
    # industrial codes, and reports each code's four corners.
    import zxingcpp
except ImportError:  # pragma: no cover - the OpenCV detectors below take over
    zxingcpp = None


def _format_name(fmt: Any) -> str:
    """'QR Code' / BarcodeFormat.QRCode -> 'QR_CODE', matching the OpenCV names."""
    name = str(fmt).split(".")[-1].strip()
    if name == "QRCode":
        return "QR_CODE"
    return name.replace(" ", "_").upper() or "BARCODE"


# ── Code types a reader can be limited to ─────────────────────────────────────
# "all", every 2D type, every 1D type, or one type the installed reader supports.
# A type is saved by its key: 'QR Code', 'QR_CODE' and 'QRCode' are all QRCODE.

CODE_TYPE_ALL, CODE_TYPE_2D, CODE_TYPE_1D = "all", "2d", "1d"
_CODE_GROUPS = (
    (CODE_TYPE_ALL, "1D and 2D codes (all types)"),
    (CODE_TYPE_2D, "2D codes (all 2D types)"),
    (CODE_TYPE_1D, "1D barcodes (all 1D types)"),
)
# For zxing-cpp 2, which does not say which of its types are 2D.
_2D_KEYS = frozenset({
    "AZTEC", "AZTECCODE", "AZTECRUNE", "DATAMATRIX", "MAXICODE", "PDF417", "COMPACTPDF417", "MICROPDF417",
    "QRCODE", "QRCODEMODEL1", "QRCODEMODEL2", "MICROQRCODE", "RMQRCODE",
})
# Without zxing-cpp: OpenCV's QR detector and the retail barcodes its barcode detector reads.
_OPENCV_TYPES = (("QRCODE", "QR Code", "2d"), ("EAN13", "EAN-13", "1d"), ("EAN8", "EAN-8", "1d"),
                 ("UPCA", "UPC-A", "1d"), ("UPCE", "UPC-E", "1d"))


def reader_backend() -> str:
    return "zxing-cpp" if zxingcpp is not None else "opencv"


def code_type_key(name: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name).split(".")[-1].upper())


class CodeFilter(NamedTuple):
    """What a reader is limited to. Everything None means every code type."""
    formats: Any = None          # handed to zxing-cpp, which then looks for nothing else
    kind: Optional[str] = None   # "1d" or "2d" when a whole group is wanted
    key: Optional[str] = None    # one type's key


@functools.lru_cache(maxsize=1)
def _code_type_table() -> Dict[str, Dict[str, Any]]:
    """Every single code type the installed reader supports: key -> label, kind and zxing-cpp format."""
    table: Dict[str, Dict[str, Any]] = {}
    if zxingcpp is None:
        for key, label, kind in _OPENCV_TYPES:
            table[key] = {"value": key, "label": label, "kind": kind, "format": None}
        return table
    formats = zxingcpp.BarcodeFormat
    listing = getattr(zxingcpp, "barcode_formats_list", None)
    if listing is not None:  # zxing-cpp 3
        members = list(listing(formats.AllReadable))
        matrix = set(listing(formats.AllMatrix))
    else:  # zxing-cpp 2
        members = [f for name, f in formats.__members__.items() if name not in ("NONE", "LinearCodes", "MatrixCodes")]
        matrix = None
    for fmt in members:
        key = code_type_key(getattr(fmt, "name", fmt))
        label = str(fmt).split(".")[-1]
        # zxing-cpp 3 has families: "QR Code" also covers Micro QR and rMQR, "EAN/UPC" every retail code.
        if listing is not None and len(listing(fmt)) > 1:
            label += " (every kind)"
        is_2d = fmt in matrix if matrix is not None else key in _2D_KEYS
        table.setdefault(key, {"value": key, "label": label, "kind": "2d" if is_2d else "1d", "format": fmt})
    return table


def code_type_options() -> List[Dict[str, str]]:
    """The choices for a reader's code type: the three groups, then each 2D and each 1D type."""
    singles = sorted(_code_type_table().values(), key=lambda t: t["kind"] != "2d")  # 2D first, each in the reader's order
    return [{"value": value, "label": label, "kind": "group"} for value, label in _CODE_GROUPS] \
        + [{"value": t["value"], "label": t["label"], "kind": t["kind"]} for t in singles]


def normalize_code_type(value: Any) -> str:
    text = str(value or CODE_TYPE_ALL).strip()
    return text.lower() if text.lower() in (CODE_TYPE_ALL, CODE_TYPE_2D, CODE_TYPE_1D) else code_type_key(text)


def is_code_type(value: Any) -> bool:
    value = normalize_code_type(value)
    return value in (CODE_TYPE_ALL, CODE_TYPE_2D, CODE_TYPE_1D) or value in _code_type_table()


@functools.lru_cache(maxsize=128)
def code_filter(code_type: Any = CODE_TYPE_ALL) -> CodeFilter:
    value = normalize_code_type(code_type)
    if value == CODE_TYPE_ALL:
        return CodeFilter()
    formats = zxingcpp.BarcodeFormat if zxingcpp is not None else None
    if value in (CODE_TYPE_2D, CODE_TYPE_1D):
        names = ("AllMatrix", "MatrixCodes") if value == CODE_TYPE_2D else ("AllLinear", "LinearCodes")
        group = next((getattr(formats, n) for n in names if hasattr(formats, n)), None) if formats else None
        return CodeFilter(formats=group, kind=value)
    entry = _code_type_table().get(value)
    if entry is None:
        # Saved under another reader version: reading everything loses less than reading nothing.
        logger.warning("Code type '%s' is not one this reader supports; reading every type instead", code_type)
        return CodeFilter()
    return CodeFilter(formats=entry["format"], kind=entry["kind"], key=value)


# Motion blur lengths (pixels) the deblurring pass tries, along and across the picture.
DEBLUR_LENGTHS = (3, 5, 7, 9, 11, 13, 15)


def code_candidates(gray: np.ndarray, max_regions: int = 3) -> List[Tuple[int, int, int, int]]:
    """Areas that look like a code (dense edges, label-sized), most textured first,
    as (x1, y1, x2, y2) with a margin for the deblurring to work with."""
    h, w = gray.shape[:2]
    f = 480.0 / w if w > 480 else 1.0
    small = cv2.resize(gray, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA) if f < 1 else gray
    gx = cv2.convertScaleAbs(cv2.Sobel(small, cv2.CV_16S, 1, 0, ksize=3))
    gy = cv2.convertScaleAbs(cv2.Sobel(small, cv2.CV_16S, 0, 1, ksize=3))
    edges = cv2.blur(cv2.addWeighted(gx, 0.5, gy, 0.5, 0), (9, 9))
    _, mask = cv2.threshold(edges, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    area = small.shape[0] * small.shape[1]
    found = []
    for i in range(1, count):
        x, y, bw, bh, a = (int(v) for v in stats[i])
        if not area * 0.002 <= a <= area * 0.3 or not 0.3 <= bw / max(bh, 1) <= 3.3:
            continue
        found.append((float(edges[y:y + bh, x:x + bw].mean()), x, y, bw, bh))
    found.sort(reverse=True)
    regions = []
    for _, x, y, bw, bh in found[:max_regions]:
        mx, my = bw * 0.6, bh * 0.6
        regions.append((
            int(max(0, (x - mx) / f)), int(max(0, (y - my) / f)),
            int(min(w, (x + bw + mx) / f)), int(min(h, (y + bh + my) / f)),
        ))
    return regions


def deblur(gray: np.ndarray, length: int, axis: int) -> np.ndarray:
    """Undo straight motion blur of ``length`` pixels along x (axis=1) or y (axis=0) (Wiener filter)."""
    h, w = gray.shape[:2]
    H, W = cv2.getOptimalDFTSize(h), cv2.getOptimalDFTSize(w)
    img = cv2.copyMakeBorder(gray.astype(np.float32) / 255.0, 0, H - h, 0, W - w, cv2.BORDER_REPLICATE)
    psf = np.zeros((H, W), np.float32)
    if axis == 1:
        psf[0, :length] = 1.0 / length
        psf = np.roll(psf, -(length // 2), axis=1)
    else:
        psf[:length, 0] = 1.0 / length
        psf = np.roll(psf, -(length // 2), axis=0)
    G = cv2.dft(img, flags=cv2.DFT_COMPLEX_OUTPUT)
    P = cv2.dft(psf, flags=cv2.DFT_COMPLEX_OUTPUT)
    pr, pi, gr, gi = P[..., 0], P[..., 1], G[..., 0], G[..., 1]
    den = pr * pr + pi * pi + 0.01  # noise-to-signal: keeps the filter from amplifying noise
    out = cv2.idft(np.dstack([(gr * pr + gi * pi) / den, (gi * pr - gr * pi) / den]),
                   flags=cv2.DFT_SCALE | cv2.DFT_REAL_OUTPUT)[:h, :w]
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)


def _sharpened_x2(gray: np.ndarray) -> np.ndarray:
    big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.addWeighted(big, 3.0, cv2.GaussianBlur(big, (0, 0), 2.0), -2.0, 0)


class QREngine:
    """
    Industrial Barcode & QR Code Localization and Decoding Engine.
    Uses zxing-cpp when it is installed (all common 1D and 2D codes, with their
    corner points), else OpenCV's QRCodeDetector and BarcodeDetector.
    """

    def __init__(self):
        # (length, axis) of the deblurring that last read a code. A camera's
        # blur barely changes from product to product, so it is tried first.
        self._last_deblur: Optional[Tuple[int, int]] = None
        self._qr_detector = cv2.QRCodeDetector()
        try:
            self._barcode_detector = cv2.barcode.BarcodeDetector()
        except Exception:
            self._barcode_detector = None

    @property
    def backend(self) -> str:
        return reader_backend()

    @staticmethod
    def _item(code_type: str, text: str, pts: List[List[int]], w: int, h: int) -> BarcodeItem:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x1, y1 = max(0, min(xs)), max(0, min(ys))
        x2, y2 = min(w, max(xs)), min(h, max(ys))
        return BarcodeItem(
            code_type=code_type,
            data=text,
            polygon=pts,
            bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2, width=x2 - x1, height=y2 - y1),
        )

    def _decode_zxing(self, img: np.ndarray, w: int, h: int, only: CodeFilter) -> Optional[List[BarcodeItem]]:
        try:
            results = zxingcpp.read_barcodes(img, formats=only.formats) if only.formats is not None else zxingcpp.read_barcodes(img)
        except Exception as exc:
            logger.debug("zxing-cpp decoding exception: %s", exc)
            return None
        codes: List[BarcodeItem] = []
        for r in results:
            text = getattr(r, "text", "") or ""
            if not text or getattr(r, "valid", True) is False:
                continue
            p = r.position
            pts = [[int(c.x), int(c.y)] for c in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)]
            codes.append(self._item(_format_name(r.format), text, pts, w, h))
        return codes

    def _decode_opencv(self, img: np.ndarray, w: int, h: int, only: CodeFilter) -> List[BarcodeItem]:
        codes: List[BarcodeItem] = []
        # 1. Multi-QR Detection & Decoding
        if only.kind in (None, CODE_TYPE_2D) and only.key in (None, "QRCODE"):
            try:
                retval, decoded_info, points, _ = self._qr_detector.detectAndDecodeMulti(img)
                if retval and decoded_info is not None:
                    for text, pts in zip(decoded_info, points):
                        if text:
                            codes.append(self._item("QR_CODE", text, [[int(pt[0]), int(pt[1])] for pt in pts], w, h))
            except Exception as exc:
                logger.debug("QR decoding exception: %s", exc)

        # 2. 1D Barcode Detection & Decoding (if available)
        if self._barcode_detector is not None and only.kind in (None, CODE_TYPE_1D):
            try:
                retval, decoded_info, decoded_type, points = self._barcode_detector.detectAndDecode(img)
                if retval and decoded_info:
                    for text, btype, pts in zip(decoded_info, decoded_type, points):
                        if only.key and code_type_key(btype) != only.key:
                            continue
                        if text:
                            codes.append(self._item(
                                str(btype) if btype else "BARCODE_1D", text,
                                [[int(pt[0]), int(pt[1])] for pt in pts], w, h,
                            ))
            except Exception as exc:
                logger.debug("1D Barcode decoding exception: %s", exc)
        return codes

    def _read(self, img: np.ndarray, w: int, h: int, only: CodeFilter) -> List[BarcodeItem]:
        codes = self._decode_zxing(img, w, h, only) if zxingcpp is not None else None
        return codes if codes is not None else self._decode_opencv(img, w, h, only)

    def _decode_hard(self, img: np.ndarray, w: int, h: int, budget_ms: float, only: CodeFilter) -> List[BarcodeItem]:
        """Second pass for codes the first pass cannot read, mostly motion blur from a
        moving conveyor: each code-like area is tried sharpened at double size, then
        deblurred for a range of blur lengths along and across the picture, until a
        code reads or ``budget_ms`` is used up."""
        start = time.perf_counter()
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for x1, y1, x2, y2 in code_candidates(gray):
            crop = gray[y1:y2, x1:x2]
            if min(crop.shape[:2]) < 24:
                continue
            blurs = [(n, a) for n in DEBLUR_LENGTHS for a in (1, 0)]
            if self._last_deblur in blurs:
                blurs.remove(self._last_deblur)
                blurs.insert(0, self._last_deblur)
            variants = [(lambda c=crop, b=b: deblur(c, *b), 1.0, b) for b in blurs]
            variants.insert(1 if self._last_deblur else 0, (lambda c=crop: _sharpened_x2(c), 2.0, None))
            for make, factor, blur in variants:
                if (time.perf_counter() - start) * 1000.0 > budget_ms:
                    return []
                variant = make()
                found = self._read(variant, variant.shape[1], variant.shape[0], only)
                if found:
                    if blur is not None:
                        self._last_deblur = blur
                    # Back to the coordinates of the whole picture.
                    return [
                        self._item(c.code_type, c.data,
                                   [[int(x1 + px / factor), int(y1 + py / factor)] for px, py in c.polygon], w, h)
                        for c in found
                    ]
        return []

    def decode(self, image_input: Any, hard_budget_ms: float = 0.0, code_type: str = CODE_TYPE_ALL) -> BarcodeDecodeResponse:
        """
        Decodes all 1D (EAN/UPC/Code128) and 2D (QR) barcodes in the image.
        With hard_budget_ms, a picture where nothing reads gets a second, slower
        pass (sharpening and motion deblurring) of at most that many milliseconds.
        code_type limits the reader to the 2D types ("2d"), the 1D types ("1d") or
        one type (see code_type_options); codes of any other type are ignored.
        """
        if isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(image_input, np.ndarray):
            img = image_input
        else:
            raise ValueError("Unsupported image input type")

        if img is None:
            raise ValueError("Failed to decode image")

        h, w = img.shape[:2]
        only = code_filter(code_type)
        start_time = time.perf_counter()
        codes = self._read(img, w, h, only)
        if not codes and hard_budget_ms > 0:
            codes = self._decode_hard(img, w, h, hard_budget_ms, only)
        decode_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return BarcodeDecodeResponse(
            total_found=len(codes),
            codes=codes,
            decode_time_ms=decode_ms,
            image_width=w,
            image_height=h,
        )

    def draw_annotations(self, image_input: Any, codes: List[BarcodeItem], decode_time_ms: float = 0.0) -> bytes:
        """
        Draws cyan highlight polygon and decoded data banner.
        Returns JPEG bytes.
        """
        if isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        else:
            img = image_input.copy()

        # Top banner
        status_text = f"BARCODE/QR SCANNER: {len(codes)} FOUND | {decode_time_ms}ms"
        cv2.rectangle(img, (0, 0), (img.shape[1], 36), (200, 100, 0), -1)
        cv2.putText(img, status_text, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        for item in codes:
            if item.polygon and len(item.polygon) >= 4:
                pts = np.array(item.polygon, np.int32).reshape((-1, 1, 2))
                cv2.polylines(img, [pts], isClosed=True, color=(255, 255, 0), thickness=3)

            if item.bbox:
                label = f"[{item.code_type}] {item.data}"
                (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
                bx = item.bbox.x1
                by = max(20, item.bbox.y1)
                cv2.rectangle(img, (bx, by - 22), (bx + lw + 8, by), (255, 255, 0), -1)
                cv2.putText(img, label, (bx + 4, by - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)

        _, jpeg = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return jpeg.tobytes()
