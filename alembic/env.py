from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so Alembic can detect them
from app.db.base import Base
import app.db.models.user        # noqa: F401
import app.db.models.api_key     # noqa: F401
import app.db.models.product     # noqa: F401
import app.db.models.production_record  # noqa: F401
import app.db.models.detection   # noqa: F401
import app.db.models.rule        # noqa: F401
import app.db.models.action      # noqa: F401
import app.db.models.camera      # noqa: F401
import app.db.models.event       # noqa: F401
import app.db.models.model       # noqa: F401
import app.db.models.flow        # noqa: F401

from app.config import settings

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Only the alembic CLI applies alembic.ini's logging. When the app runs the
# migrations at startup (it passes its connection in), fileConfig would disable
# every logger the app already created and replace its log handlers, silencing
# the application log for the rest of the run.
if config.config_file_name and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
