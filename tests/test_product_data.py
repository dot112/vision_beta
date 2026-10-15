"""Products carry data: each product list has its own fields, each product its values.

The database columns (migration 0007), the Products API with fields and data,
CSV files with extra columns, and the product going with the item into
events, messages, templates and records.
"""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]


# ── The database upgrade ──────────────────────────────────────────────────────

def _alembic(tmp_path, name):
    from alembic.config import Config

    engine = sa.create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return engine, config


def _columns(conn, table):
    return {column["name"] for column in sa.inspect(conn).get_columns(table)}


def _shape(conn):
    return sorted((table, column) for table in ("product_lists", "products") for column in _columns(conn, table))


def test_0007_adds_fields_and_data_and_keeps_every_row(tmp_path):
    from alembic import command

    engine, config = _alembic(tmp_path, "old.db")
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "0006_product_records")
            # A database made before this change: products has no data column yet.
            if "data" in _columns(conn, "products"):
                conn.execute(sa.text("ALTER TABLE products DROP COLUMN data"))
            conn.execute(sa.text("INSERT INTO products (id, code, name, list_id) VALUES ('p1', 'J-1', 'Apple', 'list-1')"))

            command.upgrade(config, "head")

            assert conn.execute(sa.text("SELECT fields FROM product_lists WHERE id='list-1'")).scalar_one() == "[]"
            assert conn.execute(sa.text("SELECT data FROM products WHERE id='p1'")).scalar_one() is None
            upgraded = _shape(conn)
            assert ("product_lists", "fields") in upgraded and ("products", "data") in upgraded
        with engine.begin() as conn:  # again: nothing changes
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            assert _shape(conn) == upgraded
            command.downgrade(config, "0006_product_records")
            assert "fields" not in _columns(conn, "product_lists")
            assert "data" not in _columns(conn, "products")
            assert conn.execute(sa.text("SELECT code FROM products")).scalar_one() == "J-1"
    finally:
        engine.dispose()

    engine, config = _alembic(tmp_path, "new.db")
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            assert _shape(conn) == upgraded
    finally:
        engine.dispose()
