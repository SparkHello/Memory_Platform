from __future__ import annotations

import argparse
from pathlib import Path

from model_gateway.config_store import (
    configuration_revision,
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    set_secret,
    write_config,
)
from model_gateway.memory_client import CHAT_ROUTES, EMBEDDING_ROUTE
from model_gateway.models import (
    AuthConfig,
    BillingPlan,
    Capabilities,
    ClientConfig,
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
    RequestTransform,
    RouteConfig,
)
from model_gateway import user_console


def _args(home: Path) -> argparse.Namespace:
    return argparse.Namespace(home=str(home), json=False)


def test_find_memgw_supports_monorepo_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model_root = tmp_path / "services" / "model-gateway"
    memgw = tmp_path / "services" / "memory-gateway" / "scripts" / "memgw"
    memgw.parent.mkdir(parents=True)
    memgw.touch()
    monkeypatch.setattr(user_console.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        user_console,
        "__file__",
        str(model_root / "model_gateway" / "user_console.py"),
    )

    assert user_console.find_memgw() == memgw


def test_user_console_adds_channel_model_and_friendly_routes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "gateway-home"
    answers = iter(
        [
            "1",  # add a channel and model
            "deepseek",
            "https://api.deepseek.example/v1",
            "3",  # DeepSeek compatibility
            "deepseek-chat",
            "",  # chat model
            "",  # author defaults to channel
            "1 2",  # tools and reasoning
            "y",  # reasoning on by default
            "",  # save
            "",  # use for all text work
            "0",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        user_console.getpass,
        "getpass",
        lambda prompt="": "upstream-sensitive-key",
    )

    assert user_console.run_user_console(_args(home)) == 0

    paths = gateway_paths(home)
    config = load_config(paths.config)
    assert len(config.connections) == 1
    connection = next(iter(config.connections.values()))
    assert connection.billing_plan == BillingPlan(type="payg", name="default")
    assert connection.usage_scope == "backend_allowed"
    deployment = next(iter(config.deployments.values()))
    assert deployment.upstream_model == "deepseek-chat"
    assert deployment.capabilities.tools is True
    assert deployment.capabilities.reasoning is True
    assert deployment.reasoning_default == "enabled"
    assert set(config.routes) == set(user_console.CHAT_PURPOSES)
    assert read_secrets(paths.secrets)[
        next(iter(config.connections.values())).auth.secret_ref
    ] == "upstream-sensitive-key"

    output = capsys.readouterr().out
    assert "本地模型服务" in output
    assert "添加渠道和模型" in output
    assert "你的套餐" not in output
    assert "upstream-sensitive-key" not in output


def test_user_console_invalid_provider_secret_changes_nothing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "gateway-home"
    paths = gateway_paths(home)
    initialize(paths)
    config_before = paths.config.read_bytes()
    secrets_before = paths.secrets.read_bytes()
    revision_before = configuration_revision(paths.config)
    answers = iter(
        [
            "1",
            "deepseek",
            "https://api.deepseek.example/v1",
            "3",
            "deepseek-chat",
            "",
            "",
            "",
            "",
            "0",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        user_console.getpass,
        "getpass",
        lambda prompt="": "invalid provider secret",
    )

    assert user_console.run_user_console(_args(home)) == 0

    assert paths.config.read_bytes() == config_before
    assert paths.secrets.read_bytes() == secrets_before
    assert configuration_revision(paths.config) == revision_before


def test_user_console_connects_memory_service_without_showing_client_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "gateway-home"
    paths = gateway_paths(home)
    initialize(paths)
    connection = ConnectionConfig(
        channel_operator="example",
        base_url="https://api.example.test/v1",
        auth=AuthConfig(type="bearer", secret_ref="CONNECTION_EXAMPLE"),
        billing_plan=BillingPlan(type="payg", name="default"),
    )
    deployment = DeploymentConfig(
        connection="example-account",
        upstream_model="example-chat",
        model_author="example",
        capabilities=Capabilities(streaming=True),
        request_transform=RequestTransform(),
    )
    routes = {
        route_id: RouteConfig(kind="chat", targets=["example-chat"], max_attempts=1)
        for route_id in user_console.CHAT_PURPOSES
    }
    write_config(
        paths.config,
        GatewayConfig(
            connections={"example-account": connection},
            deployments={"example-chat": deployment},
            routes=routes,
        ),
    )

    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(user_console, "find_memgw", lambda: Path("/fake/memgw"))
    monkeypatch.setattr(user_console, "_gateway_healthy", lambda paths, config: True)
    monkeypatch.setattr(
        user_console,
        "run_memgw",
        lambda command, arguments, input_text=None: calls.append((arguments, input_text)) or 0,
    )
    answers = iter(["3", "n", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert user_console.run_user_console(_args(home)) == 0

    config = load_config(paths.config)
    client = config.clients["memory-gateway"]
    client_key = read_secrets(paths.secrets)[client.secret_ref]
    assert len(client_key) >= 32
    assert client.allowed_routes == [*CHAT_ROUTES, EMBEDDING_ROUTE]
    assert client.allows_route("memory.future") is False
    assert calls[0][0][:3] == ["config", "set", "MODEL_GATEWAY_BASE_URL"]
    assert calls[1][0] == ["secret", "set", "model-gateway", "--stdin"]
    assert calls[1][1] == client_key + "\n"
    assert client_key not in capsys.readouterr().out


def test_user_console_preserves_existing_memory_client_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "gateway-home"
    paths = gateway_paths(home)
    initialize(paths)
    connection = ConnectionConfig(
        channel_operator="example",
        base_url="https://api.example.test/v1",
        auth=AuthConfig(type="bearer", secret_ref="CONNECTION_EXAMPLE"),
        billing_plan=BillingPlan(type="payg", name="default"),
    )
    deployment = DeploymentConfig(
        connection="example-account",
        upstream_model="example-chat",
        model_author="example",
        capabilities=Capabilities(streaming=True),
        request_transform=RequestTransform(),
    )
    original_client = ClientConfig(
        kind="interactive",
        secret_ref="CLIENT_CUSTOM_MEMORY",
        allowed_routes=["custom.memory.route"],
        allow_direct_deployments=True,
        allow_legacy_weak_secret=True,
        enabled=False,
    )
    write_config(
        paths.config,
        GatewayConfig(
            clients={"memory-gateway": original_client},
            connections={"example-account": connection},
            deployments={"example-chat": deployment},
            routes={
                route_id: RouteConfig(
                    kind="chat",
                    targets=["example-chat"],
                    max_attempts=1,
                )
                for route_id in user_console.CHAT_PURPOSES
            },
        ),
    )
    legacy_key = "legacy-client-key"
    set_secret(paths.secrets, original_client.secret_ref, legacy_key)

    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(user_console, "find_memgw", lambda: Path("/fake/memgw"))
    monkeypatch.setattr(user_console, "_gateway_healthy", lambda paths, config: True)
    monkeypatch.setattr(
        user_console,
        "run_memgw",
        lambda command, arguments, input_text=None: calls.append((arguments, input_text)) or 0,
    )
    answers = iter(["3", "n", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert user_console.run_user_console(_args(home)) == 0

    assert load_config(paths.config).clients["memory-gateway"] == original_client
    assert read_secrets(paths.secrets)[original_client.secret_ref] == legacy_key
    assert calls[1][0] == ["secret", "set", "model-gateway", "--stdin"]
    assert calls[1][1] == legacy_key + "\n"
