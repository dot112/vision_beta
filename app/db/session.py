from __future__ import annotations

import logging
import os
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)

# Ensure runtime directories exist before database connection is initialized
if "sqlite" in settings.DATABASE_URL:
    db_path = settings.DATABASE_URL.split(":///")[-1]
    if db_path and not db_path.startswith(":memory:"):
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

_journal_mode_reported = False


def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Journal mode, sync level and lock wait for every new SQLite connection."""
    global _journal_mode_reported
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {max(0, int(settings.SQLITE_BUSY_TIMEOUT_MS))}")
        cursor.execute(f"PRAGMA journal_mode = {settings.SQLITE_JOURNAL_MODE}")
        row = cursor.fetchone()
        mode = str(row[0]).upper() if row else "?"
        cursor.execute(f"PRAGMA synchronous = {settings.SQLITE_SYNCHRONOUS}")
        if not _journal_mode_reported:
            _journal_mode_reported = True
            if mode == settings.SQLITE_JOURNAL_MODE or mode == "MEMORY":
                logger.info("SQLite journal_mode=%s synchronous=%s", mode, settings.SQLITE_SYNCHRONOUS)
            else:
                # WAL needs a local filesystem; network shares and some bind
                # mounts refuse it. SQLite then keeps the mode it had.
                logger.warning(
                    "SQLite kept journal_mode=%s (asked for %s); keep the database on a local disk or Docker volume",
                    mode, settings.SQLITE_JOURNAL_MODE,
                )
    finally:
        cursor.close()


if engine.dialect.name == "sqlite":
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
