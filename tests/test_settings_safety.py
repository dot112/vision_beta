"""Data safety: the settings file keeps a history of its saved copies, and a damaged file is never replaced by the defaults."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.services.settings_persistence_service as persistence_module
from app.events.alarm_events import alarm_manager
from app.services import storage_state
from app.services.settings_persistence_service import SettingsPersistenceService as SPS
from app.services.storage_state import stamp


def test_a_save_copies_the_previous_file_into_the_history(settings_file):
    SPS.load(); SPS._state["lines"][0]["name"] = "First"; SPS.save()
    SPS._state["lines"][0]["name"] = "Second"; SPS.save()
    copies = sorted(Path(SPS._history_dir()).iterdir())
    assert copies and json.loads(copies[-1].read_text())["lines"][0]["name"] == "First"


def test_history_keeps_20_newest_and_one_per_day_for_30_days(settings_file):
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    hist = Path(SPS._history_dir()); hist.mkdir(parents=True)
    for day in range(40):                 # 3 copies a day, 40 days back
        for hour in (1, 2, 3):
            t = now - timedelta(days=day, hours=hour)
            (hist / f"system_state-{stamp(t, micro=True)}.json").write_text("{}")
    SPS._trim_history(now)
    names = sorted(p.name for p in hist.iterdir())
    assert len(names) == 20 + (30 - 7)    # the 20 newest cover about 7 days; one each for the other days inside 30
    assert min(names) > f"system_state-{stamp(now - timedelta(days=30), micro=True)}"


def test_a_damaged_file_loads_the_newest_good_copy(settings_file):
    SPS.load(); SPS._state["lines"][0]["name"] = "Packing"; SPS.save(); SPS.save()
    Path(settings_file).write_text('{"lines": [', encoding="utf-8")
    SPS._state = {}
    state = SPS.load()
    assert state["lines"][0]["name"] == "Packing"
    assert json.loads(Path(settings_file).read_text())["lines"][0]["name"] == "Packing"
    broken = list(Path(settings_file).parent.glob("system_state.broken-*.json"))
    assert len(broken) == 1 and broken[0].read_text() == '{"lines": ['
    assert alarm_manager.get("storage.settings_restored", "storage") is not None
    assert [n["code"] for n in storage_state.notices()] == ["storage.settings_restored"]


def test_a_file_that_fails_its_upgrade_counts_as_damaged(settings_file, monkeypatch):
    SPS.load(); SPS.save(); SPS.save()
    data = json.loads(Path(settings_file).read_text()); data["boom"] = True
    Path(settings_file).write_text(json.dumps(data))
    real = persistence_module.upgrade_state
    monkeypatch.setattr(persistence_module, "upgrade_state", lambda s: (_ for _ in ()).throw(ValueError("bad")) if s.get("boom") else real(s))
    SPS._state = {}
    assert "boom" not in SPS.load()
    assert len(list(Path(settings_file).parent.glob("system_state.broken-*.json"))) == 1


def test_no_good_copy_starts_with_defaults_and_keeps_everything(settings_file):
    Path(settings_file).write_text("not json", encoding="utf-8")
    SPS._state = {}
    state = SPS.load()
    assert state["lines"][0]["name"] == "Line 1"
    assert [p.read_text() for p in Path(settings_file).parent.glob("system_state.broken-*.json")] == ["not json"]
    assert "no good copy" in storage_state.notices()[0]["message"]


def test_a_leftover_tmp_file_is_removed(settings_file):
    Path(settings_file + ".tmp").write_text("half")
    SPS._state = {}; SPS.load()
    assert not Path(settings_file + ".tmp").exists()


def test_a_leftover_tmp_file_is_removed_when_the_settings_file_is_fine(settings_file):
    SPS.load(); SPS.save()
    Path(settings_file + ".tmp").write_text("half")
    SPS._state = {}; SPS.load()
    assert not Path(settings_file + ".tmp").exists()
    assert json.loads(Path(settings_file).read_text())["lines"][0]["name"] == "Line 1"


def test_a_file_that_is_not_a_json_object_counts_as_damaged(settings_file):
    SPS.load(); SPS.save()
    Path(settings_file).write_text("[1, 2]", encoding="utf-8")
    SPS._state = {}
    assert SPS.load()["lines"][0]["name"] == "Line 1"
    assert [p.read_text() for p in Path(settings_file).parent.glob("system_state.broken-*.json")] == ["[1, 2]"]


def test_a_copy_that_does_not_load_is_skipped(settings_file):
    SPS.load(); SPS._state["lines"][0]["name"] = "Packing"; SPS.save(); SPS.save()
    newest = sorted(Path(SPS._history_dir()).iterdir())[-1]
    newest.write_text("cut off", encoding="utf-8")
    Path(settings_file).write_text("{", encoding="utf-8")
    SPS._state = {}
    # The copy before the newest is the defaults of the first start: it still beats starting empty-handed.
    assert SPS.load()["lines"][0]["name"] == "Line 1"
    assert alarm_manager.get("storage.settings_restored", "storage") is not None
    assert "no good copy" not in storage_state.notices()[0]["message"]


def test_a_healthy_start_raises_nothing(settings_file):
    SPS.load(); SPS.save()
    SPS._state = {}
    SPS.load()
    assert alarm_manager.get("storage.settings_restored", "storage") is None
    assert storage_state.notices() == []
    assert not list(Path(settings_file).parent.glob("system_state.broken-*.json"))


def test_a_damaged_file_is_not_copied_into_the_history(settings_file):
    SPS.load(); SPS.save()
    before = sorted(Path(SPS._history_dir()).iterdir())
    Path(settings_file).write_text("{", encoding="utf-8")
    SPS.save()
    assert sorted(Path(SPS._history_dir()).iterdir()) == before


def test_a_save_still_writes_when_the_history_cannot_be_kept(settings_file, monkeypatch):
    SPS.load()
    monkeypatch.setattr(SPS, "_copy_to_history", classmethod(lambda cls, now=None: (_ for _ in ()).throw(OSError("disk full"))))
    SPS._state["lines"][0]["name"] = "Packing"
    SPS.save()
    assert json.loads(Path(settings_file).read_text())["lines"][0]["name"] == "Packing"


def test_a_notice_that_cannot_be_written_does_not_stop_the_load(settings_file, monkeypatch):
    SPS.load(); SPS._state["lines"][0]["name"] = "Packing"; SPS.save(); SPS.save()
    Path(settings_file).write_text("{", encoding="utf-8")
    SPS._state = {}
    monkeypatch.setattr(storage_state, "add_notice", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert SPS.load()["lines"][0]["name"] == "Packing"
    assert alarm_manager.get("storage.settings_restored", "storage") is not None
