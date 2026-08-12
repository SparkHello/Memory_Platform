from __future__ import annotations

import asyncio
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
import re
import socket
from typing import Iterable
from urllib.parse import unquote_to_bytes, urlparse


MAX_DISCOVERY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_MODELS = 1_000
MAX_MODEL_ID_LENGTH = 300
_RFC2544_BENCHMARK_SUPERNET = ip_network("198.18.0.0/15")
_ALLOWED_PRIVATE_SUPERNETS = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        # RFC 2544 benchmarking space is non-global and is used by Clash/Surge
        # style TUN fake-ip mappings.  It remains denied by default and may
        # only be enabled with an explicit caller-supplied CIDR inside the
        # supernet (anything from the full /15 down to a single /32).
        str(_RFC2544_BENCHMARK_SUPERNET),
        "fc00::/7",
        "fe80::/10",
    )
)


def normalize_private_networks(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        raw = str(value)
        if raw != raw.strip() or _has_control(raw):
            raise ValueError("allowed_private_networks 包含非法空白或控制字符")
        try:
            network = ip_network(raw, strict=True)
        except ValueError as exc:
            raise ValueError("allowed_private_networks 必须是规范 CIDR") from exc
        # RFC 2544 (198.18.0.0/15) is non-global and used by Clash/Surge TUN
        # fake-ip. Any CIDR inside that supernet may be listed explicitly
        # (including /15 or /32). Unlisted addresses remain denied by default.
        if not any(
            network.version == parent.version and network.subnet_of(parent)
            for parent in _ALLOWED_PRIVATE_SUPERNETS
        ):
            raise ValueError("allowed_private_networks 只能声明私有或链路本地网段")
        canonical = str(network)
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def normalize_base_url(
    value: str,
    *,
    allowed_private_networks: Iterable[str] = (),
) -> str:
    if not isinstance(value, str):
        raise ValueError("base_url 必须是字符串")
    if value != value.strip() or _has_control(value) or "\\" in value:
        raise ValueError("base_url 不能包含外围空白、控制字符或反斜杠")
    normalized = value.rstrip("/")
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url 端口格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url 必须是完整 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url 不能内嵌账号或密钥")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url 不能包含 query 或 fragment")
    _validate_path(parsed.path, label="base_url")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base_url 端口超出范围")
    hostname = parsed.hostname.lower()
    if "%" in hostname:
        raise ValueError("base_url 不允许带 zone id 的地址")

    literal = _literal_ip(hostname)
    if literal is None:
        if not re.fullmatch(r"[a-z0-9.-]{1,253}", hostname):
            raise ValueError("base_url hostname 必须使用 ASCII DNS 名称或规范 IP")
        labels = hostname.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            for label in labels
        ):
            raise ValueError("base_url hostname 格式无效")
        if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", hostname):
            raise ValueError("base_url 不允许非规范数字 IP 写法")
    loopback = hostname == "localhost" or (literal is not None and literal.is_loopback)
    if loopback:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("回环地址只允许 HTTP(S)")
        return normalized

    if literal is not None and not literal.is_global:
        networks = _parsed_private_networks(allowed_private_networks)
        if not any(
            literal.version == network.version and literal in network
            for network in networks
        ):
            raise ValueError("私有上游地址必须显式列入 allowed_private_networks")
        return normalized

    if parsed.scheme != "https":
        raise ValueError("远程 connection 必须使用 HTTPS；HTTP 仅允许显式本地地址")
    return normalized


def normalize_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("endpoint 不能包含外围空白")
    if _has_control(value) or "\\" in value:
        raise ValueError("endpoint 不能包含控制字符或反斜杠")
    parsed = urlparse(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint 必须是无 query/fragment 的绝对相对路径")
    _validate_path(parsed.path, label="endpoint")
    return value


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
            address.version == 4 and address in _RFC2544_BENCHMARK_SUPERNET
            for address in (ip_address(item) for item in blocked)
        ):
            fake_ip_hint = (
                " 若使用 Clash/Surge 等 TUN fake-ip，请在渠道 "
                "allowed_private_networks 中显式加入 198.18.0.0/15，或关闭 fake-ip。"
            )
        raise ValueError(
            f"base_url hostname 解析到未显式允许的本地或私有地址"
            f"（{hostname} → {shown}）。{fake_ip_hint}".rstrip()
        )


def _has_control(value: str) -> bool:
    return bool(re.search(r"[\x00-\x20\x7f]", value))


def _validate_path(path: str, *, label: str) -> None:
    """Reject path forms whose meaning can change after URL normalization.

    Base paths such as ``/compatible-mode/v1`` remain valid. Dot segments,
    malformed/double encodings and encoded structural separators are rejected
    so an upstream framework cannot reinterpret the validated destination.
    """

    if not path:
        return
    if re.search(r"%(?![0-9A-Fa-f]{2})", path):
        raise ValueError(f"{label} path 包含非法 percent encoding")
    for segment in path.split("/"):
        decoded = unquote_to_bytes(segment)
        if decoded in {b".", b".."}:
            raise ValueError(f"{label} path 不能包含 dot segment")
        if any(byte <= 0x20 or byte == 0x7F for byte in decoded):
            raise ValueError(f"{label} path 不能包含编码后的控制字符或空白")
        if any(separator in decoded for separator in (b"/", b"\\", b"?", b"#", b"%")):
            raise ValueError(f"{label} path 不能包含编码后的结构分隔符")
