"""Detections cleanup: rows older than DETECTIONS_RETENTION_DAYS, and the oldest rows when the disk runs short."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models.detection import DetectionLog
from app.services import detections_cleanup

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
# The text SQLite's own now() leaves in created_at (UTC, whole seconds, no zone).
STORED = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def det_sessions(tmp_path):
    """A throwaway database with only the detections table (and its index). Gives its session factory."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'detections.db').as_posix()}", poolclass=NullPool)

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: DetectionLog.__table__.create(sync))

    asyncio.run(create())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    asyncio.run(engine.dispose())


def _insert(sessions, ages_days):
    """One row per age, written the way the database writes created_at."""
    async def run():
        async with sessions() as db:
            for age in ages_days:
                await db.execute(
                    sa.text("INSERT INTO detections (id, model_name, total_detections, detections, inference_time_ms, created_at) "
                            "VALUES (:id, 'YOLOv8', 0, '[]', 0.0, :created_at)"),
                    {"id": str(uuid.uuid4()), "created_at": (NOW - timedelta(days=age)).strftime(STORED)},
                )
            await db.commit()
    asyncio.run(run())


def _ages(sessions):
    """The ages in days of the rows left, oldest first."""
    async def run():
        async with sessions() as db:
            return (await db.execute(sa.text("SELECT created_at FROM detections ORDER BY created_at"))).scalars().all()
    stamps = asyncio.run(run())
    return [(NOW - datetime.strptime(text, STORED).replace(tzinfo=timezone.utc)).days for text in stamps]


def _explain(sessions, sql):
    async def run():
        async with sessions() as db:
            rows = (await db.execute(sa.text("EXPLAIN QUERY PLAN " + sql))).all()
        return " | ".join(str(row[-1]) for row in rows)
    return asyncio.run(run())


def test_prune_deletes_rows_older_than_the_limit(det_sessions):
    _insert(det_sessions, ages_days=(40, 31, 29, 1))
    assert asyncio.run(detections_cleanup.prune(det_sessions, 30, NOW)) == 2
    assert asyncio.run(detections_cleanup.prune(det_sessions, 0, NOW)) == 0
    assert _ages(det_sessions) == [29, 1]


def test_delete_oldest_takes_the_oldest_first(det_sessions):
    _insert(det_sessions, ages_days=(5, 50, 20))
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions, 2)) == 2
    assert _ages(det_sessions) == [5]


def test_prune_uses_the_created_at_index(det_sessions):
    plan = _explain(det_sessions, "SELECT id FROM detections WHERE created_at < '2026-01-01' LIMIT 5000")
    assert "ix_detections_created_at" in plan


def test_both_work_in_chunks(det_sessions, monkeypatch):
    monkeypatch.setattr(detections_cleanup, "CHUNK", 2)
    _insert(det_sessions, ages_days=(90, 80, 70, 60, 50, 40, 10, 5))
    assert asyncio.run(detections_cleanup.prune(det_sessions, 30, NOW)) == 6
    assert _ages(det_sessions) == [10, 5]
    _insert(det_sessions, ages_days=(20, 15, 12))
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions, 4)) == 4
    assert _ages(det_sessions) == [5]
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions, 4)) == 1
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions)) == 0
    assert _ages(det_sessions) == []


def test_a_time_without_a_zone_is_taken_as_utc(det_sessions):
    _insert(det_sessions, ages_days=(31, 29))
    assert asyncio.run(detections_cleanup.prune(det_sessions, 30, NOW.replace(tzinfo=None))) == 1
    assert _ages(det_sessions) == [29]


def test_nothing_to_delete_deletes_nothing(det_sessions):
    assert asyncio.run(detections_cleanup.prune(det_sessions, 30, NOW)) == 0
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions, 10)) == 0
    _insert(det_sessions, ages_days=(5,))
    assert asyncio.run(detections_cleanup.delete_oldest(det_sessions, 0)) == 0
    assert _ages(det_sessions) == [5]
