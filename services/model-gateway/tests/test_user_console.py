from __future__ import annotations

import argparse
from pathlib import Path

from model_gateway.config_store import (
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    write_config,
)
from model_gateway.models import (
    AuthConfig,
    BillingPlan,
    Capabilities,
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

    assert user_console._find_memgw() == memgw


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
            "",  # pay as you go
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
    assert "upstream-sensitive-key" not in output


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
    monkeypatch.setattr(user_console, "_find_memgw", lambda: Path("/fake/memgw"))
    monkeypatch.setattr(user_console, "_gateway_healthy", lambda paths, config: True)
    monkeypatch.setattr(
        user_console,
        "_run_memgw",
        lambda command, arguments, input_text=None: calls.append((arguments, input_text)) or 0,
    )
    answers = iter(["3", "n", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert user_console.run_user_console(_args(home)) == 0

    config = load_config(paths.config)
    client = config.clients["memory-gateway"]
    client_key = read_secrets(paths.secrets)[client.secret_ref]
    assert len(client_key) >= 32
    assert client.allowed_routes == ["memory.*", "knowledge.*"]
    assert calls[0][0][:3] == ["config", "set", "MODEL_GATEWAY_BASE_URL"]
    assert calls[1][0] == ["secret", "set", "model-gateway", "--stdin"]
    assert calls[1][1] == client_key + "\n"
    assert client_key not in capsys.readouterr().out
