"""A product list says how its codes are built; the catalog reads each code by its list's format."""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from app.services.product_service import CodeMatch, ProductCatalog
from tests.test_product_lists import API, lists  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SPLIT = {"mode": "split", "separator": ";", "product_part": 1, "batch_part": 2}
PRODUCTS = [{"code": "P-100", "name": "Juice", "data": {"lane": 2}}]
GTIN = "09501101530003"


@pytest.fixture
def catalog():
    cat = ProductCatalog()
    cat.replace_all({"L": ("Juices", PRODUCTS), "G": ("Cartons", [{"code": GTIN, "name": "Carton"}]),
                     "W": ("Whole", [{"code": "P-100", "name": "Whole juice"}])},
                    formats={"L": SPLIT, "G": {"mode": "gs1"}})
    return cat


def test_match_follows_the_lists_format(catalog):
    m = catalog.match("P-100;B-2026-07", "L")
    assert (m.product["name"], m.product_id, m.batch, m.problem) == ("Juice", "P-100", "B-2026-07", None)
    assert catalog.lookup("P-100;B-2026-07", "L")["code"] == "P-100"
    assert catalog.lookup("P-100", "L") is None
    assert catalog.match("P-999;B-2026-07", "L") == CodeMatch(None, "P-999", "B-2026-07", None)


def test_a_code_that_does_not_fit_is_unknown(catalog):
    assert catalog.match("P-100", "L") == CodeMatch(None, None, None, "no separator")
    assert catalog.match("P-100;", "L") == CodeMatch(PRODUCTS[0], "P-100", None, "no batch part")


def test_a_batch_that_breaks_the_rules_is_dropped(catalog):
    for bad in ("B" * 65, "B\x07"):
        m = catalog.match(f"P-100;{bad}", "L")
        assert m.product is not None and m.batch is None and m.problem == "batch not allowed"
    assert catalog.match("P-100;" + "B" * 64, "L").batch == "B" * 64


def test_a_new_format_applies_to_the_next_read(catalog):
    catalog.replace_list("L", "Juices", PRODUCTS, code_format={"mode": "whole"})
    assert catalog.lookup("P-100", "L") is not None and catalog.lookup("P-100;B-1", "L") is None
    catalog.replace_list("L", "Juices", PRODUCTS)                       # no format sent: it keeps its format
    assert catalog.lookup("P-100", "L") is not None


def test_lookup_any_uses_each_lists_format(catalog):
    assert catalog.lookup_any(f"(01){GTIN}(10)X")["code"] == GTIN
    assert catalog.lookup_any("P-100;B-1")["name"] == "Juice"
    assert catalog.lookup_any("P-100")["name"] == "Whole juice"


def test_a_whole_list_reads_as_before(catalog):
    assert catalog.match(" P-100 ", "W") == CodeMatch({"code": "P-100", "name": "Whole juice"}, "P-100", None, None)
    assert catalog.lookup("P-100;B", "W") is None
    assert catalog.match("P-100", None) == CodeMatch(None, None, None, None)


def test_an_unreadable_saved_format_reads_whole_codes():
    cat = ProductCatalog()
    cat.replace_all({"L": ("Juices", PRODUCTS)}, formats={"L": {"mode": "regex"}})
    assert cat.formats() == {"L": {"mode": "whole"}} and cat.lookup("P-100", "L") is not None


def test_try_a_format_that_is_not_saved(catalog):
    m = catalog.match("x|P-100|B7", "W", fmt={"mode": "split", "separator": "|", "product_part": 2, "batch_part": 3})
    assert (m.product["name"], m.batch) == ("Whole juice", "B7")


# ── The API ───────────────────────────────────────────────────────────────────

def test_a_list_saves_and_returns_its_format(client, admin_headers, lists):  # noqa: F811
    made = lists("Cartons.csv", "P-100")
    assert made["code_format"] == {"mode": "whole"}
    res = client.put(f"{API}/lists/{made['id']}", json={"code_format": SPLIT}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["code_format"] == SPLIT
    shown = next(pl for pl in client.get(f"{API}/lists", headers=admin_headers).json()["lists"] if pl["id"] == made["id"])
    assert shown["code_format"] == SPLIT
    bad = client.put(f"{API}/lists/{made['id']}", json={"code_format": {**SPLIT, "separator": ""}}, headers=admin_headers)
    assert bad.status_code == 422 and "Code format" in bad.json()["detail"]
    audit = client.get("/api/v1/system/audit-logs?limit=20", headers=admin_headers).json()
    rows = audit if isinstance(audit, list) else audit.get("logs", audit.get("items", []))
    assert any("code format of 'Cartons'" in str(row.get("details")) for row in rows)
    created = client.post(f"{API}/lists", json={"name": "GS1 cartons", "code_format": {"mode": "gs1"}}, headers=admin_headers)
    assert created.status_code == 201 and created.json()["code_format"] == {"mode": "gs1"}


def test_try_a_code(client, admin_headers, lists):  # noqa: F811
    made = lists("Cartons.csv", "P-100")
    url = f"{API}/lists/{made['id']}/code/check"
    plain = client.post(url, json={"code": "P-100"}, headers=admin_headers).json()
    assert plain == {"product_id": "P-100", "batch": None, "problem": None, "product": plain["product"]}
    assert plain["product"]["code"] == "P-100"
    tried = client.post(url, json={"code": "P-100;B-2026-07", "code_format": SPLIT}, headers=admin_headers).json()
    assert (tried["product_id"], tried["batch"], tried["problem"]) == ("P-100", "B-2026-07", None)
    unknown = client.post(url, json={"code": "P-999;B-1", "code_format": SPLIT}, headers=admin_headers).json()
    assert unknown["product"] is None and unknown["product_id"] == "P-999"
    assert client.post(url, json={"code": "P-1", "code_format": {"mode": "x"}}, headers=admin_headers).status_code == 422
    assert client.post(url, json={"code": ""}, headers=admin_headers).status_code == 422
    assert client.post(url, json={"code": "x" * 513}, headers=admin_headers).status_code == 422
    assert client.post(f"{API}/lists/list-nope/code/check", json={"code": "P-1"}, headers=admin_headers).status_code == 404


def test_migration_0008_up_and_down(tmp_path):
    from alembic import command
    from alembic.config import Config

    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0007_product_data")
        if "code_format" in {c["name"] for c in sa.inspect(conn).get_columns("product_lists")}:
            with conn.begin_nested():
                conn.execute(sa.text("ALTER TABLE product_lists DROP COLUMN code_format"))
        conn.execute(sa.text("INSERT INTO product_lists (id, name) VALUES ('list-x', 'Old list')"))
        command.upgrade(config, "head")
        assert conn.execute(sa.text("SELECT code_format FROM product_lists WHERE id='list-x'")).scalar_one() == '{"mode": "whole"}'
        command.upgrade(config, "head")                                     # again: nothing changes
        command.downgrade(config, "0007_product_data")
        assert "code_format" not in {c["name"] for c in sa.inspect(conn).get_columns("product_lists")}
