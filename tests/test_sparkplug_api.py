"""Sparkplug B is a setting of an MQTT channel: its fields, and what a save refuses."""
from __future__ import annotations

ENDPOINTS = "/api/v1/system/endpoints"


def test_sparkplug_settings_of_a_channel(client, admin_headers):
    made = []

    def post(body):
        res = client.post(ENDPOINTS, json=body, headers=admin_headers)
        if res.status_code == 200:
            made.append(res.json()["id"])
        return res

    def put(channel_id, body):
        return client.put(f"{ENDPOINTS}/{channel_id}", json=body, headers=admin_headers)

    base = {"name": "Spb test broker", "protocol": "mqtt", "host": "127.0.0.1", "port": 1, "enabled": False}
    try:
        plain = post(base)
        assert plain.status_code == 200, plain.text
        channel_id = plain.json()["id"]
        assert {k: v for k, v in plain.json().items() if k.startswith("sparkplug_")} == {
            "sparkplug_enabled": False, "sparkplug_group_id": "", "sparkplug_node_id": "", "sparkplug_host_id": "",
            "sparkplug_allow_commands": False, "sparkplug_interval_ms": 1000}

        on = put(channel_id, {"protocol": "mqtt", "sparkplug_enabled": True, "sparkplug_group_id": " PlantA ",
                              "sparkplug_node_id": "Vision1", "sparkplug_allow_commands": True, "sparkplug_interval_ms": 250})
        assert on.status_code == 200, on.text
        assert on.json()["sparkplug_group_id"] == "PlantA" and on.json()["sparkplug_interval_ms"] == 250

        # A save that leaves the fields out keeps them.
        kept = put(channel_id, {"protocol": "mqtt", "name": "Spb renamed"})
        assert kept.json()["sparkplug_enabled"] is True and kept.json()["sparkplug_allow_commands"] is True
        assert kept.json()["sparkplug_node_id"] == "Vision1" and kept.json()["sparkplug_interval_ms"] == 250

        for bad, word in (({"sparkplug_group_id": ""}, "group"), ({"sparkplug_node_id": "a/b"}, "node"),
                          ({"sparkplug_host_id": "h#"}, "host"), ({"sparkplug_interval_ms": 50}, "interval"),
                          ({"sparkplug_enabled": "yes"}, "sparkplug")):
            res = put(channel_id, {"protocol": "mqtt", **bad})
            assert res.status_code == 422 and word in res.json()["detail"].lower(), res.text
        # A refused save changed nothing.
        saved = next(e for e in client.get(ENDPOINTS, headers=admin_headers).json() if e["id"] == channel_id)
        assert (saved["sparkplug_group_id"], saved["sparkplug_node_id"], saved["sparkplug_host_id"]) == ("PlantA", "Vision1", "")

        # IDs are only checked while Sparkplug is on: a channel without it needs none.
        assert put(channel_id, {"protocol": "mqtt", "sparkplug_enabled": False, "sparkplug_group_id": ""}).status_code == 200
        assert put(channel_id, {"protocol": "mqtt", "sparkplug_enabled": True, "sparkplug_group_id": "PlantA"}).status_code == 200

        # The same node on the same broker twice is one edge node connected twice.
        twin = post({**base, "name": "Spb twin", "sparkplug_enabled": True, "sparkplug_group_id": "PlantA", "sparkplug_node_id": "Vision1"})
        assert twin.status_code == 422 and "already" in twin.json()["detail"], twin.text
        other = post({**base, "name": "Spb other", "sparkplug_enabled": True, "sparkplug_group_id": "PlantA", "sparkplug_node_id": "Vision2"})
        assert other.status_code == 200, other.text
        elsewhere = post({**base, "name": "Spb elsewhere", "port": 2, "sparkplug_enabled": True,
                          "sparkplug_group_id": "PlantA", "sparkplug_node_id": "Vision1"})
        assert elsewhere.status_code == 200, elsewhere.text
    finally:
        for channel_id in made:
            client.delete(f"{ENDPOINTS}/{channel_id}", headers=admin_headers)
