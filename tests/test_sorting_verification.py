"""Camera 3: sorting verification at the accepted and rejected outfeeds.

A camera that reads codes can watch one outfeed (``sorting_lane``). Its reads never make
a product, never count and never pair: each one checks that the oldest product with that
code which is not verified yet went the way its result sent it.
"""
from __future__ import annotations

import asyncio

import pytest

import app.services.line_service as line_module
from app.events.alarm_events import ALARM_CATALOG, alarm_manager
from app.services.card_triggers import card_fires, normalize_trigger
from app.services.line_config import (
    CARD_TRIGGERS,
    READER_KEPT_KEYS,
    REASON_SORTING_MISMATCH,
    code_checks,
    normalize_camera,
    normalize_line,
)
from app.services.plc_dispatcher_service import PLCDispatcherService
from app.services.plc_input_service import PLCInputService
from app.services.settings_persistence_service import SettingsPersistenceService
from tests.test_handshake import ACK, ACK_INPUT, HANDSHAKE, OUTPUTS, _ack, _alarms, _card, _products, _request
from tests.test_joined_cameras import LIST, _run, lines  # noqa: F401  (a fixture)
from tests.test_plc_failsafe import plc  # noqa: F401  (a fixture)
from tests.test_plc_inputs import signals  # noqa: F401  (a fixture)
from tests.test_product_sequence import (  # noqa: F401  (fixtures)
    CAPTURE_INPUT, GOOD_QR, READER, SEQUENCE_INPUT, VISION, _poll, carton,
)
from tests.test_production_records import _Recorder
from tests.test_trigger_inspection import workers  # noqa: F401  (a fixture)

ACCEPTED = {"camera_id": "cam3a", "role": "qr", "product_list_id": LIST, "qr_trigger": "continuous",
            "sorting_lane": "accepted"}
REJECTED = {**ACCEPTED, "camera_id": "cam3r", "sorting_lane": "rejected"}
SORTING_CARDS = [
    _card("VerificationFault", "sorting_mismatch", "DB1.VerificationFault"),
    _card("Verified", "sorting_verified", "DB1.Verified", operation="pulse"),
    _card("ResetVerificationFault", "input", "DB1.VerificationFault", operation="reset", input_id="in_ack",
          condition="on"),
]


# ── The setting ───────────────────────────────────────────────────────────────

def test_a_reader_can_watch_one_outfeed():
    camera = normalize_camera(ACCEPTED, 0)
    assert camera["sorting_lane"] == "accepted"
    assert "sorting_lane" not in normalize_camera({**ACCEPTED, "sorting_lane": None}, 0)
    assert "sorting_lane" not in normalize_camera({**ACCEPTED, "sorting_lane": ""}, 0)
    assert "sorting_lane" in READER_KEPT_KEYS
    with pytest.raises(ValueError, match="outfeed"):
        normalize_camera({**ACCEPTED, "sorting_lane": "sideways"}, 0)
    with pytest.raises(ValueError, match="Report"):
        normalize_camera({**ACCEPTED, "qr_action": "accept_listed"}, 0)
    # A vision camera that does not read codes has no outfeed to watch.
    assert "sorting_lane" not in normalize_camera({"camera_id": "v", "model_id": "m", "sorting_lane": "accepted"}, 0)
    # It is the user's choice of what the camera does: the version 9 flag goes, and it sets no batch.
    flagged = normalize_camera({**ACCEPTED, "qr_trigger_review": True, "code_sets_batch": True}, 0)
    assert "qr_trigger_review" not in flagged and "code_sets_batch" not in flagged


def test_one_camera_per_outfeed_and_it_never_decides():
    with pytest.raises(ValueError, match="outfeed"):
        normalize_line({"name": "L", "cameras": [ACCEPTED, {**REJECTED, "sorting_lane": "accepted"}]})
    line = normalize_line({"name": "L", "cameras": [{**VISION, "count_mode": None, "triggers": None},
                                                    ACCEPTED, REJECTED], "sync": {"enabled": False}})
    assert code_checks(line["cameras"]) == {} and line["sync"]["enabled"] is False
    # Sync needs a reader that reads products' codes: a sorting camera is not one.
    with pytest.raises(ValueError, match="Sync"):
        normalize_line({"name": "L", "cameras": [{"camera_id": "v", "model_id": "m"},
                                                 {**ACCEPTED, "qr_trigger": "signal"}], "sync": {"enabled": True}})


def test_an_older_client_keeps_the_outfeed(lines):  # noqa: F811
    saved = SettingsPersistenceService.save_line({"name": "Keep", "cameras": [ACCEPTED]})
    again = SettingsPersistenceService.save_line({"id": saved["id"], "name": "Keep", "cameras": [
        {k: v for k, v in ACCEPTED.items() if k != "sorting_lane"}]})
    assert again["cameras"][0]["sorting_lane"] == "accepted"
    cleared = SettingsPersistenceService.save_line({"id": saved["id"], "name": "Keep", "cameras": [
        {**ACCEPTED, "sorting_lane": None}]})
    assert "sorting_lane" not in cleared["cameras"][0]


def test_the_new_triggers_alarm_and_reason():
    assert {"sorting_mismatch", "sorting_verified"} <= set(CARD_TRIGGERS)
    assert REASON_SORTING_MISMATCH == "sorting_mismatch"
    catalog = {entry["code"]: entry for entry in ALARM_CATALOG}
    assert catalog["line.sorting_mismatch"]["scope"] == "line"
    mismatch = {"event_type": "sorting_mismatch", "line_id": "line-1", "camera_id": "cam3a"}
    assert card_fires(normalize_trigger({"trigger": "sorting_mismatch"}), mismatch)
    assert card_fires(normalize_trigger({"trigger": "sorting_mismatch", "camera_id": "cam3a"}), mismatch)
    assert not card_fires(normalize_trigger({"trigger": "sorting_mismatch", "camera_id": "cam3r"}), mismatch)
    assert not card_fires(normalize_trigger({"trigger": "sorting_verified"}), mismatch)
    assert not card_fires(normalize_trigger({"trigger": "qr_read"}), mismatch)
    assert not card_fires(normalize_trigger({"trigger": "cross_line", "condition": "any"}), mismatch)


# ── On the carton line ────────────────────────────────────────────────────────

@pytest.fixture
def sorting_line(carton, monkeypatch):  # noqa: F811
    fired = []

    async def note(cls, card, state, event=None):
        fired.append((card["id"], event))

    monkeypatch.setattr(PLCDispatcherService, "_dispatch", classmethod(note))
    recorder = _Recorder()
    import app.services.production_records_service as records_module
    monkeypatch.setattr(records_module, "production_recorder", recorder)
    alarm_manager.reset()
    carton["plc"].values[ACK] = False

    def make(cameras=(VISION, READER, ACCEPTED, REJECTED), handshake=True):
        line = SettingsPersistenceService.save_line({
            "name": "Carton line", "enabled": True, "cameras": list(cameras),
            "plc_inputs": [CAPTURE_INPUT, SEQUENCE_INPUT, ACK_INPUT], "sync": {"window_ms": 300},
            "sequence_input_id": "in_seq", "handshake": {**HANDSHAKE, "enabled": handshake},
            "plc_actions": OUTPUTS + SORTING_CARDS})
        line_module.line_manager.apply_state()
        PLCInputService.reload()
        PLCDispatcherService.set_cards(SettingsPersistenceService.get_line_plc_actions(line["id"]), line_id=line["id"])
        runtime = line_module.line_manager.get(line["id"])
        runtime.fired, runtime.recorder = fired, recorder
        return runtime

    yield make
    alarm_manager.reset()


async def _outfeed(runtime, camera_id, code):
    """Camera 3 sees ``code`` at one outfeed (it reads Continuously)."""
    runtime.show_read(camera_id, code, "QR_CODE")
    await asyncio.sleep(0.02)


CARTONS = ((1, GOOD_QR, ["carton"]), (2, GOOD_QR, ["carton", "damage"]), (3, None, ["carton"]),
           (4, GOOD_QR, ["wrong_label"]), (5, GOOD_QR, ["carton"]), (6, "P-999;B-2026-07", ["carton"]))


def test_the_six_cartons_with_one_in_the_wrong_outfeed(carton, sorting_line):
    runtime = sorting_line()

    async def steps():
        await _poll()
        for sequence, code, names in CARTONS:
            await _request(carton, sequence, code, names, settle=0.4 if code is None else 0.08)
            await _ack(carton)
        # Camera 3: each coded carton in the outfeed its result sends it to, except BOX-004 (a reject) in
        # the accepted outfeed. BOX-003 has no code: Camera 3 reads nothing.
        for camera_id, code in (("cam3a", GOOD_QR), ("cam3r", GOOD_QR), ("cam3a", GOOD_QR),
                                ("cam3a", GOOD_QR), ("cam3r", "P-999;B-2026-07")):
            await _outfeed(runtime, camera_id, code)

    _run(steps)
    # The products: counted once each; Camera 3 counted, paired and decided nothing.
    assert [(seq, result) for seq, result, _ in _products(runtime)] == [
        (1, "good"), (2, "reject"), (3, "reject"), (4, "reject"), (5, "good"), (6, "reject")]
    counter = runtime.counter
    assert (counter.total_inspected, counter.good_count, counter.rejected_count) == (6, 2, 4)
    summary = line_module.line_manager.summary(runtime)
    assert summary["sorting"] == {"verified": 4, "mismatches": 1, "unmatched": 0}
    # Exactly one mismatch: one alarm, one event, one record.
    mismatch_alarm, = [a for a in alarm_manager.active() if a.code == "line.sorting_mismatch"]
    assert (mismatch_alarm.details["sequence"], mismatch_alarm.details["expected_lane"],
            mismatch_alarm.details["seen_lane"], mismatch_alarm.details["code"]) == (4, "rejected", "accepted", GOOD_QR)
    events = [event for card, event in runtime.fired if card == "VerificationFault"]
    assert len(events) == 1 and (events[0]["sequence"], events[0]["camera_id"]) == (4, "cam3a")
    assert [card for card, _ in runtime.fired].count("Verified") == 4
    rows = [row for row in runtime.recorder._rows if row["kind"] == "verification"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["result"], row["reject_reason"], row["counted"], row["code"]) == ("reject", REASON_SORTING_MISMATCH, False, GOOD_QR)
    assert {k: row["details"].get(k) for k in ("sequence", "expected_lane", "seen_lane")} == \
        {"sequence": 4, "expected_lane": "rejected", "seen_lane": "accepted"}
    # Camera 3's reads went nowhere else: no code event, no code cards.
    assert not [row for row in runtime.recorder._rows if row["kind"] == "code"]
    assert runtime.qr_stats["codes_read"] == 5                   # Camera 1's five codes only
    reads = [r for r in runtime.recent_reads() if r.get("sorting_lane")]
    assert len(reads) == 5 and [r["verification"] for r in reads].count("mismatch") == 1


def test_the_ack_clears_the_mismatch_alarm(carton, sorting_line):
    runtime = sorting_line()

    async def steps():
        await _poll()
        await _request(carton, 1)
        await _ack(carton)
        await _outfeed(runtime, "cam3r", GOOD_QR)               # a good carton in the rejected outfeed
        assert "line.sorting_mismatch" in _alarms(runtime)
        await _request(carton, 2)
        await _ack(carton)

    _run(steps)
    assert "line.sorting_mismatch" not in _alarms(runtime)
    assert "ResetVerificationFault" in [card for card, _ in runtime.fired]


def test_cartons_with_the_same_code_are_verified_in_order(carton, sorting_line):
    runtime = sorting_line()

    async def steps():
        await _poll()
        await _request(carton, 1)                                # good
        await _ack(carton)
        await _request(carton, 2, names=["damage"])             # reject, the same code
        await _ack(carton)
        await _outfeed(runtime, "cam3a", GOOD_QR)               # the first one leaves first
        await _outfeed(runtime, "cam3r", GOOD_QR)

    _run(steps)
    assert runtime.sorting_counts() == {"verified": 2, "mismatches": 0, "unmatched": 0}


def test_a_code_with_no_product_is_unmatched(carton, sorting_line):
    runtime = sorting_line()

    async def steps():
        await _poll()
        await _outfeed(runtime, "cam3a", "P-100;B-OTHER")
        await _request(carton, 1)
        await _ack(carton)
        await _outfeed(runtime, "cam3a", GOOD_QR)
        await _outfeed(runtime, "cam3a", GOOD_QR)               # seen twice: nothing left to verify

    _run(steps)
    assert runtime.sorting_counts() == {"verified": 1, "mismatches": 0, "unmatched": 2}
    assert "line.sorting_mismatch" not in _alarms(runtime)


def test_a_sorting_read_never_pairs_with_a_product(carton, sorting_line):
    runtime = sorting_line(handshake=False)

    async def steps():
        await _poll()
        await _request(carton, 1)
        # BOX-003: no code on Camera 1; Camera 3 reads a code while the carton waits for its code.
        carton["vision"].names = ["carton"]
        reader = carton["readers"]["cam1"]
        reader.code = None
        carton["plc"].values.update({"DB1.ProductSequence": 3, "DB1.CaptureRequest": True})
        await _poll()
        await _outfeed(runtime, "cam3a", GOOD_QR)
        await asyncio.sleep(0.4)

    _run(steps)
    assert [(seq, result, reason) for seq, result, reason in _products(runtime)] == [
        (1, "good", None), (3, "reject", "no_code")]


def test_a_triggered_sorting_camera_verifies_each_picture(carton, sorting_line):
    triggered = {**ACCEPTED, "qr_trigger": "signal", "triggers": [5]}
    runtime = sorting_line(cameras=(VISION, READER, triggered, REJECTED))

    async def steps():
        await _poll()
        await _request(carton, 1)
        await _ack(carton)
        runtime.on_qr_capture("cam3a", {"t": 0.0, "codes": [], "status": "no_read"}, None)   # an empty picture
        runtime.on_qr_capture("cam3a", {"t": 0.0, "codes": [{"code": GOOD_QR, "format": "QR_CODE", "known": True}],
                                        "status": "read"}, None)
        await asyncio.sleep(0.05)

    _run(steps)
    assert runtime.sorting_counts() == {"verified": 1, "mismatches": 0, "unmatched": 0}
    assert runtime.qr_stats["no_reads"] == 0 and not [r for r in runtime.recorder._rows if r["kind"] == "code"]


def test_the_memory_is_bounded_and_a_reset_empties_it(carton, sorting_line):
    runtime = sorting_line()
    for n in range(250):
        runtime._sorting_note(f"C-{n}", n, "good")
    assert len(runtime._sorting_memory) == 200
    runtime.reset()
    assert len(runtime._sorting_memory) == 0
    assert runtime.sorting_counts() == {"verified": 0, "mismatches": 0, "unmatched": 0}


def test_records_can_be_filtered_to_verifications(client, admin_headers):
    span = "start=2026-10-01T00:00:00Z&end=2026-10-02T00:00:00Z"
    answer = client.get(f"/api/v1/records?{span}&kind=verification", headers=admin_headers)
    assert answer.status_code == 200, answer.text
    assert client.get(f"/api/v1/records?{span}&kind=other", headers=admin_headers).status_code == 422
