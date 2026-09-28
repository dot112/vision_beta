from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple
import cv2
import numpy as np

from app.schemas.qr import BarcodeDecodeResponse, BarcodeItem
from app.schemas.vision import BoundingBox
from app.utils.logger import get_logger

logger = get_logger(__name__)


class QREngine:
    """
    Industrial Barcode & QR Code Localization and Decoding Engine.
    Combines OpenCV QRCodeDetector and BarcodeDetector for 1D/2D scanning.
    """

    def __init__(self):
        self._qr_detector = cv2.QRCodeDetector()
        try:
            self._barcode_detector = cv2.barcode.BarcodeDetector()
        except Exception:
            self._barcode_detector = None

    def decode(self, image_input: Any) -> BarcodeDecodeResponse:
        """
        Decodes all 1D (EAN/UPC/Code128) and 2D (QR) barcodes in the image.
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
        start_time = time.perf_counter()
        codes: List[BarcodeItem] = []

        # 1. Multi-QR Detection & Decoding
        try:
            retval, decoded_info, points, _ = self._qr_detector.detectAndDecodeMulti(img)
            if retval and decoded_info is not None:
                for text, pts in zip(decoded_info, points):
                    if text:
                        pts_list = [[int(pt[0]), int(pt[1])] for pt in pts]
                        xs = [p[0] for p in pts_list]
                        ys = [p[1] for p in pts_list]
                        x1, y1 = max(0, min(xs)), max(0, min(ys))
                        x2, y2 = min(w, max(xs)), min(h, max(ys))

                        codes.append(
                            BarcodeItem(
                                code_type="QR_CODE",
                                data=text,
                                polygon=pts_list,
                                bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2, width=x2 - x1, height=y2 - y1),
                            )
                        )
        except Exception as exc:
            logger.debug("QR decoding exception: %s", exc)

        # 2. 1D Barcode Detection & Decoding (if available)
        if self._barcode_detector is not None:
            try:
                retval, decoded_info, decoded_type, points = self._barcode_detector.detectAndDecode(img)
                if retval and decoded_info:
                    for text, btype, pts in zip(decoded_info, decoded_type, points):
                        if text:
                            pts_list = [[int(pt[0]), int(pt[1])] for pt in pts]
                            xs = [p[0] for p in pts_list]
                            ys = [p[1] for p in pts_list]
                            x1, y1 = max(0, min(xs)), max(0, min(ys))
                            x2, y2 = min(w, max(xs)), min(h, max(ys))

                            codes.append(
                                BarcodeItem(
                                    code_type=str(btype) if btype else "BARCODE_1D",
                                    data=text,
                                    polygon=pts_list,
                                    bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2, width=x2 - x1, height=y2 - y1),
                                )
                            )
            except Exception as exc:
                logger.debug("1D Barcode decoding exception: %s", exc)

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
