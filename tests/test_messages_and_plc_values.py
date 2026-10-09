"""Messages and PLC values (Section 1): TCP channels that do what their settings
say, text messages from templates on send cards, and PLC WRITE value sources
with a strobe.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.events.alarm_events import alarm_manager
from app.services.counting_service import _telemetry_dispatcher
from app.services.line_config import PRIMARY_LINE_ID, TEMPLATE_FIELDS, normalize_send_card, upgrade_state
from app.services.mqtt_service import MQTTChannels
from app.services.send_dispatcher_service import SendDispatcherService, build_message, deliver, render_template, template_values
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.tcp_channels import CHANNEL_DOWN, TcpChannels, decode_delimiter, frame

from tests.mqtt_broker import Broker
from tests.test_plc_failsafe import FakeDriver, plc  # noqa: F401  (a fixture)
from tests.test_send_cards import _set_cards, _with_loop, lines  # noqa: F401  (a fixture)


def _run(coro):
    return asyncio.run(coro)


def _settle(future, timeout=5.0):
    """Wait for something scheduled on the dispatcher loop (TcpChannels.apply/drop/sync)."""
    assert future is not None
    return future.result(timeout=timeout)


class _RawListener:
    """A local TCP server that keeps the bytes of each connection apart, and can be restarted on its port."""

    def __init__(self, port=0):
        self.connections = []
        self._stop = threading.Event()
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.server.listen(8)
        self.server.settimeout(0.1)
        self.port = self.server.getsockname()[1]
        self._conns = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            received = bytearray()
            self.connections.append(received)
            self._conns.append(conn)
            threading.Thread(target=self._read, args=(conn, received), daemon=True).start()

    def _read(self, conn, received):
        conn.settimeout(0.1)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            received.extend(chunk)

    def data(self):
        return b"".join(bytes(c) for c in self.connections)

    def wait_for(self, size, timeout=5.0):
        deadline = time.time() + timeout
        while len(self.data()) < size and time.time() < deadline:
            time.sleep(0.02)
        return self.data()

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        for conn in self._conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.server.close()


@pytest.fixture
def raw_listeners():
    made = []

    def make(port=0):
        made.append(_RawListener(port))
        return made[-1]

    yield make
    for listener in made:
        listener.close()


def _tcp(listener_or_port, **fields):
    port = listener_or_port if isinstance(listener_or_port, int) else listener_or_port.port
    return {"id": fields.pop("id", f"tcp-test-{port}"), "name": "Test channel", "protocol": "tcp", "enabled": True,
            "host": "127.0.0.1", "port": port, **fields}


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ── TCP framing ───────────────────────────────────────────────────────────────

def test_delimiters_decode_from_their_escaped_text():
    assert decode_delimiter("\\n") == "\n"
    assert decode_delimiter("\\r\\n") == "\r\n"
    assert decode_delimiter("\\0") == "\0"
    assert decode_delimiter("") == ""
    assert decode_delimiter(None) == "\n"            # a channel saved before it had one
    assert decode_delimiter("\n") == "\n"            # an older file holding a real newline
    assert decode_delimiter("\\x03") == "\x03"       # ETX
    assert decode_delimiter("<END>\\t") == "<END>\t"
    assert decode_delimiter("\\\\n") == "\\n"        # an escaped backslash
    assert decode_delimiter("\\q") == "\\q"          # not an escape: stays as it is
    assert frame('{"a": 1}', {"delimiter": "\\r\\n"}) == b'{"a": 1}\r\n'


@pytest.mark.parametrize("delimiter, ending", [
    ("\\n", b"\n"), ("\\r\\n", b"\r\n"), ("\\0", b"\0"), ("", b""), ("<EOT>\\x04", b"<EOT>\x04"), (None, b"\n"),
])
def test_each_delimiter_reaches_the_device(raw_listeners, delimiter, ending):
    listener = raw_listeners()
    channel = _tcp(listener) if delimiter is None else _tcp(listener, delimiter=delimiter)
    ok, detail = _run(deliver(channel, {"result": "PASSED"}))
    assert ok, detail
    expected = b'{"result": "PASSED"}' + ending
    assert listener.wait_for(len(expected)) == expected


def test_a_text_message_goes_out_as_it_is(raw_listeners):
    listener = raw_listeners()
    ok, _ = _run(deliver(_tcp(listener, delimiter="\\r\\n"), "OK;1;bottle"))
    assert ok and listener.wait_for(13) == b"OK;1;bottle\r\n"


def test_the_channel_timeout_is_used(monkeypatch):
    seen = []
    real_wait_for = asyncio.wait_for

    async def watching(awaitable, timeout):
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr("app.services.tcp_channels.asyncio.wait_for", watching)
    ok, detail = _run(deliver(_tcp(_free_port(), timeout=7), {"n": 1}))
    assert not ok and "Cannot connect" in detail
    assert seen and seen[0] == 7.0
    seen.clear()
    _run(deliver(_tcp(_free_port()), {"n": 1}))   # a channel saved without a timeout keeps the old 2 s
    assert seen[0] == 2.0


def test_without_keep_open_each_message_has_its_own_connection(raw_listeners):
    listener = raw_listeners()
    channel = _tcp(listener)
    for n in range(2):
        assert _run(deliver(channel, {"n": n}))[0]
    listener.wait_for(len(b'{"n": 0}\n{"n": 1}\n'))
    assert [bytes(c) for c in listener.connections] == [b'{"n": 0}\n', b'{"n": 1}\n']


def test_keep_open_sends_on_one_connection_and_reconnects_after_a_failure(raw_listeners, monkeypatch):
    monkeypatch.setattr(TcpChannels, "BACKOFF_FIRST", 0.2)
    listener = raw_listeners()
    port = listener.port
    channel = _tcp(listener, keep_open=True, id="tcp-kept")
    source = "channel:tcp-kept"
    try:
        for n in range(2):
            assert _run(deliver(channel, {"n": n}))[0]
        assert listener.wait_for(18) == b'{"n": 0}\n{"n": 1}\n'
        assert len(listener.connections) == 1                      # both on one connection

        listener.close()                                           # the device goes away
        time.sleep(0.2)
        ok, detail = _run(deliver(channel, {"n": 2}))
        assert not ok and "Cannot connect" in detail
        alarm = alarm_manager.get(CHANNEL_DOWN, source)
        assert alarm is not None and alarm.to_dict()["all_lines"] is True
        ok, detail = _run(deliver(channel, {"n": 3}))              # within the back-off: not tried again
        assert not ok and "next try" in detail

        again = raw_listeners(port)                                # the device is back
        time.sleep(0.25)
        ok, detail = _run(deliver(channel, {"n": 4}))
        assert ok, detail
        assert again.wait_for(9) == b'{"n": 4}\n'
        assert alarm_manager.get(CHANNEL_DOWN, source) is None
    finally:
        _settle(TcpChannels.drop("tcp-kept"))


def test_keep_open_notices_a_device_that_closed_the_connection(raw_listeners):
    listener = raw_listeners()
    channel = _tcp(listener, keep_open=True, id="tcp-kept-2")
    try:
        assert _run(deliver(channel, {"n": 1}))[0]
        listener.wait_for(9)
        port = listener.port
        listener.close()
        again = raw_listeners(port)
        time.sleep(0.3)                                            # the kept connection sees the close
        ok, detail = _run(deliver(channel, {"n": 2}))
        assert ok, detail
        assert again.wait_for(9) == b'{"n": 2}\n'
        assert alarm_manager.get(CHANNEL_DOWN, "channel:tcp-kept-2") is None
    finally:
        _settle(TcpChannels.drop("tcp-kept-2"))


# ── Server mode ───────────────────────────────────────────────────────────────

def _connect(port, attempts=50):
    for _ in range(attempts):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"nothing listens on {port}")


def _receive(sock, size):
    data = b""
    sock.settimeout(3)
    while len(data) < size:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def test_a_server_channel_sends_each_message_to_every_connected_device():
    port = _free_port()
    channel = _tcp(port, mode="server", id="tcp-server", delimiter="\\r\\n")
    try:
        _settle(TcpChannels.apply(channel))
        ok, detail = _run(deliver(channel, {"n": 0}))
        assert not ok and "no device is connected" in detail.lower()

        first, second = _connect(port), _connect(port)
        status = {}
        for _ in range(50):
            status = _run(TcpChannels.status(channel))
            if status["devices"] == 2:
                break
            time.sleep(0.05)
        assert status["listening"] and status["devices"] == 2
        ok, detail = _run(deliver(channel, {"n": 1}))
        assert ok and "2 devices" in detail
        assert _receive(first, 10) == b'{"n": 1}\r\n' and _receive(second, 10) == b'{"n": 1}\r\n'

        first.close()
        time.sleep(0.2)
        assert _run(deliver(channel, "two"))[0]
        assert _receive(second, 5) == b"two\r\n"
        second.close()
    finally:
        _settle(TcpChannels.drop("tcp-server"))
    with pytest.raises(OSError):                                   # deleted: it no longer listens
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_a_server_channel_whose_port_cannot_open_raises_the_alarm():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    channel = _tcp(blocker.getsockname()[1], mode="server", id="tcp-busy")
    try:
        _settle(TcpChannels.apply(channel))
        ok, detail = _run(deliver(channel, {"n": 1}))
        assert not ok and "Cannot listen" in detail
        assert alarm_manager.get(CHANNEL_DOWN, "channel:tcp-busy") is not None
        _settle(TcpChannels.apply({**channel, "mode": "client"}))   # no longer a server: the alarm clears
        assert alarm_manager.get(CHANNEL_DOWN, "channel:tcp-busy") is None
    finally:
        _settle(TcpChannels.drop("tcp-busy"))
        blocker.close()


def test_tcp_channel_settings_are_checked_and_kept(lines):  # noqa: F811
    async def steps():
        saved = await SettingsPersistenceService.add_or_update_endpoint(
            {"name": "Printer", "protocol": "tcp", "host": "127.0.0.1", "port": _free_port(),
             "delimiter": "\\x03", "timeout": 9, "keep_open": True})
        assert (saved["delimiter"], saved["timeout"], saved["keep_open"], saved["mode"]) == ("\\x03", 9, True, "client")
        # A client that does not send the new fields keeps them.
        kept = await SettingsPersistenceService.add_or_update_endpoint({"id": saved["id"], "name": "Printer 2", "protocol": "tcp"})
        assert (kept["delimiter"], kept["timeout"], kept["keep_open"]) == ("\\x03", 9, True)
        for bad in ({"mode": "listener"}, {"keep_open": "yes"}, {"delimiter": "x" * 33}, {"delimiter": 3}, {"timeout": 31}):
            with pytest.raises(ValueError):
                await SettingsPersistenceService.add_or_update_endpoint({"id": saved["id"], "protocol": "tcp", **bad})
        SettingsPersistenceService.delete_endpoint(saved["id"])

    _run(steps())


def test_old_tcp_channels_keep_sending_what_they_sent():
    state = {"schema_version": 7, "lines": [], "communication_endpoints": [
        {"id": "tcp-a", "protocol": "tcp", "host": "10.0.0.5", "port": 9000, "delimiter": "", "mode": "server", "timeout": 5},
        {"id": "tcp-b", "protocol": "tcp", "host": "10.0.0.6", "port": 9000, "delimiter": "\\r\\n", "mode": "client"},
        {"id": "mqtt-a", "protocol": "mqtt", "host": "broker"},
    ]}
    upgrade_state(state)
    tcp_a, tcp_b, mqtt_a = state["communication_endpoints"]
    assert (tcp_a["delimiter"], tcp_a["mode"], tcp_a["keep_open"], tcp_a["timeout"]) == ("\\n", "client", False, 5)
    assert (tcp_b["delimiter"], tcp_b["mode"], tcp_b["keep_open"]) == ("\\n", "client", False)
    assert mqtt_a == {"id": "mqtt-a", "protocol": "mqtt", "host": "broker"}


def test_queued_messages_use_the_dispatcher_loop(raw_listeners, monkeypatch):
    """A send card's message is sent on the dispatcher's thread; a Test (on another loop) hops there too."""
    listener = raw_listeners()
    channel = _tcp(listener, keep_open=True, id="tcp-loop")
    loops = []
    real = TcpChannels._send

    async def spy(endpoint, data):
        loops.append(asyncio.get_running_loop())
        return await real(endpoint, data)

    monkeypatch.setattr(TcpChannels, "_send", spy)
    try:
        assert _run(deliver(channel, {"n": 1}))[0]
        _telemetry_dispatcher.submit(lambda: deliver(channel, {"n": 2}), key=("tcp", "127.0.0.1", listener.port))
        assert _telemetry_dispatcher.wait_drained(5.0)
        assert loops == [_telemetry_dispatcher._loop] * 2
        assert listener.wait_for(18) == b'{"n": 1}\n{"n": 2}\n' and len(listener.connections) == 1
    finally:
        _settle(TcpChannels.drop("tcp-loop"))


def test_the_send_test_button_and_queued_cards_frame_the_same_way(lines, raw_listeners):  # noqa: F811
    listener = raw_listeners()
    SettingsPersistenceService._state.setdefault("communication_endpoints", []).append(
        _tcp(listener, id="tcp-card", delimiter="\\0"))
    card = {"id": "c1", "name": "To printer", "endpoint_id": "tcp-card", "trigger": "cross_line", "condition": "any",
            "fields": ["result"]}
    result = _run(SendDispatcherService.send_test(card))
    assert result["success"], result
    assert json.loads(listener.wait_for(10).rstrip(b"\0")) == {"result": "PASSED", "test": True}
    assert listener.data().endswith(b"\0")


# ── Text messages from templates ──────────────────────────────────────────────

def test_a_text_template_is_checked_when_the_card_is_saved():
    card = normalize_send_card({"name": "Printer", "format": "text", "template": "{result_code};{ code };{{x}}\\r\\n"})
    assert (card["format"], card["template"]) == ("text", "{result_code};{ code };{{x}}\\r\\n")
    assert normalize_send_card({})["format"] == "json"                         # as before
    assert normalize_send_card({"format": "json", "template": "{nope}"})["template"] == "{nope}"   # kept, not used
    for template, word in (("{result};{nope}", "{nope} is not a placeholder"), ("", "enter the text"), ("  ", "enter the text"),
                           ("a } b", "'}}'"), ("{", "'{{'"), ("{}", "names no placeholder"), ("x" * 1025, "1024")):
        with pytest.raises(ValueError, match="Printer") as refused:
            normalize_send_card({"name": "Printer", "format": "text", "template": template})
        assert word in str(refused.value), (template, refused.value)
    with pytest.raises(ValueError, match="JSON or text"):
        normalize_send_card({"format": "xml"})


def test_a_template_is_filled_in_and_a_missing_value_is_empty(monkeypatch):
    from app.state.application_state import app_state

    class Camera:
        name = "Infeed camera"

    monkeypatch.setattr(app_state, "cameras", {"vis": Camera()})
    payload = {"event": "WIRELINE_OBJECT_CROSSED", "timestamp": "2026-10-05T10:00:00+00:00", "line_id": "line-1", "line_name": "Line 1",
               "camera_id": "vis", "track_id": 7, "class_name": "bottle", "result": "REJECTED", "reject_reason": "code_not_in_list",
               "confidence": 0.95, "qr_code": "SKU-9", "qr_format": "QRCODE", "qr_status": "unknown", "product_name": None,
               "metrics": {"good_count": 4, "rejected_count": 1, "total_inspected": 5, "yield_percentage": 80.0, "products_per_minute": 12.5}}
    local = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc).astimezone()
    text = render_template("{line_name}|{camera_name}|{result}={result_code}|{code}/{code_format}/{code_status}|{product_name}|"
                           "{good_count}+{rejected_count}={total_inspected}|{batch}|{alarm_code}|{date} {time}\\t{{end}}", payload)
    assert text == (f"Line 1|Infeed camera|REJECTED=2|SKU-9/QRCODE/unknown||4+1=5|||{local:%Y-%m-%d} {local:%H:%M:%S}\t{{end}}")
    assert set(template_values(payload)) == set(TEMPLATE_FIELDS)
    assert render_template("{result_code}", {"result": "PASSED"}) == "1"
    assert render_template("[{result_code}]", {"event": "LINE_STARTED"}) == "[]"
    # A text card's message is its text; a JSON card's is as before.
    assert build_message({"format": "text", "template": "{track_id}"}, payload) == "7"
    assert build_message({"format": "json", "template": "{track_id}", "fields": ["track_id"]}, payload) == {"track_id": 7}


def test_a_text_card_sends_its_text_over_tcp(lines, raw_listeners):  # noqa: F811
    from tests.test_send_cards import _cross
    from app.services.counting_service import counting_service

    listener = raw_listeners()
    SettingsPersistenceService._state.setdefault("communication_endpoints", []).append(_tcp(listener, id="tcp-text", delimiter="\\r\\n"))
    _set_cards([{"id": "text", "name": "Printer", "endpoint_id": "tcp-text", "trigger": "cross_line", "condition": "any",
                 "format": "text", "template": "{result_code};{class_name};{total_inspected}"}])

    async def steps():
        _cross(counting_service)
        _cross(counting_service, is_defect=True)
        await asyncio.sleep(0.05)

    _with_loop(steps)
    assert listener.wait_for(len(b"1;bottle;1\r\n2;defect;2\r\n")) == b"1;bottle;1\r\n2;defect;2\r\n"


class _Webhook:
    """A local HTTP server that keeps each request's Content-Type and body."""

    def __init__(self):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                received.append((self.headers.get("Content-Type"), body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_text_message_over_a_webhook_is_plain_text_unless_the_channel_says_otherwise():
    hook = _Webhook()
    try:
        async def steps():
            plain = {"id": "w1", "protocol": "webhook", "url": hook.url, "headers": ""}
            xml = {**plain, "headers": "Content-Type: application/xml\nX-Line: 1"}
            return [await deliver(plain, "OK;1"), await deliver(xml, "<r>1</r>"), await deliver(plain, {"result": "PASSED"})]

        results = _run(steps())
        assert all(ok for ok, _ in results), results
        assert hook.received == [("text/plain; charset=utf-8", b"OK;1"), ("application/xml", b"<r>1</r>"),
                                 ("application/json", b'{"result":"PASSED"}')]
    finally:
        hook.close()


def test_a_text_message_over_mqtt_is_the_payload_as_it_is(monkeypatch):
    monkeypatch.setattr(MQTTChannels, "_clients", {})
    broker = Broker()
    channel = {"id": "m-text", "name": "m-text", "protocol": "mqtt", "enabled": True, "host": "127.0.0.1", "port": broker.port,
               "client_id": "vision_m_text", "qos": 0, "retain": False}
    try:
        ok, detail = _run(deliver(channel, "REJECTED;defect", "plant/line1/text"))
        assert ok, detail
        (message,) = broker.wait(1)
        assert (message["topic"], message["raw"]) == ("plant/line1/text", b"REJECTED;defect")
    finally:
        _run(MQTTChannels.close_all())
        broker.close()


def test_the_test_button_sends_the_filled_in_sample_text(lines, raw_listeners):  # noqa: F811
    listener = raw_listeners()
    SettingsPersistenceService._state.setdefault("communication_endpoints", []).append(_tcp(listener, id="tcp-sample"))
    card = {"id": "c2", "name": "Rejects", "endpoint_id": "tcp-sample", "trigger": "cross_line", "condition": "reject",
            "format": "text", "template": "{result_code};{class_name};{camera_name};{reject_reason}"}
    result = _run(SendDispatcherService.send_test(card, line_name="Line 1"))
    assert result["success"] and result["sent"] == "2;defect;Line camera;vision_class", result
    assert listener.wait_for(32) == b"2;defect;Line camera;vision_class\n"


def test_text_cards_through_the_api(client, admin_headers, raw_listeners):
    api = "/api/v1/send"
    listener = raw_listeners()
    made = client.post("/api/v1/system/endpoints", json={"name": "Text printer", "protocol": "tcp", "host": "127.0.0.1",
                                                          "port": listener.port, "delimiter": "\\r\\n"}, headers=admin_headers)
    assert made.status_code == 200, made.text
    channel_id = made.json()["id"]
    try:
        _text_cards_through_the_api(client, admin_headers, api, channel_id, listener)
    finally:
        client.post(f"{api}/actions/batch", json=[], headers=admin_headers)
        client.delete(f"/api/v1/system/endpoints/{channel_id}", headers=admin_headers)


def _text_cards_through_the_api(client, admin_headers, api, channel_id, listener):
    placeholders = client.get(f"{api}/fields", headers=admin_headers).json()["placeholders"]
    assert [p["name"] for p in placeholders] == list(TEMPLATE_FIELDS) and all(p["label"] for p in placeholders)

    card = {"id": "send_text_1", "name": "To printer", "endpoint_id": channel_id, "trigger": "cross_line",
            "condition": "any", "format": "text", "template": "{result};{nope}"}
    refused = client.post(f"{api}/actions/batch", json=[card], headers=admin_headers)
    assert refused.status_code == 422 and "{nope}" in refused.json()["detail"], refused.text
    card["template"] = "{result};{class_name}"
    assert client.post(f"{api}/actions/batch", json=[card], headers=admin_headers).status_code == 200
    # An older dashboard that does not know of text cards does not turn this one back into JSON.
    older = {k: v for k, v in card.items() if k not in ("format", "template")}
    saved = client.post(f"{api}/actions/batch", json=[{**older, "name": "Renamed"}], headers=admin_headers).json()["cards"][0]
    assert (saved["name"], saved["format"], saved["template"]) == ("Renamed", "text", "{result};{class_name}")
    line = client.get(f"/api/v1/lines/{PRIMARY_LINE_ID}", headers=admin_headers).json()
    resaved = client.put(f"/api/v1/lines/{PRIMARY_LINE_ID}", json={"name": line["name"], "send_actions": [older]}, headers=admin_headers)
    assert resaved.status_code == 200, resaved.text
    assert resaved.json()["send_actions"][0]["format"] == "text"

    tested = client.post(f"{api}/actions/send_text_1/test", json={}, headers=admin_headers).json()
    assert tested["success"] and tested["sent"] == "PASSED;bottle", tested
    assert listener.wait_for(15) == b"PASSED;bottle\r\n"


# ── PLC WRITE value sources and the strobe ────────────────────────────────────

class _SlowDriver(FakeDriver):
    """A fake PLC whose operations take a moment, so two cards could interleave without the endpoint lock."""

    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.failing = set()

    async def _write(self, op, address, value):
        await asyncio.sleep(0.01)
        if address in self.failing:
            return False, f"{address} is not writable"
        return await super()._write(op, address, value)


@pytest.fixture
def slow_plc(plc, monkeypatch):  # noqa: F811
    from app.hardware.plc import factory as plc_factory
    endpoint, _ = plc
    driver = _SlowDriver(endpoint)
    monkeypatch.setattr(plc_factory, "_driver_pool", {"ep1": driver})
    return driver


def _write_card(cid="w", source="fixed", **extra):
    return {"id": cid, "name": cid, "enabled": True, "plc_endpoint_id": "ep1", "trigger": "cross_line", "condition": "any",
            "operation": "write", "target_address": "40001", "write_value": 7, "value_source": source,
            "debounce_ms": 0, "exec_policy": "every_frame", **extra}


class _Runtime:
    """What PLCDispatcherService reads of a line: its cameras and its counter."""

    def __init__(self):
        self.cameras = [{"camera_id": "vis", "role": "vision", "counting": True,
                         "expected_classes": ["Bottle", "can"], "defect_classes": ["cap missing"]},
                        {"camera_id": "vis-2", "role": "vision", "counting": False,
                         "expected_classes": ["label"], "defect_classes": []}]

        class Counter:
            good_count, rejected_count = 40, 2

        self.counter = Counter()

    def camera_entry(self, camera_id):
        return next((c for c in self.cameras if c["camera_id"] == camera_id), None)

    @property
    def counting_camera_id(self):
        return next((c["camera_id"] for c in self.cameras if c.get("role") == "vision" and c.get("counting")), None)


@pytest.fixture
def one_line(monkeypatch):
    import app.services.line_service as line_module

    class Manager:
        runtime = _Runtime()

        def get(self, line_id):
            return self.runtime if line_id == PRIMARY_LINE_ID else None

    monkeypatch.setattr(line_module, "line_manager", Manager())


PRODUCT = {"event_type": "crossing", "line_id": PRIMARY_LINE_ID, "camera_id": "vis", "result": "reject",
           "reject_reason": "code_in_reject_list", "good_count": 12, "reject_count": 3, "detected_classes": ["cap missing"]}


@pytest.mark.parametrize("source, event, value", [
    ("fixed", PRODUCT, 7.0),
    ("result_code", PRODUCT, 2.0),
    ("result_code", {**PRODUCT, "result": "good"}, 1.0),
    ("good_count", PRODUCT, 12.0),
    ("reject_count", PRODUCT, 3.0),
    ("total_count", PRODUCT, 15.0),
    ("good_count", {"event_type": "line_state", "line_id": PRIMARY_LINE_ID}, 40.0),   # no product: the line's totals
    ("total_count", {"event_type": "alarm", "line_id": PRIMARY_LINE_ID}, 42.0),
    ("class_index", PRODUCT, 3.0),                                                    # Bottle, can, cap missing
    ("class_index", {**PRODUCT, "detected_classes": ["BOTTLE "]}, 1.0),
    ("class_index", {**PRODUCT, "detected_classes": ["label"]}, 0.0),                 # on the other camera's list
    ("class_index", {**PRODUCT, "camera_id": "vis-2", "detected_classes": ["label"]}, 1.0),
    ("class_index", {**PRODUCT, "detected_classes": []}, 0.0),
    ("reject_reason_code", PRODUCT, 3.0),
    ("reject_reason_code", {**PRODUCT, "reject_reason": "vision_class"}, 1.0),
    ("reject_reason_code", {**PRODUCT, "reject_reason": "code_not_in_list"}, 2.0),
    ("reject_reason_code", {**PRODUCT, "reject_reason": "no_code"}, 4.0),
    ("reject_reason_code", {**PRODUCT, "reject_reason": "station_no_result"}, 5.0),
    ("reject_reason_code", {**PRODUCT, "reject_reason": "something new"}, 9.0),
    ("reject_reason_code", {**PRODUCT, "result": "good", "reject_reason": None}, 0.0),
    ("batch", {**PRODUCT, "batch": " 2026104 "}, 2026104.0),
])
def test_each_value_source(one_line, source, event, value):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    assert PLCDispatcherService.write_value(_write_card(source=source), event) == value


@pytest.mark.parametrize("event, value", [
    ({**PRODUCT, "camera_id": "free-cam"}, 3.0),                                     # a free camera feeding the line
    ({key: v for key, v in PRODUCT.items() if key != "camera_id"}, 3.0),
    ({**PRODUCT, "camera_id": "free-cam", "detected_classes": ["label"]}, 0.0),
])
def test_class_index_of_a_camera_not_on_the_line_uses_the_counting_camera(one_line, event, value):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    assert PLCDispatcherService.write_value(_write_card(source="class_index"), event) == value


def test_class_index_is_0_when_the_line_has_no_counting_camera(one_line):
    import app.services.line_service as line_module
    from app.services.plc_dispatcher_service import PLCDispatcherService
    line_module.line_manager.runtime.cameras = []
    for event in ({**PRODUCT, "camera_id": "free-cam"}, {key: v for key, v in PRODUCT.items() if key != "camera_id"}):
        assert PLCDispatcherService.write_value(_write_card(source="class_index"), event) == 0.0


@pytest.mark.parametrize("source, event, word", [
    ("result_code", {"event_type": "line_state", "line_id": PRIMARY_LINE_ID}, "no product result"),
    ("batch", PRODUCT, "no batch number"),
    ("batch", {**PRODUCT, "batch": "B-17"}, "'B-17' is not a number"),
    ("batch", {**PRODUCT, "batch": "nan"}, "is not a number"),
])
def test_a_value_the_event_does_not_have_is_refused(one_line, source, event, word):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    with pytest.raises(ValueError, match=word):
        PLCDispatcherService.write_value(_write_card(source=source), event)


def test_write_values_and_strobes_are_checked_when_saved():
    from app.services.plc_failsafe_service import validate_write_value
    validate_write_value({"id": "old", "operation": "pulse", "ack_mode": "wait_ack"})       # an old card is fine
    validate_write_value(_write_card(source="class_index", strobe_address="M0.1", strobe_pulse_ms=250))
    for bad, word in (({"value_source": "speed"}, "value_source"),
                      ({"operation": "pulse", "value_source": "good_count"}, "only a Write card"),
                      ({"write_value": "seven"}, "must be a number"),
                      ({"strobe_address": "M" * 129}, "strobe address"),
                      ({"strobe_address": "M0.1", "strobe_pulse_ms": 5}, "10 to 10000"),
                      ({"strobe_pulse_ms": 20000}, "10 to 10000"),
                      ({"strobe_pulse_ms": 12.5}, "10 to 10000"),
                      ({"strobe_pulse_ms": True}, "10 to 10000")):
        with pytest.raises(ValueError, match=word):
            validate_write_value({**_write_card(), **bad})


def test_the_strobe_pulses_after_the_write_inside_the_endpoint_lock(slow_plc, one_line):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    PLCDispatcherService.set_cards([
        _write_card("a", "result_code", target_address="40001", strobe_address="M0.1", strobe_pulse_ms=50),
        _write_card("b", "good_count", target_address="40002", strobe_address="M0.2"),
    ])

    async def run():
        await PLCDispatcherService.evaluate(PRODUCT)
        await asyncio.gather(*list(PLCDispatcherService._dispatch_tasks))

    _run(run())
    ops = slow_plc.ops
    assert sorted(ops) == sorted([("WRITE", "40001", 2.0), ("PULSE", "M0.1", 0), ("WRITE", "40002", 12.0), ("PULSE", "M0.2", 0)])
    # Each card's data and its strobe go out together: nothing comes between them.
    first = ops.index(("WRITE", "40001", 2.0))
    second = ops.index(("WRITE", "40002", 12.0))
    assert ops[first + 1] == ("PULSE", "M0.1", 0) and ops[second + 1] == ("PULSE", "M0.2", 0)
    status = {s["card_id"]: s for s in PLCDispatcherService.get_status()}
    assert status["a"]["status"] == "sent" and "strobe M0.1 pulsed 50 ms" in status["a"]["last_result"]["message"]
    assert status["a"]["last_result"]["value"] == 2.0


def test_a_failed_strobe_fails_the_card_and_is_not_retried(slow_plc, one_line):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    slow_plc.failing.add("M9.9")
    PLCDispatcherService.set_cards([_write_card("s", strobe_address="M9.9", on_failure="retry", retry_attempts=2, retry_delay_ms=0)])

    async def run():
        await PLCDispatcherService.evaluate(PRODUCT)
        await asyncio.gather(*list(PLCDispatcherService._dispatch_tasks))

    try:
        _run(run())
        assert slow_plc.ops == [("WRITE", "40001", 7.0)]       # written once, not again
        (status,) = PLCDispatcherService.get_status(card_id="s")
        assert status["status"] == "failed" and "the strobe M9.9 failed" in status["last_result"]["message"]
        assert alarm_manager.get("plc.action_failed", "plc_card:s") is not None
    finally:
        alarm_manager.clear_source("plc_card:s")


def test_a_value_the_event_lacks_writes_nothing_and_says_why(slow_plc, one_line):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    PLCDispatcherService.set_cards([_write_card("b", "batch", trigger="cross_line")])

    async def run():
        await PLCDispatcherService.evaluate(PRODUCT)
        await asyncio.gather(*list(PLCDispatcherService._dispatch_tasks))

    try:
        _run(run())
        assert slow_plc.ops == []
        (status,) = PLCDispatcherService.get_status(card_id="b")
        assert status["status"] == "failed" and status["last_result"]["message"] == "Nothing was written: the line has no batch number to write"
    finally:
        alarm_manager.clear_source("plc_card:b")


def test_the_test_button_writes_example_values(slow_plc, one_line):
    from app.services.plc_dispatcher_service import PLCDispatcherService

    async def run():
        for source in ("result_code", "good_count", "reject_count", "total_count", "class_index", "reject_reason_code"):
            result = await PLCDispatcherService.dispatch_manual(_write_card(f"t-{source}", source))
            assert result["success"], result

    _run(run())
    assert [value for _, _, value in slow_plc.ops] == [2.0, 1.0, 1.0, 1.0, 1.0, 1.0]


def test_an_old_wait_for_ack_card_reports_sent(slow_plc):
    from app.services.plc_dispatcher_service import PLCDispatcherService
    card = {"id": "ack", "name": "ack", "enabled": True, "plc_endpoint_id": "ep1", "operation": "pulse",
            "target_address": "Q0.0", "ack_mode": "wait_ack"}
    result = _run(PLCDispatcherService.dispatch_manual(card))
    assert result["success"] and result["status"] == "sent"


def test_plc_cards_through_the_api(client, admin_headers):
    api = "/api/v1/plc/actions"
    card = _write_card("api-w", "good_count", strobe_address="M0.1", strobe_pulse_ms=80)
    try:
        assert client.post(f"{api}/batch", json=[card], headers=admin_headers).status_code == 200
        for bad in ({"value_source": "speed"}, {"operation": "pulse"}, {"strobe_pulse_ms": 1}):
            refused = client.post(f"{api}/batch", json=[{**card, **bad}], headers=admin_headers)
            assert refused.status_code == 422, refused.text
        # An older dashboard that does not send the new fields keeps them.
        older = {k: v for k, v in card.items() if k not in ("value_source", "strobe_address", "strobe_pulse_ms")}
        assert client.post(f"{api}/batch", json=[{**older, "name": "Renamed"}], headers=admin_headers).status_code == 200
        (saved,) = client.get(api, headers=admin_headers).json()
        assert (saved["name"], saved["value_source"], saved["strobe_address"], saved["strobe_pulse_ms"]) == ("Renamed", "good_count", "M0.1", 80)
        # The Test button refuses a card that cannot be written.
        refused = client.post(f"{api}/nope/test", json={"confirm": True, "card": {**card, "id": "nope", "value_source": "speed"}},
                              headers=admin_headers)
        assert refused.status_code == 422 and "value_source" in refused.json()["detail"]
    finally:
        client.post(f"{api}/batch", json=[], headers=admin_headers)
