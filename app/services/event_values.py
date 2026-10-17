"""One set of value names for every event a card can act on.

The names are the same for the value an output action writes, a formula, a
message template and an only-when rule:

    result, result_code, reject_reason   the product's one result (product events)
    class, confidence                    what the counting camera saw
    code, code_status                    the code read (known / unknown / no_read)
    product_id                           the product ID the code holds (by its list's code format)
    good_count, reject_count, total_count, batch, line, camera
    product.name, product.code, product.list, product.<field>   the product of a known code
    input.<value name>                   the last value an input of the line read

A name an event does not have is left out: it is missing, not 0 and not empty text.
"""
from __future__ import annotations

from typing import Any, Dict

from app.services.line_config import PRIMARY_LINE_ID
from app.services.product_fields import flat_product_values


def _runtime(line_id: str) -> Any:
    import app.services.line_service as line_service
    return line_service.line_manager.get(line_id)


def values_for(event: Dict[str, Any]) -> Dict[str, Any]:
    """Every value name the event has, with its value."""
    values: Dict[str, Any] = {}
    result = str(event.get("result") or "").lower()
    if result in ("good", "reject"):
        values["result"] = result
        values["result_code"] = 1 if result == "good" else 2
        if result == "reject" and event.get("reject_reason"):
            values["reject_reason"] = event["reject_reason"]
    class_name = event.get("class_name")
    if class_name in (None, ""):
        classes = event.get("detected_classes") or []
        class_name = classes[0] if classes else None
    if class_name not in (None, ""):
        values["class"] = class_name
    if event.get("confidence") is not None:
        values["confidence"] = event["confidence"]
    if event.get("qr_code") not in (None, ""):
        values["code"] = event["qr_code"]
    if event.get("qr_status"):
        values["code_status"] = event["qr_status"]
    if event.get("product_id") not in (None, ""):
        values["product_id"] = event["product_id"]

    line_id = str(event.get("line_id") or PRIMARY_LINE_ID)
    runtime = _runtime(line_id)
    good, rejected = event.get("good_count"), event.get("reject_count")
    if (good is None or rejected is None) and runtime is not None:
        # An event that is not a product (a line start, an input, a timer): the line's counts now.
        good, rejected = runtime.counter.good_count, runtime.counter.rejected_count
    if good is not None:
        values["good_count"] = good
    if rejected is not None:
        values["reject_count"] = rejected
    total = event.get("total_count")
    if total is None and good is not None and rejected is not None:
        total = int(good) + int(rejected)
    if total is not None:
        values["total_count"] = total
    batch = event.get("batch") or (getattr(runtime, "batch", "") if runtime is not None else "")
    if batch:
        values["batch"] = batch
    if runtime is not None:
        values["line"] = runtime.name
    camera_id = event.get("camera_id")
    if camera_id:
        from app.services.send_dispatcher_service import _camera_name
        values["camera"] = _camera_name(camera_id) or camera_id

    product = event.get("product")
    values.update(flat_product_values(product if isinstance(product, dict) else None))
    if event.get("event_type") == "input":
        # The input that changed: its new and its old value.
        for name in ("value", "previous"):
            if event.get(name) is not None:
                values[f"input.{name}"] = event[name]
    from app.services.plc_input_service import PLCInputService
    for name, value in PLCInputService.values(line_id).items():
        if value is not None:
            values.setdefault(f"input.{name}", value)
    return values
