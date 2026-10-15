"""Products carry data: each product list has its own fields, each product its values.

The database columns (migration 0007), the Products API with fields and data,
CSV files with extra columns, and the product going with the item into
events, messages, templates and records.
"""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from app.services.product_service import product_catalog
from tests.test_plc_inputs import operator_headers  # noqa: F401  (a fixture)
from tests.test_product_lists import _upload, lists  # noqa: F401  (a fixture)

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1/products"


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


# ── Lists with fields, products with data (API) ───────────────────────────────

def test_a_list_gets_fields_and_its_products_carry_data(client, admin_headers, lists):
    made = lists("Juices.csv", "J-1")
    fields = [{"key": "lane", "label": "Lane", "type": "number"}, {"key": "recipe", "type": "text"}]
    res = client.put(f"{API}/lists/{made['id']}", json={"fields": fields}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert [f["key"] for f in res.json()["fields"]] == ["lane", "recipe"]
    assert res.json()["dropped"] == 0 and res.json()["name"] == "Juices"
    listed = next(entry for entry in client.get(f"{API}/lists", headers=admin_headers).json()["lists"] if entry["id"] == made["id"])
    assert listed["fields"] == [{"key": "lane", "label": "Lane", "type": "number"}, {"key": "recipe", "label": "recipe", "type": "text"}]

    added = client.post(API, json={"list_id": made["id"], "code": "J-2", "name": "Orange",
                                   "data": {"lane": 2, "recipe": "R-7"}}, headers=admin_headers)
    assert added.status_code == 201, added.text
    assert added.json()["data"] == {"lane": 2, "recipe": "R-7"}
    assert product_catalog.lookup("J-2", made["id"])["data"] == {"lane": 2, "recipe": "R-7"}
    assert [f["key"] for f in product_catalog.fields(made["id"])] == ["lane", "recipe"]
    bad = client.post(API, json={"list_id": made["id"], "code": "J-3", "name": "Grape", "data": {"lane": "two"}},
                      headers=admin_headers)
    assert bad.status_code == 422 and "lane" in bad.json()["detail"]
    assert client.get(f"{API}?list_id={made['id']}", headers=admin_headers).json()["total"] == 2

    # A product changed without "data" keeps it; a rename keeps the fields.
    product_id = added.json()["id"]
    assert client.put(f"{API}/{product_id}", json={"name": "Orange juice"}, headers=admin_headers).status_code == 200
    assert client.get(f"{API}/{product_id}", headers=admin_headers).json()["data"] == {"lane": 2, "recipe": "R-7"}
    renamed = client.put(f"{API}/lists/{made['id']}", json={"name": "Juice range"}, headers=admin_headers).json()
    assert [f["key"] for f in renamed["fields"]] == ["lane", "recipe"] and "dropped" not in renamed
    changed = client.put(f"{API}/{product_id}", json={"data": {"lane": "5"}}, headers=admin_headers).json()
    assert changed["data"] == {"lane": 5}

    # Lane becomes yes/no and recipe is deleted: both values go, and the answer says so.
    client.put(f"{API}/{product_id}", json={"data": {"lane": 2, "recipe": "R-7"}}, headers=admin_headers)
    res = client.put(f"{API}/lists/{made['id']}", json={"fields": [{"key": "lane", "type": "yes_no"}]}, headers=admin_headers)
    assert res.json()["dropped"] == 2
    assert client.get(f"{API}/{product_id}", headers=admin_headers).json()["data"] is None
    assert product_catalog.lookup("J-2", made["id"])["data"] is None
    assert client.put(f"{API}/lists/{made['id']}", json={}, headers=admin_headers).status_code == 422
    bad_fields = client.put(f"{API}/lists/{made['id']}", json={"fields": [{"key": "name", "type": "text"}]}, headers=admin_headers)
    assert bad_fields.status_code == 422 and "product itself" in bad_fields.json()["detail"]


def test_a_new_list_can_be_made_with_fields(client, admin_headers, lists):
    made = client.post(f"{API}/lists", json={"name": "Bottles", "fields": [{"key": "lane", "type": "number"}]}, headers=admin_headers)
    assert made.status_code == 201, made.text
    assert made.json()["fields"] == [{"key": "lane", "label": "lane", "type": "number"}]
    assert product_catalog.fields(made.json()["id"]) == made.json()["fields"]
    assert product_catalog.fields("no-such-list") == []


def test_an_operator_cannot_change_fields(client, admin_headers, operator_headers, lists):  # noqa: F811
    made = lists("Juices.csv", "J-1")
    res = client.put(f"{API}/lists/{made['id']}", json={"fields": [{"key": "lane", "type": "number"}]}, headers=operator_headers)
    assert res.status_code == 403
    assert client.get(f"{API}/lists", headers=operator_headers).status_code == 200
