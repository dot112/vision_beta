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


# ── CSV files with fields ─────────────────────────────────────────────────────

JUICES = ("code,name,description,Lane,Recipe,Fragile,Note\n"
          'J-1,Apple,,1,R-1,yes,"1,5"\nJ-2,Orange,,2,R-2,no,2\nJ-3,Grape,,,R-3,,\n')


def _juices(client, admin_headers):
    res = client.post(f"{API}/import", files=_upload(JUICES, "Juices.csv"), headers=admin_headers)
    assert res.status_code == 200, res.text
    return res.json()["list"]


def _data(client, admin_headers, list_id):
    products = client.get(f"{API}?list_id={list_id}", headers=admin_headers).json()["products"]
    return {p["code"]: p["data"] for p in products}


def test_csv_columns_get_their_types(client, admin_headers, lists):
    made = _juices(client, admin_headers)
    assert [(f["key"], f["label"], f["type"]) for f in made["fields"]] == [
        ("lane", "Lane", "number"), ("recipe", "Recipe", "text"), ("fragile", "Fragile", "yes_no"), ("note", "Note", "text")]
    shown = _data(client, admin_headers, made["id"])
    assert shown["J-1"] == {"lane": 1, "recipe": "R-1", "fragile": True, "note": "1,5"}
    assert shown["J-2"] == {"lane": 2, "recipe": "R-2", "fragile": False, "note": "2"}
    assert shown["J-3"] == {"recipe": "R-3"}            # empty cells are missing, not 0 / "" / no
    # A file without extra columns makes a list without fields, as before.
    plain = lists("Plain.csv", "A-1")
    assert plain["fields"] == []


def test_an_exported_list_replaces_itself_unchanged(client, admin_headers, lists):
    made = _juices(client, admin_headers)
    exported = client.get(f"{API}/export?list_id={made['id']}", headers=admin_headers).text
    assert exported.splitlines()[0] == "code,name,description,lane,recipe,fragile,note"
    assert exported.splitlines()[1] == 'J-1,Apple,,1,R-1,yes,"1,5"'
    assert exported.splitlines()[3] == "J-3,Grape,,,R-3,,"
    again = client.post(f"{API}/lists/{made['id']}/replace", files=_upload(exported), headers=admin_headers)
    assert again.status_code == 200, again.text
    assert (again.json()["added"], again.json()["updated"], again.json()["removed"]) == (0, 0, 0)


def test_replace_keeps_fields_the_file_has_no_column_for(client, admin_headers, lists):
    made = _juices(client, admin_headers)
    # An older file with only code and name: every product keeps its values.
    old = client.post(f"{API}/lists/{made['id']}/replace", files=_upload("code,name\nJ-1,Apple\nJ-2,Orange\nJ-3,Grape\n"),
                      headers=admin_headers)
    assert old.status_code == 200, old.text
    assert old.json()["updated"] == 0
    assert _data(client, admin_headers, made["id"])["J-1"] == {"lane": 1, "recipe": "R-1", "fragile": True, "note": "1,5"}
    # A Lane column (matched by its label, any case) replaces only the lanes.
    lanes = client.post(f"{API}/lists/{made['id']}/replace",
                        files=_upload("code,name,LANE\nJ-1,Apple,7\nJ-2,Orange,2\nJ-3,Grape,\n"), headers=admin_headers)
    assert lanes.json()["updated"] == 1
    shown = _data(client, admin_headers, made["id"])
    assert shown["J-1"] == {"lane": 7, "recipe": "R-1", "fragile": True, "note": "1,5"}
    assert shown["J-3"] == {"recipe": "R-3"}
    bad = client.post(f"{API}/lists/{made['id']}/replace", files=_upload("code,name,lane\nJ-1,Apple,seven\n"),
                      headers=admin_headers)
    assert bad.status_code == 422 and "Line 2" in bad.json()["detail"] and "lane" in bad.json()["detail"]


def test_replace_refuses_columns_the_list_has_no_field_for(client, admin_headers, lists):
    made = _juices(client, admin_headers)
    res = client.post(f"{API}/lists/{made['id']}/replace", files=_upload("code,name,Colour,Size\nJ-1,Apple,red,L\n"),
                      headers=admin_headers)
    assert res.status_code == 422
    assert "Colour" in res.json()["detail"] and "Size" in res.json()["detail"] and "field" in res.json()["detail"]
    assert _data(client, admin_headers, made["id"])["J-1"]["lane"] == 1


def test_a_formula_looking_text_value_is_exported_safely(client, admin_headers, lists):
    made = _juices(client, admin_headers)
    j1 = next(p for p in client.get(f"{API}?list_id={made['id']}", headers=admin_headers).json()["products"] if p["code"] == "J-1")
    client.put(f"{API}/{j1['id']}", json={"data": {"recipe": "=cmd|' /C calc'!A0", "lane": 1}}, headers=admin_headers)
    exported = client.get(f"{API}/export?list_id={made['id']}", headers=admin_headers).text
    assert "'=cmd|' /C calc'!A0" in exported.splitlines()[1]
    client.post(f"{API}/lists/{made['id']}/replace", files=_upload(exported), headers=admin_headers)
    assert _data(client, admin_headers, made["id"])["J-1"]["recipe"] == "=cmd|' /C calc'!A0"
