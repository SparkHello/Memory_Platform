"""Pure URL syntax normalization used by configuration validation.

This module deliberately performs no DNS resolution and imports no HTTP
client. Runtime destination checks remain the Model Gateway's responsibility.
"""

from collections.abc import Iterable
from ipaddress import ip_address, ip_network
import re
from urllib.parse import unquote_to_bytes, urlparse


_RFC2544_BENCHMARK_SUPERNET = ip_network("198.18.0.0/15")
_ALLOWED_PRIVATE_SUPERNETS = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
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
        return normalized

    if literal is not None and not literal.is_global:
        networks = tuple(
            ip_network(item, strict=True)
            for item in normalize_private_networks(allowed_private_networks)
        )
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


def _literal_ip(hostname: str):
    try:
        return ip_address(hostname)
    except ValueError:
        return None


def _has_control(value: str) -> bool:
    return bool(re.search(r"[\x00-\x20\x7f]", value))


def _validate_path(path: str, *, label: str) -> None:
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


__all__ = [
    "normalize_base_url",
    "normalize_endpoint",
    "normalize_private_networks",
]
