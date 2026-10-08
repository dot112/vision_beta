# Sparkplug B Edge Node and Bundled Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each production line is published as a Sparkplug B device that Ignition reads by itself, with rebirth, optional start/stop/reset commands, and an optional Mosquitto service in Compose.

**Architecture:** Sparkplug is a setting of an MQTT channel. `SparkplugService` runs one `SparkplugNode` per enabled channel on its own broker connection (the existing `MQTTClient`), polls every line's metrics on an interval and publishes births, deaths and changed values. Payloads are encoded by a pure codec module on the `protobuf` package; a line's metric list is built by a pure module.

**Tech Stack:** Python 3.14, FastAPI, paho-mqtt 2.1, protobuf (runtime descriptors, no `protoc`), pytest, Mosquitto 2, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-06-sparkplug-b-design.md`

## Global Constraints

- Topics: `spBv1.0/<group>/<type>/<node>[/<device>]`; host state on `spBv1.0/STATE/<host id>` (JSON) and `STATE/<host id>` (`ONLINE` / `OFFLINE`).
- Fixed by the specification, never taken from the channel: clean session; NDEATH will QoS 1, not retained; every other publish QoS 0, not retained; NCMD, DCMD and STATE subscriptions QoS 1.
- Sequence number 0 to 255 across all of a node's messages, NBIRTH is 0. `bdSeq` 0 to 255, up by one on every connect, kept in `data/sparkplug_bdseq.json`.
- Every metric carries name, datatype and value in every message. No aliases, templates or datasets.
- Group, node and host IDs: 1 to 128 characters, none of `/ + #`. `sparkplug_interval_ms`: 100 to 60000, default 1000.
- Sparkplug client ID: `<channel client id>-spb`.
- No generated protobuf file and no `protoc` in the repo. `protobuf>=4.25` is named in `requirements.txt` beside `paho-mqtt`.
- Commands act only when `sparkplug_allow_commands` is on; the rebirth request always acts.
- Nothing on the hot path may block or raise: the product-event hook is one dictionary write inside `try`.
- Run tests with `.venv/Scripts/python -m pytest <files> -q -p no:cacheprovider`. Baseline on this Windows machine: 8 known failures (`test_error_alarms::test_dispatch_to_unreachable_plc_raises_both_alarms`, six `TestModbusTCPDriver` tests, `TestDispatcherHardening::test_bad_address_is_not_retried`) and two timing tests in `test_camera_roi_and_qr.py`.
- No commits: `main` carries the owner's uncommitted work. Leave every change uncommitted unless the owner asks.
- Never start the server from the repo folder (its `data/` is real). Live runs use `backup/scratch-tools/launcher.py`. The owner's own broker on port 1883 must not be stopped.

## Review Focus

1. Two lines whose names give the same device ID, or a line named only with `/ + #` or spaces: each line must still be its own device with a usable ID. (Task 3)
2. Two cameras with the same name, or a class or camera name containing `/`: a birth must never list one metric name twice. (Task 3)
3. The broker is down when the server starts and comes up later: startup is not delayed, and the births go out by themselves once it is up. (Task 5)
4. A host writes the wrong thing: bytes that are not a Sparkplug payload, a String `"true"` to `Line/Running`, a command for a device that no longer exists. The node ignores it and keeps publishing. (Tasks 5 and 7)
5. `data/sparkplug_bdseq.json` is missing, empty or not JSON: the node starts from 0 and rewrites the file; it never fails to start. (Task 5)

---

### Task 1: Payload codec

**Files:**
- Create: `app/hardware/mqtt/sparkplug_codec.py`
- Modify: `requirements.txt` (add `protobuf>=4.25` beside `paho-mqtt`; `requirements-runtime.txt` only includes this file)
- Test: `tests/test_sparkplug_codec.py`

**Interfaces:**
- Produces:
  - `NAMESPACE = "spBv1.0"`
  - `class DataType(IntEnum)`: `Int8=1, Int16=2, Int32=3, Int64=4, UInt8=5, UInt16=6, UInt32=7, UInt64=8, Float=9, Double=10, Boolean=11, String=12, DateTime=13, Text=14, UUID=15`
  - `@dataclass(frozen=True) class Metric: name: str; datatype: int; value: Any; timestamp: Optional[int] = None`
  - `@dataclass(frozen=True) class Payload: metrics: Tuple[Metric, ...]; timestamp: Optional[int]; seq: Optional[int]`
  - `encode_payload(metrics: Sequence[Metric], *, timestamp: Optional[int] = None, seq: Optional[int] = None) -> bytes`
  - `decode_payload(data: bytes) -> Payload` (raises `ValueError` for bytes that are not a payload)
  - `topic(group: str, kind: str, node: str, device: Optional[str] = None) -> str`
  - `@dataclass(frozen=True) class Topic: group: str; kind: str; node: str; device: Optional[str]` and `parse_topic(text: str) -> Optional[Topic]`
  - `id_problem(value: Any) -> Optional[str]` (why a group, node or host ID is not valid)
  - `now_ms() -> int`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_payload_is_the_bytes_the_specification_gives():
    # timestamp=1, one Boolean metric "a" = true, seq=0
    data = encode_payload([Metric("a", DataType.Boolean, True)], timestamp=1, seq=0)
    assert data == bytes.fromhex("080112070a0161200b70011800")

@pytest.mark.parametrize("datatype, value", [
    (DataType.Int8, -128), (DataType.Int16, -2), (DataType.Int32, -2_000_000_000), (DataType.Int64, -5),
    (DataType.UInt8, 255), (DataType.UInt32, 4_000_000_000), (DataType.UInt64, 2**64 - 1),
    (DataType.Float, 1.5), (DataType.Double, 97.93), (DataType.Boolean, False),
    (DataType.String, "PASSED"), (DataType.Text, ""), (DataType.DateTime, 1_700_000_000_000),
])
def test_every_scalar_type_round_trips(datatype, value):
    back = decode_payload(encode_payload([Metric("m", datatype, value, timestamp=5)], timestamp=9, seq=255))
    assert (back.timestamp, back.seq) == (9, 255)
    assert back.metrics == (Metric("m", datatype, value, timestamp=5),)

def test_negative_small_integers_decode_from_either_width():
    # Java hosts sign-extend an Int8 to 32 bits; C clients send 8 bits.
    wide = bytes.fromhex("120b0a016d200150ffffffff0f")   # name "m", Int8, int_value 0xFFFFFFFF
    narrow = bytes.fromhex("12080a016d200150ff01")       # int_value 0xFF
    assert decode_payload(wide).metrics[0].value == -1 == decode_payload(narrow).metrics[0].value

def test_a_payload_without_seq_or_timestamp_has_none():   # an NDEATH has no seq
    back = decode_payload(encode_payload([Metric("bdSeq", DataType.Int64, 7)]))
    assert (back.seq, back.timestamp) == (None, None)

def test_bytes_that_are_not_a_payload_are_refused():
    for bad in (b"\xff\xff\xff", b"ONLINE", b'{"online": true}'):
        with pytest.raises(ValueError):
            decode_payload(bad)

def test_topics():
    assert topic("PlantA", "DBIRTH", "Vision1", "Line 1") == "spBv1.0/PlantA/DBIRTH/Vision1/Line 1"
    assert parse_topic("spBv1.0/PlantA/NCMD/Vision1") == Topic("PlantA", "NCMD", "Vision1", None)
    assert parse_topic("spBv1.0/PlantA/DCMD/Vision1/Line 1") == Topic("PlantA", "DCMD", "Vision1", "Line 1")
    assert parse_topic("spBv1.0/STATE/ignition") is None and parse_topic("other/topic") is None
    assert id_problem("PlantA") is None
    for bad in ("", "  ", "a/b", "a+b", "a#b", "x" * 129, None):
        assert id_problem(bad)
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/Scripts/python -m pytest tests/test_sparkplug_codec.py -q -p no:cacheprovider`
Expected: collection error, `No module named 'app.hardware.mqtt.sparkplug_codec'`.

- [ ] **Step 3: Implement the module**

Build the two message types at import time in a private `descriptor_pool.DescriptorPool()` from a `descriptor_pb2.FileDescriptorProto` (syntax `proto2`, package `org.eclipse.tahu.protobuf`, every field `optional` except `metrics`, which is `repeated`), and get the classes with `message_factory.GetMessageClass`. Field numbers, from Eclipse Tahu's `sparkplug_b.proto`:

| Message | Field | Number | Type |
|---|---|---|---|
| Payload | timestamp | 1 | uint64 |
| Payload | metrics | 2 | Metric |
| Payload | seq | 3 | uint64 |
| Metric | name | 1 | string |
| Metric | alias | 2 | uint64 |
| Metric | timestamp | 3 | uint64 |
| Metric | datatype | 4 | uint32 |
| Metric | is_null | 7 | bool |
| Metric | int_value | 10 | uint32 |
| Metric | long_value | 11 | uint64 |
| Metric | float_value | 12 | float |
| Metric | double_value | 13 | double |
| Metric | boolean_value | 14 | bool |
| Metric | string_value | 15 | string |

Value field by datatype: `Int8/16/32`, `UInt8/16/32` use `int_value`; `Int64`, `UInt64`, `DateTime` use `long_value`; `Float` `float_value`; `Double` `double_value`; `Boolean` `boolean_value`; `String`, `Text`, `UUID` `string_value`. Negative integers are written as two's complement of the field's width (32 or 64 bits). On decode, mask to the datatype's own width (8, 16, 32 or 64 bits) and then apply the sign. A metric with `is_null` decodes to value `None`. `decode_payload` raises `ValueError` when parsing fails, when a metric has no name, or when the bytes parse to a payload with no metrics, no timestamp and no seq.

- [ ] **Step 4: Run the tests and see them pass**

Run: the command of step 2. Expected: all pass.

---

### Task 2: MQTT client: hook before connect, raw messages, subscription QoS

**Files:**
- Modify: `app/hardware/mqtt/client.py`
- Create: `tests/mqtt_broker.py` (the `_Broker` of `tests/test_mqtt_channels.py`, moved and extended)
- Modify: `tests/test_mqtt_channels.py` (import `Broker` from `tests.mqtt_broker`; no behaviour change)
- Test: `tests/test_mqtt_client.py`

**Interfaces:**
- Produces, on `MQTTClient`:
  - `before_connect: Optional[Callable[[MQTTClient], None]] = None`: called before every CONNECT, the automatic reconnects included. It may set `will_topic`, `will_payload`, `will_qos`, `will_retain`, `close_topic`, `close_payload`, `close_qos`; the will sent is the one set when it returns.
  - `will_payload` and `close_payload` may be `bytes`.
  - `async subscribe_raw(topic: str, callback: Callable[[str, bytes], None], qos: int = 0) -> bool`: the callback gets the undecoded payload, on paho's thread.
  - `subscribe(topic, callback=None, qos=0)` keeps its QoS for the resubscribe after a reconnect (today it resubscribes at QoS 0).
- Produces, in `tests/mqtt_broker.py`: `class Broker(username=None, password=None, port=0)` with `port`, `connects` (each `{"level", "client_id", "ok", "will": {"topic", "payload": bytes, "qos", "retain"} or None}`), `messages` (each `{"topic", "raw": bytes, "payload": parsed JSON or None, "qos", "retain"}`), `subscriptions` (list of `(client_id, topic filter, qos)`), `wait(count, timeout=5.0)`, `publish(topic: str, payload: bytes)` (delivers to every subscriber whose filter matches, `#` and `+` honoured), `drop_clients()` (closes every client socket without a DISCONNECT), `close()`.

- [ ] **Step 1: Move the broker, extend it, keep the existing tests green**

Run: `.venv/Scripts/python -m pytest tests/test_mqtt_channels.py -q -p no:cacheprovider`. Expected: every test passes, as before the move.

- [ ] **Step 2: Write the failing tests** in `tests/test_mqtt_client.py`

```python
def test_the_will_is_set_again_before_every_connect():
    broker = Broker(); calls = []
    def before(c):
        calls.append(1); c.will_topic = "t/death"; c.will_payload = bytes([len(calls)]); c.will_qos = 1
    client = MQTTClient(host="127.0.0.1", port=broker.port, client_id="c1", keepalive=5); client.before_connect = before
    # connect, broker.drop_clients(), wait for the automatic reconnect, disconnect
    assert [c["will"]["payload"] for c in broker.connects] == [b"\x01", b"\x02"]
    assert broker.connects[0]["will"] == {"topic": "t/death", "payload": b"\x01", "qos": 1, "retain": False}

def test_a_raw_subscription_gets_bytes_and_survives_a_reconnect_at_its_qos():
    # subscribe_raw("cmd/#", got.append-style callback, qos=1); broker.publish("cmd/x", b"\xff\x00")
    assert got == [("cmd/x", b"\xff\x00")]
    assert ("c1", "cmd/#", 1) in broker.subscriptions
    # after broker.drop_clients() and the reconnect, the same subscription is there twice

def test_a_binary_message_does_not_break_json_subscribers():
    # one subscribe() handler on "j/#", one subscribe_raw() on "b/#"; publish binary to "b/x", then JSON to "j/x"
    assert json_got == [("j/x", {"a": 1})]

def test_bytes_close_message_goes_out_before_the_disconnect():
    # close_topic "t/death", close_payload b"\x09", close_qos 1; await client.disconnect()
    assert broker.wait(1)[-1]["raw"] == b"\x09"
```

- [ ] **Step 3: Run them and see them fail**

Run: `.venv/Scripts/python -m pytest tests/test_mqtt_client.py -q -p no:cacheprovider`
Expected: failures on `before_connect` and `subscribe_raw`.

- [ ] **Step 4: Implement in `client.py`**

Set paho's `on_pre_connect` to a method that calls `before_connect` (errors logged, not raised) and then `client.will_set(...)` from the current will fields; remove the one-time `will_set` from `_build_client`. Keep QoS per subscribed topic in a dict and use it in `_on_connect`. In `_on_message`, call raw handlers with `msg.payload`; decode and log only for topics that have a JSON handler, with `errors="replace"`.

- [ ] **Step 5: Run the tests and see them pass**

Run: `.venv/Scripts/python -m pytest tests/test_mqtt_client.py tests/test_mqtt_channels.py tests/test_resilience.py -q -p no:cacheprovider`. Expected: all pass (one skip in `test_resilience.py` on Windows).

---

### Task 3: A line's metrics

**Files:**
- Create: `app/services/sparkplug_metrics.py`
- Test: `tests/test_sparkplug_metrics.py`

**Interfaces:**
- Consumes: `Metric`, `DataType` (Task 1).
- Produces:
  - `REBIRTH = "Node Control/Rebirth"`, `RUNNING = "Line/Running"`, `RESET = "Commands/Reset Counters"`, `BD_SEQ = "bdSeq"`
  - `device_ids(lines: Sequence[Tuple[str, str]]) -> Dict[str, str]`: `(line id, line name)` pairs to `{line id: device id}`
  - `node_metrics(bd_seq: int, version: str) -> List[Metric]`
  - `line_metrics(view: Dict[str, Any]) -> List[Metric]`
  - `last_from_event(payload: Dict[str, Any]) -> Dict[str, Any]`: the `Last Product/...` or `Last Code/...` values a product or code message sets, keyed by metric name; `{}` for any other message
- The `view` a caller passes: `{"name": str, "enabled": bool, "total_inspected": int, "good_count": int, "rejected_count": int, "products_per_minute": float, "yield_percentage": float, "defect_ppm": float, "counts": {class: int}, "classes": [str], "cameras": [{"camera_id": str, "name": str or None, "connected": bool}], "alarms": [str], "last": {metric name: value}}`.

- [ ] **Step 1: Write the failing tests**

```python
VIEW = {"name": "Line 1", "enabled": True, "total_inspected": 145, "good_count": 142, "rejected_count": 3,
        "products_per_minute": 45.204, "yield_percentage": 97.931, "defect_ppm": 20689.656,
        "counts": {"bottle": 142, "defect": 3}, "classes": ["bottle", "can", "defect"],
        "cameras": [{"camera_id": "c1", "name": "Top", "connected": True}], "alarms": ["camera.fps_low", "camera.disconnected"],
        "last": {"Last Product/Result": "REJECTED"}}

def test_a_lines_metrics():
    got = {m.name: (m.datatype, m.value) for m in line_metrics(VIEW)}
    assert got["Line/Running"] == (DataType.Boolean, True) and got["Line/Name"] == (DataType.String, "Line 1")
    assert got["Counts/Inspected"] == (DataType.Int64, 145) and got["Counts/Rejected"] == (DataType.Int64, 3)
    assert got["Counts/Class/can"] == (DataType.Int64, 0)            # listed at birth before it is counted
    assert got["Rate/Products Per Minute"] == (DataType.Double, 45.2)
    assert got["Quality/Yield Percent"] == (DataType.Double, 97.93) and got["Quality/Defect PPM"] == (DataType.Double, 20689.66)
    assert got["Last Product/Result"] == (DataType.String, "REJECTED")
    assert got["Last Product/Class"] == (DataType.String, "") and got["Last Product/Confidence"] == (DataType.Double, 0.0)
    assert got["Last Code/Text"] == (DataType.String, "") and got["Last Code/Status"] == (DataType.String, "")
    assert got["Cameras/Top/Connected"] == (DataType.Boolean, True)
    assert got["Alarms/Active Count"] == (DataType.Int32, 2)
    assert got["Alarms/Active"] == (DataType.String, "camera.disconnected,camera.fps_low")
    assert got["Commands/Reset Counters"] == (DataType.Boolean, False)

def test_a_birth_never_lists_a_metric_twice():      # Review Focus 2
    view = {**VIEW, "counts": {"a/b": 1, "": 2}, "classes": ["a/b", "A/B", "a_b"],
            "cameras": [{"camera_id": "c1", "name": "Top", "connected": True}, {"camera_id": "c2", "name": "Top", "connected": False},
                        {"camera_id": "c3", "name": None, "connected": False}, {"camera_id": "c4", "name": "a/b+#", "connected": True}]}
    names = [m.name for m in line_metrics(view)]
    assert len(names) == len(set(names))
    assert {"Cameras/Top/Connected", "Cameras/Top (c2)/Connected", "Cameras/c3/Connected", "Cameras/a_b__/Connected"} <= set(names)
    assert {"Counts/Class/a_b", "Counts/Class/a_b (2)", "Counts/Class/A_B", "Counts/Class/unnamed"} <= set(names)

def test_device_ids():                              # Review Focus 1
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
        "Last Product/Reject Reason": "vision_class", "Last Product/Code": "SKU-1", "Last Product/Confidence": 0.94}
    assert last_from_event({"event": "QR_CODE_READ", "code": "X", "qr_status": "unknown"}) == {"Last Code/Text": "X", "Last Code/Status": "unknown"}
    assert last_from_event({"event": "QR_CODE_NO_READ", "code": None, "qr_status": "no_read"}) == {"Last Code/Text": "", "Last Code/Status": "no_read"}
    assert last_from_event({"event": "LINE_STARTED"}) == {} and last_from_event({}) == {}
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/Scripts/python -m pytest tests/test_sparkplug_metrics.py -q -p no:cacheprovider`. Expected: module not found.

- [ ] **Step 3: Implement the module**

Order of `line_metrics`: `Line/*`, `Counts/*` (the three totals, then classes sorted), `Rate/*`, `Quality/*`, `Last Product/*`, `Last Code/*`, `Cameras/*`, `Alarms/*`, `Commands/*`. In a class or camera name, `/`, `+` and `#` become `_`; an empty class name becomes `unnamed`; a camera without a name uses its camera id; a second camera with a name already used gets ` (<camera id>)` appended; a class name already used gets ` (2)`, ` (3)` and so on. A device ID is the line's name stripped, with the same three characters replaced, cut to 128 characters; an empty one is the line id; one already used gets ` (<line id>)` appended. `None` values from an event become `""` (strings) or `0.0` (confidence). Doubles that are not finite become `0.0`.

- [ ] **Step 4: Run the tests and see them pass**

---

### Task 4: Channel settings

**Files:**
- Modify: `app/services/settings_persistence_service.py` (the `proto == "mqtt"` branch of `add_or_update_endpoint`)
- Test: `tests/test_sparkplug_api.py`

**Interfaces:**
- Consumes: `id_problem` (Task 1).
- Produces: an MQTT channel record always holds `sparkplug_enabled: bool`, `sparkplug_group_id: str`, `sparkplug_node_id: str`, `sparkplug_host_id: str`, `sparkplug_allow_commands: bool`, `sparkplug_interval_ms: int`. A field left out of a save keeps its saved value.

- [ ] **Step 1: Write the failing test** (fixtures `client`, `admin_headers` from `tests/conftest.py`; delete the channels in `finally`)

```python
def test_sparkplug_settings_of_a_channel(client, admin_headers):
    base = {"name": "Spb test broker", "protocol": "mqtt", "host": "127.0.0.1", "port": 1, "enabled": False}
    plain = post(base)                                   # helper: POST /api/v1/system/endpoints, returns the response
    assert plain.status_code == 200
    assert {k: plain.json()[k] for k in plain.json() if k.startswith("sparkplug_")} == {
        "sparkplug_enabled": False, "sparkplug_group_id": "", "sparkplug_node_id": "", "sparkplug_host_id": "",
        "sparkplug_allow_commands": False, "sparkplug_interval_ms": 1000}
    on = put(plain.json()["id"], {"protocol": "mqtt", "sparkplug_enabled": True, "sparkplug_group_id": " PlantA ",
                                  "sparkplug_node_id": "Vision1", "sparkplug_allow_commands": True, "sparkplug_interval_ms": 250})
    assert on.status_code == 200 and on.json()["sparkplug_group_id"] == "PlantA" and on.json()["sparkplug_interval_ms"] == 250
    kept = put(plain.json()["id"], {"protocol": "mqtt", "name": "Renamed"})          # a save that leaves the fields out
    assert kept.json()["sparkplug_enabled"] is True and kept.json()["sparkplug_allow_commands"] is True
    for bad, word in (({"sparkplug_group_id": ""}, "group"), ({"sparkplug_node_id": "a/b"}, "node"),
                      ({"sparkplug_host_id": "h#"}, "host"), ({"sparkplug_interval_ms": 50}, "interval"),
                      ({"sparkplug_enabled": "yes"}, "Sparkplug")):
        res = put(plain.json()["id"], {"protocol": "mqtt", **bad})
        assert res.status_code == 422 and word in res.json()["detail"].lower(), res.text
    # The same node on the same broker twice is one edge node connected twice.
    twin = post({**base, "name": "Spb twin", "sparkplug_enabled": True, "sparkplug_group_id": "PlantA", "sparkplug_node_id": "Vision1"})
    assert twin.status_code == 422 and "already" in twin.json()["detail"]
    other = post({**base, "name": "Spb other", "sparkplug_enabled": True, "sparkplug_group_id": "PlantA", "sparkplug_node_id": "Vision2"})
    assert other.status_code == 200
```

- [ ] **Step 2: Run it and see it fail** (`KeyError`/missing fields)

- [ ] **Step 3: Implement.** Group and node IDs are checked with `id_problem` only when `sparkplug_enabled` is true; the host ID whenever it is not empty. The duplicate check compares `host`, `port`, group ID and node ID against every other MQTT channel that has Sparkplug enabled.

- [ ] **Step 4: Run the test and see it pass.** Also run `tests/test_mqtt_channels.py tests/test_send_cards.py`: unchanged.

---

### Task 5: The edge node

**Files:**
- Create: `app/services/sparkplug_service.py` (this task: `BdSeqStore`, `SparkplugNode`)
- Test: `tests/test_sparkplug_node.py` (uses `tests.mqtt_broker.Broker`)

**Interfaces:**
- Consumes: Task 1 codec; `MQTTClient.before_connect`, `subscribe_raw`, bytes will (Task 2); `node_metrics`, `REBIRTH` (Task 3); `client_for_channel(endpoint, client_id=...)` from `app/services/mqtt_service.py`.
- Produces:
  - `class BdSeqStore(path: Path)` with `next(channel_id: str) -> int`
  - `class SparkplugNode(channel: Dict[str, Any], store: BdSeqStore, on_command: Optional[Callable[["SparkplugNode", str, Metric], None]] = None)`
    - `channel_id`, `group`, `node`, `host_id`, `interval` (seconds, float), `allow_commands`
    - `async start() -> None`: returns at once; connecting continues in the background and never raises
    - `async stop() -> None`: NDEATH, then disconnect
    - `publish_lines(lines: Dict[str, List[Metric]]) -> None`: `{device id: every metric of that line}`; sends what changed since the last call (see step 3)
    - `resend(device: str, names: Sequence[str]) -> None`: a DDATA with the last sent values of these metrics
    - `rebirth() -> None`
    - `status() -> Dict[str, Any]`: `{"connected": bool, "host_online": Optional[bool], "devices": int, "last_birth_at": Optional[float], "last_error": str}`

- [ ] **Step 1: Write the failing tests.** Helpers in the test file: `channel(broker, **settings)` returning a channel record with `sparkplug_enabled`, group `G`, node `N`, interval 100; `decoded(broker)` returning `[(Topic, Payload)]` of the `spBv1.0` messages in order; `LINE = {"Line 1": [Metric("Line/Running", DataType.Boolean, True), Metric("Counts/Good", DataType.Int64, 1)]}`.

```python
def test_connect_then_births(node_on):                       # fixture: started node + broker
    connect = broker.connects[0]
    assert connect["client_id"] == "vision_m1-spb"
    will = decode_payload(connect["will"]["payload"])
    assert connect["will"]["topic"] == "spBv1.0/G/NDEATH/N" and connect["will"]["qos"] == 1 and will.seq is None
    assert {("vision_m1-spb", "spBv1.0/G/NCMD/N", 1), ("vision_m1-spb", "spBv1.0/G/DCMD/N/#", 1)} <= set(broker.subscriptions)
    node.publish_lines(LINE)
    (t1, nbirth), (t2, dbirth) = decoded(broker)[:2]
    assert (t1.kind, nbirth.seq, t2.kind, t2.device, dbirth.seq) == ("NBIRTH", 0, "DBIRTH", "Line 1", 1)
    assert nbirth.metrics[0] == Metric("bdSeq", DataType.Int64, will.metrics[0].value, timestamp=ANY)   # same bdSeq as the will
    assert [m.name for m in dbirth.metrics] == ["Line/Running", "Counts/Good"] and all(m.timestamp for m in dbirth.metrics)

def test_only_changed_metrics_are_sent_and_seq_wraps():
    # publish_lines(LINE) then 300 calls with Counts/Good = 2..301
    assert all(t.kind == "DDATA" and [m.name for m in p.metrics] == ["Counts/Good"] for t, p in data)
    assert [p.seq for _, p in decoded(broker)] == [n % 256 for n in range(302)]
    # a call with nothing changed sends nothing

def test_lines_come_go_and_gain_metrics():
    # LINE; then LINE + "Line 2"; then "Line 1" gains Metric("Counts/Class/can", ...); then without "Line 2"
    assert kinds == ["NBIRTH", "DBIRTH", "DBIRTH", "DBIRTH", "DDEATH"] and devices == [None, "Line 1", "Line 2", "Line 1", "Line 2"]

def test_rebirth_request():
    broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.Boolean, True)], timestamp=1))
    # NBIRTH (seq 0, same bdSeq) and the DBIRTH again
    broker.publish("spBv1.0/G/NCMD/N", b"\xff\xff")                                   # Review Focus 4: not a payload
    broker.publish("spBv1.0/G/NCMD/N", encode_payload([Metric("Node Control/Rebirth", DataType.String, "true")], timestamp=1))
    # neither causes a birth; a later publish_lines() change still goes out as DDATA

def test_device_commands_reach_the_callback():
    broker.publish("spBv1.0/G/DCMD/N/Line 1", encode_payload([Metric("Line/Running", DataType.Boolean, False)], timestamp=1))
    assert got == [(node, "Line 1", Metric("Line/Running", DataType.Boolean, False))]

def test_bdseq_goes_up_on_reconnect_and_survives_a_restart(tmp_path):
    # start, broker.drop_clients(), wait for births again; stop; a new node with a new BdSeqStore on the same file
    assert [decode_payload(c["will"]["payload"]).metrics[0].value for c in broker.connects] == [0, 1, 2]
    # after the reconnect: NBIRTH seq 0 and every DBIRTH again, without a publish_lines() call

def test_a_missing_or_broken_bdseq_file_starts_at_zero(tmp_path):                     # Review Focus 5
    for content in (None, "", "not json", "[1, 2]", '{"m1": "x"}'):
        # write content (or no file); BdSeqStore(path).next("m1") == 0, and the file is valid JSON afterwards
    assert BdSeqStore(path).next("m1") == 1 and BdSeqStore(path).next("m2") == 0
    # 255 is followed by 0

def test_primary_host(broker):
    # host_id "ign": subscribed to "spBv1.0/STATE/ign" and "STATE/ign" at QoS 1; publish_lines(LINE) sends nothing yet
    broker.publish("spBv1.0/STATE/ign", b'{"online": true, "timestamp": 2000}')        # -> NBIRTH, DBIRTH
    broker.publish("spBv1.0/STATE/ign", b'{"online": false, "timestamp": 1000}')       # older than the online one: ignored
    broker.publish("spBv1.0/STATE/ign", b'{"online": false, "timestamp": 3000}')       # -> NDEATH, a new connect (bdSeq + 1), no births
    broker.publish("STATE/ign", b"ONLINE")                                             # Sparkplug 2.2 -> births again
    assert node.status()["host_online"] is True

def test_stop_publishes_ndeath(broker):
    # await node.stop(): last message is spBv1.0/G/NDEATH/N with the bdSeq of this connection, then the client is gone

def test_a_broker_that_is_down_does_not_hold_start_and_is_found_later():               # Review Focus 3
    # port = a free port with nothing listening; t0 = time.monotonic(); await node.start(); assert time.monotonic() - t0 < 1.0
    # node.publish_lines(LINE); status()["connected"] is False and status()["last_error"]
    # Broker(port=port) starts; within 10 s decoded() begins with NBIRTH, DBIRTH for "Line 1"
```

- [ ] **Step 2: Run them and see them fail** (`cannot import name 'SparkplugNode'`)

- [ ] **Step 3: Implement `BdSeqStore` and `SparkplugNode`**

- The client is `client_for_channel(connection settings, client_id=f"{channel client id}-spb")` where the connection settings are the channel without its `birth_*`, `close_*` and `will_*` keys and with `clean_session` true.
- `before_connect`: take `store.next(channel_id)`, build the NDEATH payload (one `bdSeq` metric, timestamp, no seq), set it as will and as close message (QoS 1).
- On connected: reset the sequence number and the record of what was sent. With no host ID, send the births; with one, wait for an online state message.
- Sending the births: NBIRTH (`node_metrics`, seq 0), then a DBIRTH for every device of the last `publish_lines` call.
- `publish_lines`: remember the call's lines. While births have been sent on this connection: unknown device, DBIRTH; device no longer present, DDEATH; a metric name not in its last birth, DBIRTH; otherwise one DDATA with the metrics whose value or datatype changed, none when nothing changed. Otherwise send nothing.
- Every metric in a message carries `timestamp=now_ms()`; so does the payload.
- Host state: JSON on `spBv1.0/STATE/<id>` (ignore an offline whose timestamp is lower than the last online timestamp), text on `STATE/<id>`. Offline while births were sent: disconnect (which publishes the close message, the NDEATH) and connect again, scheduled on the event loop captured in `start()`; never from paho's thread.
- One `threading.RLock` guards the sequence number and the sent record: messages arrive on paho's thread, `publish_lines` runs on the event loop.
- Every handler catches and logs its own errors.

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/Scripts/python -m pytest tests/test_sparkplug_node.py -q -p no:cacheprovider`

---

### Task 6: The service: nodes follow the channels, lines are published

**Files:**
- Modify: `app/services/sparkplug_service.py` (add `SparkplugService`), `app/services/settings_persistence_service.py` (save, delete, channel test), `app/services/counting_service.py` (`CountingService.send`), `app/services/health_service.py` (`check_mqtt`), `main.py` (startup after `_start_saved_connections()`, shutdown before `MQTTService.disconnect()`)
- Test: `tests/test_sparkplug_service.py`

**Interfaces:**
- Consumes: `SparkplugNode`, `BdSeqStore` (Task 5); `device_ids`, `line_metrics`, `last_from_event` (Task 3); `line_manager.all()`, `line_manager.summary(runtime)`, `runtime.counter.counts_by_class`, `alarm_manager.active()`.
- Produces, all class methods of `SparkplugService`:
  - `async start() -> None`: a node for every enabled MQTT channel with `sparkplug_enabled`; starts the publish loop
  - `async stop() -> None`
  - `apply(channel: Dict[str, Any]) -> None`: a channel was saved; starts, replaces or ends its node. Safe without a running loop (then it only records the channel).
  - `drop(channel_id: str) -> None`
  - `note_event(payload: Dict[str, Any]) -> None`
  - `publish_now() -> None`: one pass over every node, whatever its interval
  - `status() -> Dict[str, Dict[str, Any]]`: `{channel id: node.status()}`
  - `device_of(line_id: str) -> Optional[str]` and `line_of(device_id: str) -> Optional[str]`

- [ ] **Step 1: Write the failing tests** (fixture `lines` as in `tests/test_send_cards.py`: throwaway settings and `LineManager`; `Broker`; `monkeypatch` `SparkplugService` state and the store path to `tmp_path`)

```python
def test_a_saved_channel_publishes_every_line_and_follows_changes(lines, broker):
    # two lines "Line 1", "Packing"; apply(channel) inside a running loop; publish_now()
    assert [(t.kind, t.device) for t, _ in decoded(broker)] == [("NBIRTH", None), ("DBIRTH", "Line 1"), ("DBIRTH", "Packing")]
    names = {m.name for m in dbirth_of("Line 1").metrics}
    assert {"Line/Running", "Counts/Inspected", "Last Product/Result", "Alarms/Active Count", "Commands/Reset Counters"} <= names
    # count one rejected product on Line 1 (the `_cross(counter, is_defect=True)` helper), publish_now()
    changed = {m.name: m.value for m in last_ddata("Line 1").metrics}
    assert changed["Counts/Inspected"] == 1 and changed["Counts/Rejected"] == 1 and changed["Last Product/Result"] == "REJECTED"
    assert "Line/Name" not in changed
    # rename "Packing" to "Boxing" (settings + manager.apply_state()), publish_now(): DDEATH "Packing", DBIRTH "Boxing"
    # apply({**channel, "sparkplug_enabled": False}): NDEATH; status() == {}
    # apply({**channel, "enabled": False}) and drop(channel id) end a node the same way

def test_a_changed_channel_gets_a_new_node(lines, broker):
    # apply(channel); apply({**channel, "sparkplug_node_id": "N2"}): NDEATH on .../NDEATH/N, then NBIRTH on .../NBIRTH/N2
    # apply({**channel, "name": "Renamed"}) keeps the node (no new connect)

def test_the_event_hook_never_raises_and_keeps_the_latest(lines):
    SparkplugService.note_event(None); SparkplugService.note_event({"event": "WIRELINE_OBJECT_CROSSED"})   # no line_id
    SparkplugService.note_event({"line_id": "line-1", "event": "QR_CODE_READ", "code": "A", "qr_status": "known"})
    SparkplugService.note_event({"line_id": "line-1", "event": "QR_CODE_READ", "code": "B", "qr_status": "unknown"})
    # the next birth of Line 1 has Last Code/Text == "B"

def test_a_line_that_cannot_be_read_keeps_its_last_values(lines, broker, monkeypatch):
    # after the births, make line_manager.summary raise for "Packing" only; publish_now() twice
    assert not any(t.kind == "DDEATH" for t, _ in decoded(broker))      # it is not taken for deleted
    # a product counted on "Line 1" in the same pass still arrives as DDATA

def test_health_and_the_channel_test_report_the_node(client, admin_headers, broker):
    # a channel saved through the API with Sparkplug on, pointing at the broker
    entry = next(c for c in get("/api/v1/telemetry/health")["components"]["mqtt"]["channels"] if c["id"] == channel_id)
    assert entry["sparkplug"] == {"connected": True, "host_online": None, "devices": ANY, "last_birth_at": ANY, "last_error": ""}
    assert "Sparkplug B" in post(f"/api/v1/system/endpoints/{channel_id}/test").json()["message"]
    # delete the channel: the broker's last spBv1.0 message is the NDEATH
```

- [ ] **Step 2: Run them and see them fail**

- [ ] **Step 3: Implement**

- The loop is one asyncio task that wakes every 100 ms and, for each node whose `interval` has passed, builds `{device id: line_metrics(view)}` for every line and calls `node.publish_lines`. One node's exception is logged and does not stop the others.
- The view of a line is `line_manager.summary(runtime)` plus `counts` (`runtime.counter.counts_by_class`), `classes` (every `expected_classes` and `defect_classes` of the summary's cameras), `alarms` (codes of the active alarms whose `details["line_id"]` is the line) and `last` (what `note_event` kept for the line).
- A line whose view cannot be built in a pass (an exception) is passed with the metric list of the pass before, so it is neither changed nor taken for deleted; the error is logged once per line until a pass succeeds.
- A node is replaced when any of these changed: the connection keys `mqtt_service._CONNECTION_KEYS` use, or a `sparkplug_*` field. `sparkplug_allow_commands` and `sparkplug_interval_ms` are applied to the running node instead.
- `note_event` in `CountingService.send`, after the `dispatch_telemetry` check and before the send cards, inside its own `try`.
- Health: each entry of `components.mqtt.channels` gains `sparkplug` when the channel has a node; a channel with a node but no entry gets one.
- The channel test's success message ends with `; Sparkplug B: <n> line(s) published` or `; Sparkplug B: waiting for host '<id>'` or `; Sparkplug B: not connected (<reason>)`.

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/Scripts/python -m pytest tests/test_sparkplug_service.py tests/test_health.py tests/test_send_cards.py -q -p no:cacheprovider`

---

### Task 7: Commands

**Files:**
- Modify: `app/routes/v1/lines.py` (`start_line`, `stop_line`), `app/routes/v1/counting.py` (`reset_counts`), `app/services/sparkplug_service.py`
- Test: `tests/test_sparkplug_commands.py`

**Interfaces:**
- Consumes: `SparkplugNode(on_command=...)`, `node.resend`, `SparkplugService.line_of` (Tasks 5, 6); `RUNNING`, `RESET` (Task 3).
- Produces:
  - `app/routes/v1/lines.py`: `async def set_line_running(line_id: str, running: bool, actor: Any) -> Dict[str, Any]`: what the two routes did, for any `actor` with `username`, `role`, `clearance_level`; raises `HTTPException(404)` for an unknown line. The routes call it.
  - `app/routes/v1/counting.py`: `def reset_line_counters(runtime: LineRuntime, actor: Any) -> CountingStatsResponse`: resets and writes the audit entry. The route's `reset_all` branch calls it.
  - `app/services/sparkplug_service.py`: `@dataclass(frozen=True) class CommandActor: username: str; role: str = "SPARKPLUG"; clearance_level: int = 2`

- [ ] **Step 1: Write the failing tests** (API `client`, a channel saved with Sparkplug on and `sparkplug_allow_commands` as each case needs, a line created through the API, `Broker`)

```python
# Every test starts with a started line that has counted three products. Helpers in the test file:
# dcmd(name, datatype, value, device=<the line's device>) publishes a DCMD through the broker and waits for the node's answer;
# line() is GET /api/v1/lines/{id}; sparkplug_audit() is the audit entries whose username starts with "sparkplug:".

def test_commands_are_ignored_while_the_switch_is_off(...):
    dcmd("Line/Running", DataType.Boolean, False)
    assert last_ddata(device).metrics == (Metric("Line/Running", DataType.Boolean, True, timestamp=ANY),)   # the real value, sent back
    dcmd("Commands/Reset Counters", DataType.Boolean, True)
    assert line()["enabled"] is True and line()["status"]["total_inspected"] == 3 and sparkplug_audit() == []

def test_start_stop_and_reset_with_the_switch_on(...):
    dcmd("Line/Running", DataType.Boolean, False)
    assert line()["enabled"] is False and metric_now("Line/Running") is False
    dcmd("Line/Running", DataType.Boolean, True);  assert line()["enabled"] is True
    dcmd("Commands/Reset Counters", DataType.Boolean, True)
    assert line()["status"]["total_inspected"] == 0 and metric_now("Commands/Reset Counters") is False
    assert [(a["username"], a["action"]) for a in sparkplug_audit()] == [("sparkplug:Spb cmd broker", "STOP_LINE"), ("sparkplug:Spb cmd broker", "START_LINE"),
                     ("sparkplug:Spb cmd broker", "RESET_COUNTERS")]

def test_what_is_not_a_command_changes_nothing(...):                                  # Review Focus 4
    dcmd("Line/Running", DataType.String, "true"); dcmd("Line/Running", DataType.Int32, 0)
    dcmd("Commands/Reset Counters", DataType.Boolean, False); dcmd("Counts/Good", DataType.Int64, 0)
    dcmd("Line/Running", DataType.Boolean, False, device="No such line")
    assert line()["enabled"] is True and line()["status"]["total_inspected"] == 3 and sparkplug_audit() == []
    # the node still publishes: a counted product afterwards arrives as DDATA

def test_the_dashboard_routes_still_start_stop_and_reset(client, admin_headers):
    # POST /lines/{id}/stop, /start, /counting/reset?line_id=: same responses and audit entries as before the refactor
```

- [ ] **Step 2: Run them and see them fail**

- [ ] **Step 3: Implement.** The node calls `on_command` on paho's thread; the service runs the command as a coroutine on the event loop (`asyncio.run_coroutine_threadsafe`). `RUNNING` needs a Boolean value; `RESET` needs Boolean true. After every DCMD, acted on or not, for a device that exists: `publish_now()`, then `node.resend(device, [metric name])`. Each refusal is logged at info level with the channel's name, the device and the metric; an `HTTPException` from the two shared functions is a refusal too.

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/Scripts/python -m pytest tests/test_sparkplug_commands.py tests/test_lines_api.py tests/test_api.py -q -p no:cacheprovider`

---

### Task 8: Dashboard

**Files:**
- Modify: `dashboard.html` (MQTT section of the channel dialog near `mqttRetainInput`; `openAddCommModal`, `openEditCommModal`, `saveCommEndpoint`; the channel card in `renderCommsGrid`)

**Interfaces:**
- Consumes: the six channel fields (Task 4); `components.mqtt.channels[].sparkplug` of `GET /api/v1/telemetry/health` (Task 6).
- Produces: element ids `mqttSparkplugInput` (switch), `mqttSparkplugFields` (shown while the switch is on), `mqttSpbGroupInput`, `mqttSpbNodeInput`, `mqttSpbHostInput`, `mqttSpbCommandsInput`, `mqttSpbIntervalInput`.

- [ ] **Step 1: Add the section "6. Sparkplug B (Ignition and other SCADA hosts)"** after the lifecycle section, with these labels and hints:

| Control | Label | Hint |
|---|---|---|
| switch | Publish the production lines as Sparkplug B | Each line appears in the host as a device with its counts, state and alarms. |
| text | Group ID * | For example the plant or area: PlantA |
| text | Edge node ID * | This server's name in the host: VisionServer1 |
| text | Primary host ID | Optional. The host ID set in Ignition's MQTT Engine; the lines are then published only while that host is online. |
| switch | Allow commands | Lets the host start and stop a line and reset its counters. Anyone who can publish to the broker can then do the same. |
| number | Publish interval (ms) | 100 to 60000. Changed values are sent at most this often. |

- [ ] **Step 2: Load, reset and save the fields.** The save refuses, in the dialog, an empty group or node ID or one containing `/ + #` while the switch is on, with the message `Enter a Group ID and an Edge node ID without / + #.` The interval is sent as a number only when it parses.

- [ ] **Step 3: Channel card.** An MQTT channel with Sparkplug on shows a badge `Sparkplug B` and one line from the health report: `Publishing <n> line(s)`, `Waiting for host '<id>'`, or `Not connected`.

- [ ] **Step 4: Verify in the browser** on the scratch server (see `backup/scratch-tools/README.md`): open and save a channel with Sparkplug on; reopen it and see the six values; an ID with `/` is refused in the dialog; a channel saved before this task opens with the switch off and saves without errors in the console.

---

### Task 9: Bundled broker

**Files:**
- Create: `docker/mosquitto/mosquitto.conf`, `docker/mosquitto/start.sh`, `.gitattributes` (`*.sh text eol=lf`)
- Modify: `docker-compose.yml`, `.env.example`
- Test: `tests/test_bundled_broker.py`

**Interfaces:**
- Produces: Compose service `mqtt` in profile `broker`; volume `vision-broker`; settings `BROKER_USERNAME` (default `vision`), `BROKER_PASSWORD` (no default), `BROKER_PORT` (default `1883`), `BROKER_BIND_ADDRESS` (default `0.0.0.0`).

- [ ] **Step 1: Write the failing tests**

```python
def test_the_broker_service_is_opt_in_and_needs_a_login():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    mqtt = compose["services"]["mqtt"]
    assert mqtt["profiles"] == ["broker"] and "profiles" not in compose["services"]["vision"]
    assert mqtt["image"].startswith("eclipse-mosquitto:2") and mqtt["restart"] == "unless-stopped"
    assert mqtt["ports"] == ["${BROKER_BIND_ADDRESS:-0.0.0.0}:${BROKER_PORT:-1883}:1883"]
    assert "vision-broker" in compose["volumes"] and "no-new-privileges:true" in mqtt["security_opt"]
    conf = Path("docker/mosquitto/mosquitto.conf").read_text(encoding="utf-8")
    assert "allow_anonymous false" in conf and "password_file" in conf and "listener 1883" in conf
    assert b"\r\n" not in Path("docker/mosquitto/start.sh").read_bytes()
    env = Path(".env.example").read_text(encoding="utf-8")
    assert all(name in env for name in ("BROKER_USERNAME", "BROKER_PASSWORD", "BROKER_PORT", "BROKER_BIND_ADDRESS"))

@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_start_script_refuses_an_empty_password():
    run = subprocess.run(["sh", "docker/mosquitto/start.sh"], env={**os.environ, "BROKER_USERNAME": "vision", "BROKER_PASSWORD": ""},
                         capture_output=True, text=True, timeout=20)
    assert run.returncode != 0 and "BROKER_PASSWORD" in run.stderr
```

- [ ] **Step 2: Run them and see them fail**

- [ ] **Step 3: Write the files.** `start.sh` (POSIX `sh`, `set -eu`): exit 1 with a message naming `BROKER_PASSWORD` on stderr when it is empty; write `/mosquitto/data/passwd` with `mosquitto_passwd -b -c`; `exec mosquitto -c /mosquitto/config/mosquitto.conf`. `mosquitto.conf`: `listener 1883`, `allow_anonymous false`, `password_file /mosquitto/data/passwd`, `persistence true`, `persistence_location /mosquitto/data/`, log to stdout. The service: the two files mounted read-only, the script as entrypoint, `mem_limit: 256m`, the same `logging` block as `vision`, and a health check that reads `$SYS/broker/uptime` once with the login. The file's header comment gains the `--profile broker` command.

- [ ] **Step 4: Run the tests and see them pass.** Then run the config with the local Mosquitto on another port (a copy of `mosquitto.conf` with the listener and paths changed) and check that a login is accepted and an anonymous client is refused. `docker compose --profile broker config` only if Docker is running; say so in the report if it was not.

---

### Task 10: README and the live run

**Files:**
- Modify: `README.md`, `FILE_TREE.md`
- Create: `backup/scratch-tools/check_sparkplug.py` (ignored by git)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: README.** A section "Sparkplug B (Ignition)" under the production lines text: what is published (the metric table of the spec), the setup steps in the channel dialog, the Ignition side (MQTT Engine: add the broker, the same Primary Host ID if one is used; the lines appear under `Edge Nodes/<group>/<node>/<line>`), the commands and the warning about broker logins, and that `Last Product` shows the latest product of an interval while send cards carry every one. A section "Bundled MQTT broker": the four `.env` settings, `docker compose --profile broker up -d`, host `mqtt` port 1883 from the server and the machine's address from other devices, and that it has no TLS. Add the new files to `FILE_TREE.md`.

- [ ] **Step 2: Live run.** Start a Mosquitto with a password on a spare port and the scratch server. `check_sparkplug.py` adds a channel with Sparkplug on, subscribes to `spBv1.0/#` itself, and checks: NBIRTH then one DBIRTH per line; a test product changes `Counts/Inspected`; a rebirth request is answered; with the switch on, a `Line/Running` command stops and starts the line; stopping and starting the broker gives a new NBIRTH with `bdSeq` one higher; deleting the channel gives NDEATH.

- [ ] **Step 3: Independent decoder.** In a throwaway virtual environment outside the repo: install `grpcio-tools` and `paho-mqtt`, compile Eclipse Tahu's `sparkplug_b.proto` (from the `eclipse-tahu/tahu` repository), and decode the messages captured in step 2 with the generated class. Expected: every message parses, and names, datatypes and values equal what `decode_payload` gives.

- [ ] **Step 4: Full suite.** `.venv/Scripts/python -m pytest tests -q -p no:cacheprovider`. Expected: only the baseline failures of Global Constraints.

- [ ] **Step 5: Clean up.** Stop the test broker by the PID that owns its port, stop the scratch server, delete `.claude/launch.json`, restore the scratch copy's `system_state.json`.
