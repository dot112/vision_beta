"""Which requests the configuration lock blocks.

Every route of the protected API that is not a GET is either locked or free while the
configuration is locked. A route added later is in neither list and fails the first test
until it is put in one on purpose (the lock blocks it meanwhile: it fails closed).
"""
from __future__ import annotations

from app.security.config_lock import LOCK_FREE_ROUTES, LOCKED_ROUTES, blocks_when_locked


def _all_changing_routes():
    import main

    seen = set()
    for module in main.PROTECTED_ROUTERS:
        for route in module.router.routes:
            for method in route.methods - {"GET", "HEAD"}:
                seen.add((method, "/api/v1" + route.path))
    return seen


def test_every_changing_route_is_classified():
    routes = _all_changing_routes()
    assert routes == LOCKED_ROUTES | LOCK_FREE_ROUTES, sorted(routes ^ (LOCKED_ROUTES | LOCK_FREE_ROUTES))
    assert not LOCKED_ROUTES & LOCK_FREE_ROUTES


def test_running_the_line_is_free_and_setup_is_locked():
    assert not blocks_when_locked("POST", "/api/v1/lines/{line_id}/start")
    assert not blocks_when_locked("PUT", "/api/v1/lines/{line_id}/batch")
    assert not blocks_when_locked("POST", "/api/v1/lines/{line_id}/trigger")
    assert not blocks_when_locked("GET", "/api/v1/lines")
    assert blocks_when_locked("PUT", "/api/v1/lines/{line_id}")
    assert blocks_when_locked("POST", "/api/v1/cameras/{camera_id}/connect")
    assert blocks_when_locked("POST", "/api/v1/products/import")
    assert blocks_when_locked("POST", "/api/v1/brand/new")  # unknown: fail closed
