"""Which requests the configuration lock blocks.

While the configuration is locked, every request that is not a GET is refused unless its
route is in LOCK_FREE_ROUTES: running the line, tests and checks that save nothing, and the
lock itself. LOCKED_ROUTES names the rest, so that a test can tell a route nobody classified
(it is blocked meanwhile: the lock fails closed).

Routes are (method, path as the route declares it), e.g. ("POST", "/api/v1/lines/{line_id}/start").
"""
from __future__ import annotations

from typing import FrozenSet, Tuple

LOCKED_MESSAGE = "The configuration is locked. An Admin, or a Level 2 user with the lock code, can unlock it."

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}

Route = Tuple[str, str]

LOCK_FREE_ROUTES: FrozenSet[Route] = frozenset({
    # Running the line.
    ("POST", "/api/v1/lines/{line_id}/start"),
    ("POST", "/api/v1/lines/{line_id}/stop"),
    ("PUT", "/api/v1/lines/{line_id}/batch"),
    ("POST", "/api/v1/lines/{line_id}/trigger"),
    ("POST", "/api/v1/counting/reset"),
    ("POST", "/api/v1/alarms/{alarm_id}/acknowledge"),
    ("POST", "/api/v1/control/trigger/{camera_id}"),
    ("POST", "/api/v1/actions/{action_id}/execute"),
    # Inspection.
    ("POST", "/api/v1/vision/detect"),
    ("POST", "/api/v1/vision/detect/camera/{camera_id}"),
    ("POST", "/api/v1/qr/decode"),
    ("POST", "/api/v1/qr/decode/camera/{camera_id}"),
    ("POST", "/api/v1/qr/mobile-scan"),
    # Line setup's text reader: a held picture and the reading of unsaved settings (nothing saved).
    ("POST", "/api/v1/ocr/picture/camera/{camera_id}"),
    ("POST", "/api/v1/ocr/preview/camera/{camera_id}"),
    # Line setup's pattern check: a held picture and the check of unsaved settings (nothing saved).
    ("POST", "/api/v1/match/picture/camera/{camera_id}"),
    ("POST", "/api/v1/match/preview/camera/{camera_id}"),
    # Tests and checks: they send a test or read something, and save nothing.
    ("POST", "/api/v1/system/endpoints/{endpoint_id}/test"),
    ("POST", "/api/v1/send/actions/{card_id}/test"),
    ("POST", "/api/v1/plc/actions/{card_id}/test"),
    ("POST", "/api/v1/flows/{flow_id}/test"),
    ("POST", "/api/v1/cameras/{camera_id}/trigger"),
    ("POST", "/api/v1/rules/evaluate"),
    ("POST", "/api/v1/lines/{line_id}/formula/check"),
    ("POST", "/api/v1/products/lists/{list_id}/code/check"),
    ("POST", "/api/v1/lines/{line_id}/plc-inputs/{input_id}/read"),
    # A saved copy of a line's setup changes nothing that runs.
    ("POST", "/api/v1/lines/{line_id}/setups"),
    ("POST", "/api/v1/plc/opcua/scan"),
    ("POST", "/api/v1/comms/tcp/check"),
    ("POST", "/api/v1/comms/tcp/test"),
    ("POST", "/api/v1/comms/webhook/test"),
    ("POST", "/api/v1/comms/endpoints/{endpoint_id}/test"),
    # The MQTT session.
    ("POST", "/api/v1/mqtt/connect"),
    ("POST", "/api/v1/mqtt/disconnect"),
    ("POST", "/api/v1/mqtt/publish"),
    ("POST", "/api/v1/mqtt/subscribe"),
    # Data and backups: they change no configuration.
    ("POST", "/api/v1/system/backups"),
    ("POST", "/api/v1/system/data/clock-ok"),
    ("POST", "/api/v1/system/data/notices/{code}/ok"),
    # The lock itself.
    ("POST", "/api/v1/system/lock"),
    ("POST", "/api/v1/system/unlock"),
    ("PUT", "/api/v1/system/lock/code"),
    ("DELETE", "/api/v1/system/lock/code"),
})

LOCKED_ROUTES: FrozenSet[Route] = frozenset({
    # Lines and Line setup.
    ("POST", "/api/v1/lines"),
    ("PUT", "/api/v1/lines/{line_id}"),
    ("DELETE", "/api/v1/lines/{line_id}"),
    ("POST", "/api/v1/lines/{line_id}/clone"),
    ("POST", "/api/v1/lines/{line_id}/setups/{setup_id}/load"),
    ("DELETE", "/api/v1/lines/{line_id}/setups/{setup_id}"),
    # Line setup's pattern check: its training draft and Train write files a saved card may name.
    ("PUT", "/api/v1/match/draft/camera/{camera_id}"),
    ("PUT", "/api/v1/match/draft/camera/{camera_id}/mask"),
    ("POST", "/api/v1/match/draft/camera/{camera_id}/samples"),
    ("DELETE", "/api/v1/match/draft/camera/{camera_id}/samples/{n}"),
    ("POST", "/api/v1/match/draft/camera/{camera_id}/train"),
    ("POST", "/api/v1/match/draft/camera/{camera_id}/edit"),
    ("DELETE", "/api/v1/match/draft/camera/{camera_id}"),
    ("POST", "/api/v1/send/actions/batch"),
    ("POST", "/api/v1/plc/actions"),
    ("POST", "/api/v1/plc/actions/batch"),
    ("DELETE", "/api/v1/plc/actions/{card_id}"),
    ("POST", "/api/v1/plc/actions/reload"),
    ("POST", "/api/v1/counting/config"),
    # Cameras.
    ("POST", "/api/v1/cameras"),
    ("PUT", "/api/v1/cameras/{camera_id}"),
    ("PATCH", "/api/v1/cameras/{camera_id}"),
    ("DELETE", "/api/v1/cameras/{camera_id}"),
    ("POST", "/api/v1/cameras/{camera_id}/connect"),
    ("POST", "/api/v1/cameras/{camera_id}/disconnect"),
    ("POST", "/api/v1/system/ip-cameras"),
    ("DELETE", "/api/v1/system/ip-cameras/{cam_id}"),
    # Connections and settings.
    ("POST", "/api/v1/system/settings"),
    ("PATCH", "/api/v1/system/settings"),
    ("POST", "/api/v1/system/endpoints"),
    ("PUT", "/api/v1/system/endpoints/{endpoint_id}"),
    ("DELETE", "/api/v1/system/endpoints/{endpoint_id}"),
    ("POST", "/api/v1/mqtt/certs/upload"),
    ("DELETE", "/api/v1/mqtt/certs/{filename}"),
    ("POST", "/api/v1/plc/opcua/certs/upload"),
    # AI models.
    ("POST", "/api/v1/models/register"),
    ("POST", "/api/v1/models/upload"),
    ("POST", "/api/v1/models/{model_id}/activate"),
    ("PATCH", "/api/v1/models/{model_id}/mode"),
    ("DELETE", "/api/v1/models/{model_id}"),
    # Product lists.
    ("POST", "/api/v1/products/lists"),
    ("PUT", "/api/v1/products/lists/{list_id}"),
    ("DELETE", "/api/v1/products/lists/{list_id}"),
    ("POST", "/api/v1/products/lists/{list_id}/replace"),
    ("POST", "/api/v1/products/import"),
    ("POST", "/api/v1/products"),
    ("PUT", "/api/v1/products/{product_id}"),
    ("DELETE", "/api/v1/products/{product_id}"),
    # Rules, flows and actions.
    ("POST", "/api/v1/rules"),
    ("PUT", "/api/v1/rules/{rule_id}"),
    ("DELETE", "/api/v1/rules/{rule_id}"),
    ("POST", "/api/v1/flows"),
    ("PUT", "/api/v1/flows/{flow_id}"),
    ("DELETE", "/api/v1/flows/{flow_id}"),
    ("POST", "/api/v1/flows/{flow_id}/deploy"),
    ("POST", "/api/v1/actions"),
    ("PUT", "/api/v1/actions/{action_id}"),
    ("DELETE", "/api/v1/actions/{action_id}"),
})


def blocks_when_locked(method: str, route_path: str) -> bool:
    """Whether a request to this route is refused while the configuration is locked."""
    method = method.upper()
    if method in _READ_METHODS:
        return False
    return (method, route_path) not in LOCK_FREE_ROUTES
