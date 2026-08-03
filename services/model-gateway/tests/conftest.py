from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from model_gateway.auth import AuthenticatedClient
from model_gateway.config_store import GatewayPaths, gateway_paths, initialize, write_config, write_secrets
from model_gateway.models import GatewayConfig


def config_payload() -> dict[str, Any]:
    return {
        "clients": {
            "memory-gateway": {
                "kind": "backend",
                "secret_ref": "CLIENT_MEMORY_GATEWAY",
                "allowed_routes": ["memory.*"],
            },
            "desktop": {
                "kind": "interactive",
                "secret_ref": "CLIENT_DESKTOP",
                "allowed_routes": ["*"],
                "allow_direct_deployments": True,
            },
            "memory-console-admin": {
                "kind": "admin",
                "secret_ref": "CLIENT_MEMORY_CONSOLE_ADMIN",
                "allowed_routes": ["*"],
            },
        },
        "connections": {
            "official": {
                "channel_operator": "official-vendor",
                "base_url": "https://official.example/v1",
                "auth": {"secret_ref": "UPSTREAM_OFFICIAL"},
            },
            "reseller": {
                "channel_operator": "reseller-vendor",
                "base_url": "https://reseller.example/v1",
                "auth": {"secret_ref": "UPSTREAM_RESELLER"},
            },
        },
        "pricing": {
            "official-chat-2026-08": {
                "mode": "per_token",
                "currency": "USD",
                "tiers": [
                    {"input": "1.00", "cached_input": "0.10", "output": "2.00"}
                ],
                "source_url": "https://official.example/pricing",
                "checked_at": "2026-08-02",
            }
        },
        "deployments": {
            "chat-official": {
                "connection": "official",
                "upstream_model": "author/chat-v1",
                "model_author": "author",
                "capabilities": {
                    "streaming": True,
                    "tools": True,
                    "parallel_tools": True,
                    "reasoning": True,
                    "multimodal_input": True,
                    "json_object": True,
                    "json_schema": True,
                },
                "pricing": "official-chat-2026-08",
            },
            "chat-reseller": {
                "connection": "reseller",
                "upstream_model": "author/chat-v1-resold",
                "model_author": "author",
                "capabilities": {
                    "streaming": True,
                    "tools": True,
                    "parallel_tools": True,
                    "reasoning": True,
                    "multimodal_input": True,
                    "json_object": True,
                    "json_schema": True,
                },
            },
            "embed-official": {
                "connection": "official",
                "upstream_model": "author/embed-v1",
                "model_author": "author",
                "kind": "embedding",
                "dimensions": 4,
                "embedding_space": "author.embed-v1:4",
                "capabilities": {"streaming": False},
            },
        },
        "routes": {
            "memory.chat": {
                "kind": "chat",
                "targets": ["chat-official", "chat-reseller"],
                "required_capabilities": ["tools", "reasoning"],
            },
            "memory.embedding": {
                "kind": "embedding",
                "targets": ["embed-official"],
            },
        },
    }


@pytest.fixture
def gateway_config() -> GatewayConfig:
    return GatewayConfig.model_validate(config_payload())


@pytest.fixture
def backend_client(gateway_config: GatewayConfig) -> AuthenticatedClient:
    return AuthenticatedClient(
        id="memory-gateway",
        config=gateway_config.clients["memory-gateway"],
    )


@pytest.fixture
def gateway_home(tmp_path: Path, gateway_config: GatewayConfig) -> GatewayPaths:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
    write_config(paths.config, gateway_config)
    write_secrets(
        paths.secrets,
        {
            "CLIENT_MEMORY_GATEWAY": "local-client-token",
            "CLIENT_DESKTOP": "desktop-token",
            "CLIENT_MEMORY_CONSOLE_ADMIN": "admin-token",
            "UPSTREAM_OFFICIAL": "official-secret",
            "UPSTREAM_RESELLER": "reseller-secret",
        },
    )
    return paths
