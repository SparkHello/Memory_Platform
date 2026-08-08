from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import httpx

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
    discover_model_ids,
    load_quickstart_file,
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


def test_quickstart_requires_explicit_permission_to_replace_existing_routes(
    tmp_path: Path,
) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
    apply_quickstart(paths, _base_spec())

    try:
        apply_quickstart(paths, _base_spec(chat_model="chat-v2"))
    except QuickstartError as exc:
        assert "replace_existing_routes" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("quickstart 不得静默覆盖已有 route")

    result = apply_quickstart(
        paths,
        _base_spec(chat_model="chat-v2", replace_existing_routes=True),
    )
    assert load_config(paths.config).routes["memory.chat"].targets == [
        result.chat_deployment_id
    ]


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


def test_quickstart_file_is_non_secret_and_configures_optional_embedding(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "gateway-home"
    recipe = tmp_path / "quickstart.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "example-channel",
                "base_url": "https://api.example.test/v1",
                "chat_model": "chat-pro",
                "adapter": "generic",
                "chat_capabilities": ["tools", "reasoning"],
                "embedding": {
                    "model": "embed-v1",
                    "dimensions": 768,
                    "space": "example-embed-v1",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("upstream-sensitive-token\n"))
    monkeypatch.setattr(
        "model_gateway.user_console._find_memgw",
        lambda: recipe,
    )

    def fake_run_memgw(command, arguments, *, input_text=None, quiet=False):
        if not quiet:
            print("memgw-noise")
        return 0

    monkeypatch.setattr("model_gateway.user_console._run_memgw", fake_run_memgw)

    def fake_start(args):
        print("start-noise")
        return 0

    monkeypatch.setattr("model_gateway.cli._cmd_start", fake_start)

    exit_code = main(
        [
            "--home",
            str(home),
            "quickstart",
            "--config",
            str(recipe),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["memgw_wired"] is True
    assert payload["started"] is True
    assert payload["embedding_deployment_id"]
    config = load_config(gateway_paths(home).config)
    chat = config.deployments[payload["chat_deployment_id"]]
    assert chat.capabilities.tools is True
    assert chat.capabilities.reasoning is True
    embedding = config.deployments[payload["embedding_deployment_id"]]
    assert embedding.dimensions == 768
    assert embedding.embedding_space == "example-embed-v1"
    assert "upstream-sensitive-token" not in recipe.read_text(encoding="utf-8")


def test_quickstart_file_rejects_secret_fields(tmp_path: Path) -> None:
    recipe = tmp_path / "unsafe-quickstart.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "example",
                "base_url": "https://api.example.test/v1",
                "chat_model": "chat",
                "api_key": "must-not-be-stored-here",
            }
        ),
        encoding="utf-8",
    )

    try:
        load_quickstart_file(recipe, api_key="stdin-key")
    except QuickstartError as exc:
        assert "api_key" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("quickstart 配置中的密钥字段必须被拒绝")


def test_quickstart_file_can_use_maintained_channel_preset(tmp_path: Path) -> None:
    recipe = tmp_path / "preset-quickstart.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "preset": "deepseek",
                "chat_model": "exact-model-id",
                "embedding": None,
            }
        ),
        encoding="utf-8",
    )

    spec = load_quickstart_file(recipe, api_key="stdin-key")

    assert spec.channel_operator == "deepseek"
    assert spec.base_url == "https://api.deepseek.com"
    assert spec.adapter == "deepseek"


def test_model_discovery_reads_models_without_following_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "chat-pro"}, {"id": "embed-v1"}]},
        )

    model_ids = discover_model_ids(
        base_url="https://provider.example/v1",
        api_key="stdin-sensitive-key",
        transport=httpx.MockTransport(handler),
    )

    assert model_ids == ("chat-pro", "embed-v1")
    assert len(requests) == 1
    assert requests[0].url == "https://provider.example/v1/models"
    assert requests[0].headers["authorization"] == "Bearer stdin-sensitive-key"

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://attacker.example/models"})

    try:
        discover_model_ids(
            base_url="https://provider.example/v1",
            api_key="stdin-sensitive-key",
            transport=httpx.MockTransport(redirect_handler),
        )
    except QuickstartError as exc:
        assert "重定向" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("模型发现不得携带凭证跟随重定向")


def test_discover_cli_is_machine_readable_and_does_not_write_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "unused-home"
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-sensitive-key\n"))
    monkeypatch.setattr(
        "model_gateway.quickstart.discover_model_ids",
        lambda **kwargs: ("chat-a", "chat-b"),
    )

    assert (
        main(
            [
                "--home",
                str(home),
                "discover",
                "--preset",
                "deepseek",
                "--non-interactive",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["model_ids"] == ["chat-a", "chat-b"]
    assert payload["inference_sent"] is False
    assert payload["configuration_changed"] is False
    assert not home.exists()
