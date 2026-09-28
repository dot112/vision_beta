"""Wire-level tests for the PLC drivers against small in-process fake PLC servers."""
from __future__ import annotations

import asyncio
import struct

import pytest

from app.hardware.plc.ethernet_ip_driver import EtherNetIPDriver, _encode_route_path, _encode_tag_path
from app.hardware.plc.factory import PLCDriverFactory
from app.hardware.plc.fins_driver import OmronFINSDriver, _parse_fins_address
from app.hardware.plc.generic_tcp_driver import GenericTCPDriver
from app.hardware.plc.melsec_driver import MelsecSLMPDriver, _parse_melsec_address
from app.hardware.plc.modbus_driver import ModbusTCPDriver, _parse_modbus_target
from app.hardware.plc.opcua_driver import OPCUADriver
from app.hardware.plc.s7_driver import S7Driver, _parse_s7_address


class _Server:
    """Stop listening on exit without waiting for client sockets (Server.wait_closed would)."""

    def __init__(self, server):
        self.server = server

    async def __aenter__(self):
        return self.server

    async def __aexit__(self, *exc):
        self.server.close()


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return _Server(server), server.sockets[0].getsockname()[1]


# ── Modbus TCP ────────────────────────────────────────────────────────────────

class FakeModbus:
    def __init__(self):
        self.coils = {}
        self.registers = {}
        self.writers = []

    async def handle(self, reader, writer):
        self.writers.append(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                tx, _, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                fn = pdu[0]
                if fn == 0x05:
                    addr, value = struct.unpack(">HH", pdu[1:5])
                    self.coils[addr] = value == 0xFF00
                    reply = pdu
                elif fn == 0x06:
                    addr, value = struct.unpack(">HH", pdu[1:5])
                    if addr == 9999:
                        reply = bytes((0x86, 0x02))
                    else:
                        self.registers[addr] = value
                        reply = pdu
                elif fn == 0x10:
                    addr, count, _ = struct.unpack(">HHB", pdu[1:6])
                    for i in range(count):
                        self.registers[addr + i] = struct.unpack(">H", pdu[6 + i * 2:8 + i * 2])[0]
                    reply = pdu[:5]
                elif fn == 0x01:
                    addr, count = struct.unpack(">HH", pdu[1:5])
                    reply = bytes((0x01, 1, 1 if self.coils.get(addr) else 0))
                else:
                    reply = bytes((fn | 0x80, 0x01))
                writer.write(struct.pack(">HHHB", tx, 0, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


class TestModbusAddresses:
    def test_read_only_notations_are_rejected(self):
        for address in ("10001", "30001", "300001"):
            with pytest.raises(ValueError, match="read-only"):
                _parse_modbus_target(address)

    def test_six_digit_holding_register(self):
        assert _parse_modbus_target("400010") == ("register", 9, "UINT")

    def test_typed_register(self):
        assert _parse_modbus_target("R10:dint") == ("register", 10, "DINT")
        with pytest.raises(ValueError):
            _parse_modbus_target("C1:REAL")

    def test_plain_coil_still_zero_based(self):
        assert _parse_modbus_target("10")[:2] == ("coil", 10)


class TestModbusWire:
    def test_writes_high_coils_and_32bit_values(self):
        async def run():
            fake = FakeModbus()
            server, port = await _serve(fake.handle)
            async with server:
                drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1})
                assert await drv.connect()
                ok, msg = await drv.set("C1000")          # used to fail on the 512-entry cache
                assert ok, msg
                assert fake.coils[1000] is True
                ok, msg = await drv.write("R20:DINT", -2)
                assert ok, msg
                assert (fake.registers[20], fake.registers[21]) == (0xFFFF, 0xFFFE)
                ok, msg = await drv.write("R30:REAL", 1.5)
                assert ok, msg
                assert struct.unpack(">f", struct.pack(">HH", fake.registers[30], fake.registers[31]))[0] == 1.5
                ok, msg = await drv.write("R40:INT", -5)
                assert ok and fake.registers[40] == 0xFFFB
                ok, msg = await drv.write("R40", 1.5)
                assert not ok and "whole number" in msg
                await drv.disconnect()
        asyncio.run(run())

    def test_low_word_first_order(self):
        async def run():
            fake = FakeModbus()
            server, port = await _serve(fake.handle)
            async with server:
                drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1,
                                       "modbus_word_order": "low_first"})
                await drv.connect()
                ok, _ = await drv.write("R0:UDINT", 0x12345678)
                assert ok and (fake.registers[0], fake.registers[1]) == (0x5678, 0x1234)
                await drv.disconnect()
        asyncio.run(run())

    def test_exception_reply_keeps_connection(self):
        async def run():
            fake = FakeModbus()
            server, port = await _serve(fake.handle)
            async with server:
                drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1})
                await drv.connect()
                ok, msg = await drv.write("R9999", 1)
                assert not ok and "illegal data address" in msg
                assert drv.is_connected
                await drv.disconnect()
        asyncio.run(run())

    def test_lost_link_is_reported_so_dispatcher_reconnects(self):
        async def run():
            fake = FakeModbus()
            server, port = await _serve(fake.handle)
            async with server:
                drv = ModbusTCPDriver({"id": "m", "host": "127.0.0.1", "port": port, "timeout": 1})
                await drv.connect()
                for writer in fake.writers:
                    writer.close()
                await asyncio.sleep(0.05)
                ok, _ = await drv.set("C1")
                assert not ok
                assert drv.is_connected is False     # used to stay True forever
                assert await drv.connect()
                ok, msg = await drv.set("C1")
                assert ok, msg
                await drv.disconnect()
        asyncio.run(run())


# ── Siemens S7 ────────────────────────────────────────────────────────────────

class FakeS7:
    def __init__(self):
        self.memory = {0: 0b0000_0100}
        self.bit_writes = []
        self.byte_writes = []

    async def _read(self, reader):
        header = await reader.readexactly(4)
        return header + await reader.readexactly(int.from_bytes(header[2:4], "big") - 4)

    @staticmethod
    def _reply(ref, params, data=b""):
        s7 = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, ref, len(params), len(data), 0, 0) + params + data
        return struct.pack(">BBH", 3, 0, 7 + len(s7)) + b"\x02\xF0\x80" + s7

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
                data = s7[10 + plen:]
                transport = params[5]
                start = int.from_bytes(params[11:14], "big")
                if params[0] == 0x04:                             # read one byte
                    value = self.memory.get(start // 8, 0)
                    writer.write(self._reply(ref, b"\x04\x01", bytes([0xFF, 0x04, 0x00, 0x08, value])))
                else:
                    if transport == 0x01:
                        self.bit_writes.append((start // 8, start % 8, data[4]))
                        byte = self.memory.get(start // 8, 0)
                        mask = 1 << (start % 8)
                        self.memory[start // 8] = byte | mask if data[4] else byte & ~mask
                    else:
                        self.byte_writes.append((start // 8, data[4:]))
                    writer.write(self._reply(ref, b"\x05\x01", b"\xFF"))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


class TestS7:
    def test_parser_area_words_and_real(self):
        assert _parse_s7_address("MW10") == {"type": "word", "area": "memory", "byte": 10}
        assert _parse_s7_address("QD4")["type"] == "dword"
        assert _parse_s7_address("DB1.DBD8:REAL")["real"] is True
        with pytest.raises(ValueError):
            _parse_s7_address("DB1.DBW2:REAL")

    def test_bool_ops_use_native_bit_writes(self):
        async def run():
            fake = FakeS7()
            server, port = await _serve(fake.handle)
            async with server:
                drv = S7Driver({"id": "s", "host": "127.0.0.1", "port": port, "timeout": 1})
                ok, msg = await drv.set("Q0.0")
                assert ok, msg
                ok, msg = await drv.toggle("Q0.2")
                assert ok, msg
                # Only the addressed bits were written; bit 2's neighbours were never rewritten.
                assert fake.bit_writes == [(0, 0, 1), (0, 2, 0)]
                assert fake.byte_writes == []
                await drv.disconnect()
        asyncio.run(run())

    def test_word_writes_check_range_and_sign(self):
        async def run():
            fake = FakeS7()
            server, port = await _serve(fake.handle)
            async with server:
                drv = S7Driver({"id": "s", "host": "127.0.0.1", "port": port, "timeout": 1})
                ok, msg = await drv.write("DB1.DBW2", -1)
                assert ok, msg
                ok, msg = await drv.write("MW4", 70000)
                assert not ok and "does not fit" in msg
                ok, msg = await drv.write("DB1.DBD8:REAL", 2.5)
                assert ok, msg
                assert fake.byte_writes == [(2, b"\xff\xff"), (8, struct.pack(">f", 2.5))]
                await drv.disconnect()
        asyncio.run(run())


# ── EtherNet/IP ───────────────────────────────────────────────────────────────

class FakeLogix:
    TYPES = {b"Reject": 0x00C1, b"Count": 0x00C4, b"Speed": 0x00CA}

    def __init__(self):
        self.values = {b"Reject": b"\x00", b"Count": b"\x00" * 4, b"Speed": b"\x00" * 4}
        self.routed = []

    @staticmethod
    def _tag(path):
        return path[2:2 + path[1]]

    def _service(self, cip):
        service, words = cip[0], cip[1]
        path, data = cip[2:2 + words * 2], cip[2 + words * 2:]
        if service == 0x52:
            size = struct.unpack_from("<H", data, 2)[0]
            embedded = data[4:4 + size]
            route_at = 4 + size + (size % 2)
            self.routed.append(data[route_at + 2:route_at + 2 + data[route_at] * 2])
            return self._service(embedded)
        tag = self._tag(path)
        if tag not in self.TYPES:
            return bytes([service | 0x80, 0, 0x05, 0])
        type_code = self.TYPES[tag]
        if service == 0x4C:
            return bytes([0xCC, 0, 0, 0]) + struct.pack("<H", type_code) + self.values[tag]
        if struct.unpack_from("<H", data, 0)[0] != type_code:
            return bytes([0xCD, 0, 0xFF, 1, 0x07, 0x21])
        self.values[tag] = data[4:]
        return bytes([0xCD, 0, 0, 0])

    async def handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(24)
                command, length, session = struct.unpack_from("<HHI", header)
                body = await reader.readexactly(length)
                if command == 0x65:
                    writer.write(struct.pack("<HHIIQI", 0x65, 4, 0x1234, 0, 0, 0) + body)
                elif command == 0x6F:
                    assert session == 0x1234
                    cip = body[16:]
                    reply = self._service(cip)
                    cpf = struct.pack("<IHH", 0, 0, 2) + struct.pack("<HH", 0, 0) + struct.pack("<HH", 0xB2, len(reply)) + reply
                    writer.write(struct.pack("<HHIIQI", 0x6F, len(cpf), session, 0, 0, 0) + cpf)
                else:
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


class TestEtherNetIP:
    def test_tag_path_members_and_indices(self):
        path = _encode_tag_path("Line1.Stn[2].Reject")
        assert path == b"\x91\x05Line1\x00\x91\x03Stn\x00\x28\x02\x91\x06Reject"
        assert _encode_route_path("1,0") == b"\x01\x00"
        with pytest.raises(ValueError):
            _encode_tag_path("Word.5")

    def test_bool_and_typed_writes_routed_to_slot(self):
        async def run():
            fake = FakeLogix()
            server, port = await _serve(fake.handle)
            async with server:
                drv = EtherNetIPDriver({"id": "e", "host": "127.0.0.1", "port": port, "timeout": 1,
                                        "eip_path": "1,0", "eip_slot": 2})
                ok, msg = await drv.set("Reject")
                assert ok, msg
                assert fake.values[b"Reject"] == b"\xff"
                ok, msg = await drv.write("Count", 5)       # DINT tag with a small value
                assert ok, msg
                assert fake.values[b"Count"] == struct.pack("<i", 5)
                ok, msg = await drv.write("Speed", 12.5)
                assert ok and fake.values[b"Speed"] == struct.pack("<f", 12.5)
                ok, msg = await drv.set("Count")
                assert not ok and "not BOOL" in msg
                ok, msg = await drv.set("Missing")
                assert not ok and "0x05" in msg
                assert fake.routed and all(route == b"\x01\x02" for route in fake.routed)
                await drv.disconnect()
        asyncio.run(run())


# ── MELSEC SLMP ───────────────────────────────────────────────────────────────

class FakeMelsec:
    def __init__(self):
        self.writes = []
        self.bits = {}

    async def handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(9)
                length = struct.unpack_from("<H", header, 7)[0]
                body = await reader.readexactly(length)
                _, command, sub = struct.unpack_from("<HHH", body, 0)
                number = int.from_bytes(body[6:9], "little")
                code = body[9]
                data = b""
                if command == 0x1401:
                    self.writes.append((code, number, sub, body[12:]))
                    if sub == 1:
                        self.bits[(code, number)] = bool(body[12] & 0x10)
                elif command == 0x0401:
                    data = b"\x10" if self.bits.get((code, number)) else b"\x00"
                reply = struct.pack("<H", 0) + data
                writer.write(b"\xD0\x00\x00\xFF\xFF\x03\x00" + struct.pack("<H", len(reply)) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


class TestMelsec:
    def test_address_parsing(self):
        assert _parse_melsec_address("Y1F")["number"] == 0x1F
        assert _parse_melsec_address("Y17", xy_octal=True)["number"] == 15
        assert _parse_melsec_address("SM400")["device"] == "SM"
        assert _parse_melsec_address("D100:DINT")["type"] == "DINT"
        with pytest.raises(ValueError):
            _parse_melsec_address("Q0")

    def test_bit_and_word_writes(self):
        async def run():
            fake = FakeMelsec()
            server, port = await _serve(fake.handle)
            async with server:
                drv = MelsecSLMPDriver({"id": "mc", "host": "127.0.0.1", "port": port, "timeout": 1})
                ok, msg = await drv.pulse("Y10", 5)
                assert ok, msg
                ok, msg = await drv.toggle("M5")
                assert ok, msg
                ok, msg = await drv.write("D100:DINT", 70000)
                assert ok, msg
                assert fake.writes[0] == (0x9D, 0x10, 1, b"\x10")
                assert fake.writes[1] == (0x9D, 0x10, 1, b"\x00")
                assert fake.writes[2] == (0x90, 5, 1, b"\x10")
                assert fake.writes[3] == (0xA8, 100, 0, struct.pack("<i", 70000))
                ok, msg = await drv.set("D1")
                assert not ok and "word device" in msg
                await drv.disconnect()
        asyncio.run(run())


# ── Omron FINS ────────────────────────────────────────────────────────────────

class FakeFins:
    def __init__(self):
        self.writes = []

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
                    area, (word, bit, _count) = fins[12], struct.unpack(">HBH", fins[13:18])
                    data = b""
                    if fins[11] == 0x02:
                        self.writes.append((fins[4], area, word, bit, fins[18:]))
                    else:
                        data = b"\x01"
                    reply = bytes((0xC0, 0, 0x02, 0, 0x22, 0, 0, 0x01, 0, fins[9], fins[10], fins[11], 0, 0)) + data
                    writer.write(b"FINS" + struct.pack(">III", 8 + len(reply), 2, 0) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


class TestFins:
    def test_address_parsing(self):
        assert _parse_fins_address("CIO 0.05")["code"] == 0x30
        assert _parse_fins_address("W10.01")["word"] == 10
        assert _parse_fins_address("D100")["code"] == 0x82
        assert _parse_fins_address("D100.03")["code"] == 0x02
        with pytest.raises(ValueError):
            _parse_fins_address("CIO 1.16")

    def test_handshake_and_writes(self):
        async def run():
            fake = FakeFins()
            server, port = await _serve(fake.handle)
            async with server:
                drv = OmronFINSDriver({"id": "f", "host": "127.0.0.1", "port": port, "timeout": 1})
                assert await drv.connect()
                assert drv.server_node == 1 and drv.client_node == 0x22
                ok, msg = await drv.set("CIO 0.05")
                assert ok, msg
                ok, msg = await drv.toggle("W1.00")        # fake reads it as ON
                assert ok and "ON → OFF" in msg
                ok, msg = await drv.write("D100:REAL", 1.0)
                assert ok, msg
                real = struct.pack("<f", 1.0)
                expected_words = struct.pack(">HH", *struct.unpack("<HH", real))
                assert fake.writes == [
                    (1, 0x30, 0, 5, b"\x01"),
                    (1, 0x31, 1, 0, b"\x00"),
                    (1, 0x82, 100, 0, expected_words),
                ]
                await drv.disconnect()
        asyncio.run(run())


# ── Generic TCP ───────────────────────────────────────────────────────────────

class TestGenericTCP:
    def test_hex_template_with_wide_fields_is_binary(self):
        drv = GenericTCPDriver({"id": "g", "host": "x", "port": 1})
        assert drv._render_frame("01 05 {addr:04X} FF 00 {crc}", "10")[:6] == bytes.fromhex("0105000AFF00")

    def test_ascii_template_accepts_text_address(self):
        drv = GenericTCPDriver({"id": "g", "host": "x", "port": 1, "ascii_terminator": "crlf"})
        assert drv._render_frame("SET {address}", "Gate_A") == b"SET Gate_A\r\n"
        assert drv._render_frame("{addr} {value}", "10", 123) == b"10 123\r\n"

    def test_reply_match_and_reconnect_after_peer_close(self):
        async def run():
            connections = []

            async def handle(reader, writer):
                connections.append(writer)
                try:
                    while True:
                        line = await reader.readline()
                        if not line:
                            return
                        writer.write(b"OK\n" if line.startswith(b"SET") else b"ERR\n")
                        await writer.drain()
                except ConnectionError:
                    pass

            server, port = await _serve(handle)
            async with server:
                drv = GenericTCPDriver({"id": "g", "host": "127.0.0.1", "port": port, "timeout": 1,
                                        "set_template": "SET {address}", "reset_template": "RST {address}",
                                        "response_mode": "match", "response_match": "OK"})
                assert await drv.connect()
                ok, msg = await drv.set("A")
                assert ok, msg
                ok, msg = await drv.reset("A")
                assert not ok and "did not match" in msg
                connections[0].close()
                await asyncio.sleep(0.05)
                ok, msg = await drv.set("A")
                assert ok, msg
                assert len(connections) == 2
                await drv.disconnect()
        asyncio.run(run())


# ── OPC UA / factory ──────────────────────────────────────────────────────────

class TestOpcuaSecurity:
    def test_secured_mode_without_certificate_explains_what_is_missing(self):
        async def run():
            drv = OPCUADriver({"id": "o", "host": "127.0.0.1", "port": 1, "timeout": 1,
                               "opcua_security": "Basic256Sha256_SignAndEncrypt"})
            assert await drv.connect() is False
            assert "opcua_cert_path" in drv.last_error
        asyncio.run(run())


class TestFactory:
    @pytest.mark.parametrize("sub, cls", [("melsec", MelsecSLMPDriver), ("fins", OmronFINSDriver)])
    def test_native_drivers_for_melsec_and_fins(self, sub, cls):
        drv = PLCDriverFactory.get_driver({"id": f"ep-{sub}", "plc_sub_protocol": sub, "host": "127.0.0.1"}, fresh=True)
        assert isinstance(drv, cls)
        assert drv.port == (5007 if sub == "melsec" else 9600)


# ── Dispatcher ────────────────────────────────────────────────────────────────

class TestDispatcherHardening:
    def test_event_id_history_evicts_oldest_not_newest(self):
        from app.services.plc_dispatcher_service import PLCDispatcherService

        async def run():
            PLCDispatcherService.set_cards([{
                "id": "dedup", "enabled": True, "trigger_type": "line_cross", "trigger_condition": "any",
                "rearm_lockout_ms": 0, "execution_policy": "once_per_event", "plc_endpoint_id": "",
            }])
            state = PLCDispatcherService._states["dedup"]
            state.fired_event_ids.clear()
            for index in range(501):
                await PLCDispatcherService.evaluate({"event_id": f"e{index}"})
            await asyncio.gather(*PLCDispatcherService._dispatch_tasks, return_exceptions=True)
            assert "e500" in state.fired_event_ids
            assert "e0" not in state.fired_event_ids
            assert len(state.fired_event_ids) == 500

        asyncio.run(run())

    def test_bad_address_is_not_retried(self, monkeypatch):
        from app.services import plc_dispatcher_service as module
        from app.services.settings_persistence_service import SettingsPersistenceService

        endpoint = {"id": "ep-bad", "protocol": "plc", "enabled": True, "host": "127.0.0.1", "port": 1,
                    "timeout": 1, "plc_sub_protocol": "modbus_tcp", "simulation_mode": True}
        monkeypatch.setattr(SettingsPersistenceService, "get_endpoints", classmethod(lambda cls, protocol=None: [endpoint]))
        PLCDriverFactory.invalidate("ep-bad")
        card = {"id": "bad", "name": "Bad", "plc_endpoint_id": "ep-bad", "operation": "SET",
                "target_address": "30001", "on_failure": "retry", "retry_attempts": 3, "retry_delay_ms": 0}
        result = asyncio.run(module.PLCDispatcherService.dispatch_manual(card))
        assert result["success"] is False
        assert result["attempts"] == 1
        assert "read-only" in result["message"]
        assert result["status"] == "failed"
        PLCDriverFactory.invalidate("ep-bad")


# ── Modbus RTU ────────────────────────────────────────────────────────────────

class FakeRTUBus:
    """RS-485 bus stand-in: several slaves behind one byte stream, RTU framing with CRC."""

    def __init__(self, silent_units=(), corrupt_crc=False):
        self.registers = {1: {}, 2: {}}
        self.coils = {1: {}, 2: {}}
        self.connections = 0
        self.silent_units = set(silent_units)
        self.corrupt_crc = corrupt_crc

    async def handle(self, reader, writer):
        from app.hardware.modbus.rtu_client import crc16_modbus

        self.connections += 1
        try:
            while True:
                head = await reader.readexactly(2)
                unit, fn = head
                if fn in (0x0F, 0x10):
                    fixed = await reader.readexactly(5)
                    rest = fixed + await reader.readexactly(fixed[4] + 2)
                else:
                    rest = await reader.readexactly(6)
                frame = head + rest
                assert crc16_modbus(frame[:-2]) == frame[-2:], "master sent a bad CRC"
                if unit in self.silent_units or unit not in self.registers:
                    continue
                addr, value = struct.unpack(">HH", rest[:4])
                if fn == 0x05:
                    self.coils[unit][addr] = value == 0xFF00
                    reply = frame[:-2]
                elif fn == 0x06:
                    self.registers[unit][addr] = value
                    reply = frame[:-2]
                elif fn == 0x10:
                    for i in range(value):
                        self.registers[unit][addr + i] = struct.unpack(">H", rest[5 + i * 2:7 + i * 2])[0]
                    reply = frame[:6]
                elif fn == 0x03:
                    if addr >= 100:
                        reply = bytes((unit, 0x83, 0x02))
                    else:
                        words = b"".join(struct.pack(">H", self.registers[unit].get(addr + i, 0)) for i in range(value))
                        reply = bytes((unit, 0x03, len(words))) + words
                elif fn == 0x01:
                    reply = bytes((unit, 0x01, 1, 1 if self.coils[unit].get(addr) else 0))
                else:
                    reply = bytes((unit, fn | 0x80, 0x01))
                crc = crc16_modbus(reply)
                if self.corrupt_crc:
                    crc = bytes((crc[0] ^ 0xFF, crc[1]))
                writer.write(reply + crc)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


def _rtu_endpoint(port, **extra):
    endpoint = {"id": f"rtu{extra.get('modbus_unit_id', 1)}", "plc_sub_protocol": "modbus_rtu",
                "rtu_transport": "serial", "serial_port": f"socket://127.0.0.1:{port}",
                "serial_baudrate": 19200, "serial_parity": "E", "timeout": 1}
    endpoint.update(extra)
    return endpoint


class TestModbusRTU:
    def test_crc_matches_reference_frame(self):
        from app.hardware.modbus.rtu_client import crc16_modbus
        # Read 1 holding register at 0 from unit 1: 01 03 00 00 00 01 84 0A
        assert crc16_modbus(bytes.fromhex("010300000001")) == bytes.fromhex("840A")

    def test_serial_port_writes_and_32bit_values(self):
        from app.hardware.plc.modbus_driver import ModbusRTUDriver

        async def run():
            bus = FakeRTUBus()
            server, port = await _serve(bus.handle)
            async with server:
                drv = PLCDriverFactory.get_driver(_rtu_endpoint(port), fresh=True)
                assert isinstance(drv, ModbusRTUDriver)
                assert await drv.connect()
                ok, msg = await drv.pulse("C3", 5)
                assert ok, msg
                ok, msg = await drv.write("40011:REAL", 2.5)
                assert ok, msg
                high, low = bus.registers[1][10], bus.registers[1][11]
                assert struct.unpack(">f", struct.pack(">HH", high, low))[0] == 2.5
                ok, msg = await drv.toggle("C3")
                assert ok and "OFF → ON" in msg
                ok, msg = await drv.probe()
                assert ok, msg
                await drv.disconnect()
        asyncio.run(run())

    def test_two_units_share_one_serial_port(self):
        async def run():
            bus = FakeRTUBus()
            server, port = await _serve(bus.handle)
            async with server:
                first = PLCDriverFactory.get_driver(_rtu_endpoint(port, modbus_unit_id=1), fresh=True)
                second = PLCDriverFactory.get_driver(_rtu_endpoint(port, modbus_unit_id=2), fresh=True)
                assert await first.connect() and await second.connect()
                results = await asyncio.gather(first.write("R0", 11), second.write("R0", 22))
                assert all(ok for ok, _ in results), results
                assert bus.registers[1][0] == 11 and bus.registers[2][0] == 22
                assert bus.connections == 1          # one port handle, requests serialised
                mismatched = PLCDriverFactory.get_driver(
                    _rtu_endpoint(port, modbus_unit_id=3, serial_baudrate=9600), fresh=True)
                assert await mismatched.connect() is False
                await first.disconnect()
                await second.disconnect()
        asyncio.run(run())

    def test_silent_unit_and_bad_crc_are_reported(self):
        async def run():
            bus = FakeRTUBus(silent_units={1})
            server, port = await _serve(bus.handle)
            async with server:
                drv = PLCDriverFactory.get_driver(_rtu_endpoint(port, timeout=0.3), fresh=True)
                await drv.connect()
                ok, msg = await drv.set("C1")
                assert not ok and "no reply from Modbus unit 1" in msg
                ok, msg = await drv.probe()
                assert not ok
                await drv.disconnect()
            noisy = FakeRTUBus(corrupt_crc=True)
            server, port = await _serve(noisy.handle)
            async with server:
                drv = PLCDriverFactory.get_driver(_rtu_endpoint(port), fresh=True)
                await drv.connect()
                ok, msg = await drv.set("C1")
                assert not ok and "CRC" in msg
                await drv.disconnect()
        asyncio.run(run())

    def test_rtu_over_tcp_and_exception_reply(self):
        async def run():
            bus = FakeRTUBus()
            server, port = await _serve(bus.handle)
            async with server:
                drv = PLCDriverFactory.get_driver({"id": "gw", "plc_sub_protocol": "modbus_rtu", "rtu_transport": "tcp",
                                                   "host": "127.0.0.1", "port": port, "timeout": 1,
                                                   "modbus_unit_id": 2}, fresh=True)
                assert await drv.connect()
                ok, msg = await drv.write("R5:DINT", -3)
                assert ok, msg
                assert (bus.registers[2][5], bus.registers[2][6]) == (0xFFFF, 0xFFFD)
                ok, values, err = await drv._client.read_registers(100, 1)
                assert not ok and "illegal data address" in err
                assert drv.is_connected                 # an exception reply is a healthy link
                await drv.disconnect()
        asyncio.run(run())


class TestModbusRTUSettings:
    @pytest.fixture
    def persistence(self, monkeypatch, tmp_path):
        import copy

        import app.services.settings_persistence_service as module
        from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService

        monkeypatch.setattr(module, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(module, "STATE_FILE", str(tmp_path / "system_state.json"))
        monkeypatch.setattr(SettingsPersistenceService, "_state", copy.deepcopy(DEFAULT_STATE))
        monkeypatch.setattr(SettingsPersistenceService, "_recent_changes", [])
        return SettingsPersistenceService

    def test_serial_channel_needs_port_not_host_and_test_button_probes_device(self, persistence):
        async def run():
            with pytest.raises(ValueError, match="Serial port is required"):
                await persistence.add_or_update_endpoint({"protocol": "plc", "name": "RS485", "plc_protocol": "modbus_rtu"})
            bus = FakeRTUBus()
            server, port = await _serve(bus.handle)
            async with server:
                saved = await persistence.add_or_update_endpoint({
                    "protocol": "plc", "name": "RS485", "plc_protocol": "modbus_rtu",
                    "serial_port": f"socket://127.0.0.1:{port}", "timeout": 1,
                })
                assert saved["serial_baudrate"] == 19200 and saved["serial_parity"] == "E"
                assert saved["rtu_transport"] == "serial" and saved["host"] == ""
                result = await persistence.test_endpoint(saved["id"])
                assert result["success"] is True, result
                assert "answered" in result["message"]
            with pytest.raises(ValueError, match="baud rate"):
                await persistence.add_or_update_endpoint({"protocol": "plc", "name": "Bad", "plc_protocol": "modbus_rtu",
                                                          "serial_port": "COM3", "serial_baudrate": 12345})
        asyncio.run(run())
