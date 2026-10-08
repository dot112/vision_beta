"""When a card fires: the trigger and condition shared by PLC action cards and send cards.

A card (a PLC action or a "send results" card) names a trigger, a condition
and, optionally, one of the line's cameras. ``card_fires(card, event)`` answers
whether it reacts to an event. Both dispatchers ask it, so "good", "reject",
a code read or a line start mean the same for a PLC output and for a message.

Events:
  crossing       a product counted by a vision camera, with its final result
  code_product   a product on a line whose code reader decides on its own
  qr_read        a code read (or a missing code) that is not part of a product's event
  line_state     the line was started or stopped
  alarm          an alarm was raised or cleared

Event fields used here: event_type, line_id, camera_id, counting_camera,
result ("good" / "reject", the product's final result), good_count,
reject_count, detected_classes, qr_code, qr_status ("known" / "unknown" /
"no_read"), qr_paired, line_running, alarm_code, alarm_raised, active_alarms.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from app.events.alarm_events import ALL_LINES, alarm_line
from app.services.line_config import PRIMARY_LINE_ID

QR_TRIGGERS = ("qr_read", "qr_known", "qr_unknown", "qr_no_read")
LINE_STATE_EVENT = "line_state"
ALARM_EVENT = "alarm"
ANY_LINE = ALL_LINES  # an alarm event for every line's cards

# Dashboard trigger names and the names used here.
_TRIGGER_ALIASES = {
    "cross_line": "line_cross",
    "line_cross": "line_cross",
    "good_counter": "good_counter",
    "reject_counter": "reject_counter",
    "class": "class_detected",
    "class_detected": "class_detected",
    "qr_read": "qr_read",
    "qr_known": "qr_known",
    "qr_unknown": "qr_unknown",
    "qr_no_read": "qr_no_read",
    "line_state": "line_state",
    "alarm": "alarm",
}


def normalize_trigger(card: dict) -> dict:
    """A copy of a card with ``trigger_type`` and ``trigger_condition`` filled in.

    The dashboard stores cards with short names (trigger, condition, set_value)
    and its own condition words (counter_reach, counter_exact, counter_modulo,
    class_present, a class-filtered line crossing). trigger_matches() works on
    the names set here.
    """
    c = dict(card)   # shallow copy — don't mutate the original

    if "trigger" in c and "trigger_type" not in c:
        c["trigger_type"] = _TRIGGER_ALIASES.get(str(c["trigger"]).lower(), c["trigger"])

    if "condition" in c and "trigger_condition" not in c:
        raw_cond = str(c["condition"]).lower()
        set_val = str(c.get("set_value", "1") or "1").strip()
        trigger_type = c.get("trigger_type", "line_cross")

        if trigger_type in ("good_counter", "reject_counter"):
            # Map UI counter condition names to backend format strings
            n = int(set_val) if set_val.isdigit() else int(c.get("trigger_value", 1) or 1)
            c["trigger_value"] = n
            cond_map = {
                "counter_reach":  f">={n}",
                "counter_exact":  f"=={n}",
                "counter_modulo": f"%{n}==0",
            }
            c["trigger_condition"] = cond_map.get(raw_cond, raw_cond)
        elif trigger_type == "class_detected":
            class_name = set_val or "defect"
            if raw_cond in ("class_present", "class=="):
                c["trigger_condition"] = f"class=={class_name}"
            elif raw_cond == "class_absent":
                c["trigger_condition"] = f"class_absent=={class_name}"
            else:
                c["trigger_condition"] = raw_cond
        elif trigger_type == "line_cross" and raw_cond in ("class", "class_cross"):
            # A class-filtered crossing uses the class carried by the crossing event.
            class_filter = str(c.get("set_value", "") or "").strip()
            c["trigger_condition"] = f"class=={class_filter}" if class_filter else "class=="
        else:
            # line_cross, code, line state and alarm conditions are used as they are
            c["trigger_condition"] = raw_cond
    return c


def card_line(card: dict) -> str:
    return str(card.get("line_id") or PRIMARY_LINE_ID)


def card_alarm_codes(card: dict) -> List[str]:
    """The alarm codes an "Alarm" card picked; "*" means any alarm."""
    raw = card.get("alarm_codes") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(code).strip().lower() for code in raw if str(code).strip()]


def _alarm_picked(codes: List[str], code: Any) -> bool:
    return "*" in codes or str(code or "").lower() in codes


def trigger_matches(card: dict, event: dict) -> bool:
    """True when the card's trigger and condition match the event (normalize_trigger() first)."""
    trigger = str(card.get("trigger_type", "line_cross")).lower()
    condition = str(card.get("trigger_condition", "any")).lower()
    trigger_value = int(card.get("trigger_value", 1) or 1)

    # ── Line started / stopped ────────────────────────────────────────────────
    # A start or stop only fires these cards, and these cards fire on nothing
    # else. "on" = started, "off" = stopped, "toggled" = either.
    is_line_state = event.get("event_type") == LINE_STATE_EVENT
    if trigger == "line_state" or is_line_state:
        if not (trigger == "line_state" and is_line_state):
            return False
        running = bool(event.get("line_running"))
        if condition == "on":
            return running
        if condition == "off":
            return not running
        return condition == "toggled"

    # ── Alarm raised / cleared ────────────────────────────────────────────────
    # An alarm only fires these cards, and only for the alarms the card picked.
    # "raised" = a picked alarm goes active; "cleared" = the last active picked
    # alarm clears (so a lamp goes off only when nothing it shows is left);
    # "toggled" = either.
    is_alarm = event.get("event_type") == ALARM_EVENT
    if trigger == "alarm" or is_alarm:
        if not (trigger == "alarm" and is_alarm):
            return False
        codes = card_alarm_codes(card)
        if not _alarm_picked(codes, event.get("alarm_code")):
            return False
        if card.get("id") and card.get("id") == event.get("alarm_card_id"):
            return False  # a card's own failure never fires it again
        if event.get("alarm_raised"):
            return condition in ("raised", "toggled")
        if condition not in ("cleared", "toggled"):
            return False
        line_id = card_line(card)
        return not any(
            _alarm_picked(codes, active.get("code")) and active.get("line_id") in (ANY_LINE, line_id)
            for active in event.get("active_alarms", [])
        )

    # ── QR code triggers ──────────────────────────────────────────────────────
    # A code read on its own only fires code cards; a product's event carries
    # its code (or "no_read") and fires both kinds. With the condition
    # "unpaired" a code card leaves out the products a vision camera counted:
    # it fires only for codes that were not paired with such a product.
    qr_status = event.get("qr_status")
    if trigger in QR_TRIGGERS:
        if condition == "unpaired" and event.get("event_type") == "crossing":
            return False
        if trigger == "qr_read":
            return bool(event.get("qr_code"))
        if trigger == "qr_known":
            return qr_status == "known"
        if trigger == "qr_unknown":
            return qr_status == "unknown"
        return qr_status == "no_read"
    if event.get("event_type") == "qr_read":
        return False

    # ── Product crossed the line ──────────────────────────────────────────────
    if trigger == "line_cross":
        result = str(event.get("result", "unknown")).lower()
        if condition == "any":
            return True
        if condition.startswith("class=="):
            target = condition.split("==", 1)[1].strip().lower()
            detected = {str(name).strip().lower() for name in event.get("detected_classes", [])}
            return bool(target) and target in detected
        return result == condition      # "good" or "reject": the product's final result

    # ── Good counter ──────────────────────────────────────────────────────────
    if trigger == "good_counter":
        return eval_counter(condition, int(event.get("good_count", 0)), trigger_value)

    # ── Reject counter ────────────────────────────────────────────────────────
    if trigger == "reject_counter":
        return eval_counter(condition, int(event.get("reject_count", 0)), trigger_value)

    # ── Class detected ────────────────────────────────────────────────────────
    if trigger == "class_detected":
        detected = [c.lower() for c in event.get("detected_classes", [])]
        if condition.startswith("class=="):
            target = condition.split("==", 1)[1].strip().lower()
            return target in detected
        if condition.startswith("class_in:"):
            targets = {c.strip().lower() for c in condition[9:].split(",")}
            return bool(targets & set(detected))
        if condition.startswith("class_absent=="):
            # class absent = NOT in detected
            target = condition.split("==", 1)[1].strip().lower()
            return target not in detected

    return False


def camera_matches(card: dict, event: dict) -> bool:
    """A card naming a camera fires only for that camera. A card with no camera
    fires for the line's counting camera and QR readers, not for a second
    vision camera, so adding one does not double-fire existing cards."""
    if event.get("event_type") in (LINE_STATE_EVENT, ALARM_EVENT):
        return True  # a line starts or stops, or an alarm goes on or off, as a whole
    wanted = str(card.get("camera_id") or "").strip()
    if wanted:
        return str(event.get("camera_id") or "") == wanted
    if event.get("event_type") == "qr_read":
        return True
    return event.get("counting_camera", True) is not False


def card_fires(card: dict, event: dict) -> bool:
    """Does this card react to this event? The one check both dispatchers use.

    A line's events only ever run that line's cards; a server-wide alarm runs every line's.
    """
    line_id = str(event.get("line_id") or PRIMARY_LINE_ID)
    if line_id != ANY_LINE and card_line(card) != line_id:
        return False
    return camera_matches(card, event) and trigger_matches(card, event)


def eval_counter(condition: str, count: int, n: int) -> bool:
    """Evaluate counter condition string against a count value."""
    c = condition.strip().lower().replace(" ", "")
    if c.startswith(">="):
        return count >= n
    if c == f"=={n}" or c.startswith("=="):
        try:
            return count == int(c[2:])
        except ValueError:
            return count == n
    if c.startswith("%") and "==0" in c:
        # "%N==0" or "%10==0"
        try:
            divisor = int(c[1:c.index("==0")])
            return (divisor > 0) and (count % divisor == 0) and (count > 0)
        except (ValueError, AttributeError):
            return (n > 0) and (count % n == 0) and (count > 0)
    return False


# ── Events that are not a product or a code ───────────────────────────────────

def line_state_event(line_id: str, line_name: str, running: bool) -> dict:
    """The event a line's start (running=True) or stop sends to its cards."""
    return {
        "event_type": LINE_STATE_EVENT,
        "event_id": f"line_state_{line_id}_{time.time_ns()}",
        "line_id": line_id,
        "line_name": line_name,
        "line_running": running,
        "result": "started" if running else "stopped",
        "timestamp": time.time(),
    }


def _alarm_line(alarm: dict) -> str:
    """The line whose cards an alarm concerns (see alarm_line)."""
    return alarm_line(str(alarm.get("code", "")), alarm.get("details"))


def alarm_event(alarm: dict, raised: bool, active: List[dict]) -> dict:
    """The event an alarm being raised (or cleared) sends to "Alarm" cards.
    ``active`` is every alarm still active afterwards, as ``Alarm.to_dict()``."""
    return {
        "event_type": ALARM_EVENT,
        "event_id": f"alarm_{alarm.get('id', '')}_{'raised' if raised else 'cleared'}_{time.time_ns()}",
        "line_id": _alarm_line(alarm),
        "alarm_id": alarm.get("id"),
        "alarm_code": alarm.get("code"),
        "alarm_source": alarm.get("source"),
        "alarm_severity": alarm.get("severity"),
        "alarm_message": alarm.get("message"),
        "alarm_card_id": (alarm.get("details") or {}).get("card_id"),
        "alarm_raised": raised,
        "active_alarms": [{"code": a.get("code"), "line_id": _alarm_line(a)} for a in active],
        "result": "raised" if raised else "cleared",
        "timestamp": time.time(),
    }
