from __future__ import annotations

from collections.abc import Iterator
import atexit
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


_RUNTIME_ENVIRONMENT_EXACT = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "MODEL_GATEWAY_CONFIG_PATH",
    "MODEL_GATEWAY_HOME",
    "MODEL_GATEWAY_SECRETS_PATH",
    "MODEL_GATEWAY_USAGE_DATABASE_PATH",
    "NO_PROXY",
}
_RUNTIME_ENVIRONMENT_PREFIXES = (
    "CLIENT_",
    "MODEL_GATEWAY_",
    "PROVIDER_",
    "UPSTREAM_",
)


def _is_model_runtime_environment(name: str) -> bool:
    return name in _RUNTIME_ENVIRONMENT_EXACT or name.startswith(
        _RUNTIME_ENVIRONMENT_PREFIXES
    )


# Pytest imports this file before fixtures can run. Neutralize inherited path
# overrides first so collection itself cannot open a developer secret store.
_SESSION_ORIGINAL_ENVIRONMENT = {
    name: value
    for name, value in os.environ.items()
    if _is_model_runtime_environment(name)
}
_SESSION_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="model-gateway-pytest-session-"))
os.chmod(_SESSION_RUNTIME_ROOT, 0o700)
for name in list(os.environ):
    if _is_model_runtime_environment(name):
        os.environ.pop(name, None)
os.environ.update(
    {
        "MODEL_GATEWAY_HOME": str(_SESSION_RUNTIME_ROOT / "modelgw-home"),
        "MODEL_GATEWAY_CONFIG_PATH": str(
            _SESSION_RUNTIME_ROOT / "modelgw-home" / "config.json"
        ),
        # Empty is intentional: an explicit gateway home must use its own
        # secret store. This also keeps multiprocessing-spawned helpers from
        # inheriting a parent test's absolute secret path.
        "MODEL_GATEWAY_SECRETS_PATH": "",
        "MODEL_GATEWAY_USAGE_DATABASE_PATH": str(
            _SESSION_RUNTIME_ROOT / "modelgw-home" / "usage.db"
        ),
    }
)
_SESSION_ENVIRONMENT_RESTORED = False


def _restore_session_environment() -> None:
    global _SESSION_ENVIRONMENT_RESTORED
    if _SESSION_ENVIRONMENT_RESTORED:
        return
    _SESSION_ENVIRONMENT_RESTORED = True
    for name in list(os.environ):
        if _is_model_runtime_environment(name):
            os.environ.pop(name, None)
    os.environ.update(_SESSION_ORIGINAL_ENVIRONMENT)
    shutil.rmtree(_SESSION_RUNTIME_ROOT, ignore_errors=True)


atexit.register(_restore_session_environment)

import pytest

from model_gateway.auth import AuthenticatedClient
from model_gateway.config_store import GatewayPaths, gateway_paths, initialize, write_config, write_secrets
from model_gateway.models import GatewayConfig


def pytest_unconfigure(config) -> None:
    del config
    _restore_session_environment()


@pytest.fixture(autouse=True)
def isolate_test_runtime(tmp_path: Path) -> Iterator[None]:
    """Give each test independent Model Gateway paths and secret namespace."""

    original_environment = {
        name: value
        for name, value in os.environ.items()
        if _is_model_runtime_environment(name)
    }
    sandbox = pytest.MonkeyPatch()
    for name in original_environment:
        sandbox.delenv(name, raising=False)

    model_home = tmp_path / "modelgw-home"
    sandbox.chdir(tmp_path)
    sandbox.setenv("MODEL_GATEWAY_HOME", str(model_home))
    sandbox.setenv("MODEL_GATEWAY_CONFIG_PATH", str(model_home / "config.json"))
    sandbox.setenv("MODEL_GATEWAY_SECRETS_PATH", "")
    sandbox.setenv(
        "MODEL_GATEWAY_USAGE_DATABASE_PATH",
        str(model_home / "usage.db"),
    )

    try:
        yield
    finally:
        sandbox.undo()
        # Direct os.environ writes and a test-local monkeypatch.undo() cannot
        # escape the independent sandbox's exact restoration.
        for name in list(os.environ):
            if _is_model_runtime_environment(name) and name not in original_environment:
                os.environ.pop(name, None)
        for name, value in original_environment.items():
            os.environ[name] = value


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
