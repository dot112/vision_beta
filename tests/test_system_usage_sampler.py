"""The system usage sampler: it reads every interval while someone asks, and stops when nobody does."""
from __future__ import annotations

import threading
import time

import pytest

from app.services.system_usage import UsageSampler


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def made():
    samplers = []

    def make(**kwargs):
        calls = {"read": 0, "prime": 0, "order": []}

        def read():
            calls["read"] += 1
            calls["order"].append("read")
            return {"n": calls["read"]}

        def prime():
            calls["prime"] += 1
            calls["order"].append("prime")

        kwargs.setdefault("read", read)
        sampler = UsageSampler(prime=prime, interval=0.05, first_wait=0.0, **kwargs)
        samplers.append(sampler)
        return sampler, calls

    yield make
    for sampler in samplers:
        sampler.stop()


def _wait_until(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_starts_on_the_first_request_and_reads_every_interval(made):
    sampler, calls = made()
    assert not sampler.running and calls["read"] == 0
    first = sampler.latest()
    assert first == {"n": 1} and sampler.running
    assert calls["order"][:2] == ["prime", "read"] and calls["prime"] == 1
    time.sleep(0.3)
    assert calls["read"] >= 3
    assert sampler.latest()["n"] >= 3


def test_stops_after_idle(made):
    clock = Clock()
    sampler, _ = made(clock=clock, idle_stop=60.0)
    sampler.latest()
    clock.now += 61
    assert _wait_until(lambda: not sampler.running, 0.5)


def test_restarts_after_stopping(made):
    clock = Clock()
    sampler, calls = made(clock=clock, idle_stop=60.0)
    sampler.latest()
    clock.now += 61
    assert _wait_until(lambda: not sampler.running, 0.5)
    before = calls["read"]
    assert sampler.latest()["n"] > before and sampler.running
    assert calls["prime"] == 2


def test_waits_for_the_first_reading_only(made):
    sampler, _ = made(read=lambda: (time.sleep(0.2), {"slow": True})[1])
    started = time.monotonic()
    assert sampler.latest() == {"slow": True}
    assert time.monotonic() - started >= 0.15


# ── Review Focus ──────────────────────────────────────────────────────────────

def test_a_slow_reading_does_not_hold_the_answer(made):
    slow = threading.Event()
    count = {"n": 0}

    def read():
        count["n"] += 1
        if slow.is_set():
            time.sleep(2.0)
        return {"n": count["n"]}

    sampler, _ = made(read=read)
    assert sampler.latest() == {"n": 1}
    slow.set()
    time.sleep(0.1)                              # the thread is now inside a 2 s reading
    started = time.monotonic()
    answer = sampler.latest()
    assert time.monotonic() - started < 0.2
    assert answer["n"] >= 1
