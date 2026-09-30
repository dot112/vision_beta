"""Protection against request floods and long-lived requests.

Covers the problems a load test on 2026-09-30 found: open MJPEG streams held a
database connection each (15 of them hung every other request), detect calls
starved live counting of model time, one client could flood the API, and
parallel wrong-password logins slipped past the lockout.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

API = "/api/v1"


# ── Streams no longer hold a database connection ──────────────────────────────

def test_streaming_response_does_not_hold_a_db_connection(client, admin_headers):
    """FastAPI keeps dependencies open until a stream ends; auth must not pin a connection."""
    from fastapi import APIRouter, Depends, FastAPI
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    from app.db.session import engine
    from app.dependencies import require_operator

    held_during_stream = []

    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/stream")
    async def stream():
        async def body():
            yield b"first"
            held_during_stream.append(engine.pool.checkedout())
            yield b"second"

        return StreamingResponse(body(), media_type="application/octet-stream")

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        res = test_client.get("/stream", headers=admin_headers)

    assert res.status_code == 200
    assert res.content == b"firstsecond"
    assert held_during_stream == [0]


def test_routes_can_still_use_the_session_after_auth(client, admin_headers):
    """The auth dependency ends its transaction; the route's own queries must still work."""
    res = client.get(f"{API}/auth/me", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["username"] == "admin"
    res = client.get(f"{API}/cameras", headers=admin_headers)
    assert res.status_code == 200


# ── API inference queue ───────────────────────────────────────────────────────

class _ConnectedCamera:
    is_connected = True


def _run_gate_users(count, work_seconds):
    from app.services.vision_service import api_inference_gate

    spans = []

    def work():
        start = time.perf_counter()
        time.sleep(work_seconds)
        spans.append((start, time.perf_counter()))

    async def user():
        async with api_inference_gate.slot() as run:
            await run(work)

    async def main():
        await asyncio.gather(*(user() for _ in range(count)))

    asyncio.run(main())
    return sorted(spans)


@pytest.fixture
def fresh_gate(monkeypatch):
    from app.services import vision_service

    gate = vision_service._ApiInferenceGate()
    monkeypatch.setattr(vision_service, "api_inference_gate", gate)
    return gate


def test_api_inference_runs_one_at_a_time_and_leaves_time_for_cameras(monkeypatch, fresh_gate):
    from app.config import settings
    from app.state.application_state import app_state

    monkeypatch.setattr(settings, "API_INFERENCE_MAX_SHARE", 0.25)
    monkeypatch.setitem(app_state.cameras, "gate-cam", _ConnectedCamera())

    spans = _run_gate_users(3, 0.04)

    assert len(spans) == 3
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        # Never overlapping, and the next request waits about 3x the time the
        # previous one used (share 0.25 leaves 75% of the model to the cameras).
        assert next_start - prev_end >= 0.04 * 3 * 0.8


def test_api_inference_runs_in_parallel_without_cameras(monkeypatch, fresh_gate):
    """No camera means no counting to protect: requests overlap and never cool down."""
    from app.config import settings
    from app.state.application_state import app_state

    monkeypatch.setattr(settings, "API_INFERENCE_MAX_SHARE", 0.25)
    monkeypatch.setattr(app_state, "cameras", {})

    spans = _run_gate_users(3, 0.05)

    assert max(start for start, _ in spans) < min(end for _, end in spans)


def test_api_inference_is_charged_only_for_model_time(monkeypatch, fresh_gate):
    """Decoding, drawing and waiting for the camera's turn do not count against the share."""
    from app.config import settings
    from app.engines import inference_engine
    from app.state.application_state import app_state

    monkeypatch.setattr(settings, "API_INFERENCE_MAX_SHARE", 0.25)
    monkeypatch.setitem(app_state.cameras, "gate-cam", _ConnectedCamera())
    spans = []

    def work():
        start = time.perf_counter()
        time.sleep(0.06)  # decode and draw: not model time
        inference_engine._record_model_time(0.01)
        spans.append((start, time.perf_counter()))

    async def main():
        async def user():
            async with fresh_gate.slot() as run:
                await run(work)

        await asyncio.gather(user(), user())

    asyncio.run(main())
    (_, first_end), (second_start, _) = sorted(spans)
    # Charged 10 ms of model time, so the cooldown is about 30 ms, not 180 ms.
    assert 0.02 <= second_start - first_end < 0.1


def test_api_inference_charge_is_capped(monkeypatch, fresh_gate):
    """A one-off slow first run cannot hold up the queue for several seconds."""
    from app.config import settings
    from app.engines import inference_engine
    from app.state.application_state import app_state

    monkeypatch.setattr(settings, "API_INFERENCE_MAX_SHARE", 0.25)
    monkeypatch.setattr(type(fresh_gate), "_MAX_CHARGE_SECONDS", 0.02)
    monkeypatch.setitem(app_state.cameras, "gate-cam", _ConnectedCamera())
    spans = []

    def work():
        start = time.perf_counter()
        inference_engine._record_model_time(5.0)
        spans.append((start, time.perf_counter()))

    async def main():
        async def user():
            async with fresh_gate.slot() as run:
                await run(work)

        await asyncio.gather(user(), user())

    asyncio.run(main())
    (_, first_end), (second_start, _) = sorted(spans)
    assert second_start - first_end < 0.2


def test_api_inference_queue_limit_rejects_extra_waiters(monkeypatch, fresh_gate):
    from app.config import settings
    from app.services.vision_service import ApiInferenceBusy
    from app.state.application_state import app_state

    monkeypatch.setattr(settings, "API_INFERENCE_QUEUE_LIMIT", 2)
    monkeypatch.setitem(app_state.cameras, "gate-cam", _ConnectedCamera())
    outcomes = []

    async def main():
        release = asyncio.Event()

        async def holder():
            async with fresh_gate.slot():
                await release.wait()

        async def waiter():
            try:
                async with fresh_gate.slot():
                    outcomes.append("ran")
            except ApiInferenceBusy:
                outcomes.append("busy")

        holding = asyncio.create_task(holder())
        await asyncio.sleep(0)
        waiters = [asyncio.create_task(waiter()) for _ in range(2)]
        await asyncio.sleep(0)
        await waiter()  # third in line: over the limit of 2 waiting
        release.set()
        await asyncio.gather(holding, *waiters)

    asyncio.run(main())
    assert outcomes == ["busy", "ran", "ran"]


@pytest.mark.parametrize("method,path", [
    ("post", "/vision/detect/camera/{cid}"),
    ("get", "/vision/annotated/camera/{cid}"),
])
def test_busy_inference_queue_returns_429(client, admin_headers, fake_engine, fake_camera, monkeypatch, method, path):
    import contextlib

    from app.services import vision_service

    @contextlib.asynccontextmanager
    async def busy_slot():
        raise vision_service.ApiInferenceBusy("queue full")
        yield  # pragma: no cover

    monkeypatch.setattr(vision_service.api_inference_gate, "slot", busy_slot)
    fake_camera("busy-cam")

    try:
        res = getattr(client, method)(API + path.format(cid="busy-cam"), headers=admin_headers)
    finally:
        vision_service.CameraStreamPipeline.remove_camera("busy-cam")
    assert res.status_code == 429, res.text
    assert res.headers["retry-after"] == "1"
    assert fake_engine.calls == 0


def test_busy_inference_queue_returns_429_for_uploads(client, admin_headers, fake_engine, monkeypatch):
    import contextlib
    import io

    from app.services import vision_service

    @contextlib.asynccontextmanager
    async def busy_slot():
        raise vision_service.ApiInferenceBusy("queue full")
        yield  # pragma: no cover

    monkeypatch.setattr(vision_service.api_inference_gate, "slot", busy_slot)
    res = client.post(
        f"{API}/vision/detect",
        headers=admin_headers,
        files={"file": ("part.jpg", io.BytesIO(b"\xff\xd8\xff"), "image/jpeg")},
    )
    assert res.status_code == 429, res.text
    assert fake_engine.calls == 0


def test_queued_detect_grabs_the_frame_at_request_time(client, admin_headers, fake_engine, fake_camera, monkeypatch):
    """A request that waits for the model still inspects the frame from when it was sent."""
    from app.config import settings
    from app.services import vision_service

    monkeypatch.setattr(settings, "API_INFERENCE_MAX_SHARE", 0.25)
    original_predict = fake_engine.predict_mat

    def slow_predict(mat, conf_threshold=None, nms_threshold=None):
        time.sleep(0.3)
        return original_predict(mat, conf_threshold, nms_threshold)

    monkeypatch.setattr(fake_engine, "predict_mat", slow_predict)
    monkeypatch.setattr(vision_service, "api_inference_gate", vision_service._ApiInferenceGate())
    cam = fake_camera("conveyor-cam")
    grab_times = []
    original_grab = cam.grab_raw_frame

    def grab():
        grab_times.append(time.monotonic())
        return original_grab()

    cam.grab_raw_frame = grab
    sent = {}

    def call(tag):
        sent[tag] = time.monotonic()
        return client.post(f"{API}/vision/detect/camera/conveyor-cam", headers=admin_headers).status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, "A")
            time.sleep(0.05)
            second = pool.submit(call, "B")
            assert first.result() == 200 and second.result() == 200
    finally:
        vision_service.CameraStreamPipeline.remove_camera("conveyor-cam")

    assert len(grab_times) == 2
    assert sorted(grab_times)[1] - sent["B"] < 0.15  # not after A's 0.3 s run and the cooldown


def test_production_trigger_skips_the_api_queue(client, admin_headers, fake_engine, fake_camera, monkeypatch):
    import contextlib

    from app.services import vision_service

    @contextlib.asynccontextmanager
    async def busy_slot():
        raise vision_service.ApiInferenceBusy("queue full")
        yield  # pragma: no cover

    monkeypatch.setattr(vision_service.api_inference_gate, "slot", busy_slot)
    created = client.post(f"{API}/cameras", headers=admin_headers, json={
        "name": "Trigger cam", "type": "usb", "source": "0", "settings": {},
    }).json()
    fake_camera(created["id"])
    try:
        res = client.post(f"{API}/control/trigger/{created['id']}", headers=admin_headers)
        assert res.status_code == 200, res.text
        assert res.json()["inspection_result"]["total_detections"] == 1
    finally:
        client.delete(f"{API}/cameras/{created['id']}", headers=admin_headers)


# ── Login lockout race ────────────────────────────────────────────────────────

def test_parallel_wrong_passwords_get_only_the_allowed_checks(client, monkeypatch):
    from app.routes.v1 import auth as auth_routes
    from app.services.auth_service import AuthService

    checks = []
    lock = threading.Lock()

    async def slow_wrong_password(db, username, password):
        with lock:
            checks.append(username)
        await asyncio.sleep(0.3)  # all attempts are in flight together
        return None

    monkeypatch.setattr(AuthService, "authenticate_user", staticmethod(slow_wrong_password))
    username = "race_tester"

    def attempt(_):
        return client.post(f"{API}/auth/login", json={"username": username, "password": "wrong"}).status_code

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(attempt, range(16)))
    finally:
        with auth_routes._login_attempt_lock:
            for key in [k for k in auth_routes._login_attempts if k.endswith(f":{username}")]:
                del auth_routes._login_attempts[key]

    assert len(checks) == auth_routes._MAX_LOGIN_FAILURES
    assert codes.count(401) == auth_routes._MAX_LOGIN_FAILURES
    assert codes.count(429) == 16 - auth_routes._MAX_LOGIN_FAILURES


def test_login_errors_do_not_use_up_attempts(client, monkeypatch):
    from app.routes.v1 import auth as auth_routes
    from app.services.auth_service import AuthService

    async def broken(db, username, password):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(AuthService, "authenticate_user", staticmethod(broken))
    username = "error_tester"
    try:
        for _ in range(auth_routes._MAX_LOGIN_FAILURES + 2):
            with pytest.raises(RuntimeError):
                client.post(f"{API}/auth/login", json={"username": username, "password": "x"})
        failures = [v[0] for k, v in auth_routes._login_attempts.items() if k.endswith(f":{username}")]
        assert failures in ([], [0])
    finally:
        with auth_routes._login_attempt_lock:
            for key in [k for k in auth_routes._login_attempts if k.endswith(f":{username}")]:
                del auth_routes._login_attempts[key]


# ── Per-client rate limit ─────────────────────────────────────────────────────

def _limited_app(rate, burst):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.middleware.rate_limit import RateLimitMiddleware

    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/v1/thing", ok), Route("/dashboard", ok)])
    return RateLimitMiddleware(app, rate=rate, burst=burst)


async def _call(app, path, client_host="10.0.0.1"):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [], "client": (client_host, 5000), "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def test_rate_limit_allows_a_burst_then_returns_429(monkeypatch):
    from app.middleware import rate_limit

    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    app = _limited_app(rate=2, burst=3)

    async def main():
        codes = [(await _call(app, "/api/v1/thing"))[0] for _ in range(3)]
        status, headers = await _call(app, "/api/v1/thing")
        other_client = (await _call(app, "/api/v1/thing", client_host="10.0.0.2"))[0]
        page = (await _call(app, "/dashboard"))[0]
        now[0] += 0.5  # refills one token at 2 per second
        refilled = (await _call(app, "/api/v1/thing"))[0]
        again = (await _call(app, "/api/v1/thing"))[0]
        return codes, status, headers, other_client, page, refilled, again

    codes, status, headers, other_client, page, refilled, again = asyncio.run(main())
    assert codes == [200, 200, 200]
    assert status == 429
    assert headers[b"retry-after"] == b"1"
    assert other_client == 200  # each client address has its own bucket
    assert page == 200  # only /api/ paths are limited
    assert refilled == 200
    assert again == 429


def test_production_triggers_have_their_own_rate_limit_bucket(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.middleware import rate_limit

    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: 1000.0)

    async def ok(request):
        return PlainTextResponse("ok")

    app = rate_limit.RateLimitMiddleware(
        Starlette(routes=[Route("/api/v1/thing", ok), Route("/api/v1/control/trigger/{cid}", ok, methods=["GET"])]),
        rate=1, burst=2,
    )

    async def main():
        drained = [(await _call(app, "/api/v1/thing"))[0] for _ in range(3)]
        trigger = [(await _call(app, "/api/v1/control/trigger/cam1"))[0] for _ in range(3)]
        return drained, trigger

    drained, trigger = asyncio.run(main())
    assert drained == [200, 200, 429]
    assert trigger == [200, 200, 429]  # its own budget, still limited


def test_rate_limit_zero_turns_it_off():
    app = _limited_app(rate=0, burst=1)

    async def main():
        return [(await _call(app, "/api/v1/thing"))[0] for _ in range(20)]

    assert asyncio.run(main()) == [200] * 20


def test_rate_limit_forgets_idle_clients(monkeypatch):
    from app.middleware import rate_limit

    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(rate_limit.RateLimitMiddleware, "_MAX_CLIENTS", 3)
    app = _limited_app(rate=10, burst=5)

    async def main():
        for host in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            await _call(app, "/api/v1/thing", client_host=host)
        now[0] += 1.0  # every bucket has refilled
        await _call(app, "/api/v1/thing", client_host="10.0.0.4")

    asyncio.run(main())
    assert list(app._buckets) == ["10.0.0.4"]


def test_app_registers_the_rate_limit():
    from app.middleware.rate_limit import RateLimitMiddleware
    import main

    assert any(m.cls is RateLimitMiddleware for m in main.create_app().user_middleware)


# ── API keys ──────────────────────────────────────────────────────────────────

def test_api_key_use_is_still_recorded(client, admin_headers):
    me = client.get(f"{API}/auth/me", headers=admin_headers).json()
    created = client.post(f"{API}/auth/api-keys", headers=admin_headers, json={
        "user_id": me["id"], "name": "usage check", "scopes": ["monitor:read"], "expires_in_days": 1,
    })
    assert created.status_code == 201, created.text
    key_id = created.json()["id"]

    assert client.get(f"{API}/cameras", headers={"X-API-Key": created.json()["api_key"]}).status_code == 200

    keys = client.get(f"{API}/auth/api-keys", headers=admin_headers).json()
    assert next(k for k in keys if k["id"] == key_id)["last_used_at"] is not None
