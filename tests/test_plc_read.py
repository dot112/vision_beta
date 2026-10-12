"""read() of every PLC driver, against small in-process fake PLCs.

A signal card (PLC input) reads one address every poll. Each driver answers
``(ok, value, message)``: a bool for a bit, an int, a float for a REAL, text
for a string.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from app.hardware.plc.ethernet_ip_driver import EtherNetIPDriver
from app.hardware.plc.factory import PLCDriverFactory
from app.hardware.plc.fins_driver import OmronFINSDriver
from app.hardware.plc.generic_tcp_driver import GenericTCPDriver
from app.hardware.plc.melsec_driver import MelsecSLMPDriver
from app.hardware.plc.modbus_driver import ModbusTCPDriver
from app.hardware.plc.opcua_driver import OPCUADriver
from app.hardware.plc.s7_driver import S7Driver
from tests.test_plc_protocols import FakeFins, FakeLogix, FakeMelsec, FakeModbus, FakeRTUBus, FakeS7, _serve


def _same(got, value):
    """Equal, and of the same type: True is not 1, and 2 is not 2.0."""
    return got == value and type(got) is type(value)


# ── Modbus TCP ────────────────────────────────────────────────────────────────

class ReadModbus(FakeModbus):
    """A Modbus server that answers the four read functions."""

    def __init__(self):
        super().__init__()
        self.inputs = {}
        self.input_registers = {}
        self.requests = 0

    async def handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(7)
                tx, _, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                self.requests += 1
                fn = pdu[0]
                addr, count = struct.unpack(">HH", pdu[1:5])
                if fn in (0x01, 0x02):
                    bits = self.coils if fn == 0x01 else self.inputs
                    data = bytearray((count + 7) // 8)
                    for i in range(count):
                        if bits.get(addr + i):
                            data[i // 8] |= 1 << (i % 8)
                    reply = bytes((fn, len(data))) + bytes(data)
                elif fn in (0x03, 0x04):
                    words = self.registers if fn == 0x03 else self.input_registers
                    data = b"".join(struct.pack(">H", words.get(addr + i, 0)) for i in range(count))
                    reply = bytes((fn, len(data))) + data
                else:
                    reply = bytes((fn | 0x80, 0x01))
                writer.write(struct.pack(">HHHB", tx, 0, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


async def _modbus_read(address, data_type="auto", setup=None, **endpoint):
    fake = ReadModbus()
    for name, values in (setup or {}).items():
        getattr(fake, name).update(values)
    server, port = await _serve(fake.handle)
    async with server:
        drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1, **endpoint})
        assert await drv.connect()
        try:
            return await drv.read(address, data_type), fake
        finally:
            await drv.disconnect()


_REAL_HIGH, _REAL_LOW = struct.unpack(">HH", struct.pack(">f", 1.5))


@pytest.mark.parametrize("address, data_type, setup, endpoint, value", [
    ("C5", "auto", {"coils": {5: True}}, {}, True),
    ("C6", "auto", {"coils": {5: True}}, {}, False),
    ("10003", "auto", {"inputs": {2: True}}, {}, True),
    ("100003", "auto", {"inputs": {2: True}}, {}, True),
    ("DI2", "auto", {"inputs": {2: True}}, {}, True),
    ("40011", "auto", {"registers": {10: 65535}}, {}, 65535),
    ("40011", "int16", {"registers": {10: 65535}}, {}, -1),
    ("R20:DINT", "auto", {"registers": {20: 0xFFFF, 21: 0xFFFE}}, {}, -2),
    ("R20", "int32", {"registers": {20: 0xFFFF, 21: 0xFFFE}}, {}, -2),
    ("R30", "real", {"registers": {30: _REAL_LOW, 31: _REAL_HIGH}}, {"modbus_word_order": "low_first"}, 1.5),
    ("R30:REAL", "auto", {"registers": {30: _REAL_HIGH, 31: _REAL_LOW}}, {}, 1.5),
    ("30006", "auto", {"input_registers": {5: 7}}, {}, 7),
    ("300006", "auto", {"input_registers": {5: 7}}, {}, 7),
    ("IR5:INT", "auto", {"input_registers": {5: 0xFFFB}}, {}, -5),
    ("R10", "bool", {"registers": {10: 3}}, {}, True),
    ("R10", "bool", {"registers": {10: 0}}, {}, False),
])
def test_modbus_reads_coils_inputs_and_registers(address, data_type, setup, endpoint, value):
    (ok, got, message), _ = asyncio.run(_modbus_read(address, data_type, setup, **endpoint))
    assert ok, message
    assert _same(got, value), (got, message)


def test_modbus_has_no_text_to_read():
    (ok, got, message), _ = asyncio.run(_modbus_read("R10", "string"))
    assert (ok, got) == (False, None) and "string" in message.lower()


def test_an_unknown_data_type_is_refused():
    (ok, got, message), fake = asyncio.run(_modbus_read("C1", "word64"))
    assert (ok, got) == (False, None) and "word64" in message
    assert fake.requests == 0   # nothing was asked of the PLC


def test_a_bad_address_is_an_answer_not_an_exception():
    (ok, got, message), fake = asyncio.run(_modbus_read("Q0.0"))
    assert (ok, got) == (False, None) and "Q0.0" in message
    assert fake.requests == 0


def test_a_write_to_a_read_only_modbus_range_is_still_refused():
    async def run():
        fake = ReadModbus()
        server, port = await _serve(fake.handle)
        async with server:
            drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1})
            assert await drv.connect()
            with pytest.raises(ValueError, match="read-only"):
                await drv.write("30001", 1)
            assert fake.requests == 0
            await drv.disconnect()
    asyncio.run(run())


def test_a_read_waits_for_the_drivers_operation_lock():
    async def run():
        fake = ReadModbus()
        fake.coils[1] = True
        server, port = await _serve(fake.handle)
        async with server:
            drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1})
            assert await drv.connect()
            await drv._operation_lock.acquire()
            task = asyncio.create_task(drv.read("C1"))
            await asyncio.sleep(0.05)
            assert not task.done() and fake.requests == 0
            drv._operation_lock.release()
            ok, value, message = await task
            assert (ok, value) == (True, True), message
            await drv.disconnect()
    asyncio.run(run())


def test_a_read_without_a_connection_says_so():
    async def run():
        drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": 1, "timeout": 1})
        ok, value, message = await drv.read("C1")
        assert (ok, value) == (False, None) and message
    asyncio.run(run())


def test_modbus_simulation_mode_reads_what_was_written():
    async def run():
        # Port 1 is never a Modbus server: connect() falls back to the driver's own memory.
        drv = ModbusTCPDriver({"id": "sim", "host": "127.0.0.1", "port": 1, "timeout": 5, "simulation_mode": True})
        assert await drv.connect()
        ok, message = await drv.write("R3", 9)
        assert ok, message
        ok, value, message = await drv.read("R3")
        assert ok and _same(value, 9), message
        ok, value, message = await drv.read("IR3")
        assert ok and _same(value, 0), message
        ok, value, message = await drv.read("DI3")
        assert ok and value is False, message
    asyncio.run(run())


def test_modbus_rtu_reads_through_the_same_path():
    async def run():
        bus = FakeRTUBus()
        bus.registers[2][4] = 12
        server, port = await _serve(bus.handle)
        async with server:
            drv = PLCDriverFactory.get_driver({"id": "gw", "plc_sub_protocol": "modbus_rtu", "rtu_transport": "tcp",
                                               "host": "127.0.0.1", "port": port, "timeout": 1,
                                               "modbus_unit_id": 2}, fresh=True)
            assert await drv.connect()
            ok, value, message = await drv.read("R4")
            assert ok and _same(value, 12), message
            await drv.disconnect()
    asyncio.run(run())


# ── Siemens S7 ────────────────────────────────────────────────────────────────

class ReadS7(FakeS7):
    """An S7 CPU that answers a read of any number of bytes from ``memory``."""

    async def handle(self, reader, writer):
        try:
            await self._read(reader)                               # COTP CR
            writer.write(bytes([3, 0, 0, 11, 6, 0xD0, 0, 1, 0, 1, 0]))
            await self._read(reader)                               # setup communication
            writer.write(self._reply(0, bytes([0xF0, 0, 0, 1, 0, 1, 0x03, 0xC0])))
            while True:
                packet = await self._read(reader)
                s7 = packet[7:]
                ref = int.from_bytes(s7[4:6], "big")
                plen = int.from_bytes(s7[6:8], "big")
                params = s7[10:10 + plen]
                count = int.from_bytes(params[6:8], "big")
                start = int.from_bytes(params[11:14], "big") // 8
                data = bytes(self.memory.get(start + i, 0) for i in range(count))
                writer.write(self._reply(ref, b"\x04\x01", bytes([0xFF, 0x04]) + struct.pack(">H", count * 8) + data))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


def _s7_memory(offset, data):
    return {offset + i: byte for i, byte in enumerate(data)}


async def _s7_read(address, data_type, memory):
    fake = ReadS7()
    fake.memory = dict(memory)
    server, port = await _serve(fake.handle)
    async with server:
        drv = S7Driver({"id": "s", "host": "127.0.0.1", "port": port, "timeout": 1})
        try:
            return await drv.read(address, data_type)
        finally:
            await drv.disconnect()


@pytest.mark.parametrize("address, data_type, memory, value", [
    ("I0.2", "auto", {0: 0b100}, True),
    ("I0.1", "auto", {0: 0b100}, False),
    ("DB5.DBX2.0", "auto", {2: 0b001}, True),
    ("MB3", "auto", {3: 200}, 200),
    ("MW10", "auto", _s7_memory(10, b"\xff\xfe"), 65534),
    ("MW10", "int16", _s7_memory(10, b"\xff\xfe"), -2),
    ("DB1.DBD8", "auto", _s7_memory(8, b"\xff\xff\xff\xfe"), 4294967294),
    ("DB1.DBD8", "int32", _s7_memory(8, b"\xff\xff\xff\xfe"), -2),
    ("DB1.DBD8:REAL", "auto", _s7_memory(8, struct.pack(">f", 2.5)), 2.5),
    ("MD8", "real", _s7_memory(8, struct.pack(">f", 2.5)), 2.5),
    ("MW10", "bool", _s7_memory(10, b"\x00\x01"), True),
])
def test_s7_reads_bits_bytes_words_and_reals(address, data_type, memory, value):
    ok, got, message = asyncio.run(_s7_read(address, data_type, memory))
    assert ok, message
    assert _same(got, value), (got, message)


def test_s7_refuses_a_type_wider_than_the_address():
    ok, got, message = asyncio.run(_s7_read("MW10", "int32", {}))
    assert (ok, got) == (False, None) and "double" in message.lower()


# ── EtherNet/IP ───────────────────────────────────────────────────────────────

class ReadLogix(FakeLogix):
    """A Logix CPU with a STRING tag: a structure with the handle 0x0FCE, a DINT length and 82 bytes."""

    TYPES = {**FakeLogix.TYPES, b"Batch": 0x02A0}

    def __init__(self):
        super().__init__()
        self.values[b"Reject"] = b"\xff"
        self.values[b"Count"] = struct.pack("<i", 70000)
        self.values[b"Speed"] = struct.pack("<f", 12.5)
        self.values[b"Batch"] = b"\xce\x0f" + struct.pack("<i", 5) + b"LOT-7".ljust(82, b"\x00")


async def _eip_read(tag, data_type="auto"):
    fake = ReadLogix()
    server, port = await _serve(fake.handle)
    async with server:
        drv = EtherNetIPDriver({"id": "e", "host": "127.0.0.1", "port": port, "timeout": 1, "eip_path": "1,0"})
        try:
            return await drv.read(tag, data_type)
        finally:
            await drv.disconnect()


@pytest.mark.parametrize("tag, data_type, value", [
    ("Reject", "auto", True),
    ("Count", "auto", 70000),
    ("Speed", "auto", 12.5),
    ("Batch", "auto", "LOT-7"),
    ("Batch", "string", "LOT-7"),
    ("Count", "bool", True),
])
def test_ethernet_ip_reads_a_tag_as_its_own_type(tag, data_type, value):
    ok, got, message = asyncio.run(_eip_read(tag, data_type))
    assert ok, message
    assert _same(got, value), (got, message)


def test_ethernet_ip_names_the_cip_error_of_a_tag_that_does_not_exist():
    ok, got, message = asyncio.run(_eip_read("Missing"))
    assert (ok, got) == (False, None) and "0x05" in message


def test_ethernet_ip_does_not_read_a_number_as_text():
    ok, got, message = asyncio.run(_eip_read("Count", "string"))
    assert (ok, got) == (False, None) and "string" in message.lower()


# ── OPC UA ────────────────────────────────────────────────────────────────────

class _Node:
    def __init__(self, value):
        self._value = value

    async def read_value(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class _OpcClient:
    """What the driver uses of an asyncua client."""

    def __init__(self, values):
        self.values = values

    def get_node(self, node_id):
        return _Node(self.values[node_id])

    async def check_connection(self):
        return None

    async def disconnect(self):
        return None


def _opcua(values):
    drv = OPCUADriver({"id": "o", "host": "127.0.0.1", "port": 4840, "timeout": 1})
    drv._client = _OpcClient(values)
    drv.is_connected = True
    return drv


@pytest.mark.parametrize("node, data_type, value", [
    ("ns=2;s=Run", "auto", True),
    ("ns=2;s=Count", "auto", 7),
    ("ns=2;s=Speed", "auto", 1.5),
    ("ns=2;s=Batch", "auto", "LOT-7"),
    ("ns=2;s=Batch", "string", "LOT-7"),
    ("ns=2;s=Count", "bool", True),
    ("ns=2;s=Count", "real", 7.0),
])
def test_opc_ua_reads_a_node(node, data_type, value):
    drv = _opcua({"ns=2;s=Run": True, "ns=2;s=Count": 7, "ns=2;s=Speed": 1.5, "ns=2;s=Batch": "LOT-7"})
    ok, got, message = asyncio.run(drv.read(node, data_type))
    assert ok, message
    assert _same(got, value), (got, message)


def test_opc_ua_reports_a_node_that_cannot_be_read():
    drv = _opcua({"ns=2;s=Gone": RuntimeError("BadNodeIdUnknown")})
    ok, got, message = asyncio.run(drv.read("ns=2;s=Gone"))
    assert (ok, got) == (False, None) and "BadNodeIdUnknown" in message
    ok, got, message = asyncio.run(drv.read("Gone"))        # not a NodeId at all
    assert (ok, got) == (False, None) and "NodeId" in message


# ── MELSEC SLMP ───────────────────────────────────────────────────────────────

class ReadMelsec(FakeMelsec):
    """An SLMP server that answers word reads from ``words``."""

    def __init__(self):
        super().__init__()
        self.words = {}

    async def handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(9)
                length = struct.unpack_from("<H", header, 7)[0]
                body = await reader.readexactly(length)
                _, command, sub = struct.unpack_from("<HHH", body, 0)
                number = int.from_bytes(body[6:9], "little")
                code = body[9]
                points = struct.unpack_from("<H", body, 10)[0]
                data = b""
                if command == 0x0401 and sub == 1:
                    data = b"\x10" if self.bits.get((code, number)) else b"\x00"
                elif command == 0x0401:
                    data = b"".join(struct.pack("<H", self.words.get((code, number + i), 0)) for i in range(points))
                reply = struct.pack("<H", 0) + data
                writer.write(b"\xD0\x00\x00\xFF\xFF\x03\x00" + struct.pack("<H", len(reply)) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


_D = 0xA8   # MELSEC data register
_REAL_WORDS = struct.unpack("<HH", struct.pack("<f", 1.5))   # low word first


@pytest.mark.parametrize("address, data_type, bits, words, value", [
    ("M5", "auto", {(0x90, 5): True}, {}, True),
    ("M6", "auto", {(0x90, 5): True}, {}, False),
    ("D100", "auto", {}, {(_D, 100): 0xFFFF}, 65535),
    ("D100", "int16", {}, {(_D, 100): 0xFFFF}, -1),
    ("D100:DINT", "auto", {}, {(_D, 100): 0x1170, (_D, 101): 0x0001}, 70000),
    ("D100", "int32", {}, {(_D, 100): 0x1170, (_D, 101): 0x0001}, 70000),
    ("D100:REAL", "auto", {}, {(_D, 100): _REAL_WORDS[0], (_D, 101): _REAL_WORDS[1]}, 1.5),
    ("D100", "bool", {}, {(_D, 100): 2}, True),
])
def test_melsec_reads_bits_and_words(address, data_type, bits, words, value):
    async def run():
        fake = ReadMelsec()
        fake.bits.update(bits)
        fake.words.update(words)
        server, port = await _serve(fake.handle)
        async with server:
            drv = MelsecSLMPDriver({"id": "mc", "host": "127.0.0.1", "port": port, "timeout": 1})
            try:
                return await drv.read(address, data_type)
            finally:
                await drv.disconnect()

    ok, got, message = asyncio.run(run())
    assert ok, message
    assert _same(got, value), (got, message)


# ── Omron FINS ────────────────────────────────────────────────────────────────

class ReadFins(FakeFins):
    """A FINS/TCP server that answers memory area reads from ``bits`` and ``words``."""

    def __init__(self):
        super().__init__()
        self.bits = {}
        self.words = {}

    async def handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(16)
                length, command, _ = struct.unpack(">III", header[4:16])
                payload = await reader.readexactly(length - 8)
                if command == 0:
                    writer.write(b"FINS" + struct.pack(">IIIII", 16, 1, 0, 0x22, 0x01))
                else:
                    fins = payload
                    area, (word, bit, count) = fins[12], struct.unpack(">HBH", fins[13:18])
                    if area & 0x80:
                        data = b"".join(struct.pack(">H", self.words.get((area, word + i), 0)) for i in range(count))
                    else:
                        data = b"\x01" if self.bits.get((area, word, bit)) else b"\x00"
                    reply = bytes((0xC0, 0, 0x02, 0, 0x22, 0, 0, 0x01, 0, fins[9], fins[10], fins[11], 0, 0)) + data
                    writer.write(b"FINS" + struct.pack(">III", 8 + len(reply), 2, 0) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


_DM = 0x82   # Omron data memory, word access


@pytest.mark.parametrize("address, data_type, bits, words, value", [
    ("CIO 0.05", "auto", {(0x30, 0, 5): True}, {}, True),
    ("W10.01", "auto", {(0x30, 0, 5): True}, {}, False),
    ("D100", "auto", {}, {(_DM, 100): 1234}, 1234),
    ("D100", "int16", {}, {(_DM, 100): 0xFFFF}, -1),
    ("D100:DINT", "auto", {}, {(_DM, 100): 0x1170, (_DM, 101): 0x0001}, 70000),
    ("D100:REAL", "auto", {}, {(_DM, 100): _REAL_WORDS[0], (_DM, 101): _REAL_WORDS[1]}, 1.5),
])
def test_fins_reads_bits_and_words(address, data_type, bits, words, value):
    async def run():
        fake = ReadFins()
        fake.bits.update(bits)
        fake.words.update(words)
        server, port = await _serve(fake.handle)
        async with server:
            drv = OmronFINSDriver({"id": "f", "host": "127.0.0.1", "port": port, "timeout": 1})
            try:
                return await drv.read(address, data_type)
            finally:
                await drv.disconnect()

    ok, got, message = asyncio.run(run())
    assert ok, message
    assert _same(got, value), (got, message)


# ── Generic TCP ───────────────────────────────────────────────────────────────

def test_a_generic_tcp_channel_cannot_be_read():
    drv = GenericTCPDriver({"id": "g", "host": "127.0.0.1", "port": 1, "timeout": 1})
    assert asyncio.run(drv.read("1")) == (False, None, "Reading is not supported by GenericTCPDriver")
