"""OPC UA PLC driver using the asyncua client."""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.hardware.plc._common import coerce_read
from app.hardware.plc.base import PLCDriver
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _certificate_application_uri(cert_path: str) -> str:
    """Return the URI from a certificate's SubjectAltName, or "" if it has none."""
    try:
        from cryptography import x509

        with open(cert_path, "rb") as handle:
            data = handle.read()
        try:
            cert = x509.load_pem_x509_certificate(data)
        except ValueError:
            cert = x509.load_der_x509_certificate(data)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        return uris[0] if uris else ""
    except Exception:
        return ""


# A full NodeId as OPC UA writes it: ns=3;s="Tag_1", i=85, nsu=http://...;s=Run, ns=2;g=<guid>.
_NODE_ID = re.compile(r"^(?:(?:ns=\d+|nsu=[^;]+);)?[isgb]=", re.IGNORECASE)
# The namespace of a Siemens S7-1200/1500's own tags. There a tag's NodeId is its name in double
# quotes: ns=3;s="Tag_1", and a data block's member ns=3;s="DB1"."Speed".
SIEMENS_NAMESPACE = "http://www.siemens.com/simatic-s7-opcua"
# Namespaces that hold no PLC tags: the OPC UA base and the Device Integration model.
_NO_TAG_NAMESPACES = ("http://opcfoundation.org/UA/", "http://opcfoundation.org/UA/DI/")
_OBJECTS_FOLDER = "i=85"
_SERVER_OBJECT = "i=2253"


def siemens_identifier(name: str) -> str:
    """A tag's name the way a Siemens PLC writes it in a NodeId: every part in double quotes.

    ``Tag_1`` -> ``"Tag_1"``; ``DB1.Speed`` -> ``"DB1"."Speed"``; ``DB1.Values[2]`` -> ``"DB1"."Values"[2]``.
    A name that already has quotes is left as it is.
    """
    if '"' in name:
        return name
    parts = []
    for part in name.split("."):
        match = re.fullmatch(r"(.*?)((?:\[[^\]]*\])*)", part)
        parts.append(f'"{match.group(1)}"{match.group(2)}')
    return ".".join(parts)


def _name_of(node_id: str) -> Optional[str]:
    """The tag name inside a NodeId with a text identifier (``ns=2;s=Tag_1`` -> ``Tag_1``), else None."""
    match = re.match(r"^(?:(?:ns=\d+|nsu=[^;]+);)?s=(.+)$", node_id, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).replace('"', "").strip() or None


class OPCUADriver(PLCDriver):
    """
    Read and write OPC UA variable nodes. An address is a tag's name, or a full NodeId.

    A name (``Tag_1``, ``DB1.Speed``) is looked up on the server once per connection: on a
    Siemens PLC it is the node ``ns=3;s="Tag_1"``, on other servers the same name without
    quotes in the server's own namespace, and on a server with numbered nodes it is searched for.

    Endpoint options:
      opcua_security          None | Basic256Sha256_Sign | Basic256Sha256_SignAndEncrypt |
                              Aes128Sha256RsaOaep_Sign(AndEncrypt) | Aes256Sha256RsaPss_Sign(AndEncrypt)
      opcua_cert_path         client certificate (DER or PEM) — required for secured modes
      opcua_key_path          client private key (PEM) — required for secured modes
      opcua_server_cert_path  optional server certificate to pin
      username / password     optional user-name identity token
    """

    # Text goes to a String node.
    writes_text = True

    _INTEGER_RANGES = {
        "SByte": (-128, 127),
        "Byte": (0, 255),
        "Int16": (-32768, 32767),
        "UInt16": (0, 65535),
        "Int32": (-2147483648, 2147483647),
        "UInt32": (0, 4294967295),
        "Int64": (-9223372036854775808, 9223372036854775807),
        "UInt64": (0, 18446744073709551615),
    }

    def __init__(self, endpoint: dict):
        super().__init__(endpoint)
        self._client = None
        self.last_error = ""
        # What each address stands for on this connection: {address as typed: NodeId}.
        self._node_ids: Dict[str, str] = {}
        # Names the server did not have: {name: (when it was looked for, the message)}.
        self._missing: Dict[str, Tuple[float, str]] = {}
        self._lookup_lock: Optional[asyncio.Lock] = None

    def _server_url(self) -> str:
        host = self.host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        url_host = f"[{host}]" if ":" in host else host
        path = str(self._ep.get("opcua_path") or "").strip()
        if path and not path.startswith("/"):
            path = f"/{path}"
        if any(char in path for char in ("://", "?", "#")):
            raise ValueError("OPC UA endpoint path must be a URL path")
        return f"opc.tcp://{url_host}:{self.port}{path}"

    async def connect(self) -> bool:
        if self.is_connected and self._client is not None:
            return True
        if self._client is not None:
            await self.disconnect()

        client = None
        try:
            from asyncua import Client

            client = Client(self._server_url(), timeout=self.timeout)
            await self._configure_security(client)
            username = str(self._ep.get("username") or "").strip()
            if username:
                client.set_user(username)
                client.set_password(str(self._ep.get("password") or ""))
            await client.connect()
            self._client = client
            self._mark_connected()
            self.last_error = ""
            logger.info("OPC UA connected: %s", self._server_url())
            return True
        except ImportError:
            self.last_error = "OPC UA support requires the asyncua package; install the project requirements"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

        self._mark_connect_failed(self.last_error)
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                self._log_disconnect_error(exc)
        return False

    async def _configure_security(self, client) -> None:
        """Apply the endpoint's message security policy and client certificate."""
        security = str(self._ep.get("opcua_security", "None") or "None").strip()
        if security == "None":
            return
        policy_name, _, mode_name = security.rpartition("_")
        from asyncua import ua
        from asyncua.crypto import security_policies

        policies = {
            "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
            "Aes128Sha256RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
            "Aes256Sha256RsaPss": getattr(security_policies, "SecurityPolicyAes256Sha256RsaPss", None),
        }
        modes = {"Sign": ua.MessageSecurityMode.Sign, "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt}
        policy = policies.get(policy_name)
        if policy is None or mode_name not in modes:
            raise ValueError(f"Unsupported OPC UA security policy '{security}'")
        cert_path = str(self._ep.get("opcua_cert_path") or "").strip()
        key_path = str(self._ep.get("opcua_key_path") or "").strip()
        if not cert_path or not key_path:
            raise ValueError(
                f"OPC UA security mode '{security}' needs the client certificate and private key "
                "(opcua_cert_path and opcua_key_path) to be set on this channel"
            )
        for label, path in (("client certificate", cert_path), ("private key", key_path)):
            if not os.path.isfile(path):
                raise ValueError(f"OPC UA {label} file not found: {path}")
        server_cert = str(self._ep.get("opcua_server_cert_path") or "").strip() or None
        if server_cert and not os.path.isfile(server_cert):
            raise ValueError(f"OPC UA server certificate file not found: {server_cert}")
        application_uri = _certificate_application_uri(cert_path)
        if application_uri:
            # Servers reject sessions whose ApplicationUri differs from the certificate's.
            client.application_uri = application_uri
        await client.set_security(
            policy,
            certificate=cert_path,
            private_key=key_path,
            server_certificate=server_cert,
            mode=modes[mode_name],
        )

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self.is_connected = False
        # A PLC that was loaded again can give its tags other NodeIds: look them up again.
        self._node_ids.clear()
        self._missing.clear()
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                logger.debug("OPC UA disconnect for %s: %s", self._ep.get("id", ""), exc)

    async def _handle_failure(self, exc: Exception) -> None:
        """Drop the session when a failed request means the link is gone.

        asyncua reports a dead server with its own exception types, not only
        OSError, so ask the client instead of guessing from the exception.
        """
        if isinstance(exc, ValueError) or self._client is None:
            return
        if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
            await self.disconnect()
            return
        try:
            await asyncio.wait_for(self._client.check_connection(), timeout=self.timeout)
        except Exception:
            await self.disconnect()

    # ── From an address to a node ────────────────────────────────────────────

    # A name the server does not have is not looked for again for this long: a card can fire per product.
    MISSING_SECONDS = 10.0
    # Searching a server whose NodeIds are not built from names: where it stops.
    BROWSE_MAX_NODES = 4000
    BROWSE_MAX_DEPTH = 8
    BROWSE_SECONDS = 5.0

    async def _resolve(self, address: str) -> Tuple[Any, str]:
        """(node, its NodeId) for an address: a full NodeId as typed, or a tag's name looked up on the server.

        Raises ValueError when the address is empty or the name cannot be found.
        """
        if not self.is_connected or self._client is None:
            raise ConnectionError("OPC UA client is not connected")
        text = str(address or "").strip()
        if not text or len(text) > 512:
            raise ValueError("OPC UA address must contain 1 to 512 characters: a tag's name or a full NodeId")
        node_id = self._node_ids.get(text)
        if node_id is None:
            node_id = text if _NODE_ID.match(text) else await self._node_id_for(text)
        return self._client.get_node(node_id), node_id

    async def _node_id_for(self, name: str) -> str:
        """The NodeId of the tag called ``name``, looked up once per connection."""
        if self._lookup_lock is None:
            self._lookup_lock = asyncio.Lock()
        async with self._lookup_lock:
            known = self._node_ids.get(name)
            if known is not None:
                return known
            missing = self._missing.get(name)
            if missing is not None and time.monotonic() - missing[0] < self.MISSING_SECONDS:
                raise ValueError(missing[1])
            try:
                node_id = await self._find(name)
            except ValueError as exc:
                self._missing[name] = (time.monotonic(), str(exc))
                raise
            self._missing.pop(name, None)
            self._node_ids[name] = node_id
            logger.info("OPC UA %s: '%s' is the node %s", self._ep.get("id", ""), name, node_id)
            return node_id

    async def _find(self, name: str) -> str:
        from asyncua import ua

        namespaces = [str(uri) for uri in await self._client.get_namespace_array()]
        siemens = namespaces.index(SIEMENS_NAMESPACE) if SIEMENS_NAMESPACE in namespaces else None
        order = [index for index, uri in enumerate(namespaces)
                 if index >= 1 and index != siemens and uri not in _NO_TAG_NAMESPACES]
        if siemens is not None:
            order.insert(0, siemens)
        quoted = siemens_identifier(name)
        found: List[str] = []
        for index in order:
            # A Siemens PLC writes the name in quotes; other servers as it is.
            for identifier in dict.fromkeys([quoted, name] if index == siemens else [name, quoted]):
                node_id = f"ns={index};s={identifier}"
                try:
                    is_variable = await self._client.get_node(node_id).read_node_class() == ua.NodeClass.Variable
                except ua.UaStatusCodeError:
                    continue
                if is_variable:
                    if index == siemens:
                        return node_id      # the PLC's own tag
                    found.append(node_id)
        if not found:
            found = await self._search(name)
        if len(found) == 1:
            return found[0]
        if found:
            raise ValueError(f"Several OPC UA nodes are called '{name}': {', '.join(found[:5])}. "
                             "Type the full NodeId of the one you mean")
        example = f'ns={siemens};s={quoted}' if siemens is not None else f"ns=2;s={name}"
        raise ValueError(f"No OPC UA node called '{name}' was found on the server. Check the name, and that the tag "
                         f"can be reached from OPC UA; or type its full NodeId, such as {example}")

    async def _search(self, name: str) -> List[str]:
        """The NodeIds of the variables whose name is ``name``, searched from the Objects folder.

        For servers whose NodeIds are numbers. The server's own object is left out. Stops at
        BROWSE_MAX_NODES nodes, BROWSE_MAX_DEPTH levels or BROWSE_SECONDS.
        """
        from asyncua import ua

        exact: List[str] = []
        loose: List[str] = []
        level = [_OBJECTS_FOLDER]
        seen = set(level)
        deadline = time.monotonic() + self.BROWSE_SECONDS
        for _ in range(self.BROWSE_MAX_DEPTH):
            following: List[str] = []
            for parent in level:
                if time.monotonic() > deadline or len(seen) > self.BROWSE_MAX_NODES:
                    following = []
                    break
                try:
                    children = await self._client.get_node(parent).get_children_descriptions()
                except ua.UaStatusCodeError:
                    continue
                for child in children:
                    child_id = child.NodeId.to_string()
                    if child_id in seen or child_id == _SERVER_OBJECT:
                        continue
                    seen.add(child_id)
                    names = (str(child.BrowseName.Name or ""), str(child.DisplayName.Text or ""))
                    if child.NodeClass == ua.NodeClass.Variable:
                        if name in names:
                            exact.append(child_id)
                        elif name.lower() in (n.lower() for n in names):
                            loose.append(child_id)
                    elif child.NodeClass == ua.NodeClass.Object:
                        following.append(child_id)
            if not following:
                break
            level = following
        return exact or loose

    async def _on_node(self, address: str, action: Callable[[Any], Awaitable[Any]]) -> Tuple[Any, str]:
        """Run ``action(node)`` on the address's node. Returns (its result, the NodeId it ran on).

        When the server does not know the node, the tag's name is looked up again and the action
        is run once more: for a NodeId typed in the wrong form (``ns=2;s=Tag_1`` on a Siemens PLC is
        ``ns=3;s="Tag_1"``), and for a remembered NodeId whose namespace number has changed.
        """
        from asyncua import ua

        node, node_id = await self._resolve(address)
        try:
            return await action(node), node_id
        except ua.uaerrors.BadNodeIdUnknown:
            text = str(address or "").strip()
            # The tag's name: the address itself, or the name inside a NodeId that was typed.
            name = _name_of(text) if _NODE_ID.match(text) else text
            if name is None:
                raise
            # A NodeId that was remembered is no longer right (the PLC was loaded again and its
            # namespace has another number): forget it and look the name up again.
            for key in (text, name):
                if self._node_ids.get(key) == node_id:
                    del self._node_ids[key]
            right = await self._node_id_for(name)
            if right == node_id:
                raise
            self._node_ids[text] = right
            logger.info("OPC UA %s: the server has no node %s; using %s", self._ep.get("id", ""), text, right)
            return await action(self._client.get_node(right)), right

    @staticmethod
    def _shown(address: str, node_id: str) -> str:
        """How a message names the node: what was typed, and the NodeId it stands for when that differs."""
        text = str(address or "").strip()
        return text if text == node_id else f"{text} ({node_id})"

    async def _write_value(self, address: str, value) -> Tuple[bool, str]:
        try:
            from asyncua import ua

            # Siemens S7-1500 rejects writes with SourceTimestamp. Passing a
            # Variant directly makes asyncua add one, so send a value-only
            # DataValue just like the working PLC test client does.
            data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Boolean))
            _, node_id = await self._on_node(address, lambda node: node.write_value(data_value))
            return True, f"OPC UA wrote {value!s} to {self._shown(address, node_id)}"
        except Exception as exc:
            await self._handle_failure(exc)
            return False, f"OPC UA write failed for {address}: {type(exc).__name__}: {exc}"

    async def set(self, address: str) -> Tuple[bool, str]:
        return await self._write_value(address, True)

    async def reset(self, address: str) -> Tuple[bool, str]:
        return await self._write_value(address, False)

    async def toggle(self, address: str) -> Tuple[bool, str]:
        try:
            value, node_id = await self._on_node(address, lambda node: node.read_value())
            if not isinstance(value, bool):
                return False, f"OPC UA toggle requires a Boolean node: {self._shown(address, node_id)}"
            return await self._write_value(address, not value)
        except Exception as exc:
            await self._handle_failure(exc)
            return False, f"OPC UA toggle failed for {address}: {type(exc).__name__}: {exc}"

    async def _read(self, address: str, data_type: str) -> Tuple[bool, Any, str]:
        try:
            value, node_id = await self._on_node(address, lambda node: node.read_value())
            shown = self._shown(address, node_id)
            if not isinstance(value, (bool, int, float, str)):
                return False, None, (
                    f"OPC UA node {shown} holds {type(value).__name__}; "
                    "only a Boolean, a number or a string can be read"
                )
            value = coerce_read(value, data_type)
            return True, value, f"OPC UA read {value!s} from {shown}"
        except Exception as exc:
            await self._handle_failure(exc)
            return False, None, f"OPC UA read failed for {address}: {type(exc).__name__}: {exc}"

    async def pulse(self, address: str, duration_ms: int) -> Tuple[bool, str]:
        ok, message = await self.set(address)
        if not ok:
            # The ON write may have reached the server even though its reply was
            # lost; try OFF so the output is not left latched.
            if self.is_connected:
                off_ok, _ = await self.reset(address)
                message += "; OFF cleanup acknowledged" if off_ok else "; OFF cleanup failed"
            return False, f"OPC UA pulse ON failed: {message}"
        try:
            await self._pulse_wait(duration_ms)
        finally:
            reset_task = asyncio.create_task(self.reset(address))
            try:
                reset_ok, reset_message = await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                await reset_task
                raise
        return reset_ok, f"OPC UA pulse {address} for {duration_ms} ms: {reset_message}"

    async def write(self, address: str, value: float) -> Tuple[bool, str]:
        try:
            from asyncua import ua

            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                return False, "OPC UA numeric value must be finite"

            class _Refused(Exception):
                pass

            async def put(node) -> Any:
                variant_type = await node.read_data_type_as_variant_type()
                type_name = getattr(variant_type, "name", str(variant_type))
                if type_name in self._INTEGER_RANGES:
                    if not numeric_value.is_integer():
                        raise _Refused(f"OPC UA node {address} requires an integer value")
                    typed_value: Any = int(numeric_value)
                    minimum, maximum = self._INTEGER_RANGES[type_name]
                    if not minimum <= typed_value <= maximum:
                        raise _Refused(f"Value is outside the {type_name} range for OPC UA node {address}")
                elif type_name in {"Float", "Double"}:
                    typed_value = numeric_value
                else:
                    raise _Refused(f"OPC UA WRITE supports numeric scalar nodes; {address} is {type_name}")
                # Do not let asyncua add SourceTimestamp; Siemens S7-1500 can reject it.
                await node.write_value(ua.DataValue(ua.Variant(typed_value, variant_type)))
                return typed_value

            try:
                typed, node_id = await self._on_node(address, put)
            except _Refused as refused:
                return False, str(refused)
            return True, f"OPC UA wrote {typed} to {self._shown(address, node_id)}"
        except Exception as exc:
            await self._handle_failure(exc)
            return False, f"OPC UA write failed for {address}: {type(exc).__name__}: {exc}"
