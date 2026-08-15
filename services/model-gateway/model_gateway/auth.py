from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hmac
from math import log2
import re

from model_gateway.models import ClientConfig, GatewayConfig


class AuthenticationError(ValueError):
    pass


class SecretSnapshotError(ValueError):
    """Safe, value-free validation failure for a candidate secret snapshot."""

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(message)


MAX_CLIENT_TOKEN_BYTES = 1024
MIN_CLIENT_TOKEN_BYTES = 32
MAX_PROVIDER_SECRET_BYTES = 65_536
_CLIENT_TOKEN_RE = re.compile(rb"^[A-Za-z0-9_-]+$")
MIN_CLIENT_TOKEN_ESTIMATED_ENTROPY_BITS = 96.0


@dataclass(frozen=True, slots=True)
class AuthenticatedClient:
    id: str
    config: ClientConfig


def authenticate_client(
    authorization: str,
    *,
    config: GatewayConfig,
    secrets: dict[str, str],
) -> AuthenticatedClient:
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or token != token.strip()
    ):
        raise AuthenticationError("需要 Bearer 客户端密钥")
    try:
        # The presented token cannot be assigned a policy until its identity is
        # known.  Parse the old printable-ASCII envelope here, then compare it
        # only against each client's policy-validated stored value below.
        presented = client_token_bytes(token, allow_legacy_weak=True)
        validate_secret_domains(config=config, secrets=secrets)
    except ValueError as exc:
        raise AuthenticationError(str(exc)) from exc
    matches: list[AuthenticatedClient] = []
    for client_id, client in config.clients.items():
        if not client.enabled:
            continue
        expected = secrets.get(client.secret_ref, "")
        if not expected:
            continue
        try:
            expected_bytes = client_token_bytes(
                expected,
                allow_legacy_weak=client.allow_legacy_weak_secret,
            )
        except ValueError:
            # A malformed stored client credential must fail closed without
            # reflecting any part of the value to an unauthenticated caller.
            continue
        if hmac.compare_digest(presented, expected_bytes):
            matches.append(AuthenticatedClient(id=client_id, config=client))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AuthenticationError("客户端密钥配置冲突；每个 client 必须使用不同密钥")
    raise AuthenticationError("客户端密钥无效")


def client_token_bytes(value: str, *, allow_legacy_weak: bool = False) -> bytes:
    if not isinstance(value, str):
        raise ValueError("客户端密钥必须是可打印 ASCII")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("客户端密钥必须是可打印 ASCII") from exc
    if not encoded or len(encoded) > MAX_CLIENT_TOKEN_BYTES or any(
        character < 33 or character > 126 for character in encoded
    ):
        raise ValueError("客户端密钥必须是无空白可打印 ASCII")
    if allow_legacy_weak:
        return encoded
    if len(encoded) < MIN_CLIENT_TOKEN_BYTES or _CLIENT_TOKEN_RE.fullmatch(encoded) is None:
        raise ValueError(
            "客户端密钥必须是至少 32 字节的 URL-safe 随机 token（仅字母、数字、_、-）"
        )
    if _estimated_symbol_entropy_bits(encoded) < MIN_CLIENT_TOKEN_ESTIMATED_ENTROPY_BITS:
        raise ValueError(
            "客户端密钥的结构熵不足；请使用 secrets.token_urlsafe(32) 生成随机 token"
        )
    return encoded


def _estimated_symbol_entropy_bits(value: bytes) -> float:
    """Reject obviously low-diversity passwords without claiming true RNG proof."""

    counts = Counter(value)
    length = len(value)
    entropy_per_symbol = -sum(
        (count / length) * log2(count / length)
        for count in counts.values()
    )
    return entropy_per_symbol * length


def provider_secret_header_value(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("上游连接密钥必须是可打印 ASCII")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("上游连接密钥必须是可打印 ASCII") from exc
    if (
        not encoded
        or len(encoded) > MAX_PROVIDER_SECRET_BYTES
        or any(character < 33 or character > 126 for character in encoded)
    ):
        raise ValueError("上游连接密钥必须是 1-65536 字节的无空白可打印 ASCII")
    return value


def validate_secret_snapshot(
    *,
    config: GatewayConfig,
    secrets: dict[str, str],
) -> None:
    """Validate every configured credential used by a candidate config.

    Missing values remain valid because connections and clients may be staged
    before their credential is supplied.  Referenced non-empty values must be
    safe for their eventual use before the atomic control-plane commit starts.
    Orphan secret values are intentionally ignored until a config references
    them.
    """

    for connection_id, connection in config.connections.items():
        value = secrets.get(connection.auth.secret_ref, "")
        if not value:
            continue
        try:
            provider_secret_header_value(value)
        except ValueError as exc:
            raise SecretSnapshotError(
                f"connection {connection_id} 的上游密钥格式无效",
                reason="provider_secret_invalid",
            ) from exc

    for client_id, client in config.clients.items():
        value = secrets.get(client.secret_ref, "")
        if not value:
            continue
        try:
            client_token_bytes(
                value,
                allow_legacy_weak=client.allow_legacy_weak_secret,
            )
        except ValueError as exc:
            raise SecretSnapshotError(
                f"client {client_id} 的密钥格式或强度无效",
                reason="client_secret_invalid",
            ) from exc

    try:
        validate_secret_domains(config=config, secrets=secrets)
    except ValueError as exc:
        raise SecretSnapshotError(
            "密钥配置冲突：client 与上游连接密钥的权限域或唯一性无效",
            reason="secret_domain_conflict",
        ) from exc


def validate_secret_domains(
    *,
    config: GatewayConfig,
    secrets: dict[str, str],
) -> None:
    provider_values = {
        secrets.get(connection.auth.secret_ref, "").encode("utf-8")
        for connection in config.connections.values()
        if secrets.get(connection.auth.secret_ref, "")
    }
    seen_clients: list[bytes] = []
    for client in config.clients.values():
        value = secrets.get(client.secret_ref, "")
        if not value:
            continue
        token = client_token_bytes(
            value,
            allow_legacy_weak=client.allow_legacy_weak_secret,
        )
        if any(hmac.compare_digest(token, provider) for provider in provider_values):
            raise ValueError("客户端与上游连接密钥配置冲突；必须使用不同密钥")
        if any(hmac.compare_digest(token, existing) for existing in seen_clients):
            raise ValueError("客户端密钥配置冲突；每个 client 必须使用不同密钥")
        seen_clients.append(token)
