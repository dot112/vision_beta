"""Up to four product lists; each camera that reads codes checks them against the list chosen for it."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.db.models.product import DEFAULT_LIST_ID
from app.services.line_config import (
    DEFAULT_PRODUCT_LIST_ID,
    PRIMARY_LINE_ID,
    SCHEMA_VERSION,
    normalize_line,
    readers_using_list,
    upgrade_state,
)
from app.services.product_service import MAX_PRODUCT_LISTS, ProductCatalog, list_name_from_file, product_catalog

API = "/api/v1/products"
ROOT = Path(__file__).resolve().parents[1]


def _csv(*codes: str) -> str:
    return "code,name,description\n" + "".join(f"{code},Product {code},\n" for code in codes)


def _upload(text: str, filename: str = "codes.csv") -> dict:
    return {"file": (filename, text, "text/csv")}


@pytest.fixture
def lists(client, admin_headers):
    """The Products API with no list at all; whatever a test leaves behind is removed again."""
    def wipe():
        for product_list in client.get(f"{API}/lists", headers=admin_headers).json()["lists"]:
            res = client.delete(f"{API}/lists/{product_list['id']}", headers=admin_headers)
            assert res.status_code == 200, res.text

    wipe()

    def imported(filename: str, *codes: str) -> dict:
        res = client.post(f"{API}/import", files=_upload(_csv(*codes), filename), headers=admin_headers)
        assert res.status_code == 200, res.text
        return res.json()["list"]

    yield imported
    wipe()


# ── The lists ─────────────────────────────────────────────────────────────────

def test_each_import_makes_a_list_and_the_fifth_is_refused(client, admin_headers, lists):
    made = [lists(f"Range {n}.csv", f"A-{n}", f"B-{n}") for n in range(1, MAX_PRODUCT_LISTS + 1)]
    assert [m["name"] for m in made] == ["Range 1", "Range 2", "Range 3", "Range 4"]
    assert all(m["count"] == 2 for m in made)

    listed = client.get(f"{API}/lists", headers=admin_headers).json()
    assert listed["max_lists"] == MAX_PRODUCT_LISTS == 4
    assert [entry["id"] for entry in listed["lists"]] == [m["id"] for m in made]  # oldest first
    assert all(entry["used_by"] == [] for entry in listed["lists"])

    fifth = client.post(f"{API}/import", files=_upload(_csv("X")), headers=admin_headers)
    assert fifth.status_code == 409 and "Delete one first" in fifth.json()["detail"]
    assert client.post(f"{API}/lists", json={"name": "Fifth"}, headers=admin_headers).status_code == 409
    assert len(client.get(f"{API}/lists", headers=admin_headers).json()["lists"]) == 4

    # Each list shows and exports its own codes.
    for number, entry in enumerate(made, start=1):
        shown = client.get(f"{API}?list_id={entry['id']}", headers=admin_headers).json()
        assert [p["code"] for p in shown["products"]] == [f"A-{number}", f"B-{number}"]
        exported = client.get(f"{API}/export?list_id={entry['id']}", headers=admin_headers)
        assert exported.text == _csv(f"A-{number}", f"B-{number}")
        assert f'filename="Range {number}.csv"' in exported.headers["content-disposition"]

    # After a list is deleted there is room again.
    assert client.delete(f"{API}/lists/{made[0]['id']}", headers=admin_headers).status_code == 200
    assert product_catalog.lookup("A-1", made[0]["id"]) is None
    assert client.post(f"{API}/import", files=_upload(_csv("X")), headers=admin_headers).status_code == 200


def test_two_files_with_the_same_name_make_two_lists(client, admin_headers, lists):
    first = lists("codes.csv", "A")
    second = lists("codes.csv", "A")
    assert (first["name"], second["name"]) == ("codes", "codes (2)")
    assert first["id"] != second["id"]
    named = client.post(f"{API}/import?name=Winter", files=_upload(_csv("W")), headers=admin_headers).json()["list"]
    assert named["name"] == "Winter"
    assert list_name_from_file("C:\\lists\\Summer range.CSV") == "Summer range"
    assert list_name_from_file("") == "Product list"


def test_the_lists_are_independent(client, admin_headers, lists):
    one = lists("one.csv", "SHARED", "ONLY-1")
    two = lists("two.csv", "SHARED", "ONLY-2")
    # The same code may be in both lists, once in each.
    assert product_catalog.lookup("SHARED", one["id"])["list_id"] == one["id"]
    assert product_catalog.lookup("SHARED", two["id"])["list_id"] == two["id"]
    assert product_catalog.lookup("ONLY-1", two["id"]) is None
    assert product_catalog.lookup("ONLY-1", None) is None
    assert product_catalog.lookup_any("ONLY-2")["code"] == "ONLY-2"

    again = client.post(API, json={"code": "SHARED", "name": "Twice", "list_id": one["id"]}, headers=admin_headers)
    assert again.status_code == 422 and "already in 'one'" in again.json()["detail"]
    added = client.post(API, json={"code": "NEW", "name": "Only in two", "list_id": two["id"]}, headers=admin_headers)
    assert added.status_code == 201 and added.json()["list_id"] == two["id"]
    assert client.get(f"{API}?list_id={two['id']}&search=only", headers=admin_headers).json()["total"] == 2
    assert client.get(f"{API}?list_id={one['id']}&search=only in", headers=admin_headers).json()["total"] == 0

    # Changing and deleting a code touches its own list only.
    changed = client.put(f"{API}/{added.json()['id']}", json={"code": "SHARED-2"}, headers=admin_headers)
    assert changed.status_code == 200 and product_catalog.lookup("SHARED-2", two["id"]) is not None
    clash = client.put(f"{API}/{added.json()['id']}", json={"code": "SHARED"}, headers=admin_headers)
    assert clash.status_code == 422
    assert client.delete(f"{API}/{added.json()['id']}", headers=admin_headers).status_code == 200
    assert product_catalog.lookup("SHARED-2", two["id"]) is None and product_catalog.count(one["id"]) == 2

    assert client.get(f"{API}?list_id=nope", headers=admin_headers).status_code == 404
    assert client.post(API, json={"code": "A", "name": "B", "list_id": "nope"}, headers=admin_headers).status_code == 404


def test_rename_keeps_the_id_and_names_stay_unique(client, admin_headers, lists):
    one = lists("one.csv", "A")
    lists("two.csv", "A")
    renamed = client.put(f"{API}/lists/{one['id']}", json={"name": "  Summer   range "}, headers=admin_headers)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Summer range" and renamed.json()["id"] == one["id"]
    assert product_catalog.list_name(one["id"]) == "Summer range"
    assert client.put(f"{API}/lists/{one['id']}", json={"name": "TWO"}, headers=admin_headers).status_code == 422
    assert client.put(f"{API}/lists/{one['id']}", json={"name": ""}, headers=admin_headers).status_code == 422
    assert client.put(f"{API}/lists/nope", json={"name": "X"}, headers=admin_headers).status_code == 404


def test_replace_contents_keeps_the_list_its_readers_use(client, admin_headers, lists):
    target = lists("range.csv", "KEEP", "DROP")
    other = lists("other.csv", "DROP")
    res = client.post(f"{API}/lists/{target['id']}/replace", files=_upload("code,name\nKEEP,Renamed\nNEW,Added\n"), headers=admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["added"], body["updated"], body["removed"], body["total"]) == (1, 1, 1, 2)
    assert body["list"]["id"] == target["id"] and body["list"]["name"] == "range"
    assert product_catalog.lookup("KEEP", target["id"])["name"] == "Renamed"
    assert product_catalog.lookup("NEW", target["id"]) is not None
    assert product_catalog.lookup("DROP", target["id"]) is None
    # The other list is untouched.
    assert product_catalog.lookup("DROP", other["id"]) is not None
    # A file that cannot be read changes nothing.
    bad = client.post(f"{API}/lists/{target['id']}/replace", files=_upload("sku,label\n1,2\n"), headers=admin_headers)
    assert bad.status_code == 422 and product_catalog.count(target["id"]) == 2
    assert client.post(f"{API}/lists/nope/replace", files=_upload(_csv("A")), headers=admin_headers).status_code == 404


def test_a_reader_never_sees_a_list_empty_while_it_is_replaced():
    catalog = ProductCatalog()
    rows = [{"code": f"C{n}", "name": "x"} for n in range(20000)]
    catalog.replace_list("l1", "List", rows)
    missing = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            if catalog.lookup("C7", "l1") is None:
                missing.append(True)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(15):
            catalog.replace_list("l1", "List", rows)
    finally:
        stop.set()
        thread.join()
    assert missing == []


def test_a_list_a_reader_uses_cannot_be_deleted(client, admin_headers, lists):
    used = lists("used.csv", "A")
    camera = client.post("/api/v1/cameras", json={"name": "Reader cam", "type": "usb", "source": "97", "settings": {}}, headers=admin_headers).json()
    line = client.post("/api/v1/lines", json={
        "name": "Packing 7",
        "cameras": [{"camera_id": camera["id"], "role": "qr", "product_list_id": used["id"]}],
    }, headers=admin_headers)
    assert line.status_code == 201, line.text
    try:
        listed = client.get(f"{API}/lists", headers=admin_headers).json()["lists"]
        assert listed[0]["used_by"] == [{
            "line_id": line.json()["id"], "line_name": "Packing 7", "camera_id": camera["id"], "camera_name": "Reader cam",
        }]
        refused = client.delete(f"{API}/lists/{used['id']}", headers=admin_headers)
        assert refused.status_code == 409
        assert "Packing 7 (Reader cam)" in refused.json()["detail"]
        assert product_catalog.lookup("A", used["id"]) is not None

        # A reader cannot be set to a list that does not exist.
        missing = client.put(f"/api/v1/lines/{line.json()['id']}", json={
            "cameras": [{"camera_id": camera["id"], "role": "qr", "product_list_id": "list-gone"}],
        }, headers=admin_headers)
        assert missing.status_code == 422 and "product list" in missing.json()["detail"]

        # A client that names no list (written for the single list) gets the oldest one.
        plain = client.put(f"/api/v1/lines/{line.json()['id']}", json={
            "cameras": [{"camera_id": camera["id"], "role": "qr"}],
        }, headers=admin_headers)
        assert plain.status_code == 200 and plain.json()["cameras"][0]["product_list_id"] == used["id"]
        assert plain.json()["status"]["cameras"][0]["product_list_id"] == used["id"]

        # Once the reader uses no list, the list can go.
        client.put(f"/api/v1/lines/{line.json()['id']}", json={
            "cameras": [{"camera_id": camera["id"], "role": "qr", "product_list_id": None}],
        }, headers=admin_headers)
        assert client.delete(f"{API}/lists/{used['id']}", headers=admin_headers).status_code == 200
    finally:
        client.delete(f"/api/v1/lines/{line.json()['id']}", headers=admin_headers)
        client.delete(f"/api/v1/cameras/{camera['id']}", headers=admin_headers)


def test_the_first_code_added_by_hand_makes_list_1(client, admin_headers, lists):
    assert client.get(API, headers=admin_headers).json() == {"list_id": None, "total": 0, "products": []}
    added = client.post(API, json={"code": "FIRST", "name": "First"}, headers=admin_headers)
    assert added.status_code == 201 and added.json()["list_id"] == DEFAULT_LIST_ID
    listed = client.get(f"{API}/lists", headers=admin_headers).json()["lists"]
    assert [(entry["id"], entry["name"], entry["count"]) for entry in listed] == [(DEFAULT_LIST_ID, "List 1", 1)]
    # Callers that name no list keep working on the oldest list.
    assert client.get(API, headers=admin_headers).json()["total"] == 1
    assert client.get(f"{API}/export", headers=admin_headers).text == "code,name,description\nFIRST,First,\n"


def test_lists_are_still_there_after_a_restart(client, admin_headers, lists):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.config import settings
    from app.services.product_service import ProductService

    one = lists("one.csv", "A", "B")
    two = lists("two.csv", "C")
    product_catalog.replace_all({})  # what a stopped server remembers
    assert product_catalog.lookup("A", one["id"]) is None

    async def startup():
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with AsyncSession(engine) as db:
                return await ProductService.refresh_catalog(db)
        finally:
            await engine.dispose()

    assert asyncio.run(startup()) == 3
    assert product_catalog.list_ids() == [one["id"], two["id"]]
    assert product_catalog.lookup("A", one["id"])["name"] == "Product A"
    assert product_catalog.lookup("C", two["id"]) is not None and product_catalog.lookup("C", one["id"]) is None


# ── Readers and their list ────────────────────────────────────────────────────

def test_two_readers_on_two_lists_give_different_results_for_the_same_code(monkeypatch):
    from app.services.counting_service import CountingService
    from app.services.line_service import LineRuntime

    catalog = ProductCatalog()
    catalog.replace_list("list-a", "A", [{"code": "SKU-1", "name": "Widget"}])
    catalog.replace_list("list-b", "B", [{"code": "SKU-2", "name": "Gadget"}])
    monkeypatch.setattr("app.services.product_service.product_catalog", catalog)

    line = normalize_line({"name": "Two readers", "cameras": [
        {"camera_id": "reader-a", "role": "qr", "product_list_id": "list-a"},
        {"camera_id": "reader-b", "role": "qr", "product_list_id": "list-b"},
    ]})
    runtime = LineRuntime("line-x", "Two readers", CountingService("line-x", "Two readers", dispatch_telemetry=False))
    runtime.configure(line, {})

    on_a = runtime.on_qr_read("reader-a", "SKU-1", "QRCODE")
    on_b = runtime.on_qr_read("reader-b", "SKU-1", "QRCODE")
    assert (on_a["known"], on_a["product_name"], on_a["product_list_id"]) == (True, "Widget", "list-a")
    assert (on_b["known"], on_b["product_name"], on_b["product_list_id"]) == (False, None, "list-b")
    assert runtime.qr_stats["known"] == 1 and runtime.qr_stats["unknown"] == 1


def test_a_reader_without_a_list_knows_no_code():
    line = normalize_line({"name": "A", "cameras": [{"camera_id": "r", "role": "qr"}, {"camera_id": "v"}]})
    reader, vision = line["cameras"]
    assert reader["product_list_id"] is None
    assert "product_list_id" not in vision  # only cameras that read codes have a list
    with pytest.raises(ValueError, match="product list"):
        normalize_line({"name": "A", "cameras": [{"camera_id": "r", "role": "qr", "product_list_id": 7}]})
    assert readers_using_list([line], "list-1") == []
    line["cameras"][0]["product_list_id"] = "list-1"
    assert [(ln["name"], cam["camera_id"]) for ln, cam in readers_using_list([line], "list-1")] == [("A", "r")]


def test_readers_of_an_existing_setup_keep_checking_the_first_list():
    assert DEFAULT_PRODUCT_LIST_ID == DEFAULT_LIST_ID
    state = {"schema_version": 3, "camera_auto_connect": False, "lines": [
        {"id": PRIMARY_LINE_ID, "name": "Line 1", "enabled": True, "cameras": [
            {"camera_id": "vis", "role": "vision", "counting": True},
            {"camera_id": "qr", "role": "qr", "qr_trigger": "continuous"},
        ]},
        {"id": "line-2", "name": "Line 2", "enabled": True, "cameras": [
            {"camera_id": "both", "role": "vision", "counting": True, "read_codes": True},
        ]},
    ]}
    before = copy.deepcopy(state)
    assert upgrade_state(state) is True and state["schema_version"] == SCHEMA_VERSION
    assert "product_list_id" not in state["lines"][0]["cameras"][0]
    assert state["lines"][0]["cameras"][1]["product_list_id"] == DEFAULT_LIST_ID
    assert state["lines"][1]["cameras"][0]["product_list_id"] == DEFAULT_LIST_ID
    # Nothing else about the cameras changed (later steps add other things to the file).
    for line in state["lines"]:
        for camera in line["cameras"]:
            for added in ("product_list_id", "model_id", "expected_classes", "defect_classes"):
                camera.pop(added, None)
    assert [line["cameras"] for line in state["lines"]] == [line["cameras"] for line in before["lines"]]


# ── The database upgrade ──────────────────────────────────────────────────────

def _alembic(tmp_path, name):
    from alembic.config import Config

    engine = sa.create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return engine, config


def _products_shape(conn):
    inspector = sa.inspect(conn)
    columns = sorted(column["name"] for column in inspector.get_columns("products"))
    indexes = sorted((index["name"], tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes("products"))
    return columns, indexes


def test_existing_product_codes_move_into_list_1(tmp_path):
    from alembic import command

    engine, config = _alembic(tmp_path, "old.db")
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "0004_products")
            # A database from before product lists: the products table as it was made then.
            conn.execute(sa.text("DROP TABLE products"))
            conn.execute(sa.text(
                "CREATE TABLE products (id VARCHAR(36) NOT NULL, code VARCHAR(256) NOT NULL, name VARCHAR(128) NOT NULL, "
                "description VARCHAR(512), created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), "
                "updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), PRIMARY KEY (id))"
            ))
            conn.execute(sa.text("CREATE UNIQUE INDEX ix_products_code ON products (code)"))
            conn.execute(sa.text("INSERT INTO products (id, code, name) VALUES ('p1', 'SKU-1', 'Widget'), ('p2', 'SKU-2', 'Gadget')"))
            assert "product_lists" not in sa.inspect(conn).get_table_names()

            command.upgrade(config, "head")

            assert conn.execute(sa.text("SELECT id, name FROM product_lists")).all() == [(DEFAULT_LIST_ID, "List 1")]
            rows = conn.execute(sa.text("SELECT code, name, list_id FROM products ORDER BY code")).all()
            assert rows == [("SKU-1", "Widget", DEFAULT_LIST_ID), ("SKU-2", "Gadget", DEFAULT_LIST_ID)]
            # The same code may now be in a second list, but only once per list.
            conn.execute(sa.text("INSERT INTO product_lists (id, name) VALUES ('list-b', 'B')"))
            conn.execute(sa.text("INSERT INTO products (id, code, name, list_id) VALUES ('p3', 'SKU-1', 'Other', 'list-b')"))
            with pytest.raises(sa.exc.IntegrityError):
                with conn.begin_nested():
                    conn.execute(sa.text("INSERT INTO products (id, code, name, list_id) VALUES ('p4', 'SKU-1', 'Twice', 'list-b')"))
            upgraded = _products_shape(conn)
        # Running the upgrade again changes nothing.
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            assert conn.execute(sa.text("SELECT COUNT(*) FROM products")).scalar_one() == 3
            assert conn.execute(sa.text("SELECT COUNT(*) FROM product_lists")).scalar_one() == 2
    finally:
        engine.dispose()

    # A new database ends up with the same columns and indexes, and with "List 1".
    engine, config = _alembic(tmp_path, "new.db")
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            assert conn.execute(sa.text("SELECT id, name FROM product_lists")).all() == [(DEFAULT_LIST_ID, "List 1")]
            assert _products_shape(conn) == upgraded
            assert upgraded[1] == [("ix_products_code", ("code",), False), ("uq_products_list_code", ("list_id", "code"), True)]
    finally:
        engine.dispose()


# ── Upload limit ──────────────────────────────────────────────────────────────

def test_replace_uploads_are_size_limited_before_they_are_stored():
    from app.middleware.request_body_limit import RequestBodyLimitMiddleware

    limiter = RequestBodyLimitMiddleware(None, {"/api/v1/products/import": 10, "/api/v1/products/lists/*": 20})
    assert limiter._limit_for("/api/v1/products/import") == 10
    assert limiter._limit_for("/api/v1/products/lists/list-1/replace") == 20
    assert limiter._limit_for("/api/v1/products") is None
    assert limiter._limit_for("/api/v1/products/lists") is None
