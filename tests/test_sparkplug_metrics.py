"""What a production line looks like as a Sparkplug device: its ID and its metrics."""
from __future__ import annotations

from app.hardware.mqtt.sparkplug_codec import DataType, Metric
from app.services.sparkplug_metrics import device_ids, last_from_event, line_metrics, node_metrics

VIEW = {"name": "Line 1", "enabled": True, "total_inspected": 145, "good_count": 142, "rejected_count": 3,
        "products_per_minute": 45.204, "yield_percentage": 97.931, "defect_ppm": 20689.656,
        "counts": {"bottle": 142, "defect": 3}, "classes": ["bottle", "can", "defect"],
        "cameras": [{"camera_id": "c1", "name": "Top", "connected": True}], "alarms": ["camera.fps_low", "camera.disconnected"],
        "last": {"Last Product/Result": "REJECTED"}}


def test_a_lines_metrics():
    got = {m.name: (m.datatype, m.value) for m in line_metrics(VIEW)}
    assert got["Line/Running"] == (DataType.Boolean, True) and got["Line/Name"] == (DataType.String, "Line 1")
    assert got["Counts/Inspected"] == (DataType.Int64, 145) and got["Counts/Rejected"] == (DataType.Int64, 3)
    assert got["Counts/Good"] == (DataType.Int64, 142) and got["Counts/Class/bottle"] == (DataType.Int64, 142)
    assert got["Counts/Class/can"] == (DataType.Int64, 0)            # listed at birth before it is counted
    assert got["Rate/Products Per Minute"] == (DataType.Double, 45.2)
    assert got["Quality/Yield Percent"] == (DataType.Double, 97.93) and got["Quality/Defect PPM"] == (DataType.Double, 20689.66)
    assert got["Last Product/Result"] == (DataType.String, "REJECTED")
    assert got["Last Product/Class"] == (DataType.String, "") and got["Last Product/Confidence"] == (DataType.Double, 0.0)
    assert got["Last Product/Reject Reason"] == (DataType.String, "") and got["Last Product/Code"] == (DataType.String, "")
    assert got["Last Code/Text"] == (DataType.String, "") and got["Last Code/Status"] == (DataType.String, "")
    assert got["Cameras/Top/Connected"] == (DataType.Boolean, True)
    assert got["Alarms/Active Count"] == (DataType.Int32, 2)
    assert got["Alarms/Active"] == (DataType.String, "camera.disconnected,camera.fps_low")
    assert got["Commands/Reset Counters"] == (DataType.Boolean, False)


def test_a_birth_never_lists_a_metric_twice():
    view = {**VIEW, "counts": {"a/b": 1, "": 2}, "classes": ["a/b", "A/B", "a_b"],
            "cameras": [{"camera_id": "c1", "name": "Top", "connected": True}, {"camera_id": "c2", "name": "Top", "connected": False},
                        {"camera_id": "c3", "name": None, "connected": False}, {"camera_id": "c4", "name": "a/b+#", "connected": True}]}
    names = [m.name for m in line_metrics(view)]
    assert len(names) == len(set(names))
    assert {"Cameras/Top/Connected", "Cameras/Top (c2)/Connected", "Cameras/c3/Connected", "Cameras/a_b__/Connected"} <= set(names)
    assert {"Counts/Class/a_b", "Counts/Class/a_b (2)", "Counts/Class/A_B", "Counts/Class/unnamed"} <= set(names)


def test_values_that_cannot_be_sent_become_plain_ones():
    view = {**VIEW, "yield_percentage": float("nan"), "products_per_minute": float("inf"), "defect_ppm": None,
            "last": {"Last Product/Result": None, "Last Product/Confidence": None, "Last Code/Text": None}}
    got = {m.name: m.value for m in line_metrics(view)}
    assert (got["Quality/Yield Percent"], got["Rate/Products Per Minute"], got["Quality/Defect PPM"]) == (0.0, 0.0, 0.0)
    assert (got["Last Product/Result"], got["Last Product/Confidence"], got["Last Code/Text"]) == ("", 0.0, "")


def test_device_ids():
    assert device_ids([("line-1", "Line 1"), ("line-2", "Filling/Capping")]) == {"line-1": "Line 1", "line-2": "Filling_Capping"}
    assert device_ids([("a", "Line"), ("b", "Line"), ("c", " /+# "), ("d", "")]) == {"a": "Line", "b": "Line (b)", "c": "___", "d": "d"}
    assert all(len(v) <= 128 for v in device_ids([("x", "n" * 300)]).values())


def test_node_metrics():
    assert node_metrics(7, "1.0.0") == [Metric("bdSeq", DataType.Int64, 7), Metric("Node Control/Rebirth", DataType.Boolean, False),
                                        Metric("Node Info/Software Version", DataType.String, "1.0.0")]


def test_what_an_event_sets():
    product = {"event": "WIRELINE_OBJECT_CROSSED", "result": "REJECTED", "class_name": "defect", "reject_reason": "vision_class",
               "confidence": 0.94, "qr_code": "SKU-1"}
    assert last_from_event(product) == {"Last Product/Result": "REJECTED", "Last Product/Class": "defect",
                                        "Last Product/Reject Reason": "vision_class", "Last Product/Code": "SKU-1",
                                        "Last Product/Confidence": 0.94}
    assert last_from_event({"event": "QR_CODE_READ", "code": "X", "qr_status": "unknown"}) == {"Last Code/Text": "X", "Last Code/Status": "unknown"}
    assert last_from_event({"event": "QR_CODE_NO_READ", "code": None, "qr_status": "no_read"}) == {"Last Code/Text": "", "Last Code/Status": "no_read"}
    assert last_from_event({"event": "LINE_STARTED"}) == {} and last_from_event({}) == {}
