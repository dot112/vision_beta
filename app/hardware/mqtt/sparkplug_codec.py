"""Sparkplug B payloads to and from bytes, and the topics they travel on.

Sparkplug B is a rule book for MQTT in plants: fixed topic names, payloads in
Google Protocol Buffers, and birth and death messages that tell a host which
devices are alive. This module is the payload and topic part; it does no I/O.

The two message types are described here in code, with the field numbers of
Eclipse Tahu's sparkplug_b.proto, so the repository needs no generated file
and no protoc. Only what this server sends and what a host writes back is
described (names, scalar values, timestamps); fields a host adds beyond that
are skipped on decode.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Optional, Sequence, Tuple

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError

NAMESPACE = "spBv1.0"
MESSAGE_KINDS = ("NBIRTH", "NDEATH", "DBIRTH", "DDEATH", "NDATA", "DDATA", "NCMD", "DCMD")
MAX_ID_LENGTH = 128


class DataType(IntEnum):
    Int8 = 1
    Int16 = 2
    Int32 = 3
    Int64 = 4
    UInt8 = 5
    UInt16 = 6
    UInt32 = 7
    UInt64 = 8
    Float = 9
    Double = 10
    Boolean = 11
    String = 12
    DateTime = 13
    Text = 14
    UUID = 15


@dataclass(frozen=True)
class Metric:
    name: str
    datatype: int
    value: Any
    timestamp: Optional[int] = None


@dataclass(frozen=True)
class Payload:
    metrics: Tuple[Metric, ...]
    timestamp: Optional[int]
    seq: Optional[int]


@dataclass(frozen=True)
class Topic:
    group: str
    kind: str
    node: str
    device: Optional[str]


# (value field, bits, signed) for each datatype. Integers travel unsigned: a
# negative one is written as two's complement of the field's width.
_VALUE_FIELDS = {
    DataType.Int8: ("int_value", 8, True),
    DataType.Int16: ("int_value", 16, True),
    DataType.Int32: ("int_value", 32, True),
    DataType.Int64: ("long_value", 64, True),
    DataType.UInt8: ("int_value", 8, False),
    DataType.UInt16: ("int_value", 16, False),
    DataType.UInt32: ("int_value", 32, False),
    DataType.UInt64: ("long_value", 64, False),
    DataType.DateTime: ("long_value", 64, False),
    DataType.Float: ("float_value", 0, False),
    DataType.Double: ("double_value", 0, False),
    DataType.Boolean: ("boolean_value", 0, False),
    DataType.String: ("string_value", 0, False),
    DataType.Text: ("string_value", 0, False),
    DataType.UUID: ("string_value", 0, False),
}


# For a metric that names no datatype (a host may leave it out of a command):
# the datatype its value field stands for.
_FIELD_DATATYPES = (
    ("boolean_value", DataType.Boolean),
    ("string_value", DataType.String),
    ("double_value", DataType.Double),
    ("float_value", DataType.Float),
    ("long_value", DataType.UInt64),
    ("int_value", DataType.UInt32),
)


def _message_classes():
    field_type = descriptor_pb2.FieldDescriptorProto
    file = descriptor_pb2.FileDescriptorProto()
    file.name = "vision_server_sparkplug_b.proto"
    file.package = "org.eclipse.tahu.protobuf"
    file.syntax = "proto2"
    payload = file.message_type.add()
    payload.name = "Payload"
    metric = payload.nested_type.add()
    metric.name = "Metric"

    def add(message, name, number, kind, repeated=False, type_name=None):
        field = message.field.add()
        field.name, field.number, field.type = name, number, kind
        field.label = field_type.LABEL_REPEATED if repeated else field_type.LABEL_OPTIONAL
        if type_name:
            field.type_name = type_name

    add(payload, "timestamp", 1, field_type.TYPE_UINT64)
    add(payload, "metrics", 2, field_type.TYPE_MESSAGE, repeated=True, type_name=".org.eclipse.tahu.protobuf.Payload.Metric")
    add(payload, "seq", 3, field_type.TYPE_UINT64)
    add(metric, "name", 1, field_type.TYPE_STRING)
    add(metric, "alias", 2, field_type.TYPE_UINT64)
    add(metric, "timestamp", 3, field_type.TYPE_UINT64)
    add(metric, "datatype", 4, field_type.TYPE_UINT32)
    add(metric, "is_null", 7, field_type.TYPE_BOOL)
    add(metric, "int_value", 10, field_type.TYPE_UINT32)
    add(metric, "long_value", 11, field_type.TYPE_UINT64)
    add(metric, "float_value", 12, field_type.TYPE_FLOAT)
    add(metric, "double_value", 13, field_type.TYPE_DOUBLE)
    add(metric, "boolean_value", 14, field_type.TYPE_BOOL)
    add(metric, "string_value", 15, field_type.TYPE_STRING)

    # A pool of its own: another Sparkplug library in the same process may
    # register the same type names in the default pool.
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("org.eclipse.tahu.protobuf.Payload"))


_PayloadMessage = _message_classes()


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_payload(metrics: Sequence[Metric], *, timestamp: Optional[int] = None, seq: Optional[int] = None) -> bytes:
    message = _PayloadMessage()
    if timestamp is not None:
        message.timestamp = int(timestamp)
    for metric in metrics:
        entry = message.metrics.add()
        entry.name = metric.name
        if metric.timestamp is not None:
            entry.timestamp = int(metric.timestamp)
        entry.datatype = int(metric.datatype)
        if metric.value is None:
            entry.is_null = True
            continue
        field, bits, _ = _VALUE_FIELDS[DataType(metric.datatype)]
        if bits:
            # 32 or 64 bits on the wire, whatever the datatype's own width (as Java hosts do).
            setattr(entry, field, int(metric.value) & (0xFFFFFFFF if field == "int_value" else 0xFFFFFFFFFFFFFFFF))
        elif field == "boolean_value":
            entry.boolean_value = bool(metric.value)
        elif field == "string_value":
            entry.string_value = str(metric.value)
        else:
            setattr(entry, field, float(metric.value))
    if seq is not None:
        message.seq = int(seq)
    return message.SerializeToString()


def _datatype(entry) -> int:
    if entry.HasField("datatype"):
        return entry.datatype
    return next((int(datatype) for field, datatype in _FIELD_DATATYPES if entry.HasField(field)), 0)


def _value(entry, datatype: int) -> Any:
    if entry.is_null:
        return None
    try:
        field, bits, signed = _VALUE_FIELDS[DataType(datatype)]
    except ValueError:
        return None  # a dataset, a template or a file: not something this server reads
    if not entry.HasField(field):
        return None
    value = getattr(entry, field)
    if not bits:
        return value
    # Mask to the datatype's own width first: a negative Int8 arrives as 0xFF
    # from some clients and as 0xFFFFFFFF from others.
    value &= (1 << bits) - 1
    if signed and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def decode_payload(data: bytes) -> Payload:
    """The payload in these bytes. Raises ValueError when they are not a Sparkplug payload."""
    message = _PayloadMessage()
    try:
        message.ParseFromString(bytes(data))
    except DecodeError as exc:
        raise ValueError(f"not a Sparkplug payload: {exc}") from exc
    if not message.metrics and not message.HasField("timestamp") and not message.HasField("seq"):
        raise ValueError("not a Sparkplug payload: it holds no metrics, timestamp or sequence number")
    metrics = []
    for entry in message.metrics:
        if not entry.name:
            raise ValueError("a metric has no name (metric aliases are not used by this server)")
        datatype = _datatype(entry)
        metrics.append(Metric(entry.name, datatype, _value(entry, datatype), entry.timestamp if entry.HasField("timestamp") else None))
    return Payload(
        metrics=tuple(metrics),
        timestamp=message.timestamp if message.HasField("timestamp") else None,
        seq=message.seq if message.HasField("seq") else None,
    )


def id_problem(value: Any) -> Optional[str]:
    """Why a group, edge node or host ID cannot be used, or None when it can."""
    if not isinstance(value, str) or not value.strip():
        return "it is empty"
    if len(value) > MAX_ID_LENGTH:
        return f"it is longer than {MAX_ID_LENGTH} characters"
    if any(ch in value for ch in "/+#"):
        return "it contains one of the characters / + #"
    return None


def topic(group: str, kind: str, node: str, device: Optional[str] = None) -> str:
    parts = [NAMESPACE, group, kind, node]
    if device is not None:
        parts.append(device)
    return "/".join(parts)


def parse_topic(text: str) -> Optional[Topic]:
    """The parts of a node or device topic; None for any other topic (host state included)."""
    parts = str(text).split("/")
    if len(parts) not in (4, 5) or parts[0] != NAMESPACE or parts[2] not in MESSAGE_KINDS:
        return None
    return Topic(group=parts[1], kind=parts[2], node=parts[3], device=parts[4] if len(parts) == 5 else None)
