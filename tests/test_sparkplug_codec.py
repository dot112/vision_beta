"""Sparkplug B payloads to and from bytes, and the topics they travel on."""
from __future__ import annotations

import pytest

from app.hardware.mqtt.sparkplug_codec import (
    DataType,
    Metric,
    Topic,
    decode_payload,
    encode_payload,
    id_problem,
    parse_topic,
    topic,
)


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


def test_a_payload_without_seq_or_timestamp_has_none():
    # An NDEATH has no sequence number.
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


def test_a_metric_without_a_datatype_takes_it_from_its_value():
    # A host may leave the datatype out of a command; the value field says what it is.
    name = b"Node Control/Rebirth"
    rebirth = bytes([0x12, 2 + len(name) + 2, 0x0A, len(name)]) + name + bytes([0x70, 0x01])    # boolean_value = true
    assert decode_payload(rebirth).metrics == (Metric("Node Control/Rebirth", DataType.Boolean, True),)
    text = bytes.fromhex("12060a016d7a0178")        # name "m", string_value "x"
    number = bytes.fromhex("12050a016d5005")        # name "m", int_value 5
    assert decode_payload(text).metrics == (Metric("m", DataType.String, "x"),)
    assert decode_payload(number).metrics == (Metric("m", DataType.UInt32, 5),)
