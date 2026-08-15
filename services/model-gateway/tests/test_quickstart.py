from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import httpx
import pytest

from model_gateway.cli import main
from model_gateway.config_store import (
    configuration_revision,
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    set_secret,
    write_config,
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
from model_gateway.models import ClientConfig, GatewayConfig, RouteConfig


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
    assert config.clients["memory-gateway"].allowed_routes == [
        *CHAT_ROUTES,
        EMBEDDING_ROUTE,
    ]


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


def test_quickstart_invalid_provider_secret_changes_nothing(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
    config_before = paths.config.read_bytes()
    secrets_before = paths.secrets.read_bytes()
    revision_before = configuration_revision(paths.config)

    with pytest.raises(QuickstartError):
        apply_quickstart(paths, _base_spec(api_key="invalid provider secret"))

    assert paths.config.read_bytes() == config_before
    assert paths.secrets.read_bytes() == secrets_before
    assert configuration_revision(paths.config) == revision_before


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


def test_apply_quickstart_derives_embedding_space_when_ordinary_setup_omits_it(
    tmp_path: Path,
) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)

    result = apply_quickstart(
        paths,
        _base_spec(
            embedding_model="text-embed-v4",
            embedding_dimensions=1024,
        ),
    )

    deployment = load_config(paths.config).deployments[
        result.embedding_deployment_id
    ]
    assert deployment.embedding_space == result.embedding_space
    assert deployment.embedding_space.startswith("mgw-embedding-v1-1024-")


def test_apply_quickstart_reuses_existing_memory_client_key(tmp_path: Path) -> None:
    # Simulate a prior `stack install`: the backend client and its synced key
    # already exist. Quickstart must not diverge from that key.
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
    client_arguments = [
        "--home",
        str(paths.home),
        "client",
        "add",
        "memory-gateway",
        "--kind",
        "backend",
    ]
    for route_id in (*CHAT_ROUTES, EMBEDDING_ROUTE):
        client_arguments.extend(["--route", route_id])
    assert main(client_arguments) == 0
    config = load_config(paths.config)
    original_client = config.clients["memory-gateway"]
    synced_ref = config.clients["memory-gateway"].secret_ref
    synced_key = "already_synced_backend_key_0123456789_ABC"
    set_secret(paths.secrets, synced_ref, synced_key)

    result = apply_quickstart(paths, _base_spec())

    assert result.created_memory_client is False
    assert result.memory_client_key == synced_key
    assert load_config(paths.config).clients["memory-gateway"] == original_client


def test_apply_quickstart_preserves_every_existing_memory_client_attribute(
    tmp_path: Path,
) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
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
        GatewayConfig(clients={"memory-gateway": original_client}),
    )
    legacy_key = "legacy-client-key"
    set_secret(paths.secrets, original_client.secret_ref, legacy_key)

    result = apply_quickstart(paths, _base_spec())

    assert result.created_memory_client is False
    assert result.memory_client_key == legacy_key
    assert load_config(paths.config).clients["memory-gateway"] == original_client


def test_new_matching_route_is_not_implicitly_authorized_for_fresh_client(
    tmp_path: Path,
) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)
    result = apply_quickstart(paths, _base_spec())
    config = load_config(paths.config)
    payload = config.model_dump(mode="python")
    payload["routes"]["memory.future"] = RouteConfig(
        kind="chat",
        targets=[result.chat_deployment_id],
    ).model_dump(mode="python")
    write_config(paths.config, GatewayConfig.model_validate(payload))

    client = load_config(paths.config).clients["memory-gateway"]
    assert client.allowed_routes == [*CHAT_ROUTES, EMBEDDING_ROUTE]
    assert client.allows_route("memory.future") is False


def test_apply_quickstart_rejects_incomplete_embedding_spec(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "gateway-home")
    initialize(paths)

    try:
        apply_quickstart(paths, _base_spec(embedding_model="text-embed-1"))
    except QuickstartError:
        pass
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("缺少维度的向量配置应当被拒绝")


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
        "model_gateway.user_console.find_memgw",
        lambda: recipe,
    )

    def fake_run_memgw(command, arguments, *, input_text=None, quiet=False):
        if not quiet:
            print("memgw-noise")
        return 0

    monkeypatch.setattr("model_gateway.user_console.run_memgw", fake_run_memgw)

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


def test_model_discovery_caps_body_count_and_identifier_shape() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    with pytest.raises(QuickstartError, match="2 MiB"):
        discover_model_ids(
            base_url="https://provider.example/v1",
            api_key="stdin-sensitive-key",
            transport=httpx.MockTransport(oversized),
        )

    yielded: list[int] = []

    class CountingStream(httpx.SyncByteStream):
        def __iter__(self):
            for index, chunk in enumerate(
                [b"a" * (1024 * 1024), b"b" * (1024 * 1024 + 1), b"SECRET"]
            ):
                yielded.append(index)
                yield chunk

        def close(self) -> None:
            return None

    def chunked_oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream())

    with pytest.raises(QuickstartError, match="2 MiB"):
        discover_model_ids(
            base_url="https://provider.example/v1",
            api_key="stdin-sensitive-key",
            transport=httpx.MockTransport(chunked_oversized),
        )
    assert yielded == [0, 1]

    def at_limit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": f"model-{index}"} for index in range(1_000)]},
        )

    assert len(
        discover_model_ids(
            base_url="https://provider.example/v1",
            api_key="stdin-sensitive-key",
            transport=httpx.MockTransport(at_limit),
        )
    ) == 1_000

    def too_many(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": f"model-{index}"} for index in range(1_001)]},
        )

    with pytest.raises(QuickstartError, match="条目过多"):
        discover_model_ids(
            base_url="https://provider.example/v1",
            api_key="stdin-sensitive-key",
            transport=httpx.MockTransport(too_many),
        )

    def invalid_ids(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "模型\nsecret"}]})

    with pytest.raises(QuickstartError, match="没有解析到模型 ID"):
        discover_model_ids(
            base_url="https://provider.example/v1",
            api_key="stdin-sensitive-key",
            transport=httpx.MockTransport(invalid_ids),
        )


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
