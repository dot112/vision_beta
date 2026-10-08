from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.core import security
from app.services.vision_service import ContinuousVisionRunner
from app.services.settings_persistence_service import SettingsPersistenceService


def test_session_duration_is_2_days():
    assert settings.SESSION_DURATION_DAYS == 2
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 60 * 24 * 2
    assert settings.REFRESH_TOKEN_EXPIRE_DAYS == 2


def test_token_valid_under_current_boot_id():
    token = security.create_access_token("operator_test", 1, "operator")
    payload = security.decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "operator_test"
    assert payload["clearance_level"] == 1
    assert payload["boot_id"] == security.SERVER_BOOT_ID


def test_token_invalidated_when_server_restarts():
    token = security.create_access_token("operator_test", 1, "operator")
    orig_boot_id = security.SERVER_BOOT_ID

    try:
        security.SERVER_BOOT_ID = "restarted_new_boot_id_12345"
        stale_payload = security.decode_access_token(token)
        assert stale_payload is None, "Stale token from prior boot should be rejected"
    finally:
        security.SERVER_BOOT_ID = orig_boot_id

    restored_payload = security.decode_access_token(token)
    assert restored_payload is not None


def test_continuous_vision_runner_lifecycle():
    assert not ContinuousVisionRunner._running
    ContinuousVisionRunner.start()
    assert ContinuousVisionRunner._running
    ContinuousVisionRunner.stop()
    assert not ContinuousVisionRunner._running


def test_settings_camera_auto_connect_persistence():
    state = SettingsPersistenceService.get_state()
    assert "camera_auto_connect" in state
    assert state["camera_auto_connect"] in (True, False)


def test_settings_camera_auto_connect_toggle_and_active_cam():
    # Toggle to False
    res = SettingsPersistenceService.update_settings({"camera_auto_connect": False})
    assert res["camera_auto_connect"] is False
    assert SettingsPersistenceService.get_state()["camera_auto_connect"] is False

    # Toggle to True with active camera id
    test_cam_id = "cam-test-unit-001"
    res2 = SettingsPersistenceService.update_settings({
        "camera_auto_connect": True,
        "active_camera_id": test_cam_id,
    })
    assert res2["camera_auto_connect"] is True
    assert res2["active_camera_id"] == test_cam_id
    assert SettingsPersistenceService.get_state()["active_camera_id"] == test_cam_id



# What a line sends, and when, is tested in test_send_cards.py: the per-protocol
# "send results" settings became send cards.


def test_single_active_session_tracking():
    from app.services.auth_service import AuthService
    from app.db.models.user import User

    test_user = User(
        id="test-uid-1",
        username="operator_single",
        role="operator",
        clearance_level=1,
    )

    # Reset
    AuthService.reset_all_sessions()
    assert not AuthService.is_user_online("operator_single")

    # Issue initial token
    resp1 = AuthService.generate_token(test_user)
    AuthService.touch_user("operator_single")
    assert AuthService.is_user_online("operator_single")

    payload1 = security.decode_access_token(resp1.access_token)
    assert payload1 is not None
    sid1 = payload1.get("sid")
    assert sid1 is not None
    assert AuthService.is_session_valid("operator_single", sid1)

    # Issue new token with new session (force login takeover)
    resp2 = AuthService.generate_token(test_user)
    payload2 = security.decode_access_token(resp2.access_token)
    assert payload2 is not None
    sid2 = payload2.get("sid")
    assert sid2 is not None
    assert sid1 != sid2

    # Old session is invalidated, new session is valid
    assert not AuthService.is_session_valid("operator_single", sid1)
    assert AuthService.is_session_valid("operator_single", sid2)

    # Logout marks offline and revokes session
    AuthService.mark_offline("operator_single")
    assert not AuthService.is_user_online("operator_single")
    assert not AuthService.is_session_valid("operator_single", sid2)


