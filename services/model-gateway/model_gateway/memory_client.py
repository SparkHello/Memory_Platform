from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import secrets

from model_gateway.auth import client_token_bytes
from model_gateway.ids import default_secret_ref
from model_gateway.models import ClientConfig, GatewayConfig


# The eight stable business routes Memory Gateway calls by default. Keeping the
# bootstrap policy exact prevents a future matching route from gaining access
# merely because Model Gateway learned about it.
CHAT_ROUTES: tuple[str, ...] = (
    "memory.chat",
    "memory.extract",
    "memory.compact",
    "memory.core",
    "memory.review",
    "knowledge.fast",
    "knowledge.pro",
)
EMBEDDING_ROUTE = "memory.embedding"
DEFAULT_MEMORY_GATEWAY_ROUTES = (*CHAT_ROUTES, EMBEDDING_ROUTE)


@dataclass(frozen=True, slots=True)
class MemoryGatewayClientProvision:
    client: ClientConfig
    key: str
    created: bool


def provision_memory_gateway_client(
    config: GatewayConfig,
    secret_values: Mapping[str, str],
) -> MemoryGatewayClientProvision:
    """Return existing Memory client unchanged, or create its exact default policy."""

    existing = config.clients.get("memory-gateway")
    client = existing or ClientConfig(
        kind="backend",
        secret_ref=default_secret_ref("CLIENT", "memory-gateway"),
        allowed_routes=list(DEFAULT_MEMORY_GATEWAY_ROUTES),
        allow_direct_deployments=False,
    )
    key = secret_values.get(client.secret_ref) or secrets.token_urlsafe(32)
    client_token_bytes(
        key,
        allow_legacy_weak=client.allow_legacy_weak_secret,
    )
    return MemoryGatewayClientProvision(
        client=client,
        key=key,
        created=existing is None,
    )
