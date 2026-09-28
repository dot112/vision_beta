"""Bounded discovery of OPC UA servers on an explicitly selected private IPv4 range."""
from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit
from typing import Any, Dict, List


TCP_PROBE_TIMEOUT = 0.45
DISCOVERY_TIMEOUT = 4.0
MAX_CONCURRENT_PROBES = 24
MAX_NETWORK_ADDRESSES = 256


def _parse_network(target: str) -> ipaddress.IPv4Network:
    target = str(target or "").strip()
    if not target:
        raise ValueError("Enter an IPv4 address or subnet to scan")
    try:
        if "/" in target:
            network = ipaddress.ip_network(target, strict=False)
        else:
            address = ipaddress.ip_address(target)
            if not isinstance(address, ipaddress.IPv4Address):
                raise ValueError("OPC UA scan currently supports IPv4 addresses only")
            network = ipaddress.ip_network(f"{address}/24", strict=False)
    except ValueError as exc:
        raise ValueError("Enter a valid IPv4 address or CIDR subnet, such as 192.168.0.0/24") from exc

    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("OPC UA scan currently supports IPv4 subnets only")
    if not network.is_private or network.is_loopback or network.is_link_local or network.is_multicast:
        raise ValueError("Scan only a private factory IPv4 subnet")
    if network.num_addresses > MAX_NETWORK_ADDRESSES:
        raise ValueError("Scan range is too large; use a subnet with at most 256 addresses")
    return network


def _endpoint_record(host: str, scan_port: int, endpoint: Any) -> Dict[str, Any] | None:
    endpoint_url = str(getattr(endpoint, "EndpointUrl", "") or "")
    try:
        parsed_url = urlsplit(endpoint_url)
        if parsed_url.scheme.lower() != "opc.tcp":
            return None
        endpoint_port = parsed_url.port or scan_port
    except ValueError:
        return None

    path = parsed_url.path.rstrip("/")
    server = getattr(endpoint, "Server", None)
    application_name = getattr(server, "ApplicationName", None)
    server_name = str(getattr(application_name, "Text", "") or "OPC UA Server")

    policy_uri = str(getattr(endpoint, "SecurityPolicyUri", "") or "")
    policy_name = policy_uri.rsplit("#", 1)[-1] or "Unknown"
    try:
        security_mode_value = int(getattr(endpoint, "SecurityMode", 0))
    except (TypeError, ValueError):
        security_mode_value = 0
    mode_names = {1: "None", 2: "Sign", 3: "Sign & Encrypt"}
    mode_name = mode_names.get(security_mode_value, f"Mode {security_mode_value}")

    security_setting = ""
    if policy_name == "None" and security_mode_value == 1:
        security_setting = "None"
    elif policy_name == "Basic256Sha256" and security_mode_value == 2:
        security_setting = "Basic256Sha256_Sign"
    elif policy_name == "Basic256Sha256" and security_mode_value == 3:
        security_setting = "Basic256Sha256_SignAndEncrypt"

    token_policies = getattr(endpoint, "UserIdentityTokens", None) or []
    token_names = {0: "Anonymous", 1: "Username", 2: "Certificate", 3: "Issued Token"}
    identity_tokens = []
    anonymous_available = False if token_policies else None
    for token_policy in token_policies:
        token_type = getattr(token_policy, "TokenType", None)
        try:
            token_value = int(token_type)
        except (TypeError, ValueError):
            token_value = -1
        token_name = token_names.get(token_value, getattr(token_type, "name", "Unknown"))
        if token_name not in identity_tokens:
            identity_tokens.append(token_name)
        if token_value == 0:
            anonymous_available = True

    is_discovery_server = "discovery server" in server_name.lower() or "local discovery" in server_name.lower()
    selectable = bool(security_setting == "None" and not is_discovery_server and anonymous_available is not False)
    if is_discovery_server:
        selection_reason = "This is a discovery service, not the PLC application endpoint."
    elif not security_setting:
        selection_reason = "This security policy is not supported by the current channel configuration."
    elif anonymous_available is False:
        selection_reason = "This endpoint does not advertise anonymous login; username authentication is not configured."
    elif not selectable:
        selection_reason = "This endpoint cannot be selected as a PLC channel."
    else:
        selection_reason = ""

    return {
        "host": host,
        "port": endpoint_port,
        "path": path,
        "endpoint_url": endpoint_url,
        "server_name": server_name,
        "security_policy": policy_name,
        "security_mode": mode_name,
        "security_mode_value": security_mode_value,
        "user_identity_tokens": identity_tokens,
        "anonymous_available": anonymous_available,
        "security_setting": security_setting,
        "selectable": selectable,
        "selection_reason": selection_reason,
    }


async def scan_opcua_servers(target: str, port: int = 4840) -> Dict[str, Any]:
    """Probe one private /24-or-smaller range and return advertised OPC UA endpoints."""
    network = _parse_network(target)
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("OPC UA port must be between 1 and 65535")
    port = int(port)
    try:
        from asyncua import Client
    except ImportError as exc:
        raise RuntimeError("OPC UA scanning requires asyncua; install the project requirements") from exc

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
    open_addresses: List[str] = []
    discovery_failures: List[Dict[str, str]] = []

    async def inspect_host(address: ipaddress.IPv4Address) -> List[Dict[str, Any]]:
        host = str(address)
        async with semaphore:
            writer = None
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=TCP_PROBE_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                return []
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

            open_addresses.append(host)

            client = Client(f"opc.tcp://{host}:{port}", timeout=DISCOVERY_TIMEOUT)
            try:
                endpoints = await asyncio.wait_for(
                    client.connect_and_get_server_endpoints(),
                    timeout=DISCOVERY_TIMEOUT + 1,
                )
            except Exception as exc:
                discovery_failures.append({
                    "host": host,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                })
                return []

            results = []
            for endpoint in endpoints:
                try:
                    record = _endpoint_record(host, port, endpoint)
                except (AttributeError, TypeError, ValueError):
                    continue
                if record:
                    results.append(record)
            return results

    addresses = list(network.hosts())
    batches = await asyncio.gather(*(inspect_host(address) for address in addresses))
    candidates = [item for batch in batches for item in batch]
    candidates.sort(key=lambda item: (not item["selectable"], item["host"], item["port"], item["server_name"], item["security_policy"], item["security_mode_value"]))
    return {
        "target_network": str(network),
        "port": port,
        "scanned_addresses": len(addresses),
        "tcp_open_addresses": len(open_addresses),
        "discovery_failure_count": len(discovery_failures),
        "discovery_failures": discovery_failures[:5],
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
