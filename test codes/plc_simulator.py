"""
Industrial PLC Modbus TCP Simulator
===================================
A standalone interactive PLC simulator for testing vision system communications.
Listens on Port 502 (or custom port) and displays all live commands sent by the
FastAPI Industrial Vision dashboard or camera line-crossing triggers in real time.

Usage:
  python plc_simulator.py
  python plc_simulator.py --port 502
  python plc_simulator.py --port 8502
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import functools
import os
import struct
import sys

# Redefine print to always flush immediately for real-time console streaming
print = functools.partial(print, flush=True)

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ANSI Color formatting
class Colors:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    BG_GREEN = "\033[42m\033[30m"
    BG_RED   = "\033[41m\033[37m"
    BG_BLUE  = "\033[44m\033[37m"


def now_str() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class PLCSimulator:
    def __init__(self, host: str = "127.0.0.1", port: int = 502):
        self.host = host
        self.port = port
        self.coils = [False] * 512
        self.registers = [0] * 512
        self.total_connections = 0
        self.total_commands = 0
        self.last_pulse_times: dict[int, float] = {}

    def start_banner(self):
        print(Colors.CYAN + "=" * 76 + Colors.RESET)
        print(Colors.BOLD + Colors.CYAN + "      INDUSTRIAL PLC SIMULATOR (MODBUS TCP / SLMP / GENERIC TCP)" + Colors.RESET)
        print(Colors.CYAN + "=" * 76 + Colors.RESET)
        print(f" {Colors.GREEN}●{Colors.RESET} Listening on:        {Colors.BOLD}{self.host}:{self.port}{Colors.RESET}")
        print(f" {Colors.GREEN}●{Colors.RESET} Supported Protocols: Modbus TCP (MBAP), Raw Socket / ASCII, Heartbeat")
        print(f" {Colors.GREEN}●{Colors.RESET} Status:              {Colors.BG_GREEN} READY & WAITING FOR DASHBOARD {Colors.RESET}")
        print(f" {Colors.GREEN}●{Colors.RESET} Instructions:        1. Open Dashboard -> Communications -> Add Channel")
        print(f"                       2. Set Protocol: Modbus TCP | Host: 127.0.0.1 | Port: {self.port}")
        print(f"                       3. Click 'Test' or '⚡ Test Output' on Action Cards")
        print(Colors.CYAN + "=" * 76 + Colors.RESET + "\n")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.total_connections += 1
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "unknown"
        peer_port = peer[1] if peer else 0
        conn_id = self.total_connections

        print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.BLUE}[CONNECT #{conn_id}]{Colors.RESET} Dashboard/Backend connected from {Colors.BOLD}{peer_ip}:{peer_port}{Colors.RESET}")

        try:
            while True:
                # Read data stream
                data = await reader.read(1024)
                if not data:
                    break

                self.total_commands += 1
                await self.process_packet(data, writer, peer_ip)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.RED}[ERROR #{conn_id}]{Colors.RESET} Client error: {exc}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.YELLOW}[DISCONNECT #{conn_id}]{Colors.RESET} Connection closed from {peer_ip}:{peer_port}\n")

    async def process_packet(self, data: bytes, writer: asyncio.StreamWriter, peer_ip: str):
        # 1. Check if standard Modbus TCP packet (>= 8 bytes, protocol ID == 0)
        if len(data) >= 8 and data[2] == 0x00 and data[3] == 0x00:
            await self.handle_modbus_packet(data, writer)
            return

        # 2. Check for generic text / JSON / heartbeat / ASCII ping
        text = data.decode("utf-8", errors="replace").strip()
        print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.MAGENTA}>>> RECEIVED RAW / GENERIC TCP FRAME <<<{Colors.RESET}")
        print(f"  {Colors.BOLD}Bytes:{Colors.RESET}   {len(data)} bytes -> {data.hex(' ')}")
        print(f"  {Colors.BOLD}Payload:{Colors.RESET} '{text}'")

        # Echo or send generic acknowledgment
        if "ping" in text.lower():
            writer.write(b'{"status": "pong", "plc": "ok"}\n')
        else:
            writer.write(b"ACK\n")
        await writer.drain()
        print(f"  {Colors.GREEN}✓ Replied: ACK{Colors.RESET}\n")

    async def handle_modbus_packet(self, data: bytes, writer: asyncio.StreamWriter):
        tx_id, proto_id, length, unit_id = struct.unpack(">HHHB", data[:7])
        fc = data[7]

        # ── FC 0x05: Write Single Coil ────────────────────────────────────────
        if fc == 0x05 and len(data) >= 12:
            coil_addr, raw_val = struct.unpack(">HH", data[8:12])
            is_on = (raw_val == 0xFF00)
            self.coils[coil_addr] = is_on

            # Visual State Badge
            if is_on:
                badge = f"{Colors.BG_GREEN} ON / HIGH (1) {Colors.RESET}"
                action_text = f"{Colors.GREEN}{Colors.BOLD}SOLENOID / ACTUATOR ENERGIZED{Colors.RESET}"
            else:
                badge = f"{Colors.BG_RED} OFF / LOW (0) {Colors.RESET}"
                action_text = f"{Colors.DIM}SOLENOID DE-ENERGIZED (END OF PULSE){Colors.RESET}"

            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.GREEN}{Colors.BOLD}>>> MODBUS TCP: WRITE SINGLE COIL (0x05) <<<{Colors.RESET}")
            print(f"  ├─ {Colors.BOLD}Target Output:{Colors.RESET}  {Colors.CYAN}Coil {coil_addr}{Colors.RESET} (C{coil_addr} / 0x{coil_addr:04X})")
            print(f"  ├─ {Colors.BOLD}Output State:{Colors.RESET}   {badge} -> {action_text}")
            print(f"  ├─ {Colors.BOLD}Unit ID:{Colors.RESET}        {unit_id} | Transaction ID: {tx_id}")
            print(f"  └─ {Colors.BOLD}Raw MBAP+PDU:{Colors.RESET}  {data[:12].hex(' ').upper()}")

            # Echo response (Standard Modbus FC 0x05 response is exact copy of 12 request bytes)
            writer.write(data[:12])
            await writer.drain()
            print(f"     {Colors.GREEN}↳ [ACK SENT TO DASHBOARD]{Colors.RESET}\n")
            return

        # ── FC 0x06: Write Single Holding Register ────────────────────────────
        elif fc == 0x06 and len(data) >= 12:
            reg_addr, reg_val = struct.unpack(">HH", data[8:12])
            self.registers[reg_addr] = reg_val

            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.YELLOW}{Colors.BOLD}>>> MODBUS TCP: WRITE HOLDING REGISTER (0x06) <<<{Colors.RESET}")
            print(f"  ├─ {Colors.BOLD}Target Register:{Colors.RESET} {Colors.CYAN}Register {reg_addr}{Colors.RESET} (R{reg_addr} / 4{reg_addr+1:04d})")
            print(f"  ├─ {Colors.BOLD}Stored Value:{Colors.RESET}    {Colors.BOLD}{reg_val}{Colors.RESET} (0x{reg_val:04X})")
            print(f"  ├─ {Colors.BOLD}Unit ID:{Colors.RESET}         {unit_id} | Transaction ID: {tx_id}")
            print(f"  └─ {Colors.BOLD}Raw MBAP+PDU:{Colors.RESET}   {data[:12].hex(' ').upper()}")

            # Echo response (Standard Modbus FC 0x06 response is exact copy of 12 request bytes)
            writer.write(data[:12])
            await writer.drain()
            print(f"     {Colors.GREEN}↳ [ACK SENT TO DASHBOARD]{Colors.RESET}\n")
            return

        # ── FC 0x01: Read Coils ───────────────────────────────────────────────
        elif fc == 0x01 and len(data) >= 12:
            start_addr, count = struct.unpack(">HH", data[8:12])
            byte_count = (count + 7) // 8
            coil_bytes = bytearray(byte_count)
            for i in range(count):
                idx = start_addr + i
                if idx < len(self.coils) and self.coils[idx]:
                    coil_bytes[i // 8] |= (1 << (i % 8))

            resp_pdu = struct.pack(">BB", 0x01, byte_count) + bytes(coil_bytes)
            resp_mbap = struct.pack(">HHHB", tx_id, 0x0000, len(resp_pdu) + 1, unit_id)
            writer.write(resp_mbap + resp_pdu)
            await writer.drain()
            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.CYAN}MODBUS TCP: READ COILS (0x01){Colors.RESET} Start={start_addr}, Count={count}")
            return

        # ── FC 0x03: Read Holding Registers ───────────────────────────────────
        elif fc == 0x03 and len(data) >= 12:
            start_addr, count = struct.unpack(">HH", data[8:12])
            byte_count = count * 2
            reg_bytes = bytearray()
            for i in range(count):
                idx = start_addr + i
                val = self.registers[idx] if idx < len(self.registers) else 0
                reg_bytes.extend(struct.pack(">H", val))

            resp_pdu = struct.pack(">BB", 0x03, byte_count) + bytes(reg_bytes)
            resp_mbap = struct.pack(">HHHB", tx_id, 0x0000, len(resp_pdu) + 1, unit_id)
            writer.write(resp_mbap + resp_pdu)
            await writer.drain()
            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.CYAN}MODBUS TCP: READ REGISTERS (0x03){Colors.RESET} Start={start_addr}, Count={count}")
            return

        # ── Other / Unsupported FC ────────────────────────────────────────────
        else:
            print(f"{Colors.DIM}[{now_str()}]{Colors.RESET} {Colors.YELLOW}MODBUS TCP: Function Code 0x{fc:02X}{Colors.RESET} (Length: {len(data)})")
            # Return Modbus Exception 0x01 (Illegal Function)
            err_pdu = struct.pack(">BB", fc | 0x80, 0x01)
            err_mbap = struct.pack(">HHHB", tx_id, 0x0000, len(err_pdu) + 1, unit_id)
            writer.write(err_mbap + err_pdu)
            await writer.drain()


async def main():
    parser = argparse.ArgumentParser(description="Industrial Vision PLC Modbus TCP Simulator")
    parser.add_argument("--port", type=int, default=502, help="Port to listen on (default: 502)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface to bind (default: loopback only)")
    args = parser.parse_args()

    sim = PLCSimulator(host=args.host, port=args.port)
    sim.start_banner()

    try:
        server = await asyncio.start_server(sim.handle_client, sim.host, sim.port)
    except PermissionError:
        print(f"{Colors.RED}[ERROR] Permission denied opening port {sim.port}. Try running with Administrator privileges or use --port 8502{Colors.RESET}")
        return
    except OSError as err:
        print(f"{Colors.RED}[ERROR] Could not bind to port {sim.port}: {err}{Colors.RESET}")
        return

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}PLC Simulator stopped by user.{Colors.RESET}")
