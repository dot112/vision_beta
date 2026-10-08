"""What a production line looks like as a Sparkplug device: its ID and its metrics.

The edge node is the server and each line is one of its devices. This module
turns a line's live figures (a plain "view" dictionary, put together by
sparkplug_service) into the metric list a birth or data message carries. It
does no I/O.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Set, Tuple

from app.hardware.mqtt.sparkplug_codec import MAX_ID_LENGTH, DataType, Metric

BD_SEQ = "bdSeq"
REBIRTH = "Node Control/Rebirth"
RUNNING = "Line/Running"
RESET = "Commands/Reset Counters"

# The metrics a product or a code read sets, with the value each has before the first one.
_LAST_DEFAULTS: Tuple[Tuple[str, int, Any], ...] = (
    ("Last Product/Result", DataType.String, ""),
    ("Last Product/Class", DataType.String, ""),
    ("Last Product/Reject Reason", DataType.String, ""),
    ("Last Product/Code", DataType.String, ""),
    ("Last Product/Confidence", DataType.Double, 0.0),
    ("Last Code/Text", DataType.String, ""),
    ("Last Code/Status", DataType.String, ""),
)


def _clean(name: Any) -> str:
    """A name without the characters MQTT topics and Sparkplug IDs reserve."""
    text = str(name if name is not None else "").strip()
    for ch in "/+#":
        text = text.replace(ch, "_")
    return text


def _double(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number, 2) if math.isfinite(number) else 0.0


def device_ids(lines: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """{line id: device ID} for (line id, line name) pairs: the name, made usable and unique."""
    ids: Dict[str, str] = {}
    used: Set[str] = set()
    for line_id, name in lines:
        device = _clean(name)[:MAX_ID_LENGTH] or _clean(line_id)[:MAX_ID_LENGTH] or "line"
        if device in used:
            suffix = f" ({_clean(line_id)})"
            device = device[:MAX_ID_LENGTH - len(suffix)] + suffix
        used.add(device)
        ids[line_id] = device
    return ids


def node_metrics(bd_seq: int, version: str) -> List[Metric]:
    """The edge node's own metrics, as its birth lists them."""
    return [
        Metric(BD_SEQ, DataType.Int64, int(bd_seq)),
        Metric(REBIRTH, DataType.Boolean, False),
        Metric("Node Info/Software Version", DataType.String, str(version)),
    ]


def line_metrics(view: Dict[str, Any]) -> List[Metric]:
    """Every metric of one line, with its current value. No name occurs twice."""
    metrics = [
        Metric(RUNNING, DataType.Boolean, bool(view.get("enabled"))),
        Metric("Line/Name", DataType.String, str(view.get("name") or "")),
        Metric("Counts/Inspected", DataType.Int64, int(view.get("total_inspected") or 0)),
        Metric("Counts/Good", DataType.Int64, int(view.get("good_count") or 0)),
        Metric("Counts/Rejected", DataType.Int64, int(view.get("rejected_count") or 0)),
    ]

    # One metric per class: the classes the cameras are set to, counted or not
    # (a metric has to be in the birth before data can be sent for it), and
    # any other class that was counted.
    counts = view.get("counts") or {}
    class_names = list(dict.fromkeys([*(view.get("classes") or []), *counts]))
    used: Set[str] = set()
    for class_name in sorted(class_names, key=str):
        label = base = _clean(class_name) or "unnamed"
        number = 2
        while label in used:
            label, number = f"{base} ({number})", number + 1
        used.add(label)
        metrics.append(Metric(f"Counts/Class/{label}", DataType.Int64, int(counts.get(class_name) or 0)))

    metrics += [
        Metric("Rate/Products Per Minute", DataType.Double, _double(view.get("products_per_minute"))),
        Metric("Quality/Yield Percent", DataType.Double, _double(view.get("yield_percentage"))),
        Metric("Quality/Defect PPM", DataType.Double, _double(view.get("defect_ppm"))),
    ]

    last = view.get("last") or {}
    for name, datatype, default in _LAST_DEFAULTS:
        value = last.get(name)
        if datatype == DataType.Double:
            number = _double(value)
            value = number if value is not None else default
        else:
            value = str(value) if value is not None else default
        metrics.append(Metric(name, datatype, value))

    used = set()
    for camera in view.get("cameras") or []:
        camera_id = _clean(camera.get("camera_id")) or "camera"
        label = _clean(camera.get("name")) or camera_id
        if label in used:
            label = f"{label} ({camera_id})"
        number = 2
        while label in used:
            label, number = f"{label} ({number})", number + 1
        used.add(label)
        metrics.append(Metric(f"Cameras/{label}/Connected", DataType.Boolean, bool(camera.get("connected"))))

    alarms = sorted(str(code) for code in view.get("alarms") or [])
    metrics += [
        Metric("Alarms/Active Count", DataType.Int32, len(alarms)),
        Metric("Alarms/Active", DataType.String, ",".join(alarms)),
        Metric(RESET, DataType.Boolean, False),
    ]
    return metrics


def last_from_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The "Last Product" or "Last Code" values a product's or a code read's message sets."""
    event = payload.get("event") if isinstance(payload, dict) else None
    if event == "WIRELINE_OBJECT_CROSSED":
        return {
            "Last Product/Result": payload.get("result") or "",
            "Last Product/Class": payload.get("class_name") or "",
            "Last Product/Reject Reason": payload.get("reject_reason") or "",
            "Last Product/Code": payload.get("qr_code") or "",
            "Last Product/Confidence": payload.get("confidence") or 0.0,
        }
    if event in ("QR_CODE_READ", "QR_CODE_NO_READ"):
        return {"Last Code/Text": payload.get("code") or "", "Last Code/Status": payload.get("qr_status") or ""}
    return {}
