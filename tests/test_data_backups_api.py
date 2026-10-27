"""Data and backups: the Admin routes for the storage status, backups, the clock and the notices."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from app.events.alarm_events import AlarmSeverity, alarm_manager, raise_alarm
from app.security.api_key_scopes import required_api_key_scopes
from app.services import storage_state
from app.services.data_keeper import ClockGuard
from tests.test_config_lock_api import _reset_lock, _user_headers

API = "/api/v1"


@pytest.fixture(autouse=True)
def _clean_state():
    """No clock pause, notices or alarms from one test in the next."""
    def clear():
        storage_state.update(lambda state: [state.pop(key, None) for key in ("clock", "notices")])
        alarm_manager.reset()
    clear()
    yield
    clear()


def _active(code):
    return alarm_manager.get(code, storage_state.STORAGE_SOURCE) is not None


def test_signed_out_and_lower_levels_are_refused(client, admin_headers):
    assert client.get(f"{API}/system/data").status_code == 401
    for level in (1, 2):
        headers = _user_headers(client, admin_headers, level)
        assert client.get(f"{API}/system/data", headers=headers).status_code == 403
        assert client.post(f"{API}/system/backups", headers=headers).status_code == 403
        assert client.post(f"{API}/system/data/clock-ok", headers=headers).status_code == 403


def test_the_status_has_every_part(client, admin_headers):
    status = client.get(f"{API}/system/data", headers=admin_headers)
    assert status.status_code == 200
    body = status.json()
    assert set(body) == {"disk", "database", "archive", "backups", "copy", "clock", "notices", "last"}
    assert body["disk"]["total"] > 0 and body["database"]["size"] > 0
    assert body["clock"] == {"paused": False, "reason": None, "since": None}


def test_back_up_now_and_download(client, admin_headers):
    made = client.post(f"{API}/system/backups", headers=admin_headers)
    assert made.status_code == 200 and made.json()["kind"] == "manual"
    name = made.json()["name"]
    assert name in [b["name"] for b in client.get(f"{API}/system/data", headers=admin_headers).json()["backups"]]
    got = client.get(f"{API}/system/backups/{name}", headers=admin_headers)
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
    assert f'filename="{name}.zip"' in got.headers["content-disposition"]
    assert sorted(zipfile.ZipFile(io.BytesIO(got.content)).namelist()) == ["config.db", "manifest.json", "system_state.json"]
    actions = [e["action"] for e in client.get(f"{API}/system/audit-logs?limit=5", headers=admin_headers).json()]
    assert "BACKUP_MADE" in actions and "BACKUP_DOWNLOADED" in actions
    for bad in ("x", "20261026T001000Z-weekly", "..%2F..%2Fdata", "20991231T000000Z-manual"):
        assert client.get(f"{API}/system/backups/{bad}", headers=admin_headers).status_code == 404


def test_a_backup_that_fails_says_why(client, admin_headers, monkeypatch):
    from app.services import backup_service

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(backup_service, "make_backup", full)
    answer = client.post(f"{API}/system/backups", headers=admin_headers)
    assert answer.status_code == 503 and "No space left" in answer.json()["detail"]


def test_clock_ok_ends_the_pause(client, admin_headers):
    ClockGuard().check_start(datetime.now(timezone.utc) - timedelta(days=400), datetime.now(timezone.utc), 90)
    assert client.get(f"{API}/system/data", headers=admin_headers).json()["clock"]["paused"] is True
    answer = client.post(f"{API}/system/data/clock-ok", headers=admin_headers)
    assert answer.status_code == 200 and answer.json()["paused"] is False
    assert not _active("storage.clock_check")
    assert client.get(f"{API}/system/data", headers=admin_headers).json()["clock"]["paused"] is False
    actions = [e["action"] for e in client.get(f"{API}/system/audit-logs?limit=3", headers=admin_headers).json()]
    assert "CLOCK_CONFIRMED" in actions


def test_notice_ok_closes_the_notice_and_clears_its_alarm(client, admin_headers):
    storage_state.add_notice("storage.records_lost", "2 records deleted")
    raise_alarm("storage.records_lost", storage_state.STORAGE_SOURCE, "2 records deleted", AlarmSeverity.CRITICAL)
    assert [n["code"] for n in client.get(f"{API}/system/data", headers=admin_headers).json()["notices"]] == ["storage.records_lost"]
    assert client.post(f"{API}/system/data/notices/storage.records_lost/ok", headers=admin_headers).status_code == 200
    assert storage_state.notices() == [] and not _active("storage.records_lost")
    assert client.post(f"{API}/system/data/notices/storage.records_lost/ok", headers=admin_headers).status_code == 404
    actions = [e["action"] for e in client.get(f"{API}/system/audit-logs?limit=3", headers=admin_headers).json()]
    assert "NOTICE_CLOSED" in actions


def test_api_keys_cannot_reach_data_or_backups():
    for path in ("/api/v1/system/data", "/api/v1/system/backups", "/api/v1/system/backups/x",
                 "/api/v1/system/data/clock-ok", "/api/v1/system/data/notices/storage.full/ok"):
        assert required_api_key_scopes("GET", path) is None and required_api_key_scopes("POST", path) is None
    # The rest of the system routes keep their scopes.
    assert required_api_key_scopes("GET", "/api/v1/system/usage") == {"monitor:read"}
    assert required_api_key_scopes("GET", "/api/v1/system/endpoints") == {"configuration:read"}


def test_the_post_routes_work_while_the_configuration_is_locked(client, admin_headers):
    try:
        assert client.post(f"{API}/system/lock", headers=admin_headers).json()["locked"] is True
        assert client.post(f"{API}/system/backups", headers=admin_headers).status_code == 200
        assert client.post(f"{API}/system/data/clock-ok", headers=admin_headers).status_code == 200
    finally:
        _reset_lock()
