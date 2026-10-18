"""Trigger pairing: a trigger notice and a camera picture belong together when they are at most the window apart."""
from __future__ import annotations

import sys
import threading

import pytest

from app.services.trigger_pairing import Expired, Notice, Picture, PicturePairer

W = 1.0


def test_notice_then_picture_pair():
    p = PicturePairer(W)
    assert p.add_notice(10.0, {"n": 1}) is None
    assert p.waiting() == (1, 0)
    pair = p.add_picture(10.4, "f", 1)
    assert pair is not None
    notice, picture = pair
    assert isinstance(notice, Notice) and isinstance(picture, Picture)
    assert notice.at == 10.0 and notice.context == {"n": 1}
    assert picture.at == 10.4 and picture.frame == "f" and picture.frame_id == 1
    assert p.waiting() == (0, 0)


def test_picture_then_notice_pair():
    p = PicturePairer(W)
    assert p.add_picture(10.0, "f", 1) is None
    assert p.waiting() == (0, 1)
    pair = p.add_notice(10.3, {"n": 1})
    assert pair is not None
    notice, picture = pair
    assert notice.context == {"n": 1}
    assert picture.frame_id == 1
    assert p.waiting() == (0, 0)


def test_outside_the_window_no_pair():
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    assert p.add_picture(11.2, "f", 1) is None
    assert p.waiting() == (1, 1)


def test_picture_then_notice_outside_the_window_no_pair():
    p = PicturePairer(W)
    p.add_picture(10.0, "f", 1)
    assert p.add_notice(11.2, {}) is None
    assert p.waiting() == (1, 1)


def test_exactly_the_window_apart_still_pairs_notice_first():
    p = PicturePairer(W)
    p.add_notice(10.0, {"n": 1})
    pair = p.add_picture(11.0, "f", 1)
    assert pair is not None and pair[0].context == {"n": 1}


def test_exactly_the_window_apart_still_pairs_picture_first():
    p = PicturePairer(W)
    p.add_picture(10.0, "f", 1)
    pair = p.add_notice(11.0, {"n": 1})
    assert pair is not None and pair[1].frame_id == 1


def test_notice_expires_as_missing():
    p = PicturePairer(W)
    p.add_notice(10.0, {"n": 1})
    e = p.expire(11.01)
    assert isinstance(e, Expired)
    assert [n.context for n in e.missing] == [{"n": 1}]
    assert not e.late and not e.extra
    assert p.waiting() == (0, 0)


def test_a_notice_is_not_missing_exactly_at_the_window():
    p = PicturePairer(W)
    p.add_notice(10.0, {"n": 1})
    e = p.expire(11.0)  # exactly W old: its picture can still pair
    assert not e.missing and not e.late and not e.extra
    assert p.waiting() == (1, 0)
    assert p.add_picture(11.0, "f", 1) is not None


def test_a_picture_is_not_gone_exactly_at_the_window():
    p = PicturePairer(W)
    p.add_picture(10.0, "f", 1)
    e = p.expire(11.0)  # exactly W old: its notice can still pair
    assert not e.missing and not e.late and not e.extra
    assert p.waiting() == (0, 1)
    assert p.add_notice(11.0, {"n": 1}) is not None


def test_a_notice_still_inside_the_window_is_kept_on_expire():
    p = PicturePairer(W)
    p.add_notice(10.0, {"n": 1})
    p.add_notice(10.8, {"n": 2})
    e = p.expire(11.25)
    assert [n.context for n in e.missing] == [{"n": 1}]
    assert p.waiting() == (1, 0)
    pair = p.add_picture(11.5, "f", 1)
    assert pair is not None and pair[0].context == {"n": 2}


def test_picture_after_an_expired_notice_is_late():
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.expire(11.01)
    p.add_picture(11.5, "f", 1)
    e = p.expire(12.6)
    assert len(e.late) == 1 and not e.extra and not e.missing
    assert e.late[0].frame_id == 1
    assert p.waiting() == (0, 0)


def test_late_does_not_depend_on_when_expire_runs():
    # the notice's window ended at 11.0; the picture came 0.25 s after that,
    # whether or not expire had already run by then
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.add_picture(11.25, "f", 1)
    p.expire(11.5)  # the notice goes missing here
    e = p.expire(12.5)
    assert len(e.late) == 1 and not e.extra

    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.add_picture(11.25, "f", 1)
    e = p.expire(12.5)  # one sweep removes both
    assert len(e.missing) == 1 and len(e.late) == 1 and not e.extra


@pytest.mark.parametrize("picture_at", [11.0, 12.0])
def test_late_window_edges_are_inside(picture_at):
    # the notice's window ended at 11.0: a picture from 11.0 to 12.0 is late
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.expire(11.25)
    p.add_picture(picture_at, "f", 1)
    e = p.expire(picture_at + 1.25)
    assert len(e.late) == 1 and not e.extra


def test_a_picture_long_after_a_missing_notice_is_extra():
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.expire(11.25)
    p.add_picture(12.5, "f", 1)
    e = p.expire(13.75)
    assert len(e.extra) == 1 and not e.late


def test_the_latest_missing_notice_decides_late():
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.expire(11.25)  # missing, its window ended at 11.0
    p.add_notice(20.0, {})
    p.expire(21.25)  # missing, its window ended at 21.0
    p.add_picture(21.5, "f", 1)
    e = p.expire(23.0)
    assert len(e.late) == 1 and not e.extra


def test_picture_with_no_notice_is_extra():
    p = PicturePairer(W)
    p.add_picture(10.0, "f", 1)
    e = p.expire(11.01)
    assert len(e.extra) == 1 and not e.late and not e.missing
    assert e.extra[0].frame_id == 1
    assert p.waiting() == (0, 0)


def test_a_lost_picture_does_not_shift_the_next_pairs():
    # notices every 0.6 s with W=0.25; the 2nd picture is lost
    p = PicturePairer(0.25)
    p.add_notice(0.0, {"n": 1})
    assert p.add_picture(0.1, "a", 1)[0].context["n"] == 1
    p.add_notice(0.6, {"n": 2})
    e = p.expire(0.86)
    assert [n.context["n"] for n in e.missing] == [2]
    p.add_notice(1.2, {"n": 3})
    pair = p.add_picture(1.3, "c", 3)
    assert pair[0].context["n"] == 3 and pair[1].frame == "c"
    assert p.waiting() == (0, 0)


def test_several_in_flight_pair_in_order():
    p = PicturePairer(W)
    p.add_notice(0.0, {"n": 1})
    p.add_notice(0.1, {"n": 2})
    assert p.add_picture(0.2, "a", 1)[0].context["n"] == 1
    assert p.add_picture(0.3, "b", 2)[0].context["n"] == 2
    assert p.waiting() == (0, 0)


def test_several_pictures_in_flight_pair_in_order():
    p = PicturePairer(W)
    p.add_picture(0.0, "a", 1)
    p.add_picture(0.1, "b", 2)
    pair = p.add_notice(0.2, {"n": 1})
    assert pair[1].frame_id == 1
    pair = p.add_notice(0.3, {"n": 2})
    assert pair[1].frame_id == 2
    assert p.waiting() == (0, 0)


def test_clear_drops_everything():
    p = PicturePairer(W)
    p.add_notice(0, {})
    p.add_picture(5, "f", 1)
    assert p.waiting() == (1, 1)
    p.clear()
    assert p.waiting() == (0, 0)
    e = p.expire(100.0)
    assert not e.missing and not e.late and not e.extra


def test_clear_forgets_a_missing_notice():
    p = PicturePairer(W)
    p.add_notice(10.0, {})
    p.expire(11.25)
    p.clear()
    p.add_picture(11.5, "f", 1)
    e = p.expire(12.75)
    assert len(e.extra) == 1 and not e.late


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.add_notice(1.0, {}),
        lambda p: p.add_picture(1.0, "f", 1),
        lambda p: p.expire(5.0),
        lambda p: p.clear(),
        lambda p: p.waiting(),
    ],
    ids=["add_notice", "add_picture", "expire", "clear", "waiting"],
)
def test_every_method_takes_the_lock(call):
    p = PicturePairer(W)
    done = threading.Event()

    def run():
        call(p)
        done.set()

    t = threading.Thread(target=run)
    with p._lock:  # while someone else holds the lock, the call has to wait
        t.start()
        assert not done.wait(0.2)
    assert done.wait(5)
    t.join()


def test_two_threads_pair_every_notice_once():
    # the PLC input (event loop) and the camera runner thread use one pairer:
    # each notice meets its own picture exactly once, whoever comes second.
    # A smoke test: the GIL hides most races, the lock itself is checked above.
    rounds = 500
    p = PicturePairer(W)
    barrier = threading.Barrier(2, timeout=30)
    pairs = []
    errors = []

    def notices():
        try:
            for i in range(rounds):
                barrier.wait()
                got = p.add_notice(i * 10.0, {"n": i})
                if got is not None:
                    pairs.append(got)
        except Exception as exc:  # pragma: no cover - only on a broken run
            errors.append(exc)
            barrier.abort()

    def pictures():
        try:
            for i in range(rounds):
                barrier.wait()
                got = p.add_picture(i * 10.0, "f", i)
                if got is not None:
                    pairs.append(got)
        except Exception as exc:  # pragma: no cover - only on a broken run
            errors.append(exc)
            barrier.abort()

    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=notices), threading.Thread(target=pictures)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        sys.setswitchinterval(switch)
    assert not errors
    assert len(pairs) == rounds
    assert all(notice.context["n"] == picture.frame_id for notice, picture in pairs)
    assert p.waiting() == (0, 0)
