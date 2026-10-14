"""Helpers shared by the stream-based PLC drivers."""
from __future__ import annotations

import asyncio
import math
import struct
from typing import Any, Awaitable, Callable, List, Tuple, Union

Result = Tuple[bool, str]

# 32-bit address suffixes shared by the MELSEC and FINS drivers ("D100:DINT").
WIDE_TYPES = {"DINT": "<i", "UDINT": "<I", "DWORD": "<I", "REAL": "<f", "FLOAT": "<f"}

# The data types read() can be asked for, as the address suffix that means the same.
READ_SUFFIX = {"int16": "INT", "uint16": "UINT", "int32": "DINT", "real": "REAL"}


async def pulse_with_reset(
    on: Callable[[], Awaitable[Result]],
    off: Callable[[], Awaitable[Result]],
    duration_ms: int,
    label: str,
    wait: Callable[[int], Awaitable[None]],
) -> Result:
    """Turn a bit ON, wait (the driver's _pulse_wait), and always turn it OFF again — even if the caller is cancelled."""
    ok_on, msg_on = await on()
    if not ok_on:
        # The ON write may have landed even though its reply was lost.
        ok_off, _ = await off()
        suffix = "OFF cleanup acknowledged" if ok_off else "OFF cleanup failed"
        return False, f"{label} PULSE ON failed: {msg_on}; {suffix}"
    try:
        await wait(duration_ms)
    finally:
        reset_task = asyncio.create_task(off())
        try:
            ok_off, msg_off = await asyncio.shield(reset_task)
        except asyncio.CancelledError:
            await reset_task
            raise
    if not ok_off:
        return False, f"{label} PULSE OFF failed — output may be stuck ON: {msg_off}"
    return True, f"{label} PULSE for {duration_ms} ms — OK"


def split_type_suffix(address: str) -> Tuple[str, str]:
    """Split "D100:DINT" into ("D100", "DINT"); the type is "" when absent."""
    text = str(address or "").strip().upper()
    if ":" not in text:
        return text, ""
    base, data_type = (part.strip() for part in text.rsplit(":", 1))
    if data_type not in WIDE_TYPES and data_type not in ("INT", "UINT", "WORD"):
        raise ValueError(f"Unknown data type '{data_type}'; use INT, UINT, DINT, UDINT or REAL")
    return base, data_type


def encode_words(value: float, data_type: str) -> List[int]:
    """
    Encode a value as 16-bit words, low word first (the MELSEC and Omron layout
    for 32-bit values: D100 holds the low word, D101 the high word).
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("value must be a number") from exc
    if not math.isfinite(numeric):
        raise ValueError("value must be finite")
    if data_type in ("REAL", "FLOAT"):
        packed = struct.pack("<f", numeric)
        if not math.isfinite(struct.unpack("<f", packed)[0]):
            raise ValueError("value is outside the REAL range")
    else:
        if not numeric.is_integer():
            raise ValueError("value must be a whole number (add :REAL for decimals)")
        integer = int(numeric)
        if data_type in WIDE_TYPES:
            fmt = WIDE_TYPES[data_type]
            try:
                packed = struct.pack(fmt, integer)
            except struct.error as exc:
                raise ValueError(f"value {integer} does not fit {data_type}") from exc
        else:
            limits = {"INT": (-32768, 32767), "UINT": (0, 65535), "WORD": (0, 65535)}.get(data_type, (-32768, 65535))
            if not limits[0] <= integer <= limits[1]:
                raise ValueError(f"value {integer} does not fit a 16-bit word ({limits[0]}..{limits[1]})")
            packed = struct.pack("<H", integer & 0xFFFF)
    return [struct.unpack_from("<H", packed, offset)[0] for offset in range(0, len(packed), 2)]


def word_type(suffix: str, data_type: str, protocol: str) -> str:
    """The register type a read uses: the one asked for, else the address's own suffix, else one unsigned word."""
    if data_type == "string":
        raise ValueError(f"{protocol} registers hold numbers; a string cannot be read from them")
    return READ_SUFFIX.get(data_type) or suffix or "UINT"


def round_real(value: float) -> float:
    """A 32-bit REAL as the number it stands for: 12.3, not 12.300000190734863."""
    return float(f"{value:.7g}")


def decode_words(words: List[int], data_type: str) -> Union[int, float]:
    """The reverse of encode_words(): 16-bit words, low word first, as one value."""
    packed = b"".join(struct.pack("<H", word & 0xFFFF) for word in words)
    if data_type in WIDE_TYPES:
        if len(packed) != 4:
            raise ValueError(f"{data_type} needs two words")
        value = struct.unpack(WIDE_TYPES[data_type], packed)[0]
        return round_real(value) if data_type in ("REAL", "FLOAT") else int(value)
    if len(packed) < 2:
        raise ValueError("no word to decode")
    return int(struct.unpack("<h" if data_type == "INT" else "<H", packed[:2])[0])


def coerce_read(value: Any, data_type: str) -> Any:
    """A value the PLC returned with its own type (a tag, a node), as the type that was asked for."""
    if data_type == "auto":
        return value
    if data_type == "string":
        if isinstance(value, str):
            return value
        raise ValueError(f"the value is {type(value).__name__}, not a string")
    if isinstance(value, str):
        raise ValueError(f"the value is a string, not {data_type}")
    if data_type == "bool":
        return bool(value)
    if data_type == "real":
        return float(value)
    number = float(value)
    if not number.is_integer():
        raise ValueError(f"the value {value} is not a whole number; read it as real")
    return int(number)
