"""Data safety: the audit log lives in monthly files (data/audit/audit-YYYY-MM.jsonl), is kept a year, and the page loads it in pages."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import audit_log
from app.services.settings_persistence_service import DEFAULT_STATE, SettingsPersistenceService as SPS

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 3, 2, 8, tzinfo=timezone.utc)


def _entry(i, when, line_id=None):
    return {"id": f"e{i}", "timestamp": when.isoformat(), "username": "admin", "role": "ADMIN",
            "clearance_level": 3, "action": "X", "category": "System", "details": str(i),
            **({"line_id": line_id} if line_id else {})}


def _ids(entries):
    return [e["id"] for e in entries]


def test_entries_are_appended_and_read_newest_first(data_dir):
    for i in range(3):
        audit_log.append(_entry(i, T0 + timedelta(minutes=i)))
    assert _ids(audit_log.read()) == ["e2", "e1", "e0"]


def test_entries_go_into_the_file_of_their_month(data_dir):
    audit_log.append(_entry(1, datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)))
    audit_log.append(_entry(2, datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)))
    assert sorted(p.name for p in (data_dir / "audit").iterdir()) == ["audit-2026-09.jsonl", "audit-2026-10.jsonl"]
    assert _ids(audit_log.read()) == ["e2", "e1"]


def test_the_month_is_the_utc_month_whatever_zone_the_entry_names(data_dir):
    # 01:30 on 1 October at +02:00 is still September in UTC.
    zone = timezone(timedelta(hours=2))
    audit_log.append(_entry(1, datetime(2026, 10, 1, 1, 30, tzinfo=zone)))
    assert [p.name for p in (data_dir / "audit").iterdir()] == ["audit-2026-09.jsonl"]


def test_an_entry_without_a_time_is_refused(data_dir):
    with pytest.raises(ValueError):
        audit_log.append({"id": "e1", "timestamp": "yesterday"})
    assert not (data_dir / "audit").exists() or not list((data_dir / "audit").iterdir())


def test_before_and_line_id(data_dir):
    for i in range(6):
        audit_log.append(_entry(i, T0 + timedelta(minutes=i), "line-a" if i % 2 else None))
    assert _ids(audit_log.read(limit=2, before=T0 + timedelta(minutes=4))) == ["e3", "e2"]
    assert _ids(audit_log.read(line_id="line-a")) == ["e5", "e3", "e1"]
    assert _ids(audit_log.read(line_id="line-a", before=T0 + timedelta(minutes=3))) == ["e1"]


def test_paging_walks_back_over_the_months(data_dir):
    when = [datetime(2026, 1, 31, 23, tzinfo=timezone.utc), datetime(2026, 2, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 2, 28, 22, tzinfo=timezone.utc), datetime(2026, 3, 1, 2, tzinfo=timezone.utc)]
    for i, t in enumerate(when):
        audit_log.append(_entry(i, t))
    first = audit_log.read(limit=3)
    older = audit_log.read(limit=3, before=datetime.fromisoformat(first[-1]["timestamp"]))
    assert _ids(first) == ["e3", "e2", "e1"] and _ids(older) == ["e0"]
    assert audit_log.read(limit=3, before=datetime.fromisoformat(older[-1]["timestamp"])) == []


def test_a_cut_line_is_skipped(data_dir):
    audit_log.append(_entry(1, T0))
    with open(data_dir / "audit" / "audit-2026-03.jsonl", "a", encoding="utf-8") as f:
        f.write('{"id": "e2", "time')
    audit_log.append(_entry(3, T0 + timedelta(minutes=1)))
    assert _ids(audit_log.read()) == ["e3", "e1"]


def test_lines_that_are_not_entries_are_skipped(data_dir):
    audit_log.append(_entry(1, T0))
    with open(data_dir / "audit" / "audit-2026-03.jsonl", "ab") as f:
        f.write(b'\n[1, 2]\n"text"\n{"id": "no-time"}\n\xff\xfe not utf-8\n{"id": "bad-time", "timestamp": "soon"}\n')
    audit_log.append(_entry(2, T0 + timedelta(minutes=1)))
    assert _ids(audit_log.read()) == ["e2", "e1"]


def test_a_file_bigger_than_a_block_reads_back_whole(data_dir):
    # Moved in one go: 1500 entries are about 300 KB, several blocks of the backwards reader.
    entries = [_entry(i, T0 + timedelta(seconds=i)) for i in range(1500)]
    assert audit_log.move_from_state(list(reversed(entries))) == 1500
    assert _ids(audit_log.read(limit=2000)) == [f"e{i}" for i in range(1499, -1, -1)]
    page = audit_log.read(limit=100, before=T0 + timedelta(seconds=700))
    assert _ids(page) == [f"e{i}" for i in range(699, 599, -1)]


def test_prune_deletes_months_that_ended_past_the_retention(data_dir):
    audit_log.append(_entry(1, datetime(2025, 9, 15, tzinfo=timezone.utc)))
    audit_log.append(_entry(2, datetime(2025, 11, 15, tzinfo=timezone.utc)))
    assert audit_log.prune(365, now=datetime(2026, 10, 26, tzinfo=timezone.utc)) == 1
    assert _ids(audit_log.read()) == ["e2"]
    assert audit_log.prune(365, now=datetime(2026, 10, 26, tzinfo=timezone.utc)) == 0


def test_prune_keeps_every_entry_for_at_least_the_retention(data_dir):
    # October 2025 ended on 1 November 2025 00:00 UTC; a year (365 days) after that is 1 November 2026.
    audit_log.append(_entry(1, datetime(2025, 10, 1, 0, 1, tzinfo=timezone.utc)))
    assert audit_log.prune(365, now=datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)) == 0
    assert audit_log.prune(365, now=datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)) == 1


def test_prune_leaves_other_files_alone(data_dir):
    audit_log.append(_entry(1, datetime(2020, 1, 1, tzinfo=timezone.utc)))
    for name in ("notes.txt", "audit-2020-13.jsonl", "audit-2020-01.jsonl.bak"):
        (data_dir / "audit" / name).write_text("keep")
    assert audit_log.prune(365, now=datetime(2026, 10, 26, tzinfo=timezone.utc)) == 1
    assert sorted(p.name for p in (data_dir / "audit").iterdir()) == ["audit-2020-01.jsonl.bak", "audit-2020-13.jsonl", "notes.txt"]


def test_reading_with_no_audit_folder_gives_nothing(data_dir):
    assert audit_log.read() == []
    assert audit_log.prune(365) == 0


def test_two_threads_appending_lose_nothing(data_dir):
    threads = [threading.Thread(target=lambda k=k: [audit_log.append(_entry(f"{k}-{i}", T0)) for i in range(200)])
               for k in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(audit_log.read(limit=1000)) == 400


def test_moving_entries_adds_each_one_once(data_dir):
    entries = [_entry(2, T0 + timedelta(minutes=2)), _entry(1, T0)]
    audit_log.append(_entry(1, T0))                        # already in the files
    assert audit_log.move_from_state(entries) == 1
    assert audit_log.move_from_state(entries) == 0
    assert _ids(audit_log.read()) == ["e2", "e1"]


def test_moved_entries_land_oldest_first_whatever_order_they_come_in(data_dir):
    audit_log.move_from_state([_entry(1, T0), _entry(3, T0 + timedelta(minutes=2)), _entry(2, T0 + timedelta(minutes=1))])
    lines = (data_dir / "audit" / "audit-2026-03.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["e1", "e2", "e3"]


def test_moving_skips_what_is_not_an_entry_with_a_time(data_dir):
    junk = ["text", {"id": "no-time"}, {"id": "bad", "timestamp": "soon"}, _entry(1, T0)]
    assert audit_log.move_from_state(junk) == 1
    assert _ids(audit_log.read()) == ["e1"]


def test_the_audit_log_is_no_longer_part_of_the_settings():
    assert "audit_logs" not in DEFAULT_STATE


def test_old_entries_move_out_of_the_settings_file_once(settings_file):
    SPS.load()                                             # a settings file as it is on disk
    state = json.loads(Path(settings_file).read_text())
    state["audit_logs"] = [_entry(2, T0 + timedelta(minutes=2)), _entry(1, T0)]   # newest first, as stored today
    Path(settings_file).write_text(json.dumps(state))
    SPS._state = {}
    SPS.load()
    SPS._state = {}
    SPS.load()
    moved = [e for e in audit_log.read() if e["action"] != "SYSTEM_INIT"]
    assert _ids(moved) == ["e2", "e1"]
    assert "audit_logs" not in json.loads(Path(settings_file).read_text())
    assert "audit_logs" not in SPS.get_state()


def test_entries_in_a_settings_copy_move_too(settings_file):
    SPS.load()
    SPS.save()
    copy = sorted(Path(SPS._history_dir()).iterdir())[-1]
    state = json.loads(copy.read_text())
    state["audit_logs"] = [_entry(1, T0)]
    copy.write_text(json.dumps(state))
    Path(settings_file).write_text("{", encoding="utf-8")        # damaged: the copy is loaded instead
    SPS._state = {}
    SPS.load()
    assert "e1" in _ids(audit_log.read())
    assert "audit_logs" not in json.loads(Path(settings_file).read_text())


def test_entries_stay_in_the_settings_when_the_audit_files_cannot_be_written(settings_file, monkeypatch):
    SPS.load()
    state = json.loads(Path(settings_file).read_text())
    state["audit_logs"] = [_entry(1, T0)]
    Path(settings_file).write_text(json.dumps(state))

    def full_disk(entries):
        raise OSError("disk full")

    monkeypatch.setattr(audit_log, "move_from_state", full_disk)
    SPS._state = {}
    SPS.load()
    assert [e["id"] for e in json.loads(Path(settings_file).read_text())["audit_logs"]] == ["e1"]
    assert not list(Path(settings_file).parent.glob("system_state.broken-*.json"))


def test_the_first_start_writes_system_init_to_the_audit_file(settings_file):
    SPS.load()
    first = audit_log.read()
    assert [e["action"] for e in first] == ["SYSTEM_INIT"]
    assert "audit_logs" not in json.loads(Path(settings_file).read_text())


def test_record_audit_appends_to_the_file_and_the_recent_ring(settings_file):
    SPS.load()
    event = SPS.record_audit("admin", "admin", 3, "FILE_TEST", "System", "written", line_id="line-2")
    assert event["role"] == "ADMIN" and event["line_id"] == "line-2"
    assert audit_log.read(limit=1) == [event]
    assert SPS._recent_changes[-1] == event
    assert [e["action"] for e in SPS.get_audit_logs(line_id="line-2")] == ["FILE_TEST"]
    assert "audit_logs" not in json.loads(Path(settings_file).read_text())


def test_record_audit_goes_on_when_the_entry_cannot_be_written(settings_file, monkeypatch):
    SPS.load()

    def full_disk(entry):
        raise OSError("disk full")

    monkeypatch.setattr(audit_log, "append", full_disk)
    event = SPS.record_audit("admin", "admin", 3, "NO_ROOM", "System", "still answers")
    assert event["action"] == "NO_ROOM" and SPS._recent_changes[-1] is event


def test_there_is_no_24_hour_or_500_entry_limit(settings_file):
    SPS.load()
    old = datetime.now(timezone.utc) - timedelta(days=40)
    audit_log.move_from_state([_entry(i, old + timedelta(seconds=i)) for i in range(600)])
    SPS.record_audit("admin", "admin", 3, "NOW", "System", "x")
    assert len(SPS.get_audit_logs(limit=500)) == 500
    assert len(audit_log.read(limit=1000)) == 1 + 600 + 1      # the entry of the first start too


def test_record_audit_writes_a_file_entry_and_the_api_pages(client, admin_headers, data_dir):
    audit_log.move_from_state([_entry(i, T0 + timedelta(minutes=i)) for i in range(4)])
    SPS.record_audit("admin", "admin", 3, "PAGE_TEST", "System", "first")
    newest = client.get("/api/v1/system/audit-logs?limit=1", headers=admin_headers).json()[0]
    assert newest["action"] == "PAGE_TEST"
    older = client.get("/api/v1/system/audit-logs", params={"limit": 2, "before": newest["timestamp"]}, headers=admin_headers)
    assert older.status_code == 200 and _ids(older.json()) == ["e3", "e2"]
    rest = client.get("/api/v1/system/audit-logs", params={"before": older.json()[-1]["timestamp"]}, headers=admin_headers)
    assert _ids(rest.json()) == ["e1", "e0"]


def test_the_api_refuses_a_before_that_is_not_a_time_with_a_zone(client, admin_headers, data_dir):
    for bad in ("yesterday", "", "2026-03-02T08:00:00", "2026-13-45T00:00:00Z"):
        res = client.get("/api/v1/system/audit-logs", params={"before": bad}, headers=admin_headers)
        assert res.status_code == 422, bad
    ok = client.get("/api/v1/system/audit-logs", params={"before": "2026-03-02T08:00:00+02:00"}, headers=admin_headers)
    assert ok.status_code == 200


def test_the_audit_page_has_load_older():
    js = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    assert 'id="plAuditMore"' in js and "Load older" in js and "&before=" in js


def test_the_line_filter_sits_with_refresh():
    """The owner (2026-11-01): Refresh and the line select moved about as the window changed size. The filter was a
    third item of the header (title, Refresh, Line), which spreads its items, and its label wrapped above the select.
    It goes into the header's button group, before Refresh, on one row with its label."""
    js = (ROOT / "assets" / "production_lines.js").read_text(encoding="utf-8")
    start = js.index("function decorateAuditTable()")
    body = js[start:js.index("\n    }\n", start)]
    assert 'panelHeader.querySelector(".card-actions")' in body
    assert re.search(r'insertAdjacentHTML\("afterbegin", `<label class="pl-audit-line"', body)
    assert re.search(r"\.pl-audit-line \{[^}]*flex-wrap:nowrap", js)
    assert re.search(r"\.pl-audit-line \.custom-select-wrap \{[^}]*width:", js)
