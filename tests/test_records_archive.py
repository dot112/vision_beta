"""The records archive: one gzip CSV file per UTC day, read back exactly, listed and downloaded through the Records API."""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

import app.services.records_archive as records_archive
import app.services.records_export as records_export
from app.db.models.production_record import ProductRecord
from app.security.api_key_scopes import required_api_key_scopes
from app.services.production_records_service import COLUMNS, normalize_row
from tests.test_production_records import _product, sessions  # noqa: F401  (a fixture)

DAY0 = datetime(2026, 3, 2, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
ODD = ["=HYPERLINK(\"x\")", "'quoted", "-5", "@a", "plain", None]


def _day(offset):
    return (DAY0 + timedelta(days=offset)).date()


def _insert(sessions, rows):
    async def run():
        async with sessions() as db:
            await db.execute(sa.insert(ProductRecord), [normalize_row(r) for r in rows])
            await db.commit()
    asyncio.run(run())


def _stored(sessions):
    async def run():
        async with sessions() as db:
            return list((await db.execute(sa.select(ProductRecord).order_by(ProductRecord.recorded_at, ProductRecord.id))).scalars().all())
    return asyncio.run(run())


def _delete_where(sessions, *where):
    async def run():
        async with sessions() as db:
            await db.execute(sa.delete(ProductRecord).where(*where))
            await db.commit()
    asyncio.run(run())


def _plain(record):
    """A stored row as normalize_row gives it (no id)."""
    return normalize_row({column: getattr(record, column) for column in COLUMNS})


def _write_dummy(day, data=b"x"):
    path = records_archive.file_for(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _break_iter_chunks_after(monkeypatch, good):
    """records_export.iter_chunks gives ``good`` chunks, then fails like a full disk."""
    real = records_export.iter_chunks

    async def broken(*args, **kwargs):
        given = 0
        async for rows in real(*args, **kwargs):
            if given == good:
                raise OSError("disk full")
            given += 1
            yield rows

    monkeypatch.setattr(records_export, "iter_chunks", broken)


def _count_chunks(monkeypatch):
    """The size of every chunk records_export.iter_chunks gives, in a list that fills as they come."""
    real = records_export.iter_chunks
    sizes = []

    async def counting(*args, **kwargs):
        async for rows in real(*args, **kwargs):
            sizes.append(len(rows))
            yield rows

    monkeypatch.setattr(records_export, "iter_chunks", counting)
    return sizes


# ── The file ──────────────────────────────────────────────────────────────────

def test_a_day_is_written_and_reads_back_the_same(sessions, data_dir):
    rows = [_product(n=i, recorded_at=DAY0 + timedelta(hours=i), code=ODD[i % len(ODD)],
                     details={"stations": [{"camera_id": "c", "result": "good"}]} if i % 2 else None) for i in range(12)]
    _insert(sessions, rows + [_product(n=99, recorded_at=DAY0 + timedelta(days=1))])
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 12
    assert list(records_archive.read_day(DAY0.date())) == [normalize_row(r) for r in rows]


def test_every_kind_of_value_reads_back_exactly(sessions, data_dir):
    rows = [
        _product(n=1, recorded_at=DAY0 + timedelta(microseconds=123456), confidence=0.1 + 0.2, counted=False,
                 result="reject", reject_reason="vision_class", camera_id="cam-1", camera_name="Top, \"left\"",
                 code='a,"b"\r\nc\nd é 中 ünï', code_format="QR", code_status="known", batch="B-7",
                 product_name="Widget", product_list_id="list-1",
                 details={"product": {"fields": {"lane": "3"}}, "bbox": [1.5, 2, 3, 4], "name": "Ünï 中", "none": None}),
        _product(n=2, recorded_at=DAY0 + timedelta(microseconds=123457), kind="code", counted=False, result=None,
                 confidence=None, track_id=None, code="'", class_name="\tTab", reject_reason="\rReturn"),
        _product(n=3, recorded_at=DAY0 + timedelta(hours=23, minutes=59, seconds=59, microseconds=999999),
                 track_id=2 ** 40, code="+1", class_name="''two", line_name="=1+1"),
    ]
    _insert(sessions, rows)
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 3
    assert list(records_archive.read_day(DAY0.date())) == [normalize_row(r) for r in rows]


def test_the_quote_rule(data_dir):
    for text in ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "'x", "''x", "'"]:
        assert records_archive.cell_out(text) == "'" + text
        assert records_archive.cell_in(records_archive.cell_out(text)) == text
    for text in ["plain", "a=b", "x'", "1-2", " =x", "é"]:
        assert records_archive.cell_out(text) == text
        assert records_archive.cell_in(text) == text
    # Numbers and times are not text: a negative number stays a number.
    assert records_archive.cell_out(-5) == "-5" and records_archive.cell_out(-0.5) == "-0.5"
    assert records_archive.cell_out(True) == "true" and records_archive.cell_out(False) == "false"
    assert records_archive.cell_out(None) == ""
    assert records_archive.cell_out(datetime(2026, 3, 2, 1, 2, 3, 4, tzinfo=timezone.utc)) == "2026-03-02T01:02:03.000004Z"


def test_the_file_is_utf8_csv_with_a_bom_and_crlf(sessions, data_dir):
    _insert(sessions, [_product(recorded_at=DAY0)]); asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    raw = gzip.decompress(records_archive.file_for(DAY0.date()).read_bytes())
    assert raw.startswith("﻿".encode()) and b"\r\n" in raw
    assert raw.decode("utf-8-sig").split("\r\n")[0].split(",") == records_archive.HEADER
    # No file name in the gzip header (it would say ".partial"), and the default level 6.
    packed = records_archive.file_for(DAY0.date()).read_bytes()
    assert packed[:2] == b"\x1f\x8b" and packed[3] == 0 and packed[8] == 0


def test_the_file_holds_the_cells_the_spec_names(sessions, data_dir):
    _insert(sessions, [_product(n=7, recorded_at=DAY0 + timedelta(hours=1), counted=False, code="=cmd", confidence=None,
                                details={"a": 1, "b": [2]})])
    asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    text = gzip.decompress(records_archive.file_for(DAY0.date()).read_bytes()).decode("utf-8-sig")
    assert text.endswith("\r\n")
    header, row = list(csv.reader(io.StringIO(text, newline="")))
    cells = dict(zip(header, row))
    assert header == records_archive.HEADER and cells["id"] == str(_stored(sessions)[0].id)
    assert cells["recorded_at"] == "2026-03-02T01:00:00.000000Z"
    assert cells["counted"] == "false" and cells["confidence"] == "" and cells["code"] == "'=cmd"
    assert cells["track_id"] == "7" and cells["details"] == '{"a":1,"b":[2]}'


def test_a_day_without_records_writes_no_file(sessions, data_dir):
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 0
    assert not records_archive.has_file(DAY0.date())


def test_a_failed_write_leaves_no_partial_file(sessions, data_dir, monkeypatch):
    _insert(sessions, [_product(n=i, recorded_at=DAY0 + timedelta(minutes=i)) for i in range(5)])
    monkeypatch.setattr(records_archive, "CHUNK", 2)
    _break_iter_chunks_after(monkeypatch, 1)                      # the second chunk raises OSError
    with pytest.raises(OSError):
        asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    assert list((data_dir / "archive").iterdir()) == []


def test_a_failed_write_keeps_the_file_that_was_there(sessions, data_dir, monkeypatch):
    _insert(sessions, [_product(n=i, recorded_at=DAY0 + timedelta(minutes=i)) for i in range(5)])
    _write_dummy(DAY0.date(), b"the old file")
    monkeypatch.setattr(records_archive, "CHUNK", 2)
    _break_iter_chunks_after(monkeypatch, 1)
    with pytest.raises(OSError):
        asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    assert [p.name for p in (data_dir / "archive").iterdir()] == [records_archive.file_for(DAY0.date()).name]
    assert records_archive.file_for(DAY0.date()).read_bytes() == b"the old file"


def test_a_big_day_is_read_in_chunks(sessions, data_dir, monkeypatch):
    _insert(sessions, [_product(n=i, recorded_at=DAY0 + timedelta(seconds=i)) for i in range(7)])
    chunks = _count_chunks(monkeypatch); monkeypatch.setattr(records_archive, "CHUNK", 3)
    assert asyncio.run(records_archive.write_day(sessions, DAY0.date())) == 7
    assert chunks == [3, 3, 1]
    assert [r["track_id"] for r in records_archive.read_day(DAY0.date())] == list(range(7))


def test_a_file_that_is_not_ours_does_not_read(data_dir):
    _write_dummy(DAY0.date(), gzip.compress(b"id,recorded_at\r\n1,x\r\n"))
    with pytest.raises(ValueError):
        list(records_archive.read_day(DAY0.date()))
    header = ",".join(records_archive.HEADER)
    _write_dummy(DAY0.date(), gzip.compress((header + "\r\n1,not a time" + ",x" * 18 + "\r\n").encode()))
    with pytest.raises(ValueError, match="row 2"):
        list(records_archive.read_day(DAY0.date()))


# ── The folder ────────────────────────────────────────────────────────────────

def test_listing_ignores_partial_and_other_files(data_dir):
    assert records_archive.list_days() == []
    _write_dummy(date(2026, 3, 4), b"four"); _write_dummy(date(2026, 3, 2), b"two")
    for name in ["records-2026-03-02.csv.gz.partial", "records-2026-03-03.csv", "notes.txt", "records-2026-13-01.csv.gz",
                 "records-2026-3-5.csv.gz", "xrecords-2026-03-06.csv.gz"]:
        (data_dir / "archive" / name).write_bytes(b"other")
    (data_dir / "archive" / "records-2026-03-07.csv.gz").mkdir()
    assert records_archive.list_days() == [{"day": "2026-03-02", "size": 3}, {"day": "2026-03-04", "size": 4}]


def test_days_to_archive(sessions, data_dir):
    _insert(sessions, [_product(recorded_at=DAY0 + timedelta(days=d, hours=1)) for d in (0, 1, 2)])
    _write_dummy(DAY0.date())                                     # day 0 is archived already
    day = lambda d: (DAY0 + timedelta(days=d)).date()
    assert asyncio.run(records_archive.days_to_archive(sessions, DAY0 + timedelta(days=3, minutes=11))) == [day(1), day(2)]
    assert asyncio.run(records_archive.days_to_archive(sessions, DAY0 + timedelta(days=3, minutes=5))) == [day(1)]   # day 2 ended 5 minutes ago


def test_days_without_records_are_not_archived(sessions, data_dir):
    assert asyncio.run(records_archive.days_to_archive(sessions, NOW)) == []
    _insert(sessions, [_product(recorded_at=DAY0 + timedelta(days=d, hours=1)) for d in (0, 5)])   # four empty days between
    assert asyncio.run(records_archive.days_to_archive(sessions, NOW)) == [_day(0), _day(5)]


def test_archive_pending_writes_each_day_and_stops_at_the_first_failure(sessions, data_dir, monkeypatch):
    _insert(sessions, [_product(n=d, recorded_at=DAY0 + timedelta(days=d, hours=1)) for d in (0, 1, 2)])
    now = DAY0 + timedelta(days=3, minutes=11)
    assert asyncio.run(records_archive.archive_pending(sessions, now)) == [_day(0), _day(1), _day(2)]
    assert [d["day"] for d in records_archive.list_days()] == ["2026-03-02", "2026-03-03", "2026-03-04"]
    assert asyncio.run(records_archive.archive_pending(sessions, now)) == []

    for path in (data_dir / "archive").iterdir():
        path.unlink()
    real = records_archive.write_day
    written = []

    async def failing(factory, day):
        if day == _day(1):
            raise OSError("disk full")
        written.append(day)
        return await real(factory, day)

    monkeypatch.setattr(records_archive, "write_day", failing)
    with pytest.raises(OSError):
        asyncio.run(records_archive.archive_pending(sessions, now))
    assert written == [_day(0)] and [d["day"] for d in records_archive.list_days()] == ["2026-03-02"]


def test_archived_until(sessions, data_dir):
    assert asyncio.run(records_archive.archived_until(sessions)) is None                       # no records
    _insert(sessions, [_product(recorded_at=DAY0 + timedelta(days=d, hours=1)) for d in (0, 1, 3)])
    _write_dummy(_day(0)); _write_dummy(_day(1))
    assert asyncio.run(records_archive.archived_until(sessions)) == DAY0 + timedelta(days=3)   # day 3 has records and no file
    _write_dummy(_day(3))
    # Every day with records has its file: all of them are archived, up to the end of the newest day.
    assert asyncio.run(records_archive.archived_until(sessions)) == DAY0 + timedelta(days=4)
    (data_dir / "archive" / records_archive.file_for(_day(0)).name).unlink()
    assert asyncio.run(records_archive.archived_until(sessions)) == DAY0


def test_prune_files_and_delete_oldest(data_dir):
    old, middle, recent = [(NOW - timedelta(days=n)).date() for n in (400, 300, 10)]
    for day in (middle, recent, old):
        _write_dummy(day)
    assert records_archive.prune_files(0, NOW) == 0 and len(records_archive.list_days()) == 3
    assert records_archive.prune_files(365, NOW) == 1
    assert [d["day"] for d in records_archive.list_days()] == [middle.isoformat(), recent.isoformat()]
    assert records_archive.delete_oldest_file() == records_archive.file_for(middle).name
    assert [d["day"] for d in records_archive.list_days()] == [recent.isoformat()]
    assert records_archive.delete_oldest_file() == records_archive.file_for(recent).name
    assert records_archive.delete_oldest_file() is None


def test_a_file_is_kept_until_its_day_ended_the_retention_ago(data_dir):
    day = date(2026, 3, 2)
    _write_dummy(day)
    assert records_archive.prune_files(365, datetime(2027, 3, 3, tzinfo=timezone.utc)) == 0        # the day ended exactly 365 days ago
    assert records_archive.prune_files(365, datetime(2027, 3, 3, 0, 0, 1, tzinfo=timezone.utc)) == 1


# ── Putting records back ──────────────────────────────────────────────────────

def _two_days(sessions):
    rows = [_product(n=h, recorded_at=DAY0 + timedelta(hours=h), code=ODD[h % len(ODD)]) for h in range(24)]
    rows.append(_product(n=30, recorded_at=DAY0 + timedelta(days=1, hours=5), details={"sequence": 4}))
    _insert(sessions, rows)
    for day in (_day(0), _day(1)):
        assert asyncio.run(records_archive.write_day(sessions, day)) > 0
    return rows


def test_reimport_puts_days_back_after_the_given_time(sessions, data_dir):
    rows = _two_days(sessions)
    _delete_where(sessions)                                       # a rebuilt database: the records are gone
    _insert(sessions, [_product(line_id="other", n=1, recorded_at=DAY0 + timedelta(days=9))])
    other_id = _stored(sessions)[0].id
    noon = DAY0 + timedelta(hours=12)
    assert asyncio.run(records_archive.reimport(sessions, [_day(0), _day(1)], noon)) == 13        # noon itself and after
    stored = _stored(sessions)
    assert [_plain(r) for r in stored[:-1]] == [normalize_row(r) for r in rows if r["recorded_at"] >= noon]
    assert all(r.id > other_id for r in stored[:-1]) and len({r.id for r in stored}) == 14       # new ids


def test_reimport_without_a_time_puts_everything_back_and_a_second_time_adds_nothing(sessions, data_dir):
    rows = _two_days(sessions)
    _delete_where(sessions)
    assert asyncio.run(records_archive.reimport(sessions, [_day(0), _day(1)], None)) == 25
    assert asyncio.run(records_archive.reimport(sessions, [_day(0), _day(1)], None)) == 0
    assert [_plain(r) for r in _stored(sessions)] == [normalize_row(r) for r in rows]


def test_reimport_skips_the_rows_that_are_already_there(sessions, data_dir, monkeypatch):
    rows = _two_days(sessions)
    _delete_where(sessions, ProductRecord.recorded_at >= DAY0 + timedelta(hours=10),
                  ProductRecord.recorded_at < DAY0 + timedelta(hours=14))      # four rows lost, the rest survived
    monkeypatch.setattr(records_archive, "CHUNK", 5)
    assert asyncio.run(records_archive.reimport(sessions, [_day(0), _day(1)], DAY0 + timedelta(hours=1))) == 4
    assert sorted(_plain(r)["track_id"] for r in _stored(sessions)) == sorted(r["track_id"] for r in rows)


def test_reimport_puts_back_rows_that_are_alike_in_every_column(sessions, data_dir, monkeypatch):
    twin = _product(n=1, recorded_at=DAY0 + timedelta(hours=1), code="same")
    _insert(sessions, [twin, twin, _product(n=2, recorded_at=DAY0 + timedelta(hours=2))])
    asyncio.run(records_archive.write_day(sessions, DAY0.date()))
    _delete_where(sessions)
    monkeypatch.setattr(records_archive, "CHUNK", 1)              # the twins come in different chunks
    assert asyncio.run(records_archive.reimport(sessions, [DAY0.date()], None)) == 3
    assert [r.track_id for r in _stored(sessions)] == [1, 1, 2]


def test_reimport_leaves_out_a_day_that_ended_before_the_time(sessions, data_dir, monkeypatch):
    _two_days(sessions)
    _delete_where(sessions)
    opened = []
    real = records_archive.read_day
    monkeypatch.setattr(records_archive, "read_day", lambda day: opened.append(day) or real(day))
    assert asyncio.run(records_archive.reimport(sessions, [_day(0), _day(1)], DAY0 + timedelta(days=1))) == 1
    assert opened == [_day(1)]


# ── The Records API ───────────────────────────────────────────────────────────

def test_archive_routes(client, admin_headers, data_dir):
    _write_dummy(date(2026, 3, 2), b"gzipped")
    listing = client.get("/api/v1/records/archive", headers=admin_headers).json()
    assert listing == {"days": [{"day": "2026-03-02", "size": 7}], "oldest": "2026-03-02", "newest": "2026-03-02"}
    got = client.get("/api/v1/records/archive/2026-03-02", headers=admin_headers)
    assert got.status_code == 200 and got.content == b"gzipped" and got.headers["content-type"] == "application/gzip"
    assert got.headers["content-disposition"] == 'attachment; filename="records-2026-03-02.csv.gz"'
    for bad in ("2026-03-03", "2026-13-01", "..%2Fsystem_state.json", "x", "20260302", "2026-W10-1", "٢٠٢٦-٠٣-٠٢"):
        assert client.get(f"/api/v1/records/archive/{bad}", headers=admin_headers).status_code == 404
    assert required_api_key_scopes("GET", "/api/v1/records/archive/2026-03-02") == {"records:read"}
    assert required_api_key_scopes("GET", "/api/v1/records/archive") == {"records:read"}


def test_the_archive_list_is_empty_without_files(client, admin_headers, data_dir):
    assert client.get("/api/v1/records/archive", headers=admin_headers).json() == {"days": [], "oldest": None, "newest": None}


def test_the_archive_needs_a_sign_in(client, data_dir):
    _write_dummy(date(2026, 3, 2))
    assert client.get("/api/v1/records/archive").status_code == 401
    assert client.get("/api/v1/records/archive/2026-03-02").status_code == 401
