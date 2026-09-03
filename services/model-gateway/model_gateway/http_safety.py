from __future__ import annotations

import asyncio
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
import os
import socket
from typing import Iterable
from urllib.parse import urlparse

from model_gateway_contracts.urls import (
    normalize_base_url,
    normalize_endpoint,
    normalize_private_networks,
)


MAX_DISCOVERY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_MODELS = 1_000
MAX_MODEL_ID_LENGTH = 300
_RFC2544_BENCHMARK_SUPERNET = ip_network("198.18.0.0/15")
# Clash Meta / mihomo answer AAAA queries from fc00::/18 in fake-ip mode
# ("fake-ip-range6" default); phones with IPv6 enabled hit this range first.
_FAKE_IP6_RANGE = ip_network("fc00::/18")
FAKE_IP_RANGES: tuple[str, ...] = (str(_RFC2544_BENCHMARK_SUPERNET), str(_FAKE_IP6_RANGE))
# Clash / Surge / sing-box "fake-ip" TUN modes answer every DNS query from
# 198.18.0.0/15 (and fc00::/18 for IPv6) and proxy the connection by the
# original hostname. On a device where the operator is the only tenant (the
# Android app, a personal laptop behind a VPN) that mapping is expected, so
# MODEL_GATEWAY_ALLOW_FAKE_IP=1 accepts both fake-ip ranges in addition to
# per-connection allowed_private_networks. Servers keep it off and stay strict.
_FAKE_IP_ENV = "MODEL_GATEWAY_ALLOW_FAKE_IP"


def fake_ip_allowed() -> bool:
    return os.environ.get(_FAKE_IP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _effective_private_networks(allowed_private_networks: Iterable[str]) -> tuple[str, ...]:
    networks = tuple(allowed_private_networks)
    if fake_ip_allowed():
        networks = (*networks, *(item for item in FAKE_IP_RANGES if item not in networks))
    return networks


def upstream_url(
    base_url: str,
    endpoint: str,
    *,
    allowed_private_networks: Iterable[str] = (),
) -> str:
    base = normalize_base_url(
        base_url,
        allowed_private_networks=allowed_private_networks,
    )
    path = normalize_endpoint(endpoint)
    if path is None:
        raise ValueError("缺少上游 endpoint")
    return f"{base}{path}"


def safe_model_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (
        not normalized
        or value != normalized
        or len(normalized) > MAX_MODEL_ID_LENGTH
        or any(not 33 <= ord(character) <= 126 for character in normalized)
    ):
        return ""
    return normalized


async def require_safe_destination(
    url: str,
    *,
    allowed_private_networks: Iterable[str] = (),
) -> None:
    allowed_private_networks = _effective_private_networks(allowed_private_networks)
    base = normalize_base_url(
        url,
        allowed_private_networks=allowed_private_networks,
    )
    parsed = urlparse(base)
    hostname = parsed.hostname or ""
    literal = _literal_ip(hostname)
    if literal is not None:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await asyncio.get_running_loop().getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    _validate_resolved_addresses(
        hostname,
        (item[4][0] for item in addresses),
        allowed_private_networks=allowed_private_networks,
    )


def require_safe_destination_sync(
    url: str,
    *,
    allowed_private_networks: Iterable[str] = (),
) -> None:
    allowed_private_networks = _effective_private_networks(allowed_private_networks)
    base = normalize_base_url(
        url,
        allowed_private_networks=allowed_private_networks,
    )
    parsed = urlparse(base)
    hostname = parsed.hostname or ""
    literal = _literal_ip(hostname)
    if literal is not None:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    _validate_resolved_addresses(
        hostname,
        (item[4][0] for item in addresses),
        allowed_private_networks=allowed_private_networks,
    )


def bounded_model_ids(items: object) -> set[str]:
    if not isinstance(items, list) or len(items) > MAX_DISCOVERY_MODELS:
        raise ValueError("models 响应条目过多或格式无效")
    result: set[str] = set()
    for item in items:
        value: object = item
        if isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name")
        model_id = safe_model_id(value)
        if model_id:
            result.add(model_id)
    return result


def _literal_ip(hostname: str):
    try:
        return ip_address(hostname)
    except ValueError:
        return None


def _parsed_private_networks(
    values: Iterable[str],
) -> tuple[IPv4Network | IPv6Network, ...]:
    return tuple(
        ip_network(value, strict=True) for value in normalize_private_networks(values)
    )


def _validate_resolved_addresses(
    hostname: str,
    values: Iterable[str],
    *,
    allowed_private_networks: Iterable[str],
) -> None:
    networks = _parsed_private_networks(allowed_private_networks)
    resolved = {ip_address(value.split("%", 1)[0]) for value in values}
    if not resolved:
        raise ValueError("base_url hostname 没有可用地址")
    blocked: list[str] = []
    for address in resolved:
        if hostname == "localhost":
            if address.is_loopback:
                continue
            raise ValueError("localhost 必须只解析到回环地址")
        if address.is_global:
            continue
        if any(
            address.version == network.version and address in network
            for network in networks
        ):
            continue
        blocked.append(str(address))
    if blocked:
        shown = ", ".join(sorted(blocked)[:8])
        fake_ip_hint = ""
        if any(
            (address.version == 4 and address in _RFC2544_BENCHMARK_SUPERNET)
            or (address.version == 6 and address in _FAKE_IP6_RANGE)
            for address in (ip_address(item) for item in blocked)
        ):
            fake_ip_hint = (
                " 若使用 Clash/Surge 等 TUN fake-ip，请在渠道 "
                "allowed_private_networks 中显式加入 198.18.0.0/15 与 fc00::/18，或关闭 fake-ip。"
            )
        raise ValueError(
            f"base_url hostname 解析到未显式允许的本地或私有地址"
            f"（{hostname} → {shown}）。{fake_ip_hint}".rstrip()
        )
