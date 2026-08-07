from __future__ import annotations

import io
import json
from pathlib import Path
import sys

from model_gateway.cli import main
from model_gateway.config_store import (
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    set_secret,
)
from model_gateway.quickstart import (
    CHAT_ROUTES,
    EMBEDDING_ROUTE,
    QuickstartError,
    QuickstartSpec,
    apply_quickstart,
)


def _base_spec(**overrides: object) -> QuickstartSpec:
    values: dict[str, object] = {
        "channel_operator": "deepseek",
        "base_url": "https://api.deepseek.example/v1",
        "chat_model": "deepseek-chat",
        "api_key": "upstream-sensitive-token",
    }
    values.update(overrides)
    return QuickstartSpec(**values)  # type: ignore[arg-type]


def test_apply_quickstart_wires_connection_model_and_all_chat_routes(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)

    result = apply_quickstart(paths, _base_spec())

    config = load_config(paths.config)
    assert result.connection_id in config.connections
    assert result.chat_deployment_id in config.deployments
    # Every chat purpose points at the single chat deployment.
    for route_id in CHAT_ROUTES:
        route = config.routes[route_id]
        assert route.targets == [result.chat_deployment_id]
        assert route.kind == "chat"
    # A standalone quickstart creates the backend client so memgw can connect.
    assert result.created_memory_client is True
    assert "memory-gateway" in config.clients
    assert config.clients["memory-gateway"].allowed_routes == ["memory.*", "knowledge.*"]


def test_apply_quickstart_stores_secrets_without_leaking_into_config(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)

    result = apply_quickstart(paths, _base_spec(api_key="upstream-sensitive-token"))

    secret_values = read_secrets(paths.secrets)
    # Both the upstream key and the generated client key are stored by ref.
    assert "upstream-sensitive-token" in secret_values.values()
    assert result.memory_client_key in secret_values.values()
    # The raw API key must never appear in the plaintext config file.
    config_text = paths.config.read_text(encoding="utf-8")
    assert "upstream-sensitive-token" not in config_text
    assert result.memory_client_key not in config_text


def test_apply_quickstart_configures_embedding_route_when_requested(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)

    result = apply_quickstart(
        paths,
        _base_spec(
            embedding_model="text-embed-1",
            embedding_dimensions=1024,
            embedding_space="deepseek-embed-v1",
        ),
    )

    config = load_config(paths.config)
    assert result.embedding_deployment_id in config.deployments
    embedding_route = config.routes[EMBEDDING_ROUTE]
    assert embedding_route.kind == "embedding"
    assert embedding_route.targets == [result.embedding_deployment_id]
    deployment = config.deployments[result.embedding_deployment_id]
    assert deployment.dimensions == 1024
    assert deployment.embedding_space == "deepseek-embed-v1"


def test_apply_quickstart_reuses_existing_memory_client_key(tmp_path: Path) -> None:
    # Simulate a prior `stack install`: the backend client and its synced key
    # already exist. Quickstart must not diverge from that key.
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
    assert (
        main(
            [
                "--home",
                str(paths.home),
                "client",
                "add",
                "memory-gateway",
                "--kind",
                "backend",
                "--route",
                "memory.*",
                "--route",
                "knowledge.*",
            ]
        )
        == 0
    )
    config = load_config(paths.config)
    synced_ref = config.clients["memory-gateway"].secret_ref
    set_secret(paths.secrets, synced_ref, "already-synced-backend-key")

    result = apply_quickstart(paths, _base_spec())

    assert result.created_memory_client is False
    assert result.memory_client_key == "already-synced-backend-key"


def test_apply_quickstart_rejects_incomplete_embedding_spec(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)

    try:
        apply_quickstart(paths, _base_spec(embedding_model="text-embed-1"))
    except QuickstartError:
        pass
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("缺少维度/空间的向量配置应当被拒绝")


def test_quickstart_cli_non_interactive_reads_key_from_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "gateway-home"
    monkeypatch.setattr(sys, "stdin", io.StringIO("upstream-sensitive-token\n"))

    exit_code = main(
        [
            "--home",
            str(home),
            "--json",
            "quickstart",
            "--non-interactive",
            "--channel",
            "deepseek",
            "--base-url",
            "https://api.deepseek.example/v1",
            "--chat-model",
            "deepseek-chat",
            "--no-connect-memory",
            "--no-start",
        ]
    )
    assert exit_code == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["connection_id"]
    assert len(payload["chat_routes"]) == len(CHAT_ROUTES)
    assert payload["memgw_wired"] is False
    assert payload["started"] is False
    # The key from stdin must not surface in CLI output.
    assert "upstream-sensitive-token" not in output
    paths = gateway_paths(home)
    assert "upstream-sensitive-token" not in paths.config.read_text(encoding="utf-8")
