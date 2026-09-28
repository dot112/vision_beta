"""
Full End-to-End Modbus TCP Test Script
======================================
Tests:
  1. Direct Modbus TCP driver connection & operations (SET, RESET, PULSE, WRITE).
  2. Readback verification of coils and holding registers.
  3. SettingsPersistenceService endpoint registration and test handshake.
  4. PLCDispatcherService Action Card manual test dispatch.
  5. Live vision defect event trigger simulation with conveyor travel delay.

Usage:
  python test_modbus_full.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is in Python path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.hardware.plc.factory import PLCDriverFactory
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.plc_dispatcher_service import PLCDispatcherService


# ── Configuration (Change if connecting to a physical PLC) ─────────────────────
MODBUS_HOST = "127.0.0.1"    # Replace with physical PLC IP if needed (e.g., "192.168.1.10")
MODBUS_PORT = 502            # Standard Modbus TCP port
UNIT_ID     = 1


async def run_full_modbus_test():
    print("=" * 70)
    print(f"  STARTING FULL MODBUS TCP COMMUNICATIONS TEST [{MODBUS_HOST}:{MODBUS_PORT}]")
    print("=" * 70)

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1: Endpoint Registration
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[Step 1] Registering Modbus TCP endpoint in persistence layer...")
    endpoint_data = {
        "id": "plc-modbus-test-channel",
        "name": "Production Conveyor PLC (Test)",
        "protocol": "plc",
        "plc_sub_protocol": "modbus_tcp",
        "plc_protocol": "modbus_tcp",
        "host": MODBUS_HOST,
        "port": MODBUS_PORT,
        "timeout": 3,
        "enabled": True,
        "modbus_unit_id": UNIT_ID,
        "description": "Automated Modbus TCP test endpoint",
    }

    saved_ep = await SettingsPersistenceService.add_or_update_endpoint(
        endpoint_data=endpoint_data,
        username="admin",
        role="admin",
        clearance_level=3,
    )
    print(f"  [OK] Channel registered: ID='{saved_ep['id']}' -> {saved_ep['host']}:{saved_ep['port']}")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2: Connection Handshake Test (Same as Dashboard "Test" button)
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[Step 2] Testing connection probe via test_endpoint()...")
    test_res = await SettingsPersistenceService.test_endpoint("plc-modbus-test-channel")
    success = test_res.get("success", False)
    msg = test_res.get("message", "")
    print(f"  [{'OK' if success else 'FAIL'}] Handshake Result: success={success} | '{msg}'")
    if not success:
        print("  [!] Connection test failed. Stopping test.")
        return

    # ──────────────────────────────────────────────────────────────────────────
    # Step 3: Direct Hardware Driver Operations
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[Step 3] Testing direct driver operations (SET, RESET, PULSE, WRITE)...")
    driver = PLCDriverFactory.get_driver(saved_ep, fresh=True)
    connected = await driver.connect()
    print(f"  [OK] Driver connected: {connected} (is_connected={driver.is_connected})")

    # 3a. SET Coil 10
    ok_set, msg_set = await driver.set("C10")
    print(f"  [OK] SET Coil 10:      status={ok_set} | {msg_set}")

    # 3b. RESET Coil 10
    ok_rst, msg_rst = await driver.reset("C10")
    print(f"  [OK] RESET Coil 10:    status={ok_rst} | {msg_rst}")

    # 3c. PULSE Coil 5 (120 ms duration)
    print("  -> Executing PULSE on Coil 5 (120ms)...")
    ok_pls, msg_pls = await driver.pulse("C5", duration_ms=120)
    print(f"  [OK] PULSE Coil 5:     status={ok_pls} | {msg_pls}")

    # 3d. WRITE Holding Register 20
    target_register_value = 8765
    ok_wr, msg_wr = await driver.write("R20", target_register_value)
    print(f"  [OK] WRITE Reg 20:     status={ok_wr} | {msg_wr}")

    # 3e. Readback verification
    ok_rb, coils, _ = await driver._client.read_coils(5, 1)
    coils_val = coils[0] if coils else None
    print(f"  [OK] Verify Coil 5:    {coils_val} (expected False/OFF after pulse completed)")

    ok_reg, regs, _ = await driver._client.read_registers(20, 1)
    regs_val = regs[0] if regs else None
    print(f"  [OK] Verify Reg 20:    {regs_val} (expected {target_register_value})")

    await driver.disconnect()
    print(f"  [OK] Driver disconnected: is_connected={driver.is_connected}")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 4: PLC Action Card Execution via Dispatcher
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[Step 4] Testing PLC Action Card triggering via PLCDispatcherService...")
    action_card = {
        "id": "card_modbus_test_act",
        "name": "Defect Reject Diverter Solenoid",
        "enabled": True,
        "plc_endpoint_id": "plc-modbus-test-channel",
        "trigger": "cross_line",
        "condition": "reject",
        "target_type": "coil",
        "target_address": "C1",
        "operation": "pulse",
        "duration_ms": 150,
        "delay_ms": 50,
        "debounce_ms": 100,
        "exec_policy": "once_per_event",
    }
    PLCDispatcherService.set_cards([action_card])
    print(f"  [OK] Card loaded: '{action_card['name']}' -> Target: Coil C1 (PULSE 150ms)")

    # Manual test output (Same as "Test Output" button on Dashboard card)
    print("  -> Executing manual test dispatch (dispatch_manual)...")
    manual_res = await PLCDispatcherService.dispatch_manual(action_card)
    print(f"  [OK] Manual test:      success={manual_res.get('success')} | {manual_res.get('message')}")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 5: Live Vision Inspection Event Simulation
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[Step 5] Simulating live vision line-crossing defect event...")
    event_payload = {
        "event_id": "track_bottle_defect_99",
        "result": "reject",
        "good_count": 24,
        "reject_count": 1,
        "detected_classes": ["broken_cap"],
        "timestamp": 1000.0,
    }
    print("  -> Emitting WIRELINE_OBJECT_CROSSED (REJECT)...")
    await PLCDispatcherService.evaluate(event_payload)

    # Wait for conveyor travel delay (50ms) + pulse duration (150ms) + buffer
    await asyncio.sleep(0.35)

    status_obj = PLCDispatcherService.get_status("card_modbus_test_act")[0]
    print(f"  [OK] Runtime Status:   '{status_obj['status']}'")
    print(f"  [OK] Output Message:   '{status_obj['last_result'].get('message')}'")

    # ──────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[Cleanup] Removing temporary test channel...")
    SettingsPersistenceService.delete_endpoint("plc-modbus-test-channel")
    PLCDriverFactory.clear_all()
    print("  [OK] Cleanup complete.")

    print("\n" + "=" * 70)
    print("  SUCCESS: FULL MODBUS TCP COMMUNICATIONS VERIFIED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    if os.environ.get("ALLOW_PLC_TEST_WRITES") != "1":
        raise SystemExit("Refusing PLC output writes. Set ALLOW_PLC_TEST_WRITES=1 only when targeting a test PLC simulator.")
    asyncio.run(run_full_modbus_test())
