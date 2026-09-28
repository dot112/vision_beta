"""End-to-end integration test for Modbus TCP on port 502."""
from __future__ import annotations

import asyncio
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.hardware.plc.factory import PLCDriverFactory
from app.services.settings_persistence_service import SettingsPersistenceService
from app.services.plc_dispatcher_service import PLCDispatcherService


async def main():
    print("=" * 65)
    print("  RUNNING MODBUS TCP PORT 502 COMPREHENSIVE COMMUNICATIONS TEST")
    print("=" * 65)

    # 1. Define Modbus TCP Endpoint on Port 502
    endpoint_data = {
        "id": "plc-modbus-test-502",
        "name": "Line 1 Main Rejector PLC",
        "protocol": "plc",
        "plc_sub_protocol": "modbus_tcp",
        "plc_protocol": "modbus_tcp",
        "host": "127.0.0.1",
        "port": 502,
        "timeout": 3,
        "enabled": True,
        "modbus_unit_id": 1,
        "description": "Test Modbus TCP controller on port 502",
    }

    print("\n[Step 1] Registering Modbus TCP endpoint on 127.0.0.1:502...")
    saved_ep = await SettingsPersistenceService.add_or_update_endpoint(
        endpoint_data=endpoint_data,
        username="admin",
        role="admin",
        clearance_level=3,
    )
    print(f" -> Endpoint registered: ID={saved_ep['id']} ({saved_ep['host']}:{saved_ep['port']})")

    # 2. Test Connection Handshake via SettingsPersistenceService
    print("\n[Step 2] Testing endpoint connection handshake via test_endpoint()...")
    test_result = await SettingsPersistenceService.test_endpoint("plc-modbus-test-502")
    print(f" -> Result: success={test_result.get('success')}, message='{test_result.get('message')}'")
    assert test_result.get("success") is True, "Connection test failed!"

    # 3. Direct Driver Hardware Operations
    print("\n[Step 3] Testing direct driver operations (SET, RESET, PULSE, WRITE)...")
    driver = PLCDriverFactory.get_driver(saved_ep, fresh=True)
    conn_ok = await driver.connect()
    print(f" -> Connect status: {conn_ok} (is_connected={driver.is_connected})")
    assert conn_ok is True

    # Test SET coil 10
    ok_set, msg_set = await driver.set("C10")
    print(f" -> SET Coil 10: {ok_set} | {msg_set}")
    assert ok_set is True

    # Test RESET coil 10
    ok_rst, msg_rst = await driver.reset("C10")
    print(f" -> RESET Coil 10: {ok_rst} | {msg_rst}")
    assert ok_rst is True

    # Test PULSE coil 5 (100ms)
    print(" -> Executing PULSE Coil 5 for 100ms...")
    ok_pls, msg_pls = await driver.pulse("C5", duration_ms=100)
    print(f" -> PULSE Coil 5: {ok_pls} | {msg_pls}")
    assert ok_pls is True

    # Test WRITE Holding Register 20
    ok_wr, msg_wr = await driver.write("R20", 4321)
    print(f" -> WRITE Register 20: {ok_wr} | {msg_wr}")
    assert ok_wr is True

    # Read back to verify in-memory client
    ok_rb, coils, _ = await driver._client.read_coils(5, 1)
    print(f" -> Verify Coil 5 state after pulse: {coils[0] if coils else 'None'} (expected False/OFF)")
    ok_reg, regs, _ = await driver._client.read_registers(20, 1)
    print(f" -> Verify Register 20 value: {regs[0] if regs else 'None'} (expected 4321)")
    assert regs[0] == 4321

    await driver.disconnect()
    print(f" -> Driver disconnected: is_connected={driver.is_connected}")

    # 4. Test PLC Dispatcher Service with Action Card
    print("\n[Step 4] Testing PLC Action Card triggering through PLCDispatcherService...")
    action_card = {
        "id": "card_modbus_kicker_502",
        "name": "Defect Reject Diverter",
        "enabled": True,
        "plc_endpoint_id": "plc-modbus-test-502",
        "trigger": "cross_line",
        "condition": "reject",
        "target_type": "coil",
        "target_address": "C1",
        "operation": "pulse",
        "duration_ms": 100,
        "delay_ms": 50,
        "debounce_ms": 50,
        "exec_policy": "once_per_event",
    }
    PLCDispatcherService.set_cards([action_card])
    print(f" -> Card loaded: '{action_card['name']}' targeting endpoint 'plc-modbus-test-502' (Coil C1, PULSE 100ms)")

    # Execute manual test
    print(" -> Firing manual test via dispatch_manual()...")
    manual_res = await PLCDispatcherService.dispatch_manual(action_card)
    print(f" -> Manual Test Result: success={manual_res.get('success')}, status='{manual_res.get('message')}'")
    assert manual_res.get("success") is True

    # Check live status
    statuses = PLCDispatcherService.get_status("card_modbus_kicker_502")
    print(f" -> Card Live Status: {statuses[0]['status']}, last_result: {statuses[0]['last_result']['message']}")

    # 5. Simulate Vision Inspection Line-Crossing Event
    print("\n[Step 5] Simulating live vision inspection event (WIRELINE_OBJECT_CROSSED: REJECT)...")
    event = {
        "event_id": "track_42_defect",
        "result": "reject",
        "good_count": 15,
        "reject_count": 3,
        "detected_classes": ["bottle_defect"],
        "timestamp": 1234567.89,
    }
    await PLCDispatcherService.evaluate(event)
    # Wait for travel delay (50ms) + pulse (100ms) + buffer
    await asyncio.sleep(0.3)

    status_after_event = PLCDispatcherService.get_status("card_modbus_kicker_502")[0]
    print(f" -> Post-vision trigger status: {status_after_event['status']}")
    print(f" -> Post-vision trigger message: {status_after_event['last_result']['message']}")
    assert status_after_event['last_result']['success'] is True

    print("\n" + "=" * 65)
    print("  ALL TESTS PASSED: MODBUS TCP PORT 502 WORKING 100% PERFECTLY!")
    print("=" * 65)

if __name__ == "__main__":
    if os.environ.get("ALLOW_PLC_TEST_WRITES") != "1":
        raise SystemExit("Refusing PLC output writes. Set ALLOW_PLC_TEST_WRITES=1 only when targeting a test PLC simulator.")
    asyncio.run(main())
