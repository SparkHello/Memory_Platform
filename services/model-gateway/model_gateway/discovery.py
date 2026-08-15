"""Read-only upstream probing primitives.

Health checks, capability probes and quickstart model discovery all perform
the same credential-guarded ``GET`` against an OpenAI-compatible provider:
no redirect following, no environment proxies, bounded response reads and one
shared ``/models`` listing parser.  Callers keep their own error wording and
timeout policy; the wire behavior lives here exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Literal

import httpx

from model_gateway.auth import provider_secret_header_value
from model_gateway.http_safety import (
    MAX_DISCOVERY_RESPONSE_BYTES,
    bounded_model_ids,
    require_safe_destination,
    require_safe_destination_sync,
    upstream_url,
)
from model_gateway_contracts import ConnectionConfig


class DiscoveryResponseTooLarge(ValueError):
    pass


def upstream_auth_headers(connection: ConnectionConfig, secret: str) -> dict[str, str]:
    provider_secret_header_value(secret)
    headers = {"Accept": "application/json"}
    if connection.auth.type == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    else:
        headers["X-Api-Key"] = secret
    return headers


def probe_client(
    connection: ConnectionConfig,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    """One probe client; a probe must not carry a credential to another host."""

    arguments: dict[str, Any] = {
        "follow_redirects": False,
        "trust_env": False,
        "timeout": httpx.Timeout(
            connect=min(timeout_seconds, connection.connect_timeout_seconds),
            read=min(timeout_seconds, connection.read_timeout_seconds),
            write=min(timeout_seconds, connection.write_timeout_seconds),
            pool=min(timeout_seconds, connection.pool_timeout_seconds),
        ),
    }
    if transport is not None:
        arguments["transport"] = transport
    return httpx.AsyncClient(**arguments)


async def read_bounded_response(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise DiscoveryResponseTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def read_bounded_response_sync(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise DiscoveryResponseTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class ListingFetch:
    """Wire-level outcome of one ``GET`` discovery attempt, before mapping."""

    status: Literal["ok", "http", "network_error", "too_large", "unsafe"]
    http_status: int | None = None
    content: bytes = b""
    error_type: str = ""
    error_detail: str = ""


async def fetch_model_listing(
    connection: ConnectionConfig,
    secret: str,
    *,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> ListingFetch:
    """Read the connection's models endpoint; error bodies are not downloaded."""

    try:
        # Callers gate on ``models_endpoint is not None`` before fetching.
        url = upstream_url(
            connection.base_url,
            connection.models_endpoint or "",
            allowed_private_networks=connection.allowed_private_networks,
        )
        if transport is None:
            await require_safe_destination(
                url,
                allowed_private_networks=connection.allowed_private_networks,
            )
        async with probe_client(connection, timeout_seconds, transport) as client:
            async with client.stream(
                "GET",
                url,
                headers=upstream_auth_headers(connection, secret),
            ) as response:
                if not response.is_success:
                    return ListingFetch(
                        status="http",
                        http_status=response.status_code,
                    )
                content = await read_bounded_response(
                    response,
                    min(
                        connection.response_limit_bytes,
                        MAX_DISCOVERY_RESPONSE_BYTES,
                    ),
                )
                return ListingFetch(
                    status="ok",
                    http_status=response.status_code,
                    content=content,
                )
    except DiscoveryResponseTooLarge:
        return ListingFetch(status="too_large")
    except (httpx.HTTPError, OSError) as exc:
        return ListingFetch(status="network_error", error_type=type(exc).__name__)
    except ValueError as exc:
        # SSRF / fake-ip / URL policy errors carry actionable text for the UI.
        return ListingFetch(status="unsafe", error_detail=str(exc).strip())


def fetch_model_listing_sync(
    *,
    base_url: str,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
    allowed_private_networks: tuple[str, ...] = (),
) -> ListingFetch:
    """Synchronous variant for CLI flows outside an event loop.

    The body is read (bounded) for every status so callers can classify
    redirects and auth failures without a second request.
    """

    try:
        url = upstream_url(
            base_url,
            "/models",
            allowed_private_networks=allowed_private_networks,
        )
        if transport is None:
            require_safe_destination_sync(
                url,
                allowed_private_networks=allowed_private_networks,
            )
        with httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(10.0),
            trust_env=False,
        ) as client:
            with client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
            ) as response:
                content = read_bounded_response_sync(
                    response,
                    MAX_DISCOVERY_RESPONSE_BYTES,
                )
                return ListingFetch(
                    status="ok" if response.is_success else "http",
                    http_status=response.status_code,
                    content=content,
                )
    except DiscoveryResponseTooLarge:
        return ListingFetch(status="too_large")
    except (httpx.HTTPError, OSError) as exc:
        return ListingFetch(status="network_error", error_type=type(exc).__name__)
    except ValueError as exc:
        return ListingFetch(status="unsafe", error_type=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class ModelListing:
    """Parsed ``/models`` payload; ``error`` names the failure category."""

    model_ids: frozenset[str] = field(default_factory=frozenset)
    error: Literal["", "invalid_json", "unrecognized_shape", "invalid_entries"] = ""


def parse_model_listing(content: bytes) -> ModelListing:
    """Parse the OpenAI ``data`` list, a ``models`` list or a bare list."""

    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return ModelListing(error="invalid_json")
    candidates: Any
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        candidates = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        candidates = payload["models"]
    else:
        return ModelListing(error="unrecognized_shape")
    try:
        model_ids = bounded_model_ids(candidates)
    except ValueError:
        return ModelListing(error="invalid_entries")
    return ModelListing(model_ids=frozenset(model_ids))
