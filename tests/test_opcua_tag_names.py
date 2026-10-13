"""OPC UA: an address can be a tag's name; the driver finds its full NodeId on the server.

On a Siemens S7-1200/1500 the PLC tag ``Tag_1`` is the node ``ns=3;s="Tag_1"``:
its own namespace, and the name in double quotes. Other servers use other
namespaces and no quotes. The driver looks the name up once and remembers it.
The stand-in server below is laid out like a real S7-1500 (checked 2026-10-13).
"""
from __future__ import annotations

import asyncio

import pytest
from asyncua import ua

from app.hardware.plc.opcua_driver import OPCUADriver, siemens_identifier

SIEMENS = ["http://opcfoundation.org/UA/", "urn:SIMATIC.S7-1500.OPC-UA.Application:PLC_1",
           "http://opcfoundation.org/UA/DI/", "http://www.siemens.com/simatic-s7-opcua"]
PLAIN = ["http://opcfoundation.org/UA/", "urn:server", "http://example.com/plc"]


class _Id:
    def __init__(self, text):
        self.text = text

    def to_string(self):
        return self.text


class _Node:
    def __init__(self, server, node_id):
        self.server, self.node_id, self.nodeid = server, node_id, _Id(node_id)

    def _known(self):
        if self.node_id not in self.server.values and self.node_id not in self.server.tree:
            raise ua.uaerrors.BadNodeIdUnknown()

    async def read_node_class(self):
        self._known()
        return ua.NodeClass.Variable if self.node_id in self.server.values else ua.NodeClass.Object

    async def read_value(self):
        self._known()
        return self.server.values[self.node_id]

    async def read_data_type_as_variant_type(self):
        self._known()
        value = self.server.values[self.node_id]
        return ua.VariantType.Boolean if isinstance(value, bool) else ua.VariantType.Int16

    async def write_value(self, data_value):
        self._known()
        self.server.writes.append((self.node_id, data_value.Value.Value))
        self.server.values[self.node_id] = data_value.Value.Value

    async def get_children_descriptions(self, **kwargs):
        self.server.browsed += 1
        rows = []
        for child, name in self.server.tree.get(self.node_id, []):
            rows.append(ua.ReferenceDescription(
                NodeId=ua.NodeId.from_string(child), BrowseName=ua.QualifiedName(name, 2), DisplayName=ua.LocalizedText(name),
                NodeClass=ua.NodeClass.Variable if child in self.server.values else ua.NodeClass.Object))
        return rows


class _Server:
    """What the driver uses of an asyncua client, over a dict of nodes."""

    def __init__(self, namespaces, values, tree=None):
        self.namespaces, self.values, self.tree = namespaces, dict(values), tree or {}
        self.asked, self.writes, self.browsed = [], [], 0

    def get_node(self, node_id):
        self.asked.append(str(node_id))
        return _Node(self, str(node_id))

    async def get_namespace_array(self):
        return list(self.namespaces)

    async def check_connection(self):
        return None

    async def disconnect(self):
        return None


def _driver(namespaces, values, tree=None):
    drv = OPCUADriver({"id": "o", "host": "127.0.0.1", "port": 4840, "timeout": 1})
    drv._client = _Server(namespaces, values, tree)
    drv.is_connected = True
    return drv, drv._client


def _plc():
    return _driver(SIEMENS, {'ns=3;s="Tag_1"': False, 'ns=3;s="Tag_2"': False, 'ns=3;s="DB1"."Speed"': 5,
                             'ns=3;s="DB1"."Values"[2]': 7})


# ── The Siemens way of writing a name ─────────────────────────────────────────


@pytest.mark.parametrize("name, identifier", [
    ("Tag_1", '"Tag_1"'),
    ("DB1.Speed", '"DB1"."Speed"'),
    ("DB1.Values[2]", '"DB1"."Values"[2]'),
    ("DB1.Motor.Speed", '"DB1"."Motor"."Speed"'),
    ("Data block.My tag", '"Data block"."My tag"'),
    ('"DB1"."Speed"', '"DB1"."Speed"'),               # already written the Siemens way
])
def test_a_name_written_the_siemens_way(name, identifier):
    assert siemens_identifier(name) == identifier


# ── A tag by its name ─────────────────────────────────────────────────────────


def test_a_siemens_tag_is_read_and_written_by_its_name():
    drv, server = _plc()
    ok, value, message = asyncio.run(drv.read("Tag_1"))
    assert (ok, value) == (True, False) and 'ns=3;s="Tag_1"' in message and "Tag_1" in message
    ok, message = asyncio.run(drv.set("Tag_2"))
    assert ok and server.writes == [('ns=3;s="Tag_2"', True)] and 'ns=3;s="Tag_2"' in message
    ok, message = asyncio.run(drv.reset(" Tag_2 "))
    assert ok and server.writes[-1] == ('ns=3;s="Tag_2"', False)


def test_a_data_block_member_and_an_array_element_by_name():
    drv, server = _plc()
    assert asyncio.run(drv.read("DB1.Speed"))[:2] == (True, 5)
    ok, message = asyncio.run(drv.write("DB1.Values[2]", 12))
    assert ok and server.writes == [('ns=3;s="DB1"."Values"[2]', 12)], message


def test_toggle_works_on_a_name():
    drv, server = _plc()
    ok, message = asyncio.run(drv.toggle("Tag_1"))
    assert ok and server.writes == [('ns=3;s="Tag_1"', True)], message


def test_a_name_is_looked_up_once():
    drv, server = _plc()

    async def run():
        await drv.read("Tag_1")
        first = len(server.asked)
        await drv.read("Tag_1")
        await drv.set("Tag_1")
        return first

    first = asyncio.run(run())
    assert server.asked[first:] == ['ns=3;s="Tag_1"', 'ns=3;s="Tag_1"']     # no more looking, only the node itself
    assert server.browsed == 0


def test_a_server_without_quotes_takes_the_name_as_it_is():
    drv, server = _driver(PLAIN, {"ns=2;s=Line1.Rejector": True})
    ok, value, message = asyncio.run(drv.read("Line1.Rejector"))
    assert (ok, value) == (True, True) and "ns=2;s=Line1.Rejector" in message


def test_a_full_node_id_is_used_as_typed():
    drv, server = _plc()
    ok, value, message = asyncio.run(drv.read('ns=3;s="Tag_1"'))
    assert ok and server.asked == ['ns=3;s="Tag_1"'] and message == 'OPC UA read False from ns=3;s="Tag_1"'
    for typed in ("i=85", "ns=2;i=1001", "nsu=http://example.com/plc;s=Run", "NS=3;S=x", "ns=3;g=09087e75-8e5e-499b-954f-f2a9603db28a"):
        drv, server = _plc()
        asyncio.run(drv.read(typed))
        assert server.asked[0] == typed, typed          # tried as typed, before anything else


def test_a_node_id_in_the_wrong_form_is_put_right():
    """What the owner typed: the right name, the wrong namespace and no quotes."""
    drv, server = _plc()
    ok, message = asyncio.run(drv.set("ns=2;s=Tag_1"))
    assert ok and server.writes == [('ns=3;s="Tag_1"', True)], message
    assert "ns=2;s=Tag_1" in message and 'ns=3;s="Tag_1"' in message
    before = len(server.asked)
    assert asyncio.run(drv.read("ns=2;s=Tag_1"))[:2] == (True, True)
    assert server.asked[before:] == ['ns=3;s="Tag_1"']                      # remembered, not tried wrong again


def test_a_name_the_server_does_not_have_says_so():
    drv, server = _plc()

    async def run():
        first = await drv.set("Tag_9")
        asked = len(server.asked)
        second = await drv.set("Tag_9")             # a card that fires for every product
        return first, second, asked

    first, second, asked = asyncio.run(run())
    assert first[0] is False and "Tag_9" in first[1] and "NodeId" in first[1] and server.writes == []
    assert second[0] is False and len(server.asked) == asked                 # not looked for again at once
    assert drv.is_connected                                                  # a wrong name is not a lost link


def test_a_name_that_two_namespaces_have_is_not_guessed():
    drv, server = _driver(PLAIN + ["http://example.com/other"], {"ns=2;s=Run": True, "ns=3;s=Run": False})
    ok, message = asyncio.run(drv.set("Run"))
    assert ok is False and "ns=2;s=Run" in message and "ns=3;s=Run" in message and server.writes == []


def test_a_server_with_numbered_nodes_is_searched_by_name():
    tree = {"i=85": [("i=2253", "Server"), ("ns=2;i=5000", "Line")], "i=2253": [("i=2254", "Speed")],
            "ns=2;i=5000": [("ns=2;i=1001", "Speed"), ("ns=2;i=1002", "Run")]}
    drv, server = _driver(PLAIN, {"ns=2;i=1001": 42, "ns=2;i=1002": True, "i=2254": 0}, tree)
    ok, value, message = asyncio.run(drv.read("Speed"))
    assert (ok, value) == (True, 42) and "ns=2;i=1001" in message          # not the server's own "Speed"
    assert asyncio.run(drv.read("run"))[:2] == (True, True)                # the only one, whatever its capitals


def test_after_a_reconnect_a_name_is_looked_up_again():
    drv, server = _plc()

    async def run():
        await drv.read("Tag_1")
        await drv.disconnect()
        fresh = _Server(SIEMENS, {'ns=4;s="Tag_1"': True})                # the PLC was loaded again
        fresh.namespaces = SIEMENS[:3] + ["urn:other", SIEMENS[3]]
        drv._client, drv.is_connected = fresh, True
        return await drv.read("Tag_1")

    ok, value, message = asyncio.run(run())
    assert (ok, value) == (True, True) and 'ns=4;s="Tag_1"' in message


def test_an_empty_address_is_refused():
    drv, _ = _plc()
    ok, message = asyncio.run(drv.set("  "))
    assert ok is False and "address" in message.lower()
