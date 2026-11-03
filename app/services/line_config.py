"""Production line settings: shape, validation and the version 1 upgrade.

A production line is one camera-in, decisions-out unit. Lines are saved in
``data/system_state.json`` under ``lines``. Line 1 keeps its logic in the old
top-level ``action_trigger`` and ``plc_actions`` keys, so version 1 of the
server still reads the settings file correctly; every other line keeps its
own ``action_trigger`` and ``plc_actions`` inside its entry.

This module has no runtime dependencies so it can be used from the settings
service, the line manager and the API without import cycles.
"""
from __future__ import annotations

import copy
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.engines.text_format import check_format
from app.services.product_fields import VALUE_NAME_RE

PRIMARY_LINE_ID = "line-1"
# Version of the settings file. Each step in _UPGRADES below takes a file one
# version up; a file is upgraded once, when the server first loads it.
#   2  production lines (the version 1 setup becomes Line 1)
#   3  one "Connect cameras when the server starts" switch instead of one per line
#   4  product lists: each camera that reads codes names the list it checks
#   5  send cards instead of one "send results" setting per protocol
#   6  each vision camera names its own model and its own class lists
#   7  each send card on an MQTT channel names its own topic
#   8  each vision camera holds its own counting settings (count lines, direction,
#      tracking, whether defect names reject); the line's count lines are copied into it
SCHEMA_VERSION = 9
MAX_CAMERAS_PER_LINE = 8
CAMERA_ROLES = ("vision", "qr", "ocr", "match")
# A text reader (role "ocr") reads printed text (dates, lot codes) in up to MAX_TEXT_FIELDS fields and
# checks each against a format (app.engines.text_format). Read Continuously it only shows the text;
# with count_mode "trigger" it reads one picture per trigger and is a station, own or joined.
OCR_MODELS = ("tiny", "small", "medium")
OCR_FIND = ("region", "yolo", "all")
OCR_INK = ("dark", "light")
MAX_TEXT_FIELDS = 4
JOIN_DOTS_LIMITS = (3, 15)
TEXT_CONFIDENCE_LIMITS = (0.30, 0.99)
DEFAULT_TEXT_CONFIDENCE = 0.5
MIN_REGION = 0.02
# A pattern check (role "match") finds a trained reference by its edges and compares up to MAX_MATCH_CHECKS
# boxes with good samples (app.engines.pattern_check). Its reference set is files under
# data/match-references (app.services.match_references); read Continuously it only shows the result;
# with count_mode "trigger" it checks one picture per trigger and is a station, own or joined.
MATCH_ANGLE_LIMITS = (0, 45)
DEFAULT_MATCH_ANGLE = 15
MAX_MATCH_CHECKS = 4
MATCH_SENSITIVITY = ("low", "normal", "high")
DEFAULT_MIN_SCORE = 80
DEFAULT_MAX_DEFECT_PX = 30
MAX_DEFECT_PX = 1_000_000
REFERENCE_ID_RE = re.compile(r"ref-[0-9a-f]{8}")
# What a vision camera that is not the counting camera does with its result:
#   own   counts and rejects on its own and fires only the cards that name it
#   join  looks at the counting camera's products, before or after it on the belt:
#         each of its crossings is matched to one of them by travel time, and the
#         product gets one result from both (it does not count on its own)
STATIONS = ("own", "join")
# A joined camera's travel time from the counting camera (negative: it is before
# it), how far from that time a crossing still matches, and what a product it saw
# nothing of gets.
JOIN_OFFSET_LIMIT_MS = 60000
JOIN_WINDOW_LIMITS_MS = (50, 10000)
DEFAULT_JOIN_WINDOW_MS = 500
JOIN_MISSING = ("reject", "ignore")
JOIN_KEYS = ("join_offset_ms", "join_window_ms", "join_missing")
# The model confidence a vision camera may ask for instead of the model's own threshold.
CONFIDENCE_LIMITS = (0.05, 0.99)
QR_DISPATCH_MODES = ("off", "all", "known", "unknown")
# When a QR reader decodes: every frame, one picture each time a product
# crosses wire line 1 or 2 of the line's counting camera, or one picture each
# time a PLC signal card with the action "capture" asks for it.
QR_TRIGGERS = ("continuous", "line1", "line2", "signal")
MAX_QR_TRIGGER_DELAY_MS = 10000
# Which codes a reader looks for: every type, the 2D or the 1D types, or one
# type by its key (QRCODE, DATAMATRIX, EAN13...). The types on offer depend on
# the installed reader, so the API checks a key against it (qr_engine.is_code_type).
CODE_TYPE_GROUPS = ("all", "2d", "1d")
# What a camera that reads codes does with each code:
#   report         show it and fire code triggers; the product count is not touched
#   accept_listed  a code that is not in the camera's product list rejects the product
#   reject_listed  a code that is in the camera's product list rejects the product
QR_ACTIONS = ("report", "accept_listed", "reject_listed")
# A product on which no code is read, on a camera with one of the two checks:
# reject it, or ignore that (the vision camera alone decides).
QR_NO_READ = ("reject", "ignore")

# Why a product was rejected. One product has one result and, when rejected, one reason.
REASON_VISION = "vision_class"
REASON_NOT_LISTED = "code_not_in_list"
REASON_LISTED = "code_in_reject_list"
REASON_NO_CODE = "no_code"
# A joined vision camera saw nothing of the product within its match window, and is set to reject then.
REASON_STATION_NO_RESULT = "station_no_result"
# A camera in trigger mode found nothing in its picture, and is set to reject then.
REASON_NOTHING_DETECTED = "nothing_detected"
# A product check of the line matched the product of the item's code (product_checks).
REASON_PRODUCT_CHECK = "product_check"
# The line's result handshake got no result for a capture request in time, and is set to reject then.
REASON_INSPECTION_TIMEOUT = "inspection_timeout"
# A trigger camera's trigger came and no picture followed (a notice with no picture, or a camera that did not answer).
REASON_NO_PICTURE = "no_picture"
# A text reader's picture: a field's text does not fit its format, a field has no text, the reader could not read.
REASON_TEXT_WRONG = "text_wrong"
REASON_NO_TEXT = "no_text"
REASON_READER_ERROR = "reader_error"
# A pattern check's picture: its main reference is not found, another check does not match, a spot
# differs from the good samples too much, or it could not check.
REASON_PATTERN_NOT_FOUND = "pattern_not_found"
REASON_PATTERN_MISMATCH = "pattern_mismatch"
REASON_PATTERN_DEFECT = "pattern_defect"
REASON_PATTERN_ERROR = "pattern_error"
REJECT_REASONS = (REASON_VISION, REASON_NOT_LISTED, REASON_LISTED, REASON_NO_CODE, REASON_STATION_NO_RESULT,
                  REASON_NOTHING_DETECTED, REASON_PRODUCT_CHECK, REASON_INSPECTION_TIMEOUT, REASON_NO_PICTURE,
                  REASON_TEXT_WRONG, REASON_NO_TEXT, REASON_READER_ERROR, REASON_PATTERN_NOT_FOUND,
                  REASON_PATTERN_MISMATCH, REASON_PATTERN_DEFECT, REASON_PATTERN_ERROR)
# A sorting verification camera saw a product in the other outfeed (a record's reason; no product gets it).
REASON_SORTING_MISMATCH = "sorting_mismatch"
# The reasons that come from a product's code.
CODE_REASONS = (REASON_NOT_LISTED, REASON_LISTED, REASON_NO_CODE)

# What a PLC WRITE card writes ("value_source"): its fixed number (as before),
# or a value of the product or the line at the moment of the write. The last five:
# a field of the item's product (value_field), the code, the product's name, the
# last value of an input of the line (value_input) and a formula (app.services.formula).
PLC_VALUE_SOURCES = ("fixed", "result_code", "good_count", "reject_count", "total_count",
                     "class_index", "reject_reason_code", "batch",
                     "product_field", "code", "product_name", "input", "formula")
# What a card of those five sources does when there is no value (an unknown code, an
# empty field, a formula that gives none): skip the write, or write default_value.
IF_MISSING = ("skip", "default")
# A rejected product's reason as a number for the PLC (0: not rejected, 9: any other reason).
REJECT_REASON_CODES = {REASON_VISION: 1, REASON_NOT_LISTED: 2, REASON_LISTED: 3, REASON_NO_CODE: 4,
                       REASON_STATION_NO_RESULT: 5, REASON_NOTHING_DETECTED: 6, REASON_PRODUCT_CHECK: 7,
                       REASON_INSPECTION_TIMEOUT: 8, REASON_NO_PICTURE: 10,
                       REASON_TEXT_WRONG: 11, REASON_NO_TEXT: 12, REASON_READER_ERROR: 13,
                       REASON_PATTERN_NOT_FOUND: 14, REASON_PATTERN_MISMATCH: 15, REASON_PATTERN_DEFECT: 16,
                       REASON_PATTERN_ERROR: 17}
# Product checks: up to MAX_PRODUCT_CHECKS per line, each "reject when these rules pass".
MAX_PRODUCT_CHECKS = 10
# A product check decides the result, so its rules cannot use it.
CHECK_FORBIDDEN_NAMES = ("result", "result_code", "reject_reason")
MAX_PRODUCT_CHECK_NAME_LENGTH = 64
OTHER_REJECT_REASON_CODE = 9
# PLC card settings an older client does not know of: kept when it leaves them out.
PLC_CARD_KEPT_KEYS = ("value_source", "strobe_address", "strobe_pulse_ms", "confirm_input_id", "confirm_timeout_ms",
                      "value_field", "value_input", "formula", "if_missing", "default_value", "only_when", "input_id")
# Reject confirmation: a PLC card may name a signal card of its line with the action
# "confirm_card" (confirm_input_id) and how long it waits for that signal's edge after
# its output was sent (confirm_timeout_ms).
CONFIRM_TIMEOUT_LIMITS_MS = (50, 10000)
DEFAULT_CONFIRM_TIMEOUT_MS = 1000

DEFAULT_SYNC_WINDOW_MS = 500
DEFAULT_QR_HOLD_MS = 1500
# The list the product codes of a server without lists were moved into
# (the same id as app.db.models.product.DEFAULT_LIST_ID).
DEFAULT_PRODUCT_LIST_ID = "list-1"

_LINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Fields a line entry may carry; anything else sent by a client is dropped.
_LOGIC_KEYS = ("action_trigger", "plc_actions", "send_actions")

# Kept by the server in a line's entry, never taken from a client: when the
# line's counters were last reset (ISO UTC), and when single classes were. A
# counter gets its totals back from the product records since then.
COUNTS_RESET_KEYS = ("counts_reset_at", "counts_reset_classes")
# The line's batch (work order) number is kept the same way: only the batch
# endpoint, the dashboard field and a "set batch" PLC signal change it. It is as
# long as the batch column of the product records.
BATCH_MAX_LENGTH = 64


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_batch(value: Any) -> str:
    """A batch number as the text that is saved; "" for none. Raises ValueError.

    A number read from a PLC is written as people write it (1001.0 is "1001").
    A PLC string padded with NULs to its full length loses the padding.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("The batch number must be text or a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("The batch number must be text or a number")
        value = int(value) if value.is_integer() else value
    text = str(value).strip().rstrip("\x00").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("The batch number contains a character that is not allowed (a line break or a control character)")
    if len(text) > BATCH_MAX_LENGTH:
        raise ValueError(f"The batch number must be {BATCH_MAX_LENGTH} characters or fewer")
    return text

# The kinds of channel a send card can send to (Connections).
SEND_PROTOCOLS = ("mqtt", "tcp", "webhook")
# When a card fires: the same choices for PLC action cards and send cards
# (card_triggers decides what each means).
CARD_TRIGGERS = (
    "cross_line", "good_counter", "reject_counter", "class",
    "qr_read", "qr_known", "qr_unknown", "qr_no_read", "line_state", "alarm", "timer", "input",
    "sorting_mismatch", "sorting_verified", "handshake_fault",
)
# "sorting_mismatch" / "sorting_verified": a sorting verification camera saw a product in the
# other outfeed / in its own. "handshake_fault": the result handshake went wrong (no result in
# time under "fault", no ack in time, a capture that skipped the ack).
# "input": an input of the line changed (input_id): off to on, on to off, or any change.
INPUT_CONDITIONS = ("on", "off", "change")
# "timer" (send cards only): a message every set_value ms, while the line runs or always.
TIMER_CONDITIONS = ("while_running", "always")
TIMER_MIN_MS = 100
TIMER_MAX_MS = 86_400_000           # 24 hours
TIMER_DEFAULT_MS = 1000
MAX_SEND_CARDS = 32
# The settings send cards replaced, once kept in a line's action_trigger.
OLD_SEND_KEYS = (
    "send_mqtt", "mqtt_topic", "mqtt_endpoint_id", "send_tcp", "tcp_host", "tcp_port", "tcp_endpoint_id",
    "send_webhook", "webhook_url", "webhook_headers", "webhook_endpoint_id",
    "dispatch_trigger", "dispatched_fields",
    "mqtt_dispatch_trigger", "mqtt_dispatched_fields", "tcp_dispatch_trigger", "tcp_dispatched_fields",
    "webhook_dispatch_trigger", "webhook_dispatched_fields",
    "mqtt_qr_dispatch", "tcp_qr_dispatch", "webhook_qr_dispatch",
)

# What a vision camera holds since version 6: the model that runs on it, and
# which of that model's classes count as products and which as defects.
CAMERA_MODEL_KEYS = ("model_id", "expected_classes", "defect_classes")
# Which way products cross the count lines: A then B, B then A, or either way.
COUNT_DIRECTIONS = ("forward", "backward", "both")
# How a vision camera makes its products:
#   track    moving products cross its count lines (what every camera did before)
#   trigger  one picture per trigger (a PLC signal or the API), for indexing machines,
#            rotary tables and parts that stand still
COUNT_MODES = ("track", "trigger")
# What a triggered picture in which nothing was found counts as: no product, a reject or a good product.
NO_PRODUCT = ("ignore", "reject", "good")
# Where a trigger camera's picture comes from:
#   system  the server takes it when a trigger happens in the system (after the Picture delay)
#   camera  an industrial camera triggers itself and sends it; a trigger notice (a "Take a picture"
#           input) may tell the server it is coming, and Wait for the picture is how long it may take
PICTURE_SOURCES = ("system", "camera")
PICTURE_DELAY_LIMITS_MS = (0, 5000)
PICTURE_WAIT_LIMITS_MS = (50, 10000)
DEFAULT_PICTURE_WAIT_MS = 1000
# Tracking settings a vision camera may change: (lowest, highest, whole number).
TRACKING_LIMITS = {
    "track_high_thresh": (0.05, 1.0, False),
    "track_low_thresh": (0.01, 0.9, False),
    "match_threshold": (0.1, 2.0, False),
    "max_missed_frames": (1, 120, True),
    "max_speed_pixels": (10.0, 1000.0, False),
    "min_hits": (1, 10, True),
    "position_tolerance": (20.0, 600.0, False),
}
TRACKING_KEYS = tuple(TRACKING_LIMITS)
# Settings of a vision camera that a client written before them does not send:
# a camera sent without one keeps what is saved.
CAMERA_KEPT_KEYS = CAMERA_MODEL_KEYS + (
    "name_based_defects", "direction", "tracking", "line1_position", "line2_position", "orientation",
    "confidence", "station", "count_mode", "no_product",
    "picture_source", "picture_delay_ms", "notice_trigger", "picture_wait_ms",
) + JOIN_KEYS
# Settings of a camera that reads codes that a client written before them does not send.
READER_KEPT_KEYS = ("qr_trigger_review", "code_sets_batch", "sorting_lane")
# Settings of a text reader that a client written before them does not send: kept when left out.
TEXT_READER_KEPT_KEYS = ("ocr", "count_mode", "station", "picture_source", "picture_delay_ms", "notice_trigger",
                         "picture_wait_ms") + JOIN_KEYS
# And of a pattern check.
PATTERN_CHECK_KEPT_KEYS = ("match",) + TEXT_READER_KEPT_KEYS[1:]
# A camera that reads codes may watch one outfeed instead (sorting verification): its reads check
# that each product went the way its result sent it. One camera per outfeed on a line.
SORTING_LANES = ("accepted", "rejected")
# The count lines a vision camera copies from its line's action_trigger (version 8 step).
COUNT_LINE_KEYS = ("line1_position", "line2_position", "orientation")
# Once kept per line in action_trigger; an older client that still sends them is ignored.
OLD_CLASS_KEYS = ("expected_classes", "defect_classes")
MAX_CLASSES_PER_LIST = 200
# The class lists a line used before version 6 when its settings named none.
_V5_EXPECTED_CLASSES = ("bottle", "can", "cup")
_V5_DEFECT_CLASSES = ("defect", "scratch", "broken")


def new_line_id() -> str:
    return f"line-{uuid.uuid4().hex[:8]}"


def _bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be true or false")


def _number(value: Any, label: str, default: float, minimum: float, maximum: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if not (minimum <= number <= maximum):
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return number


def _position(value: Any, label: str) -> Optional[float]:
    if value is None or value == "":
        return None
    return _number(value, label, 0.5, 0.0, 1.0)


def reads_codes(camera: Dict[str, Any]) -> bool:
    """A QR reader, or a vision camera that also reads codes."""
    return camera.get("role") == "qr" or bool(camera.get("read_codes"))


def sorting_lane(camera: Dict[str, Any]) -> Optional[str]:
    """'accepted' or 'rejected' for a camera that verifies sorting at that outfeed, else None."""
    lane = camera.get("sorting_lane")
    return lane if reads_codes(camera) and lane in SORTING_LANES else None


def product_reader(camera: Dict[str, Any]) -> bool:
    """A camera whose codes belong to the products being inspected (not a sorting verification camera)."""
    return reads_codes(camera) and sorting_lane(camera) is None


def reads_codes_triggered(camera: Dict[str, Any]) -> bool:
    """A camera whose code reads reach its line: it reads codes on a wire line or when an input asks.

    A camera that reads Continuously (or names no capture) only shows its codes.
    """
    return reads_codes(camera) and (camera.get("qr_trigger") or "continuous") != "continuous"


def code_check(camera: Dict[str, Any]) -> Optional[str]:
    """'accept_listed' or 'reject_listed' when this camera's codes decide good or reject, else None."""
    action = camera.get("qr_action")
    return action if product_reader(camera) and action in ("accept_listed", "reject_listed") else None


def code_checks(cameras: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{camera_id: {"action", "no_read"}} for every camera whose codes decide good or reject."""
    return {
        cam["camera_id"]: {"action": code_check(cam), "no_read": cam.get("qr_no_read") or "ignore"}
        for cam in cameras
        if code_check(cam)
    }


def code_verdict(action: Optional[str], known: bool) -> Optional[str]:
    """Why a code rejects its product under a camera's action, or None when the code passes."""
    if action == "accept_listed" and not known:
        return REASON_NOT_LISTED
    if action == "reject_listed" and known:
        return REASON_LISTED
    return None


def product_verdict(vision_reject: bool, read: Optional[Dict[str, Any]],
                    checks: Dict[str, Dict[str, str]],
                    stations: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
                    missing: Optional[Dict[str, str]] = None) -> Tuple[bool, Optional[str]]:
    """(reject, reason) for one product, decided once (product_result() also says which camera rejected it).

    ``vision_reject``: the counting camera found a defect (False on a line without one).
    ``read``: the code paired with the product (``camera_id``, ``known``), or None when none was read.
    ``checks``: code_checks() of the line.
    ``stations``: what each joined camera saw of the product, in card order:
    ``{camera_id: {"is_defect", "class_name", "confidence"}}``, or None for a
    camera that saw nothing of it in its match window.
    ``missing``: each joined camera's "When no result" (``"reject"`` / ``"ignore"``).

        counting  joined cameras      code check          result
        good      all good            passed              good
        good      all good            failed              reject (the code's reason)
        reject    any                 any                 reject (vision_class)
        good      one rejects         any                 reject (vision_class, that camera)
        any       ...                 no code, "ignore"   as the cameras say
        any       ...                 no code, "reject"   reject (no_code)
        good      one saw nothing,    passed              good
                  "ignore"
        good      one saw nothing,    passed              reject (station_no_result)
                  "reject"

    When several reject, the reason is the first of: the counting camera, the
    joined cameras in card order, the code, a joined camera that saw nothing.
    """
    reject, reason, _ = product_result(vision_reject, read, checks, stations, missing)
    return reject, reason


def product_result(vision_reject: bool, read: Optional[Dict[str, Any]],
                   checks: Dict[str, Dict[str, str]],
                   stations: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
                   missing: Optional[Dict[str, str]] = None,
                   counting_camera_id: Optional[str] = None,
                   product_check: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """(reject, reason, the camera whose result rejected the product) for one product; see product_verdict().

    ``product_check``: the name of the line's product check that matched the product of the
    item's code, if one did. It rejects only when nothing before it did.
    """
    if vision_reject:
        return True, REASON_VISION, counting_camera_id
    stations = stations or {}
    for camera_id, seen in stations.items():
        if seen is not None and seen.get("is_defect"):
            return True, REASON_VISION, camera_id
    if read is not None:
        check = checks.get(read.get("camera_id"))
        reason = code_verdict(check["action"], bool(read.get("known"))) if check else None
        if reason is not None:
            return True, reason, read.get("camera_id")
    else:
        refusing = next((camera_id for camera_id, check in checks.items() if check["no_read"] == "reject"), None)
        if refusing is not None:
            return True, REASON_NO_CODE, refusing
    missing = missing or {}
    for camera_id, seen in stations.items():
        if seen is None and missing.get(camera_id) == "reject":
            return True, REASON_STATION_NO_RESULT, camera_id
    if product_check:
        return True, REASON_PRODUCT_CHECK, read.get("camera_id") if read else None
    return False, None, None


def _class_names(value: Any, label: str) -> List[str]:
    """A list of model class names, as the model writes them, each once."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_CLASSES_PER_LIST:
        raise ValueError(f"{label} must be a list of at most {MAX_CLASSES_PER_LIST} class names")
    names: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > 128:
            raise ValueError(f"{label} must be a list of class names")
        name = item.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def _code_type(value: Any, label: str) -> str:
    text = str(value or "all").strip()
    if text.lower() in CODE_TYPE_GROUPS:
        return text.lower()
    key = re.sub(r"[^A-Z0-9]", "", text.split(".")[-1].upper())
    if not key or len(key) > 32:
        raise ValueError(f"{label} code type is not valid")
    return key


def normalize_camera(raw: Any, index: int) -> Dict[str, Any]:
    label = f"Camera {index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    camera_id = str(raw.get("camera_id") or "").strip()
    if not camera_id or len(camera_id) > 64:
        raise ValueError(f"{label} needs a camera_id")
    role = str(raw.get("role") or "vision").strip().lower()
    if role not in CAMERA_ROLES:
        raise ValueError(f"{label} role must be 'vision', 'qr', 'ocr' or 'match'")
    camera: Dict[str, Any] = {
        "camera_id": camera_id,
        "role": role,
        "counting": _bool(raw.get("counting"), f"{label} counting", False) if role == "vision" else False,
        "qr_hold_ms": int(_number(raw.get("qr_hold_ms"), f"{label} QR hold time", DEFAULT_QR_HOLD_MS, 100, 60000)),
    }
    # A vision camera may read codes too, on its own thread beside the model.
    if role == "vision" and _bool(raw.get("read_codes"), f"{label} read codes", False):
        camera["read_codes"] = True
    if reads_codes(camera):
        trigger = str(raw.get("qr_trigger") or "continuous").strip().lower()
        if trigger not in QR_TRIGGERS:
            raise ValueError(f"{label} capture must be 'continuous', 'line1', 'line2' or 'signal'")
        camera["qr_trigger"] = trigger
        camera["qr_trigger_delay_ms"] = int(_number(
            raw.get("qr_trigger_delay_ms"), f"{label} capture delay", 0, 0, MAX_QR_TRIGGER_DELAY_MS,
        ))
        camera["code_type"] = _code_type(raw.get("code_type"), label)
        # The product list this camera checks its codes against (Products page); None = no list.
        list_id = raw.get("product_list_id")
        if list_id not in (None, "") and (not isinstance(list_id, str) or len(list_id.strip()) > 36):
            raise ValueError(f"{label} product list is not valid")
        camera["product_list_id"] = list_id.strip() if isinstance(list_id, str) and list_id.strip() else None
        action = str(raw.get("qr_action") or "report").strip().lower()
        if action not in QR_ACTIONS:
            raise ValueError(f"{label} action must be 'report', 'accept_listed' or 'reject_listed'")
        camera["qr_action"] = action
        if action != "report" and not camera["product_list_id"]:
            raise ValueError(f"{label}: choose the product list its codes are checked against")
        no_read = str(raw.get("qr_no_read") or "ignore").strip().lower()
        if no_read not in QR_NO_READ:
            raise ValueError(f"{label}: when no code is read must be 'reject' or 'ignore'")
        camera["qr_no_read"] = no_read
        # Set by the version 9 upgrade: since then this camera only shows its codes, and the
        # user has not chosen when it reads them. Kept until it reads on a trigger, or is cleared.
        if trigger == "continuous" and raw.get("qr_trigger_review") is True:
            camera["qr_trigger_review"] = True
        # A listed product's batch (from its code, by its list's code format) becomes the line's batch.
        if _bool(raw.get("code_sets_batch"), f"{label} code sets the line's batch", False):
            camera["code_sets_batch"] = True
        # Sorting verification: the camera watches one outfeed. It reports, it never decides,
        # so it does not check codes, sets no batch, and reads Continuously too (no v9 flag).
        lane = raw.get("sorting_lane")
        if lane not in (None, ""):
            lane = str(lane).strip().lower()
            if lane not in SORTING_LANES:
                raise ValueError(f"{label}: the outfeed it verifies must be 'accepted' or 'rejected' (or none)")
            if action != "report":
                raise ValueError(f"{label} verifies sorting, so it cannot check codes: set what its codes do to Report")
            camera["sorting_lane"] = lane
            camera.pop("qr_trigger_review", None)
            camera.pop("code_sets_batch", None)
    if role == "vision":
        # The model that runs on this camera (AI models page). None only on a
        # camera saved before models were picked per camera: nothing is detected on it.
        model_id = raw.get("model_id")
        if model_id not in (None, "") and (not isinstance(model_id, str) or len(model_id.strip()) > 64):
            raise ValueError(f"{label} vision model is not valid")
        camera["model_id"] = model_id.strip() if isinstance(model_id, str) and model_id.strip() else None
        # Which of the model's classes count as products, and which as defects.
        camera["expected_classes"] = _class_names(raw.get("expected_classes"), f"{label} products to count")
        camera["defect_classes"] = _class_names(raw.get("defect_classes"), f"{label} defects to reject")
        # A second vision camera may use its own wirelines; blank means the line's.
        for key in ("line1_position", "line2_position"):
            pos = _position(raw.get(key), f"{label} {key.replace('_', ' ')}")
            if pos is not None:
                camera[key] = pos
        orientation = raw.get("orientation")
        if orientation not in (None, ""):
            if orientation not in ("horizontal", "vertical"):
                raise ValueError(f"{label} orientation must be 'horizontal' or 'vertical'")
            camera["orientation"] = orientation
        direction = raw.get("direction")
        if direction not in (None, ""):
            if direction not in COUNT_DIRECTIONS:
                raise ValueError(f"{label} count direction must be 'forward', 'backward' or 'both'")
            camera["direction"] = direction
        tracking = _tracking(raw.get("tracking"), label)
        if tracking:
            camera["tracking"] = tracking
        # A class whose name contains "defect", "scratch" or "broken" also rejects
        # (what every camera did before this could be switched off). Left out, a
        # camera does what cameras always did: the name rule applies.
        if raw.get("name_based_defects") is not None:
            camera["name_based_defects"] = _bool(raw.get("name_based_defects"), f"{label} defects by name", True)
        # The model confidence below which a detection is ignored; blank = the model's own.
        confidence = raw.get("confidence")
        if confidence not in (None, ""):
            low, high = CONFIDENCE_LIMITS
            camera["confidence"] = round(_number(confidence, f"{label} confidence threshold", low, low, high), 4)
        station = raw.get("station")
        if station not in (None, ""):
            station = str(station).strip().lower()
            if station not in STATIONS:
                raise ValueError(f"{label} station must be 'own' (its own result) or 'join' (joins the product result)")
            camera["station"] = station
        if camera.get("station") == "join":
            camera.update(_join_settings(raw, label))
        # One picture per trigger instead of tracking. Left out, a camera tracks, as before.
        count_mode = raw.get("count_mode")
        if count_mode not in (None, ""):
            count_mode = str(count_mode).strip().lower()
            if count_mode not in COUNT_MODES:
                raise ValueError(f"{label} count mode must be 'track' (moving products) or 'trigger' (one picture per trigger)")
            if count_mode == "trigger":
                no_product = str(raw.get("no_product") or "ignore").strip().lower()
                if no_product not in NO_PRODUCT:
                    raise ValueError(f"{label}: when nothing is found must be 'ignore', 'reject' or 'good'")
                camera["count_mode"] = "trigger"
                camera["no_product"] = no_product
                camera.update(_picture_settings(raw, label))
    if role == "ocr":
        camera.update(_text_reader_reading(raw, label))
    if role == "match":
        camera.update(_reading_and_station(raw, label))
        camera["match"] = normalize_match(raw.get("match"), label)
    # The triggers ("Take a picture" inputs, by their number) that take this camera's picture.
    sent = raw.get("triggers")
    if sent not in (None, "", []) and (camera.get("count_mode") == "trigger"
                                        or (reads_codes(camera) and camera.get("qr_trigger") == "signal")):
        numbers = [_trigger_number(value) for value in sent] if isinstance(sent, list) else [None]
        if None in numbers:
            raise ValueError(f"{label}: its triggers must be a list of trigger numbers from 1 to {MAX_TRIGGER_NO}")
        camera["triggers"] = sorted(set(numbers))
    if camera.get("picture_source") == "camera":
        # A camera that triggers itself has no trigger of the system: only its notice, if it has one.
        notice = camera.get("notice_trigger")
        camera["triggers"] = [notice] if notice else []
    return camera


def _text_reader_reading(raw: Dict[str, Any], label: str) -> Dict[str, Any]:
    """A text reader's reading mode, station and picture settings (_reading_and_station) and its ``ocr``
    settings."""
    settings = _reading_and_station(raw, label)
    settings["ocr"] = normalize_text_reader(raw.get("ocr"), label)
    return settings


def _reading_and_station(raw: Dict[str, Any], label: str) -> Dict[str, Any]:
    """A text reader's or a pattern check's mode (count_mode "trigger", else Continuously), and its station
    and picture settings when it takes one picture per trigger."""
    settings: Dict[str, Any] = {}
    count_mode = raw.get("count_mode")
    if count_mode not in (None, ""):
        count_mode = str(count_mode).strip().lower()
        if count_mode not in COUNT_MODES:
            raise ValueError(f"{label} count mode must be 'track' (continuously) or 'trigger' (one picture per trigger)")
    if count_mode == "trigger":
        settings["count_mode"] = "trigger"
        settings.update(_picture_settings(raw, label))
        station = str(raw.get("station") or "own").strip().lower()
        if station not in STATIONS:
            raise ValueError(f"{label} station must be 'own' (its own result) or 'join' (joins the product result)")
        settings["station"] = station
        if station == "join":
            settings.update(_join_settings(raw, label))
    return settings


def _whole(value: Any, what: str, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not low <= value <= high:
        raise ValueError(f"{what} must be a whole number from {low} to {high}")
    return int(value)


def _match_check(raw: Any, index: int, label: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} check {index + 1} must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not VALUE_NAME_RE.fullmatch(name.strip()):
        raise ValueError(f"{label} check {index + 1}: the name must start with a letter and have only letters, "
                         f"digits and _ (at most 32)")
    name = name.strip()
    if name.lower() == "score":
        raise ValueError(f"{label}: a check cannot be named 'score' (match.score is the reference's score)")
    where = f"{label} check '{name}'"
    sensitivity = str(raw.get("sensitivity") or "normal").strip().lower()
    if sensitivity not in MATCH_SENSITIVITY:
        raise ValueError(f"{where}: the sensitivity must be 'low', 'normal' or 'high'")
    return {
        "name": name,
        "min_score": _whole(raw.get("min_score"), f"{where}: the lowest match (%)", DEFAULT_MIN_SCORE, 0, 100),
        "max_defect_px": _whole(raw.get("max_defect_px"), f"{where}: the biggest defect (px)", DEFAULT_MAX_DEFECT_PX,
                                0, MAX_DEFECT_PX),
        "colour": _bool(raw.get("colour"), f"{where} colour", False),
        "sensitivity": sensitivity,
    }


def normalize_match(raw: Any, label: str) -> Dict[str, Any]:
    """A pattern check's ``match`` settings: its trained reference set, where it searches, how far it
    searches turned, and 1 to MAX_MATCH_CHECKS checks (name, lowest Match %, biggest defect, colour,
    sensitivity), in the order of the set's checks."""
    if not isinstance(raw, dict):
        raw = {}
    reference = raw.get("reference_id")
    if not isinstance(reference, str) or not REFERENCE_ID_RE.fullmatch(reference.strip()):
        raise ValueError(f"{label}: train its reference on Pattern and checks")
    area = raw.get("search_area")
    if area in (None, "", []):
        area = None
    else:
        bad = ValueError(f"{label}: the search area must be [x1, y1, x2, y2] inside the picture (0 to 1), "
                         f"at least {MIN_REGION:g} wide and high")
        if not isinstance(area, (list, tuple)) or len(area) != 4 \
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in area):
            raise bad
        x1, y1, x2, y2 = (float(v) for v in area)
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1) or x2 - x1 < MIN_REGION or y2 - y1 < MIN_REGION:
            raise bad
        area = [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]
    low, high = MATCH_ANGLE_LIMITS
    angle = _whole(raw.get("angle"), f"{label}: the angle searched (degrees)", DEFAULT_MATCH_ANGLE, low, high)
    checks = raw.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_MATCH_CHECKS:
        raise ValueError(f"{label} needs 1 to {MAX_MATCH_CHECKS} checks")
    normalized = [_match_check(check, i, label) for i, check in enumerate(checks)]
    seen = set()
    for check in normalized:
        if check["name"].lower() in seen:
            raise ValueError(f"{label}: two checks are named '{check['name']}'")
        seen.add(check["name"].lower())
    return {"reference_id": reference.strip(), "search_area": area, "angle": angle, "checks": normalized}


def is_pattern_check(camera: Dict[str, Any]) -> bool:
    return camera.get("role") == "match"


def require_match_references(line: Dict[str, Any], info: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    """A line is not saved with a pattern check whose reference set is missing, has another number of checks,
    or is bigger than its search area. ``info``: match_references.set_info."""
    for index, cam in enumerate(line.get("cameras") or []):
        if not is_pattern_check(cam):
            continue
        match = cam["match"]
        meta = info(match["reference_id"])
        if meta is None:
            raise ValueError(f"Camera {index + 1}: train its reference on Pattern and checks")
        if meta["checks"] != len(match["checks"]):
            raise ValueError(f"Camera {index + 1}: its reference set has {meta['checks']} checks, not "
                             f"{len(match['checks'])}: train it again")
        area = match.get("search_area")
        if area:
            width, height = meta["picture_size"]
            ref_w, ref_h = meta["reference_size"]
            if (area[2] - area[0]) * width < ref_w or (area[3] - area[1]) * height < ref_h:
                raise ValueError(f"Camera {index + 1}: its search area is smaller than its reference")


def _text_field(raw: Any, index: int, label: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} text field {index + 1} must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not VALUE_NAME_RE.fullmatch(name.strip()):
        raise ValueError(f"{label} text field {index + 1}: the name must start with a letter and have only "
                         f"letters, digits and _ (at most 32)")
    name = name.strip()
    where = f"{label} text field '{name}'"
    find = str(raw.get("find") or "").strip().lower()
    if find not in OCR_FIND:
        raise ValueError(f"{where} must find its text in a 'region', by 'yolo' class or in 'all' the picture")
    field: Dict[str, Any] = {"name": name, "find": find}
    if find == "region":
        region = raw.get("region")
        bad = ValueError(f"{where}: the region must be [x1, y1, x2, y2] inside the picture (0 to 1), "
                         f"at least {MIN_REGION:g} wide and high")
        if not isinstance(region, (list, tuple)) or len(region) != 4 \
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in region):
            raise bad
        x1, y1, x2, y2 = (float(v) for v in region)
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1) or x2 - x1 < MIN_REGION or y2 - y1 < MIN_REGION:
            raise bad
        field["region"] = [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]
    elif find == "yolo":
        cls = raw.get("yolo_class")
        if not isinstance(cls, str) or not cls.strip() or len(cls.strip()) > 128:
            raise ValueError(f"{where}: choose the YOLO class it reads in")
        field["yolo_class"] = cls.strip()
    field["format"] = check_format(raw.get("format"), where)
    return field


def normalize_text_reader(raw: Any, label: str) -> Dict[str, Any]:
    """A text reader's ``ocr`` settings: model, YOLO model, orientation, Join dots, ink, lowest
    confidence, and 1 to MAX_TEXT_FIELDS fields (name, where to find the text, format)."""
    if not isinstance(raw, dict):
        raise ValueError(f"{label} text reader settings must be an object")
    model = str(raw.get("model") or "small").strip().lower()
    if model not in OCR_MODELS:
        raise ValueError(f"{label} reader model must be 'tiny', 'small' or 'medium'")
    low, high = JOIN_DOTS_LIMITS
    join = raw.get("join_dots")
    join = 0 if join in (None, "") else join
    if isinstance(join, bool) or not isinstance(join, (int, float)) or join != int(join) \
            or (join and not (low <= join <= high and int(join) % 2 == 1)):
        raise ValueError(f"{label} join dots must be 0 (off) or an odd size from {low} to {high} px")
    ink = str(raw.get("ink") or "dark").strip().lower()
    if ink not in OCR_INK:
        raise ValueError(f"{label} ink must be 'dark' or 'light'")
    low, high = TEXT_CONFIDENCE_LIMITS
    confidence = _number(raw.get("min_confidence"), f"{label} lowest confidence", DEFAULT_TEXT_CONFIDENCE, low, high)
    fields = raw.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_TEXT_FIELDS:
        raise ValueError(f"{label} needs 1 to {MAX_TEXT_FIELDS} text fields")
    normalized = [_text_field(field, i, label) for i, field in enumerate(fields)]
    seen = set()
    for field in normalized:
        if field["name"].lower() in seen:
            raise ValueError(f"{label}: two text fields are named '{field['name']}'")
        seen.add(field["name"].lower())
    locator = None
    if any(field["find"] == "yolo" for field in normalized):
        locator = raw.get("locator_model_id")
        if not isinstance(locator, str) or not locator.strip() or len(locator.strip()) > 64:
            raise ValueError(f"{label}: choose the YOLO model whose boxes its fields read in")
        locator = locator.strip()
    return {
        "model": model,
        "locator_model_id": locator,
        "upside_down": _bool(raw.get("upside_down"), f"{label} text can be upside down", False),
        "join_dots": int(join),
        "ink": ink,
        "min_confidence": round(confidence, 2),
        "fields": normalized,
    }


def is_text_reader(camera: Dict[str, Any]) -> bool:
    return camera.get("role") == "ocr"


def picture_per_trigger(camera: Dict[str, Any]) -> bool:
    """A vision camera, a text reader or a pattern check that takes one picture per trigger (count_mode "trigger")."""
    return camera.get("role", "vision") in ("vision", "ocr", "match") and camera.get("count_mode") == "trigger"


def _picture_settings(raw: Dict[str, Any], label: str) -> Dict[str, Any]:
    """A trigger camera's picture source (left out when not sent), delay, trigger notice and wait."""
    settings: Dict[str, Any] = {}
    source = raw.get("picture_source")
    if source not in (None, ""):
        source = str(source).strip().lower()
        if source not in PICTURE_SOURCES:
            raise ValueError(f"{label} picture source must be 'system' (the server takes it) or 'camera' (the camera triggers itself)")
        settings["picture_source"] = source
    low, high = PICTURE_DELAY_LIMITS_MS
    settings["picture_delay_ms"] = int(round(_number(raw.get("picture_delay_ms"), f"{label} picture delay (ms)", low, low, high)))
    low, high = PICTURE_WAIT_LIMITS_MS
    settings["picture_wait_ms"] = int(round(_number(
        raw.get("picture_wait_ms"), f"{label} wait for the picture (ms)", DEFAULT_PICTURE_WAIT_MS, low, high,
    )))
    notice = raw.get("notice_trigger")
    if notice in (None, ""):
        settings["notice_trigger"] = None
    else:
        settings["notice_trigger"] = _trigger_number(notice)
        if settings["notice_trigger"] is None:
            raise ValueError(f"{label}: its trigger notice must be a trigger number from 1 to {MAX_TRIGGER_NO} (or none)")
    return settings


def picture_source(camera: Dict[str, Any], trigger_mode: str = "off") -> str:
    """Where a trigger camera's picture comes from: "system" or "camera".

    The saved value; a camera saved before the card held it follows its camera's Trigger
    (``trigger_mode``): "hardware" is "camera" (without a notice), anything else "system".
    """
    saved = camera.get("picture_source")
    if saved in PICTURE_SOURCES:
        return saved
    return "camera" if trigger_mode == "hardware" else "system"


def trigger_mode_for_card(camera: Dict[str, Any], current_mode: str = "off") -> str:
    """The Trigger an industrial camera gets from its line's card: "hardware", "software" or "off".

    One picture per trigger from the camera is "hardware", from the system "software"; any other
    count mode leaves the camera free running ("off"). A card saved without a picture source
    follows the camera's ``current_mode`` (picture_source): on "hardware" it stays "hardware".
    Only a card with count mode "trigger" controls the Trigger (``locks_trigger_mode``); what a save
    does for the others is ``trigger_mode_to_set``.
    """
    if not picture_per_trigger(camera):
        return "off"
    return "hardware" if picture_source(camera, current_mode) == "camera" else "software"


def locks_trigger_mode(camera: Dict[str, Any]) -> bool:
    """True when the camera's card controls the camera's Trigger: it sets it and the camera's own Settings
    may not change it. Only a vision camera with Count mode "One picture per trigger" does (the card
    says how its pictures are taken); any other card leaves the Trigger to the camera (``trigger_mode_to_set``)."""
    return picture_per_trigger(camera)


def trigger_mode_to_set(camera: Dict[str, Any], current_mode: str = "off") -> Optional[str]:
    """The Trigger a save of the line sets on an industrial camera, or None to leave it as it is.

    - A card that controls the Trigger (``locks_trigger_mode``): ``trigger_mode_for_card``.
    - Another vision card: the camera keeps its Trigger (a tracking camera on Hardware, a camera that
      reads codes on any trigger: a reader at a wire line is fired by the counting camera's crossing),
      except Software that nothing on the card triggers and that reads no codes, which is Off.
    - A QR reader keeps its own Trigger.
    """
    if locks_trigger_mode(camera):
        return trigger_mode_for_card(camera, current_mode)
    if camera.get("role", "vision") != "vision":
        return None
    if current_mode == "software" and not takes_a_picture_on_a_trigger(camera) and not reads_codes(camera):
        return "off"
    return None


def _join_settings(raw: Dict[str, Any], label: str) -> Dict[str, Any]:
    """A joined camera's travel time from the counting camera, match window and "When no result"."""
    offset = _number(raw.get("join_offset_ms"), f"{label} travel time from the counting camera", 0,
                     -JOIN_OFFSET_LIMIT_MS, JOIN_OFFSET_LIMIT_MS)
    low, high = JOIN_WINDOW_LIMITS_MS
    window = _number(raw.get("join_window_ms"), f"{label} match window", DEFAULT_JOIN_WINDOW_MS, low, high)
    missing = str(raw.get("join_missing") or "ignore").strip().lower()
    if missing not in JOIN_MISSING:
        raise ValueError(f"{label}: when it sees nothing of a product must be 'reject' or 'ignore'")
    return {"join_offset_ms": int(round(offset)), "join_window_ms": int(round(window)), "join_missing": missing}


def join_settings(camera: Dict[str, Any]) -> Tuple[int, int, str]:
    """(travel time ms, match window ms, "reject" / "ignore") of a joined camera; defaults where it has none."""
    try:
        settings = _join_settings(camera, "")
    except ValueError:
        settings = {"join_offset_ms": 0, "join_window_ms": DEFAULT_JOIN_WINDOW_MS, "join_missing": "ignore"}
    return settings["join_offset_ms"], settings["join_window_ms"], settings["join_missing"]


def joined_cameras(cameras: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The vision cameras whose result joins the counting camera's product, in card order."""
    return [cam for cam in cameras if camera_station(cam) == "join"]


def camera_station(camera: Dict[str, Any]) -> Optional[str]:
    """'own' or 'join' for a vision camera that is not the counting camera, or a text reader or a pattern
    check that takes one picture per trigger; else None."""
    if is_text_reader(camera) or is_pattern_check(camera):
        return (camera.get("station") or "own") if camera.get("count_mode") == "trigger" else None
    if camera.get("role", "vision") != "vision" or camera.get("counting"):
        return None
    return camera.get("station") or "own"


def _tracking(value: Any, label: str) -> Dict[str, Any]:
    """A vision camera's tracking settings; the ones left out keep their defaults."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} tracking settings must be an object")
    out: Dict[str, Any] = {}
    for key, raw in value.items():
        if key not in TRACKING_LIMITS:
            raise ValueError(f"{label}: '{key}' is not a tracking setting")
        if raw in (None, ""):
            continue
        low, high, whole = TRACKING_LIMITS[key]
        number = _number(raw, f"{label} {key.replace('_', ' ')}", low, low, high)
        if whole and number != int(number):
            raise ValueError(f"{label} {key.replace('_', ' ')} must be a whole number")
        out[key] = int(number) if whole else number
    return out


def publish_topic_problem(topic: Any) -> Optional[str]:
    """Why an MQTT topic cannot be published to, or None when it can."""
    if not isinstance(topic, str) or not topic.strip():
        return "the MQTT topic is empty"
    if len(topic) > 512:
        return "the MQTT topic is longer than 512 characters"
    if "#" in topic or "+" in topic:
        return "the MQTT topic cannot contain the wildcards # or + (they are for subscribing)"
    if any(ord(ch) < 32 for ch in topic):
        return "the MQTT topic contains a character that is not allowed"
    return None


# A send card's message: JSON (the fields it picked) or text made from its template.
MESSAGE_FORMATS = ("json", "text")
MAX_TEMPLATE_LENGTH = 1024

# The placeholders a text message's template can use, as {name}, and what each
# stands for (send_dispatcher_service.template_values fills them in).
TEMPLATE_FIELDS: Dict[str, str] = {
    "line_id": "Line id",
    "line_name": "Line name",
    "line_state": "Line state (started / stopped)",
    "event": "Event name",
    "timestamp": "Time of the event (ISO 8601, UTC)",
    "date": "Date (server's local time, YYYY-MM-DD)",
    "time": "Time of day (server's local time, HH:MM:SS)",
    "camera_id": "Camera id",
    "camera_name": "Camera name",
    "track_id": "Product track number",
    "class_name": "Class",
    "confidence": "Confidence (0 to 1)",
    "result": "Result (PASSED / REJECTED)",
    "result_code": "Result code (1 good, 2 reject)",
    "reject_reason": "Reject reason",
    "reject_camera": "Camera that rejected the product",
    "code": "Code read",
    "code_format": "Code type",
    "code_status": "Code status (known / unknown / no_read)",
    "product_name": "Product of the code",
    "product_id": "Product ID in the code",
    "good_count": "Good count",
    "rejected_count": "Rejected count",
    "total_inspected": "Inspected count",
    "yield_percentage": "Yield %",
    "products_per_minute": "Products per minute",
    "batch": "Batch number",
    "alarm_code": "Alarm code",
    "alarm_message": "Alarm message",
    "input_name": "Input that changed",
    "product_check": "Product check that rejected the product",
    "sequence": "Product sequence number (from the PLC)",
    "text_status": "Text reader's result (good / wrong / none)",
    "match_status": "Pattern check's result (good / not_found / mismatch / defect / error)",
}

_TEMPLATE_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|\\[rnt\\]|[{}]")
_TEMPLATE_ESCAPES = {"\\r": "\r", "\\n": "\n", "\\t": "\t", "\\\\": "\\"}


def parse_template(template: str) -> Tuple[Tuple[bool, str], ...]:
    """A text template as its parts: (False, text) and (True, placeholder name).

    {name} is a placeholder, {{ and }} are a literal brace, and \\r \\n \\t \\\\
    are escapes. Raises ValueError naming what is wrong.
    """
    parts: List[Tuple[bool, str]] = []
    text: List[str] = []
    pos = 0
    for match in _TEMPLATE_TOKEN.finditer(template):
        text.append(template[pos:match.start()])
        pos = match.end()
        token = match.group(0)
        if token in ("{{", "}}"):
            text.append(token[0])
        elif token in _TEMPLATE_ESCAPES:
            text.append(_TEMPLATE_ESCAPES[token])
        elif match.group(1) is not None:
            name = match.group(1).strip()
            root = name.split(".", 1)[0].lower()
            if root == "match" and "." in name:
                # A pattern check's value: match.score, match.<check>.score, match.<check>.defect.
                from app.services.formula import is_match_name

                name = f"match.{name.split('.', 1)[1]}"
                if not is_match_name(name):
                    raise ValueError(f"{{{match.group(1)}}} is not a placeholder")
            elif "." in name and root in ("product", "input", "text"):
                # A product's values, an input's value, a text reader's field: the root in any case, the name as it is written.
                key = name.split(".", 1)[1]
                if not VALUE_NAME_RE.fullmatch(key):
                    raise ValueError(f"{{{match.group(1)}}} is not a placeholder")
                name = f"{root}.{key}"
            else:
                name = name.lower()
                if name not in TEMPLATE_FIELDS:
                    raise ValueError(f"{{{match.group(1)}}} is not a placeholder" if name else "{} names no placeholder")
            if any(text):
                parts.append((False, "".join(text)))
            text = []
            parts.append((True, name))
        else:
            raise ValueError(f"a single '{token}' must be written '{token}{token}'")
    text.append(template[pos:])
    if any(text):
        parts.append((False, "".join(text)))
    return tuple(parts)


# A card's id: letters, digits and _ . : -, up to 64 characters. The dashboard
# puts ids into its pages, so an id must not be able to close a quote or a tag.
CARD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def check_output_action_ids(cards: List[Any]) -> None:
    """ValueError for an output action (PLC card) whose id breaks CARD_ID_RE. A card without an id is left as it was."""
    for index, card in enumerate(cards):
        card_id = card.get("id") if isinstance(card, dict) else None
        if card_id is None or card_id == "":
            continue
        if isinstance(card_id, bool) or not isinstance(card_id, (str, int)) or not CARD_ID_RE.fullmatch(str(card_id)):
            raise ValueError(
                f"Output action {index + 1} has an id that is not valid: use letters, digits and _ . : - "
                f"(up to 64 characters)"
            )


def new_send_card_id() -> str:
    return f"send_{uuid.uuid4().hex[:8]}"


def normalize_send_card(raw: Any, index: int = 0) -> Dict[str, Any]:
    """Validate one send card: where a message goes, when, and what it contains."""
    label = f"Send card {index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    card_id = str(raw.get("id") or "").strip() or new_send_card_id()
    if not CARD_ID_RE.fullmatch(card_id):
        raise ValueError(f"{label} has an id that is not valid")
    name = str(raw.get("name") or "").strip() or f"Send {index + 1}"
    if len(name) > 128:
        raise ValueError(f"{label}: the name must be 128 characters or fewer")
    label = f"Send card '{name}'"
    trigger = str(raw.get("trigger") or "cross_line").strip().lower()
    if trigger not in CARD_TRIGGERS:
        raise ValueError(f"{label}: '{trigger}' is not a trigger")
    endpoint_id = str(raw.get("endpoint_id") or "").strip()
    if len(endpoint_id) > 128:
        raise ValueError(f"{label}: the channel is not valid")
    topic = raw.get("topic")
    topic = topic.strip() if isinstance(topic, str) else ""
    if topic:
        problem = publish_topic_problem(topic)
        if problem:
            raise ValueError(f"{label}: {problem}")
    fields = raw.get("fields")
    if fields in (None, ""):
        fields = None
    elif not isinstance(fields, list) or len(fields) > 64 or any(not isinstance(f, str) or not f.strip() or len(f) > 64 for f in fields):
        raise ValueError(f"{label}: the message contents must be a list of field names")
    else:
        fields = list(dict.fromkeys(f.strip() for f in fields)) or None
    message_format = str(raw.get("format") or "json").strip().lower()
    if message_format not in MESSAGE_FORMATS:
        raise ValueError(f"{label}: the message format must be JSON or text")
    template = raw.get("template")
    if template is None:
        template = ""
    if not isinstance(template, str) or len(template) > MAX_TEMPLATE_LENGTH:
        raise ValueError(f"{label}: the text must be at most {MAX_TEMPLATE_LENGTH} characters")
    if message_format == "text":
        if not template.strip():
            raise ValueError(f"{label}: enter the text of the message")
        try:
            parse_template(template)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from exc
    codes = raw.get("alarm_codes") or []
    if isinstance(codes, str):
        codes = codes.split(",")
    if not isinstance(codes, list) or len(codes) > 200:
        raise ValueError(f"{label}: the alarms must be a list")
    condition = str(raw.get("condition") or "any").strip().lower()[:32]
    set_value = str(raw.get("set_value") if raw.get("set_value") is not None else "").strip()[:128]
    if trigger == "timer":
        condition, set_value = _timer_setting(raw, label)
    input_id = ""
    if trigger == "input":
        condition = str(raw.get("condition") or "on").strip().lower()
        input_id = str(raw.get("input_id") or "").strip()
        if not CARD_ID_RE.fullmatch(input_id):
            raise ValueError(f"{label}: choose the input whose change sends this message")
        if condition not in INPUT_CONDITIONS:
            raise ValueError(f"{label}: an input change is 'on', 'off' or 'change', not '{condition}'")
    card: Dict[str, Any] = {
        "id": card_id,
        "name": name,
        "enabled": _bool(raw.get("enabled"), f"{label} on/off", True),
        # One MQTT, TCP or webhook channel from Connections; "" = not chosen yet.
        "endpoint_id": endpoint_id,
        # The topic the message is published to when the channel is an MQTT broker.
        "topic": topic,
        "trigger": trigger,
        "condition": condition,
        # The counter value or class name; for a timer, the interval in ms.
        "set_value": set_value,
        # What the message contains (send_dispatcher_service.MESSAGE_FIELDS); None = everything.
        "fields": fields,
        # "json": the fields above as JSON; "text": the template with its {placeholders} filled in.
        "format": message_format,
        "template": template,
    }
    camera_id = str(raw.get("camera_id") or "").strip()
    if camera_id and trigger != "timer":            # a timer is the line's, not a camera's
        card["camera_id"] = camera_id[:64]
    if trigger == "alarm":
        card["alarm_codes"] = [str(code).strip() for code in codes if str(code).strip()]
    if input_id:
        card["input_id"] = input_id
    # "Only when": rules on the event's values (app.services.rules).
    from app.services.rules import normalize_only_when
    only_when = normalize_only_when(raw.get("only_when"), label)
    if only_when is not None:
        card["only_when"] = only_when
    return card


def _timer_setting(raw: Dict[str, Any], label: str) -> Tuple[str, str]:
    """(condition, interval in ms as text) of a timer send card, checked."""
    condition = str(raw.get("condition") or TIMER_CONDITIONS[0]).strip().lower()
    if condition not in TIMER_CONDITIONS:
        raise ValueError(f"{label}: a timer sends while the line runs or always, not '{condition}'")
    text = str(raw.get("set_value") if raw.get("set_value") not in (None, "") else TIMER_DEFAULT_MS).strip()
    if not text.isdigit():
        raise ValueError(f"{label}: the timer's interval must be a whole number of ms, not '{text[:32]}'")
    every = int(text)
    if not TIMER_MIN_MS <= every <= TIMER_MAX_MS:
        raise ValueError(f"{label}: the timer's interval must be {TIMER_MIN_MS} to {TIMER_MAX_MS} ms, not {every}")
    return condition, str(every)


def timer_interval_ms(card: Dict[str, Any]) -> int:
    """A timer send card's interval in ms (its set_value, as normalize_send_card checked it)."""
    try:
        every = int(str(card.get("set_value") or TIMER_DEFAULT_MS).strip())
    except ValueError:
        every = TIMER_DEFAULT_MS
    return min(TIMER_MAX_MS, max(TIMER_MIN_MS, every))


# Send card settings an older client does not know of: kept when it leaves them out.
SEND_CARD_KEPT_KEYS = ("format", "template", "only_when", "input_id")


def keep_card_fields(cards: Any, saved: Any, keys: Iterable[str]) -> Any:
    """Cards sent by a client, with the settings ``keys`` taken from the saved card
    of the same id where the client left them out (an older dashboard)."""
    if not isinstance(cards, list) or not isinstance(saved, list):
        return cards
    by_id = {card.get("id"): card for card in saved if isinstance(card, dict) and card.get("id")}
    filled = []
    for card in cards:
        old = by_id.get(card.get("id")) if isinstance(card, dict) else None
        if old is not None:
            card = {**{key: copy.deepcopy(old[key]) for key in keys if key in old and key not in card}, **card}
        filled.append(card)
    return filled


def normalize_send_cards(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("Send cards must be a list")
    if len(raw) > MAX_SEND_CARDS:
        raise ValueError(f"A line can have at most {MAX_SEND_CARDS} send cards")
    cards = [normalize_send_card(card, index) for index, card in enumerate(raw)]
    ids = [card["id"] for card in cards]
    if len(set(ids)) != len(ids):
        raise ValueError("Two send cards have the same id")
    return cards


# ── PLC signals (inputs) ──────────────────────────────────────────────────────
# A signal card reads one PLC address every poll, and the line does something
# when the value changes:
#   start_line / stop_line  start or stop the line
#   run_level               the line runs while the value is on
#   reset_counters          reset the line's counters
#   product_sensor          one product (a line without a vision camera); with the data
#                           type "counter", the increase of a counter in the PLC is the
#                           number of products
#   capture                 one picture with the card's camera
#   set_batch               the value becomes the line's batch number
#   confirm_card            confirms the PLC action cards that name this card
#   alarm                   an alarm while the value is on
#   none                    nothing: the input only fires the cards with the trigger "Input changed"
PLC_INPUT_ACTIONS = ("start_line", "stop_line", "run_level", "reset_counters", "product_sensor",
                     "capture", "set_batch", "confirm_card", "alarm", "none")
# How the value is read (PLCDriver.read), plus "counter": a 16-bit counter in the PLC.
PLC_INPUT_DATA_TYPES = ("auto", "bool", "int16", "uint16", "int32", "real", "string", "counter")
# Which change of the value a card reacts to: off to on, on to off, or either.
PLC_INPUT_EDGES = ("rising", "falling", "change")
PLC_INPUT_POLL_LIMITS_MS = (50, 10000)
DEFAULT_PLC_INPUT_POLL_MS = 100
# A new value counts once it has lasted this long.
PLC_INPUT_DEBOUNCE_LIMITS_MS = (0, 5000)
PLC_INPUT_ALARM_SEVERITIES = ("info", "warning", "critical")
MAX_PLC_INPUTS = 32
# input.value and input.previous name the input that changed, so no input may be called that.
INPUT_EVENT_NAMES = ("value", "previous")
# A "Take a picture" input has a trigger number. A camera that takes one picture per trigger
# lists the numbers that take its picture (``triggers``): one or more.
MAX_TRIGGER_NO = 99


def _trigger_number(value: Any) -> Optional[int]:
    """A trigger number (1 to MAX_TRIGGER_NO) from what a client sent; None when it is not one."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) and 1 <= number <= MAX_TRIGGER_NO else None


def capture_cameras(card: Dict[str, Any]) -> List[str]:
    """The cameras a "Take a picture" input names itself (``camera_id`` or ``camera_ids``).

    Only a card saved before cameras chose their triggers, or sent by an older client, has any.
    """
    ids = card.get("camera_ids")
    named = [str(camera_id).strip() for camera_id in ids] if isinstance(ids, list) else []
    named.append(str(card.get("camera_id") or "").strip())
    return list(dict.fromkeys(camera_id for camera_id in named if camera_id))


def triggered_cameras(cameras: List[Dict[str, Any]], card: Dict[str, Any]) -> List[str]:
    """The ids of the cameras a "Take a picture" input takes a picture with, in card order:
    those that ticked its trigger number, then any the card names itself (capture_cameras)."""
    number = card.get("trigger_no")
    found = [cam.get("camera_id") for cam in cameras if number is not None and number in (cam.get("triggers") or [])]
    return found + [camera_id for camera_id in capture_cameras(card) if camera_id not in found]


def new_plc_input_id() -> str:
    return f"plcin_{uuid.uuid4().hex[:8]}"


def normalize_plc_input(raw: Any, index: int = 0) -> Dict[str, Any]:
    """Validate one signal card: which PLC address is read, how, and what the line does with it."""
    label = f"Input {index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    card_id = str(raw.get("id") or "").strip() or new_plc_input_id()
    if not CARD_ID_RE.fullmatch(card_id):
        raise ValueError(f"{label} has an id that is not valid")
    name = str(raw.get("name") or "").strip() or f"Signal {index + 1}"
    if len(name) > 128:
        raise ValueError(f"{label}: the name must be 128 characters or fewer")
    label = f"Signal '{name}'"
    action = str(raw.get("action") or "").strip().lower()
    if action not in PLC_INPUT_ACTIONS:
        raise ValueError(f"{label}: '{action}' is not an action a signal can have")
    endpoint_id = str(raw.get("plc_endpoint_id") or "").strip()
    if len(endpoint_id) > 128:
        raise ValueError(f"{label}: the PLC channel is not valid")
    address = raw.get("address")
    address = "" if address is None else address
    if not isinstance(address, str) or len(address.strip()) > 128:
        raise ValueError(f"{label}: the address must be text of at most 128 characters")
    data_type = str(raw.get("data_type") or "auto").strip().lower()
    if data_type not in PLC_INPUT_DATA_TYPES:
        raise ValueError(f"{label}: '{data_type}' is not a data type; use one of {', '.join(PLC_INPUT_DATA_TYPES)}")
    if data_type == "counter" and action != "product_sensor":
        raise ValueError(f"{label}: the PLC counter type is only for a product sensor")
    if data_type == "string" and action not in ("set_batch", "none"):
        raise ValueError(f"{label}: a string can only be read to set the batch number, or by an input that only fires cards")
    edge = str(raw.get("edge") or "rising").strip().lower()
    if edge not in PLC_INPUT_EDGES:
        raise ValueError(f"{label}: the edge must be 'rising', 'falling' or 'change'")
    # The name its last value goes by in formulas and rules: input.<value name>.
    value_name = raw.get("value_name")
    value_name = "" if value_name is None else str(value_name).strip()
    if value_name:
        if not VALUE_NAME_RE.fullmatch(value_name):
            raise ValueError(f"{label}: a value name starts with a letter and has only letters, digits and _ "
                             f"(at most 32 characters)")
        if value_name in INPUT_EVENT_NAMES:
            raise ValueError(f"{label}: input.{value_name} is the value of the input that changed; "
                             f"choose another value name")
        if data_type == "counter":
            raise ValueError(f"{label}: a PLC counter counts products and has no value name")
    low, high = PLC_INPUT_POLL_LIMITS_MS
    poll_ms = int(_number(raw.get("poll_ms"), f"{label} poll time", DEFAULT_PLC_INPUT_POLL_MS, low, high))
    low, high = PLC_INPUT_DEBOUNCE_LIMITS_MS
    debounce_ms = int(_number(raw.get("debounce_ms"), f"{label} debounce time", 0, low, high))
    card: Dict[str, Any] = {
        "id": card_id,
        "name": name,
        "enabled": _bool(raw.get("enabled"), f"{label} on/off", True),
        # A PLC channel from Connections; "" = not chosen yet.
        "plc_endpoint_id": endpoint_id,
        "address": address.strip(),
        "data_type": data_type,
        "poll_ms": poll_ms,
        "edge": edge,
        "debounce_ms": debounce_ms,
        "action": action,
    }
    if value_name:
        card["value_name"] = value_name
    if action == "capture":
        # The trigger's number. None: the list of inputs gives it the next free one (normalize_plc_inputs).
        number = raw.get("trigger_no")
        card["trigger_no"] = None if number in (None, "") else _trigger_number(number)
        if number not in (None, "") and card["trigger_no"] is None:
            raise ValueError(f"{label}: the trigger number must be a whole number from 1 to {MAX_TRIGGER_NO}")
        # Cameras choose their triggers on their own cards. A card that still names a camera
        # (an older client, an older save) is turned into a tick on that camera when the line is saved.
        named = capture_cameras(raw)
        if any(len(camera_id) > 64 for camera_id in named):
            raise ValueError(f"{label}: a camera id is too long")
        if named:
            card["camera_ids"] = named
    if action == "alarm":
        text = str(raw.get("alarm_text") or "").strip() or name
        if len(text) > 200:
            raise ValueError(f"{label}: the alarm text must be 200 characters or fewer")
        severity = str(raw.get("alarm_severity") or "warning").strip().lower()
        if severity not in PLC_INPUT_ALARM_SEVERITIES:
            raise ValueError(f"{label}: the alarm severity must be 'info', 'warning' or 'critical'")
        card["alarm_text"] = text
        card["alarm_severity"] = severity
    return card


def new_product_check_id() -> str:
    return f"check_{uuid.uuid4().hex[:8]}"


def normalize_product_checks(raw: Any) -> List[Dict[str, Any]]:
    """A line's product checks: each rejects a product whose code is known when its rules pass."""
    from app.services.rules import normalize_only_when

    if not isinstance(raw, list):
        raise ValueError("Product checks must be a list")
    if len(raw) > MAX_PRODUCT_CHECKS:
        raise ValueError(f"A line can have at most {MAX_PRODUCT_CHECKS} product checks")
    checks = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Product check {index} must be an object")
        check_id = str(entry.get("id") or "").strip() or new_product_check_id()
        if not CARD_ID_RE.fullmatch(check_id):
            raise ValueError(f"Product check {index} has an id that is not valid")
        name = re.sub(r"\s+", " ", str(entry.get("name") or "")).strip()
        if not name:
            raise ValueError(f"Product check {index} needs a name")
        if len(name) > MAX_PRODUCT_CHECK_NAME_LENGTH or any(ord(ch) < 32 for ch in name):
            raise ValueError(f"Product check {index}: the name must be {MAX_PRODUCT_CHECK_NAME_LENGTH} characters or fewer")
        label = f"Product check '{name}'"
        checks.append({
            "id": check_id,
            "name": name,
            "enabled": _bool(entry.get("enabled"), f"{label} on/off", True),
            "only_when": normalize_only_when(entry.get("only_when"), label, required=True, forbidden=CHECK_FORBIDDEN_NAMES),
        })
    ids = [check["id"] for check in checks]
    if len(set(ids)) != len(ids):
        raise ValueError("Two product checks have the same id")
    return checks


def normalize_plc_inputs(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("Inputs must be a list")
    if len(raw) > MAX_PLC_INPUTS:
        raise ValueError(f"A line can have at most {MAX_PLC_INPUTS} inputs")
    cards = [normalize_plc_input(card, index) for index, card in enumerate(raw)]
    ids = [card["id"] for card in cards]
    if len(set(ids)) != len(ids):
        raise ValueError("Two inputs have the same id")
    names = [card["value_name"] for card in cards if card.get("value_name")]
    for name in names:
        if names.count(name) > 1:
            raise ValueError(f"Two inputs have the value name '{name}'; give each its own")
    # Each "Take a picture" input has its own trigger number; one without gets the next free number.
    taking = [card for card in cards if card["action"] == "capture"]
    used = [card["trigger_no"] for card in taking if card["trigger_no"] is not None]
    for number in used:
        if used.count(number) > 1:
            raise ValueError(f"Two inputs have the trigger number {number}; give each 'Take a picture' input its own")
    for card in taking:
        if card["trigger_no"] is None:
            card["trigger_no"] = next(n for n in range(1, MAX_PLC_INPUTS + MAX_TRIGGER_NO) if n not in used)
            used.append(card["trigger_no"])
    return cards


def product_sensors(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A line's product sensor cards. A line that has one (switched on or not) gets
    its products from the PLC, so each code read is not a product of its own."""
    return [card for card in line.get("plc_inputs") or [] if card.get("action") == "product_sensor"]


def takes_a_picture_on_a_trigger(camera: Dict[str, Any]) -> bool:
    """A vision camera with Count mode "One picture per trigger", or a camera that reads codes "When an input asks"."""
    return camera.get("count_mode") == "trigger" or (reads_codes(camera) and camera.get("qr_trigger") == "signal")


def _trigger_page(camera: Dict[str, Any]) -> str:
    """The camera page whose triggers a camera's card shows."""
    if is_text_reader(camera):
        return "Reading"
    return "When to check" if is_pattern_check(camera) else "Counting"


def _check_signal_cameras(inputs: List[Dict[str, Any]], cameras: List[Dict[str, Any]]) -> None:
    """A "capture" card that names a camera itself: the camera gets a tick for the card's trigger
    number instead, and the card names none. The camera must be one of the line that takes one
    picture per trigger."""
    for card in inputs:
        if card["action"] != "capture":
            continue
        label = f"Input '{card['name']}'"
        for camera_id in card.pop("camera_ids", []):
            index = next((i for i, cam in enumerate(cameras) if cam["camera_id"] == camera_id), None)
            if index is None:
                raise ValueError(f"{label} names a camera that is not on this line")
            cam = cameras[index]
            if not takes_a_picture_on_a_trigger(cam):
                raise ValueError(
                    f"{label} takes a picture with Camera {index + 1}, so that camera must be a vision camera, "
                    f"a text reader or a pattern check with 'One picture per trigger', or read codes with Read "
                    f"codes set to "
                    f"'When an input asks'"
                )
            cam["triggers"] = sorted({*(cam.get("triggers") or []), card["trigger_no"]})


def _keep_camera_triggers(cameras: List[Any], saved: List[Dict[str, Any]]) -> List[Any]:
    """Cameras sent by a client, each with its saved ``triggers`` (and READER_KEPT_KEYS) when the client left them out."""
    by_id = {c.get("camera_id"): c for c in saved if isinstance(c, dict)}
    filled = []
    for cam in cameras:
        if isinstance(cam, dict):
            kept = by_id.get(str(cam.get("camera_id") or "").strip()) or {}
            for key in ("triggers",) + READER_KEPT_KEYS:
                if key not in cam and kept.get(key):
                    cam = {**cam, key: kept[key]}
        filled.append(cam)
    return filled


def _keep_job_settings(cameras: List[Any], saved: List[Dict[str, Any]], role: str, keys: Tuple[str, ...]) -> List[Any]:
    """Cameras of this role (text readers, pattern checks) sent by a client, with the ``keys`` it left out as
    they are saved."""
    by_id = {c.get("camera_id"): c for c in saved if isinstance(c, dict) and c.get("role") == role}
    filled = []
    for cam in cameras:
        if isinstance(cam, dict) and str(cam.get("role") or "").strip().lower() == role:
            kept = by_id.get(str(cam.get("camera_id") or "").strip()) or {}
            cam = {**{key: kept[key] for key in keys if key in kept}, **cam}
        filled.append(cam)
    return filled


def _fill_camera_models(cameras: List[Any], saved: List[Dict[str, Any]], line_model: Any) -> List[Any]:
    """Cameras sent by a client, with the settings a client left out (CAMERA_KEPT_KEYS).

    A client written before version 6 sends cameras without a model and class
    lists, and may send one ``model_id`` for the whole line. A camera sent
    without a model gets that line model, else keeps the one it has; the other
    settings that are left out stay as they are saved.
    """
    by_id = {c.get("camera_id"): c for c in saved if isinstance(c, dict) and c.get("role", "vision") == "vision"}
    filled = []
    for cam in cameras:
        if isinstance(cam, dict) and str(cam.get("role") or "vision").strip().lower() == "vision":
            kept = by_id.get(str(cam.get("camera_id") or "").strip()) or {}
            defaults = {key: kept[key] for key in CAMERA_KEPT_KEYS if key in kept}
            if line_model not in (None, ""):
                defaults["model_id"] = line_model
            cam = {**defaults, **cam}
        filled.append(cam)
    return filled


def require_camera_models(line: Dict[str, Any]) -> None:
    """A line is not saved with a vision camera that has no model."""
    for index, cam in enumerate(line.get("cameras") or []):
        if cam.get("role") == "vision" and not cam.get("model_id"):
            raise ValueError(f"Camera {index + 1}: choose the vision model that runs on this camera")


def camera_model_id(camera: Dict[str, Any]) -> Optional[str]:
    """The YOLO model a camera runs: a vision camera's model, a text reader's model for its YOLO fields."""
    if camera.get("role") == "vision":
        return camera.get("model_id") or None
    if is_text_reader(camera):
        return (camera.get("ocr") or {}).get("locator_model_id") or None
    return None


def vision_models(line: Dict[str, Any]) -> List[str]:
    """The models a line's vision cameras and text readers run, each once, in camera order."""
    return list(dict.fromkeys(
        camera_model_id(cam) for cam in line.get("cameras") or [] if camera_model_id(cam)
    ))


def counting_camera(line: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The vision camera a line's totals come from."""
    return next((c for c in line.get("cameras") or [] if c.get("role") == "vision" and c.get("counting")), None)


# The result handshake (``handshake``): capture request -> result -> the PLC's acknowledge.
HANDSHAKE_TIMEOUT_LIMITS_MS = (100, 60000)
DEFAULT_RESULT_TIMEOUT_MS = 3000
DEFAULT_ACK_TIMEOUT_MS = 5000
# What a capture request that gets no result in time becomes: a rejected product, or a fault. Never a pass.
HANDSHAKE_ON_TIMEOUT = ("reject", "fault")


def normalize_handshake(raw: Any, inputs: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """A line's result handshake settings, checked against its inputs; None for none. Raises ValueError.

    Switched on, it needs one of the line's "Take a picture" inputs (the capture request) and another
    input (the acknowledge). Switched off, it keeps its settings; inputs that are gone are dropped.
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("The result handshake must be an object")
    label = "Result handshake"
    low, high = HANDSHAKE_TIMEOUT_LIMITS_MS
    on_timeout = str(raw.get("on_timeout") or "reject").strip().lower()
    if on_timeout not in HANDSHAKE_ON_TIMEOUT:
        raise ValueError(f"{label}: no result in time must be 'reject' or 'fault' (never a pass)")
    settings = {
        "enabled": _bool(raw.get("enabled"), f"{label} on/off", False),
        "capture_input_id": str(raw.get("capture_input_id") or "").strip() or None,
        "ack_input_id": str(raw.get("ack_input_id") or "").strip() or None,
        "result_timeout_ms": int(_number(raw.get("result_timeout_ms"), f"{label} result timeout",
                                         DEFAULT_RESULT_TIMEOUT_MS, low, high)),
        "ack_timeout_ms": int(_number(raw.get("ack_timeout_ms"), f"{label} ack timeout", DEFAULT_ACK_TIMEOUT_MS, low, high)),
        "on_timeout": on_timeout,
    }
    by_id = {card["id"]: card for card in inputs or []}
    if not settings["enabled"]:
        for key in ("capture_input_id", "ack_input_id"):
            if settings[key] not in by_id:
                settings[key] = None
        return settings
    capture = by_id.get(settings["capture_input_id"] or "")
    if capture is None or capture.get("action") != "capture":
        raise ValueError(f"{label}: choose the line's 'Take a picture' input that asks for each result")
    if settings["ack_input_id"] not in by_id or settings["ack_input_id"] == settings["capture_input_id"]:
        raise ValueError(f"{label}: choose the input with which the PLC acknowledges the result "
                         f"(another input of the line, for example with the action 'Only fires cards')")
    return settings


def _line_input(value: Any, inputs: Optional[List[Dict[str, Any]]], label: str) -> Optional[str]:
    """The id of one of the line's inputs that a line setting names; None for none. Raises ValueError."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not any(card["id"] == text for card in inputs or []):
        raise ValueError(f"{label} must be one of the line's inputs ('{text[:64]}' is not)")
    return text


def normalize_line(raw: Any, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate a line sent by a client, filling gaps from ``existing``.

    Returns a new dict. The line's ``action_trigger`` and ``plc_actions`` are
    passed through as given (validated elsewhere).
    """
    if not isinstance(raw, dict):
        raise ValueError("A production line must be an object")
    base = copy.deepcopy(existing) if existing else {}
    merged = {**base, **raw}

    line_id = str(merged.get("id") or "").strip().lower() or new_line_id()
    if not _LINE_ID_RE.match(line_id):
        raise ValueError("Line id may use lowercase letters, digits, '-' and '_' (up to 48 characters)")

    name = str(merged.get("name") or "").strip()
    if not name:
        raise ValueError("Line name is required")
    if len(name) > 64:
        raise ValueError("Line name must be 64 characters or fewer")

    raw_cameras = merged.get("cameras") or []
    if not isinstance(raw_cameras, list):
        raise ValueError("cameras must be a list")
    if len(raw_cameras) > MAX_CAMERAS_PER_LINE:
        raise ValueError(f"A line can have at most {MAX_CAMERAS_PER_LINE} cameras")
    if "cameras" in raw:
        raw_cameras = _fill_camera_models(raw_cameras, base.get("cameras") or [], raw.get("model_id"))
        raw_cameras = _keep_job_settings(raw_cameras, base.get("cameras") or [], "ocr", TEXT_READER_KEPT_KEYS)
        raw_cameras = _keep_job_settings(raw_cameras, base.get("cameras") or [], "match", PATTERN_CHECK_KEPT_KEYS)
        raw_cameras = _keep_camera_triggers(raw_cameras, base.get("cameras") or [])
    elif raw.get("model_id") not in (None, ""):
        # Such a client changes "the line's model": every vision camera of the line runs it.
        raw_cameras = [
            {**cam, "model_id": raw["model_id"]} if isinstance(cam, dict) and cam.get("role") == "vision" else cam
            for cam in raw_cameras
        ]
    cameras = [normalize_camera(c, i) for i, c in enumerate(raw_cameras)]
    ids = [c["camera_id"] for c in cameras]
    if len(set(ids)) != len(ids):
        raise ValueError("The same camera is listed twice on this line")
    for lane in SORTING_LANES:
        watching = [index + 1 for index, cam in enumerate(cameras) if sorting_lane(cam) == lane]
        if len(watching) > 1:
            raise ValueError(f"Cameras {watching[0]} and {watching[1]} both verify the {lane} outfeed: "
                             f"one camera per outfeed")

    # The checks of what a camera that reads Continuously cannot do run when the line's setup is
    # sent. A save of only "enabled" or the name (start, stop, rename: the dashboard, a PLC input,
    # Sparkplug) keeps working on a line the version 9 upgrade left that way.
    setup_save = any(key in raw for key in ("cameras", "sync", "product_checks"))
    for index, cam in enumerate(cameras if setup_save else []):
        if code_check(cam) and not reads_codes_triggered(cam):
            raise ValueError(
                f"Camera {index + 1} checks codes, but Read codes 'Continuously' only shows them. "
                f"Choose a wire line or 'When an input asks'."
            )

    vision = [c for c in cameras if c["role"] == "vision"]
    counting = [c for c in vision if c["counting"]]
    if len(counting) > 1:
        names = " and ".join(f"Camera {cameras.index(c) + 1}" for c in counting[:2])
        raise ValueError(
            f"Only one vision camera can be the counting camera ({names} are both set to Counting). "
            f"Set the other one to Inspection station."
        )
    if vision and not counting:
        # The first vision camera counts, unless it is set to join the counting camera's result.
        first = next((c for c in vision if c.get("station") != "join"), None)
        if first is None:
            raise ValueError(
                f"Camera {cameras.index(vision[0]) + 1} joins the product result of the counting camera, but the line "
                f"has no counting camera. Set one vision camera to Counting."
            )
        first["counting"] = True
    for cam in vision:
        if cam["counting"]:
            # The counting camera makes the product result; it joins nothing.
            cam.pop("station", None)
            for key in JOIN_KEYS:
                cam.pop(key, None)
    if not vision and any(c.get("qr_trigger") in ("line1", "line2") for c in cameras):
        raise ValueError("Capturing when a product crosses a wire line needs a vision camera on the line")
    if any(c["counting"] and c.get("count_mode") == "trigger" for c in vision) \
            and any(c.get("qr_trigger") in ("line1", "line2") for c in cameras):
        raise ValueError(
            "A camera captures when a product crosses a wire line, but the counting camera is set to "
            "'One picture per trigger' and tracks nothing across its lines. Set the capturing camera's "
            "Read codes to 'When an input asks' or to continuous."
        )

    # PLC signal cards. A client that leaves them out keeps the saved ones.
    raw_inputs = raw.get("plc_inputs") if raw.get("plc_inputs") is not None else base.get("plc_inputs")
    inputs = normalize_plc_inputs(raw_inputs) if raw_inputs is not None else None
    sensors = [card for card in inputs or [] if card["action"] == "product_sensor"]
    if vision and sensors:
        raise ValueError(
            f"Input '{sensors[0]['name']}' is a product sensor, but this line has a vision camera, which counts "
            f"the products. Use the action 'Take a picture' to trigger a camera instead."
        )
    _check_signal_cameras(inputs or [], cameras)
    # The input whose value is each carton's sequence number. A client that leaves it out keeps it.
    sequence_input_id = _line_input(merged.get("sequence_input_id"), inputs, "The product sequence input")
    # Capture request -> result -> the PLC's acknowledge. A client that leaves it out keeps it.
    handshake = normalize_handshake(merged.get("handshake"), inputs)
    # Where the line's products come from: a vision camera, or a sensor on the PLC.
    has_source = bool(vision) or bool(sensors)

    raw_sync = merged.get("sync") or {}
    if not isinstance(raw_sync, dict):
        raise ValueError("sync must be an object")
    sync = {
        "enabled": _bool(raw_sync.get("enabled"), "Sync", False),
        "window_ms": int(_number(raw_sync.get("window_ms"), "Sync window", DEFAULT_SYNC_WINDOW_MS, 50, 10000)),
    }
    # Product checks. A client that leaves them out keeps the saved ones.
    raw_checks = raw.get("product_checks") if raw.get("product_checks") is not None else base.get("product_checks")
    product_checks = normalize_product_checks(raw_checks) if raw_checks is not None else None
    checking = [c for c in cameras if code_check(c)]
    if product_checks and any(c["enabled"] for c in product_checks) and has_source             and any(reads_codes_triggered(c) and product_reader(c) for c in cameras):
        # A product check needs each product paired with its code, like a code check.
        sync["enabled"] = True
    if checking and has_source:
        # A product gets one result, from the vision camera (or the sensor) and its
        # code together: the line has to pair each product with its code, which is
        # what Sync does.
        sync["enabled"] = True
    if not has_source:
        # Without a vision camera or a product sensor the line only knows of a
        # product when a code is read, so "no code was read" never happens.
        for cam in checking:
            cam["qr_no_read"] = "ignore"
    if sync["enabled"] and not (has_source and any(product_reader(c) for c in cameras)):
        raise ValueError(
            "Sync needs a vision camera (or a PLC product sensor) and a camera that reads codes "
            "(a QR / barcode reader, or a Vision + QR / barcode camera)"
        )
    if setup_save and sync["enabled"] and not any(reads_codes_triggered(c) and product_reader(c) for c in cameras):
        # A camera that reads Continuously only shows its codes: none would pair with a product.
        raise ValueError("Sync needs a camera that reads codes when a product crosses a wire line or when an input asks.")

    line: Dict[str, Any] = {
        "id": line_id,
        "name": name,
        "enabled": _bool(merged.get("enabled"), "enabled", True),
        "cameras": cameras,
        "sync": sync,
        "min_fps": _number(merged.get("min_fps"), "Minimum processed frame rate", 0.0, 0.0, 240.0),
        # Percent of inspected products that should pass; 0 = no target. The
        # dashboards only compare the live yield against it.
        "yield_target": _number(merged.get("yield_target"), "Yield target", 0.0, 0.0, 100.0),
    }
    if inputs is not None:
        line["plc_inputs"] = inputs
    if product_checks is not None:
        line["product_checks"] = product_checks
    if sequence_input_id:
        line["sequence_input_id"] = sequence_input_id
    if handshake is not None:
        line["handshake"] = handshake
    for key in COUNTS_RESET_KEYS:
        if key in base:
            line[key] = copy.deepcopy(base[key])
    if base.get("batch"):
        line["batch"] = base["batch"]
    for key in _LOGIC_KEYS:
        if key in merged:
            line[key] = copy.deepcopy(merged[key])
    if "action_trigger" in line and not isinstance(line["action_trigger"], dict):
        raise ValueError("action_trigger must be an object")
    if "plc_actions" in line and not isinstance(line["plc_actions"], list):
        raise ValueError("plc_actions must be a list")
    if "send_actions" in line:
        line["send_actions"] = normalize_send_cards(line["send_actions"])
    return line


def check_camera_ownership(lines: Iterable[Dict[str, Any]]) -> None:
    """A camera belongs to one line only."""
    owner: Dict[str, str] = {}
    for line in lines:
        for cam in line.get("cameras", []):
            cid = cam.get("camera_id")
            if cid in owner and owner[cid] != line.get("id"):
                raise ValueError(f"Camera '{cid}' already belongs to line '{owner[cid]}'")
            owner[cid] = line.get("id")


def upgrade_state_to_v2(state: Dict[str, Any]) -> bool:
    """Create Line 1 from a version 1 settings file. Returns True when it changed anything.

    Nothing existing is removed or renamed: Line 1 reads its logic from the old
    ``action_trigger`` and ``plc_actions`` keys, so version 1 keeps working
    against the upgraded file. upgrade_state() runs this and the later steps.
    """
    lines = state.get("lines")
    if isinstance(lines, list) and any(isinstance(ln, dict) and ln.get("id") == PRIMARY_LINE_ID for ln in lines):
        if not _is_version(state.get("schema_version")):
            state["schema_version"] = 2
            return True
        return False

    cameras = []
    active_camera = state.get("active_camera_id")
    if active_camera:
        cameras.append({"camera_id": str(active_camera), "role": "vision", "counting": True})
    line1 = normalize_line({
        "id": PRIMARY_LINE_ID,
        "name": "Line 1",
        "enabled": True,
        # Its model and class lists are set by the version 6 step, from what version 1 used.
        "cameras": cameras,
    })
    others = [ln for ln in lines if isinstance(ln, dict)] if isinstance(lines, list) else []
    state["lines"] = [line1] + others
    state["schema_version"] = 2
    return True


def _is_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 2


def _upgrade_to_v3(state: Dict[str, Any]) -> None:
    """One switch connects the cameras of every running line when the server starts.

    Version 2 had the Cameras page switch (one camera) and a switch on each
    line. The one switch that is left starts on when either was on: the old
    Cameras page switch, or the switch of a line that has cameras (a line
    without cameras connected nothing, whatever its switch said).
    """
    was_on = state.get("camera_auto_connect") is True
    for line in state.get("lines") or []:
        if not isinstance(line, dict):
            continue
        own_switch = line.pop("auto_connect", True)
        if own_switch is not False and line.get("cameras"):
            was_on = True
    state["camera_auto_connect"] = was_on


def _upgrade_to_v4(state: Dict[str, Any]) -> None:
    """Every camera that reads codes checked the one product list there was; that list is now "List 1"."""
    for line in state.get("lines") or []:
        for camera in (line.get("cameras") or []) if isinstance(line, dict) else []:
            if isinstance(camera, dict) and reads_codes(camera) and "product_list_id" not in camera:
                camera["product_list_id"] = DEFAULT_PRODUCT_LIST_ID


def _legacy_channel(protocol: str, trigger: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A channel for an address that version 1 kept in the line settings instead of under Connections."""
    if protocol == "tcp":
        host, port = str(trigger.get("tcp_host") or "").strip(), trigger.get("tcp_port")
        if not host or not port:
            return None
        return {"id": "tcp-from-settings", "name": "TCP target (from the old settings)", "protocol": "tcp", "enabled": True,
                "description": "", "host": host, "port": int(port), "delimiter": "\\n", "mode": "client", "timeout": 5}
    if protocol == "webhook":
        url = str(trigger.get("webhook_url") or "").strip()
        if not url:
            return None
        return {"id": "webhook-from-settings", "name": "Webhook (from the old settings)", "protocol": "webhook", "enabled": True,
                "description": "", "url": url, "method": "POST", "headers": "Content-Type: application/json",
                "timeout": 5, "retry_count": 2}
    broker = state.get("mqtt") if isinstance(state.get("mqtt"), dict) else {}
    topic = str(trigger.get("mqtt_topic") or "factory/inspection/wireline").strip()
    if not broker.get("host") or not topic:
        return None  # no broker was set up: nothing was being published
    return {"id": "mqtt-from-settings", "name": "MQTT broker (from the old settings)", "protocol": "mqtt",
            "enabled": True, "description": "", "host": broker.get("host"), "port": broker.get("port") or 8883,
            "username": broker.get("username") or "", "password": broker.get("password") or "",
            "ca_cert_filename": broker.get("ca_cert") or "", "client_cert_filename": broker.get("client_cert") or "",
            "client_key_filename": broker.get("client_key") or "", "tls_enabled": bool(broker.get("tls_enabled")),
            "tls_insecure": False, "keepalive": broker.get("keepalive") or 60, "topic": topic, "qos": 0, "retain": False}


_OLD_SEND_NAMES = {"results": "Results", "codes": "Code reads", "codes_none": "No code read"}


def send_cards_from_old_settings(trigger: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The send cards that send what a line's old per-protocol settings sent.

    Per protocol there was a switch, one "send to" (a channel, or all channels
    of that protocol), a trigger (all, passed or rejected products) and a field
    list; and a setting for code reads (off, all, known, unknown). A card has
    one channel, so "all channels" becomes one card per channel. May add a
    channel to ``state`` for an address version 1 kept in these settings.
    """
    endpoints = state.setdefault("communication_endpoints", [])
    cards: List[Dict[str, Any]] = []

    def channels(protocol: str) -> List[Dict[str, Any]]:
        selected = trigger.get(f"{protocol}_endpoint_id")
        own = [ep for ep in endpoints if isinstance(ep, dict) and ep.get("id") and str(ep.get("protocol", "")).lower() == protocol]
        if selected and selected != "all":
            return [ep for ep in own if ep["id"] == selected]
        if own or selected:
            return own
        legacy = _legacy_channel(protocol, trigger, state)
        if legacy is None:
            return []
        endpoints.append(legacy)
        return [legacy]

    def add(protocol: str, channel: Dict[str, Any], kind: str, **card: Any) -> None:
        cards.append({
            "id": f"send_{kind}_{channel['id']}"[:64],
            "name": f"{_OLD_SEND_NAMES[kind]} to {channel.get('name') or channel['id']}"[:128],
            "enabled": True,
            "endpoint_id": channel["id"],
            "set_value": "",
            "fields": None,
            **card,
        })

    for protocol in SEND_PROTOCOLS:
        if trigger.get(f"send_{protocol}"):
            when = str(trigger.get(f"{protocol}_dispatch_trigger", trigger.get("dispatch_trigger", "both")) or "both").lower()
            fields = trigger.get(f"{protocol}_dispatched_fields", trigger.get("dispatched_fields"))
            for channel in channels(protocol):
                add(protocol, channel, "results", trigger="cross_line",
                    condition={"passed": "good", "rejected": "reject"}.get(when, "any"),
                    fields=list(fields) if isinstance(fields, list) and fields else None)
        mode = str(trigger.get(f"{protocol}_qr_dispatch") or "off").lower()
        code_trigger = {"all": "qr_read", "known": "qr_known", "unknown": "qr_unknown"}.get(mode)
        if code_trigger:
            for channel in channels(protocol):
                # "unpaired": these settings never sent the codes that were paired
                # with a product; those went out in the product's own message.
                add(protocol, channel, "codes", trigger=code_trigger, condition="unpaired")
                if mode == "all":
                    add(protocol, channel, "codes_none", trigger="qr_no_read", condition="unpaired")
    return cards


def _upgrade_to_v5(state: Dict[str, Any]) -> None:
    """Each line's "send results" settings become send cards that send the same messages."""
    for line in state.get("lines") or []:
        if not isinstance(line, dict):
            continue
        holder = state if line.get("id") == PRIMARY_LINE_ID else line
        trigger = holder.get("action_trigger")
        cards = send_cards_from_old_settings(trigger, state) if isinstance(trigger, dict) else []
        if not isinstance(holder.get("send_actions"), list):
            holder["send_actions"] = cards
        if isinstance(trigger, dict):
            for key in OLD_SEND_KEYS:
                trigger.pop(key, None)


def _upgrade_to_v6(state: Dict[str, Any]) -> None:
    """Each vision camera gets the model and the class lists its line used.

    Before, a line named one model, or none to follow the server's one active
    model, and its two class lists were typed once per line. Every vision
    camera of a line now holds them itself, so each keeps detecting and
    counting what it did. Nothing is left on the line or in the settings.

    Line 1 without cameras was fed by the camera selected on the Cameras page,
    with the active model. That camera becomes Line 1's counting camera, as it
    did for a version 1 file, because a camera now needs a model of its own.
    """
    active_model = state.pop("active_model_id", None)
    active_model = active_model.strip() if isinstance(active_model, str) and active_model.strip() else None
    lines = [line for line in state.get("lines") or [] if isinstance(line, dict)]
    owned = {cam.get("camera_id") for line in lines for cam in line.get("cameras") or [] if isinstance(cam, dict)}
    for line in lines:
        primary = line.get("id") == PRIMARY_LINE_ID
        holder = state if primary else line
        trigger = holder.get("action_trigger") if isinstance(holder.get("action_trigger"), dict) else {}
        model_id = line.pop("model_id", None)
        model_id = model_id.strip() if isinstance(model_id, str) and model_id.strip() else active_model
        expected = trigger.pop("expected_classes", None)
        defects = trigger.pop("defect_classes", None)
        expected = [str(c) for c in expected] if isinstance(expected, list) else list(_V5_EXPECTED_CLASSES)
        defects = [str(c) for c in defects] if isinstance(defects, list) else list(_V5_DEFECT_CLASSES)

        feed = state.get("active_camera_id")
        if primary and not line.get("cameras") and model_id and isinstance(feed, str) and feed and feed not in owned:
            line["cameras"] = [{"camera_id": feed, "role": "vision", "counting": True, "qr_hold_ms": DEFAULT_QR_HOLD_MS}]
        for camera in line.get("cameras") or []:
            if isinstance(camera, dict) and camera.get("role", "vision") == "vision":
                # (Line 1 made from a version 1 file in this same load has them, empty.)
                camera["model_id"] = camera.get("model_id") or model_id
                camera["expected_classes"] = camera.get("expected_classes") or list(expected)
                camera["defect_classes"] = camera.get("defect_classes") or list(defects)


def _upgrade_to_v7(state: Dict[str, Any]) -> None:
    """Each send card on an MQTT channel gets the topic that channel published to.

    The topic was a setting of the channel, so every card on a broker shared
    one. A channel is now only the broker and each card names its own topic.
    The channel keeps its old topic: scans sent from a phone still go there.

    One MQTT connection was also kept apart from the channels and opened at
    startup with the address of the channel saved last. Each channel now has
    its own connection, so that one is no longer opened for a channel's broker.
    """
    channels = {ep.get("id"): ep for ep in state.get("communication_endpoints") or []
                if isinstance(ep, dict) and str(ep.get("protocol", "")).lower() == "mqtt"}
    holders = [state] + [line for line in state.get("lines") or [] if isinstance(line, dict)]
    for holder in holders:
        for card in holder.get("send_actions") or []:
            channel = channels.get(card.get("endpoint_id")) if isinstance(card, dict) else None
            if channel is not None and not card.get("topic"):
                card["topic"] = str(channel.get("topic") or "").strip()
    broker = state.get("mqtt")
    if isinstance(broker, dict) and any(
        ep.get("host") == broker.get("host") and ep.get("port") == broker.get("port") for ep in channels.values()
    ):
        broker["auto_connect"] = False


def _upgrade_to_v8(state: Dict[str, Any]) -> None:
    """Every vision camera holds its own counting settings, and TCP channels do what they say.

    A class whose name contains "defect", "scratch" or "broken" was always a
    defect; that is now a switch per camera, on for every existing camera so
    it keeps rejecting what it rejected.

    The count lines (and the direction and tracking settings) were the line's;
    each vision camera that has none of its own gets the line's, so it counts
    where it counted. The line keeps them too: Line 1's top-level
    ``action_trigger`` is what version 1 and older API clients read.

    A TCP channel's delimiter and mode were saved but never used: every
    message went out as a client, ending in a newline. They are used now, so
    each existing channel is set to what it really did.

    Each line gets ``counts_reset_at``: its counters are kept from then on.
    """
    for line in state.get("lines") or []:
        if not isinstance(line, dict):
            continue
        holder = state if line.get("id") == PRIMARY_LINE_ID else line
        trigger = holder.get("action_trigger") if isinstance(holder.get("action_trigger"), dict) else {}
        for camera in line.get("cameras") or []:
            if isinstance(camera, dict) and camera.get("role", "vision") == "vision":
                camera.setdefault("name_based_defects", True)
                copy_count_lines(camera, trigger)
    # The counters now come back after a restart from the product records
    # since each line's last reset. Records start with this version, so the
    # lines count from the upgrade.
    upgraded_at = utc_now_iso()
    for line in state.get("lines") or []:
        if isinstance(line, dict):
            line.setdefault("counts_reset_at", upgraded_at)
    for endpoint in state.get("communication_endpoints") or []:
        if isinstance(endpoint, dict) and str(endpoint.get("protocol", "")).lower() == "tcp":
            endpoint["delimiter"] = "\\n"
            endpoint["mode"] = "client"
            endpoint["keep_open"] = False


def _upgrade_to_v9(state: Dict[str, Any]) -> None:
    """A camera that reads codes Continuously now only shows them: each one is flagged.

    Its reads used to reach the line (pairing, output actions, messages,
    records). The upgrade does not choose a trigger for it: whether a picture
    on a wire line catches the code depends on where the camera sits. The flag
    (``qr_trigger_review``) asks the user to choose; see normalize_camera.
    """
    for line in state.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for camera in line.get("cameras") or []:
            if isinstance(camera, dict) and reads_codes(camera) and not reads_codes_triggered(camera):
                camera["qr_trigger_review"] = True


def copy_count_lines(camera: Dict[str, Any], trigger: Dict[str, Any]) -> None:
    """Give a vision camera the line's count lines, direction and tracking where it has none of its own.

    A camera reads each of these settings from itself first and from its line
    second (counting_config_from_dict), so this changes nothing it counts.
    """
    for key in COUNT_LINE_KEYS + ("direction",):
        if camera.get(key) in (None, "") and trigger.get(key) not in (None, ""):
            camera[key] = copy.deepcopy(trigger[key])
    line_tracking = trigger.get("tracking")
    if isinstance(line_tracking, dict):
        own = camera.get("tracking") if isinstance(camera.get("tracking"), dict) else {}
        merged = {k: v for k, v in line_tracking.items() if k in TRACKING_LIMITS and v not in (None, "")}
        merged.update({k: v for k, v in own.items() if v not in (None, "")})
        if merged:
            camera["tracking"] = merged


# (version reached, step). Append a step for every change to the settings' shape.
_UPGRADES = (
    (3, _upgrade_to_v3),
    (4, _upgrade_to_v4),
    (5, _upgrade_to_v5),
    (6, _upgrade_to_v6),
    (7, _upgrade_to_v7),
    (8, _upgrade_to_v8),
    (9, _upgrade_to_v9),
)


def upgrade_state(state: Dict[str, Any]) -> bool:
    """Bring a settings file up to SCHEMA_VERSION. Returns True when it changed anything."""
    changed = upgrade_state_to_v2(state)
    version = state.get("schema_version")
    for target, step in _UPGRADES:
        if version < target:
            step(state)
            version = state["schema_version"] = target
            changed = True
    return changed


def plc_address_clashes(cards_by_line: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """PLC addresses written by cards on more than one line (a warning, not an error)."""
    users: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    for line_id, cards in cards_by_line.items():
        for card in cards or []:
            if card.get("enabled", True) is not True:
                continue
            endpoint = str(card.get("plc_endpoint_id") or "").strip()
            address = str(card.get("target_address") or "").strip()
            if not endpoint or not address:
                continue
            users.setdefault((endpoint, address.lower()), {}).setdefault(line_id, []).append(
                str(card.get("name") or card.get("id") or "")
            )
    clashes = []
    for (endpoint, address), by_line in sorted(users.items()):
        if len(by_line) > 1:
            clashes.append({
                "plc_endpoint_id": endpoint,
                "address": address,
                "lines": sorted(by_line),
                "cards": {line_id: names for line_id, names in sorted(by_line.items())},
            })
    return clashes


def readers_using_list(lines: Iterable[Dict[str, Any]], list_id: str) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(line, camera) for every camera that checks its codes against this product list."""
    return [
        (line, camera)
        for line in lines
        for camera in line.get("cameras") or []
        if reads_codes(camera) and camera.get("product_list_id") == list_id
    ]


def cameras_using_model(lines: Iterable[Dict[str, Any]], model_id: str) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(line, camera) for every vision camera that runs this model, and every text reader that finds its
    fields with it."""
    return [
        (line, camera)
        for line in lines
        for camera in line.get("cameras") or []
        if camera_model_id(camera) == model_id
    ]


def model_warnings(line: Dict[str, Any], models: Dict[str, Dict[str, Any]]) -> List[str]:
    """What is wrong with the models and class lists of a line's vision cameras.

    ``models``: what the server knows of each model, ``{model_id: {"name",
    "classes", "loaded", "error"}}``; a model that is not in it no longer exists.
    """
    warnings: List[str] = []
    for index, cam in enumerate(line.get("cameras") or []):
        if cam.get("role") != "vision":
            continue
        label = f"Camera {index + 1}"
        model = models.get(cam.get("model_id") or "")
        if not cam.get("model_id"):
            warnings.append(f"{label} has no vision model, so nothing is detected or counted on it. Choose its model and save.")
            continue
        if model is None:
            warnings.append(f"{label}: its vision model no longer exists, so nothing is detected or counted on it. Choose another model.")
            continue
        if not model.get("loaded"):
            reason = f" ({model['error']})" if model.get("error") else ""
            warnings.append(
                f"{label}: its vision model '{model.get('name')}' is not loaded{reason}. "
                f"Nothing is detected or counted on this camera until it loads."
            )
        known = {str(name).strip().lower() for name in model.get("classes") or []}
        picked = list(cam.get("expected_classes") or []) + list(cam.get("defect_classes") or [])
        unknown = list(dict.fromkeys(name for name in picked if str(name).strip().lower() not in known))
        if known and unknown:
            names = ", ".join(f"'{name}'" for name in unknown)
            one = len(unknown) == 1
            warnings.append(
                f"{label}: {names} {'is not a class' if one else 'are not classes'} of its model "
                f"'{model.get('name')}', so {'it never counts' if one else 'they never count'}. "
                f"Untick {'it' if one else 'them'} and pick from the model's classes."
            )
    return warnings


def _answers_someone(line: Dict[str, Any], send_protocols: Dict[str, str]) -> bool:
    """A PLC, robot or TCP receiver waits for this line's answers: the result handshake is on, a product
    sequence input is set, or an enabled send card goes to a TCP channel."""
    if (line.get("handshake") or {}).get("enabled") or line.get("sequence_input_id"):
        return True
    return any(card.get("enabled", True) is True and send_protocols.get(str(card.get("endpoint_id"))) == "tcp"
               for card in line.get("send_actions") or [])


def line_warnings(line: Dict[str, Any], models: Optional[Dict[str, Dict[str, Any]]] = None,
                  products_per_minute: float = 0.0, trigger_modes: Optional[Dict[str, str]] = None,
                  list_formats: Optional[Dict[str, Dict[str, Any]]] = None,
                  send_protocols: Optional[Dict[str, str]] = None) -> List[str]:
    """Things about a saved line the user should look at; none of them stops the line.

    ``models`` adds the warnings about the vision cameras' models (model_warnings).
    ``products_per_minute``: how fast the line runs now; with it, a joined
    camera whose match window could reach the next product is warned about.
    ``trigger_modes``: by camera id, the Trigger of an industrial camera ("hardware":
    a signal on its own input takes each picture, "software": the server does). It is
    the camera's own setting, which a line's settings do not hold.
    ``list_formats``: each product list's code format (a list missing reads whole codes); with
    it, a camera that sets the line's batch from codes that hold none is warned about.
    ``send_protocols``: each channel's protocol by endpoint id; with it, a send card on a TCP channel
    counts as someone waiting for an answer (the line answers someone, see _answers_someone).
    """
    modes = trigger_modes or {}
    warnings: List[str] = model_warnings(line, models) if models is not None else []
    cameras = line.get("cameras") or []
    taking = [card for card in line.get("plc_inputs") or [] if card.get("action") == "capture"]
    capturing = {camera_id for card in taking if card.get("enabled", True) is True
                 for camera_id in triggered_cameras(cameras, card)}
    numbers = {card.get("trigger_no") for card in taking}
    answers = _answers_someone(line, send_protocols or {})
    for index, cam in enumerate(cameras):
        mode = modes.get(cam.get("camera_id"), "off")
        if answers and picture_per_trigger(cam):
            if picture_source(cam, mode) == "camera" and not cam.get("notice_trigger"):
                warnings.append(
                    f"Camera {index + 1} takes its own pictures without a trigger notice: a missed or double trigger "
                    f"cannot be seen. Use a trigger notice when a PLC or robot waits for each answer."
                )
            if cam.get("role", "vision") == "vision" and cam.get("no_product", "ignore") == "ignore":
                warnings.append(
                    f"Camera {index + 1} ignores a picture with nothing in it: A trigger with nothing found gets no "
                    f"answer. Set When nothing is found to Reject."
                )
        # A camera on hardware trigger needs no signal card: every picture it takes is read.
        if reads_codes(cam) and cam.get("qr_trigger") == "signal" and cam.get("camera_id") not in capturing \
                and mode != "hardware":
            warnings.append(
                f"Camera {index + 1} reads codes when an input asks, but no trigger of an input that is switched "
                f"on is ticked on its card, so it reads no codes. Tick a trigger under Read codes, or add an "
                f"input with the action 'Take a picture' under Inputs."
            )
        if list_formats is not None and cam.get("code_sets_batch") and reads_codes(cam):
            fmt = list_formats.get(cam.get("product_list_id") or "") or {"mode": "whole"}
            if not (fmt.get("mode") == "gs1" or (fmt.get("mode") == "split" and fmt.get("batch_part"))):
                warnings.append(
                    f"Camera {index + 1} sets the line's batch from its codes, but the codes of its product list "
                    f"have no batch part. Set the list's Code format on the Products page."
                )
        if mode == "hardware" and reads_codes(cam) and not reads_codes_triggered(cam):
            warnings.append(
                f"Camera {index + 1} takes a picture on its own input, but Read codes 'Continuously' only shows "
                f"the codes. Set Read codes to 'When an input asks': every picture it takes is then read."
            )
        if (picture_per_trigger(cam) and picture_source(cam, mode) == "system" and not cam.get("triggers")
                and cam.get("camera_id") not in capturing):
            warnings.append(
                f"Camera {index + 1} takes one picture per trigger, but no trigger is ticked on its card: only the "
                f"API can take its picture. Tick a trigger under {_trigger_page(cam)}, "
                f"or add an input with the action 'Take a picture'."
            )
        for number in cam.get("triggers") or []:
            if number not in numbers:
                warnings.append(
                    f"Camera {index + 1} has trigger {number} ticked, but no input has that trigger number. "
                    f"Add an input with the action 'Take a picture' and that number, or untick it."
                )
        if mode == "software":
            # It takes a picture only when the server triggers it; these uses need a steady stream.
            if cam.get("role", "vision") == "vision" and cam.get("count_mode") != "trigger":
                warnings.append(
                    f"Camera {index + 1} is on Software trigger: it takes a picture only when the server asks, so it "
                    f"cannot count moving products. Set its Count mode to 'One picture per trigger', or its Trigger "
                    f"to Off or Hardware."
                )
            if reads_codes(cam) and cam.get("qr_trigger", "continuous") == "continuous":
                warnings.append(
                    f"Camera {index + 1} is on Software trigger: it takes a picture only when the server asks, so "
                    f"Read codes 'Continuously' reads nothing. Set Read codes to 'When an input asks' or to a "
                    f"wire line."
                )
    vision = [cam for cam in cameras if cam.get("role", "vision") == "vision"]
    if float(line.get("min_fps") or 0) > 0 and vision and all(cam.get("count_mode") == "trigger" for cam in vision):
        warnings.append(
            "This line's cameras take one picture per trigger, so frames per second are not measured. "
            "Set Minimum frames/s to 0."
        )
    for card in taking:
        if card.get("enabled", True) is not True:
            continue
        used_by = triggered_cameras(cameras, card)
        if not used_by and cameras:
            warnings.append(
                f"Input '{card.get('name') or 'Take a picture'}' (trigger {card.get('trigger_no')}) takes no picture: "
                f"no camera has that trigger ticked. Tick it on a camera's card, under Counting or Read codes."
            )
        for camera_id in used_by:
            if modes.get(camera_id) != "hardware":
                continue
            cam = next((c for c in cameras if c.get("camera_id") == camera_id), {})
            # A camera that triggers itself and names this input as its trigger notice is obeyed: the input
            # only tells the server a picture is coming.
            if picture_source(cam, modes[camera_id]) == "camera" and cam.get("notice_trigger") is not None \
                    and cam.get("notice_trigger") == card.get("trigger_no"):
                continue
            number = next((i + 1 for i, cam in enumerate(cameras) if cam.get("camera_id") == camera_id), "?")
            warnings.append(
                f"Input '{card.get('name') or 'Take a picture'}' asks Camera {number} for a picture, but that "
                f"camera is triggered by its own input, so it is not obeyed. Remove it, or set the "
                f"camera's Trigger to Software."
            )
    checks = code_checks(cameras)
    joined = joined_cameras(cameras) if counting_camera(line) else []
    if not checks and not joined:
        return warnings
    if checks and not any(c.get("role") == "vision" for c in cameras):
        return warnings
    window = int((line.get("sync") or {}).get("window_ms") or DEFAULT_SYNC_WINDOW_MS)
    # How long after its crossing a product's result can take, and what it waits for.
    # (ms, order, the joined camera, or None for the Sync window); the order breaks ties.
    waits: List[Tuple[int, int, Optional[Dict[str, Any]]]] = []
    if checks:
        waits.append((window, 0, None))
    for order, cam in enumerate(joined, start=1):
        offset, match, _ = join_settings(cam)
        waits.append((offset + match, order, cam))
    latest, _, waits_for = max(waits, key=lambda wait: wait[:2])
    for card in line.get("plc_actions") or []:
        if card.get("enabled", True) is not True:
            continue
        trigger = str(card.get("trigger") or card.get("trigger_type") or "").lower()
        condition = str(card.get("condition") or card.get("trigger_condition") or "").lower()
        acts_on_reject = trigger == "reject_counter" or (trigger in ("cross_line", "line_cross") and condition == "reject")
        try:
            delay = int(float(card.get("delay_ms", card.get("travel_delay_ms", 0)) or 0))
        except (TypeError, ValueError):
            delay = 0
        if not acts_on_reject or delay >= latest:
            continue
        name = card.get("name") or card.get("id")
        if waits_for is None:
            warnings.append(
                f"Output action '{name}' has a travel delay of {delay} ms, shorter than the "
                f"Sync window ({window} ms). A product's result can take as long as the Sync window (when no code is "
                f"read), so this action would fire late. Make its travel delay longer than the Sync window."
            )
            continue
        offset, match, _ = join_settings(waits_for)
        warnings.append(
            f"Output action '{name}' has a travel delay of {delay} ms, shorter than the time a product's result can take: "
            f"Camera {cameras.index(waits_for) + 1} joins the product result {offset} ms after the counting camera with a {match} ms match "
            f"window, so the result can come {latest} ms after the crossing and this action would fire late. "
            f"Make its travel delay longer than {latest} ms."
        )
    if products_per_minute and products_per_minute > 0:
        gap = 60000.0 / float(products_per_minute)
        for cam in joined:
            _, match, _ = join_settings(cam)
            if 2 * match > gap:
                warnings.append(
                    f"Camera {cameras.index(cam) + 1}: its match window ({match} ms) reaches the next product: products "
                    f"now pass about {gap:.0f} ms apart ({products_per_minute:g} a minute), and products must be further "
                    f"apart than twice the window. A crossing could be matched to the wrong product. Make the match "
                    f"window shorter than {int(gap / 2)} ms."
                )
    return warnings


def endpoints_used_by_line(line: Dict[str, Any]) -> Dict[str, str]:
    """{endpoint_id: what uses it} for one line's send cards, PLC cards and cameras."""
    used: Dict[str, str] = {}
    for card in line.get("send_actions") or []:
        endpoint = card.get("endpoint_id")
        if endpoint:
            used.setdefault(str(endpoint), f"send card '{card.get('name') or card.get('id')}'")
    for card in line.get("plc_actions") or []:
        endpoint = card.get("plc_endpoint_id")
        if endpoint:
            used.setdefault(str(endpoint), f"PLC card '{card.get('name') or card.get('id')}'")
    for card in line.get("plc_inputs") or []:
        endpoint = card.get("plc_endpoint_id")
        if endpoint:
            used.setdefault(str(endpoint), f"Input '{card.get('name') or card.get('id')}'")
    for cam in line.get("cameras") or []:
        if cam.get("camera_id"):
            used.setdefault(str(cam["camera_id"]), "camera")
    return used
