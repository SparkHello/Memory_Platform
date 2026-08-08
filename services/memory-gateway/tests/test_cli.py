from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys

from app.cli import main
from app.cli_config import cli_paths, read_env_file, read_json, update_env_value
from app.model_probe import ModelProbeResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _base_args(tmp_path: Path) -> list[str]:
    return [
        "--home",
        str(tmp_path / "memgw-home"),
        "--project-root",
        str(PROJECT_ROOT),
    ]


def test_cli_initializes_outside_repo_without_copying_placeholder_keys(
    tmp_path,
) -> None:
    args = _base_args(tmp_path)

    assert main([*args, "init"]) == 0

    paths = cli_paths(tmp_path / "memgw-home")
    values = read_env_file(paths.settings_env)
    assert paths.models.exists()
    assert paths.routes.exists()
    assert paths.pricing.exists()
    assert values["MODEL_CATALOG_PATH"] == str(paths.models)
    assert values["MODEL_ROUTES_PATH"] == str(paths.routes)
    assert values["PRICING_CATALOG_PATH"] == str(paths.pricing)
    assert values.get("GATEWAY_API_KEY") != "change-me"
    assert values.get("UPSTREAM_API_KEY") != "your-upstream-api-key"


def test_cli_sets_secrets_without_echoing_them(tmp_path, monkeypatch, capsys) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("super-secret-value\n"))

    assert main([*args, "secret", "set", "gateway", "--stdin"]) == 0

    output = capsys.readouterr().out
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert values["GATEWAY_API_KEY"] == "super-secret-value"
    assert "super-secret-value" not in output


def test_cli_checks_provider_after_saving_remote_api_key(
    tmp_path, monkeypatch, capsys
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("provider-secret\n"))
    calls: list[tuple[str, bool]] = []

    def fake_check(settings, models, *, provider_filter, live, timeout_seconds):
        del settings, models, timeout_seconds
        calls.append((provider_filter, live))
        return [
            ModelProbeResult(
                model_id="mimo/mimo-v2.5-pro-ultraspeed",
                provider="mimo",
                model="mimo-v2.5-pro-ultraspeed",
                status="available",
                detail="连接正常",
                configured=True,
                failed=False,
            )
        ]

    monkeypatch.setattr("app.cli.check_model_catalog", fake_check)

    assert main([*args, "secret", "set", "mimo", "--stdin"]) == 0

    output = capsys.readouterr().out
    assert calls == [("mimo", False)]
    assert "provider-secret" not in output
    assert "[正常]" in output


def test_cli_connects_memory_service_to_independent_model_gateway(
    tmp_path, monkeypatch, capsys
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("local-client-secret\n"))
    checks: list[tuple[Path, Path, float]] = []

    def fake_gateway_check(paths, project_root, *, timeout_seconds):
        checks.append((paths.home, project_root, timeout_seconds))
        return 0

    monkeypatch.setattr("app.cli._run_model_gateway_check", fake_gateway_check)

    assert main([*args, "secret", "set", "model-gateway", "--stdin"]) == 0

    output = capsys.readouterr().out
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert values["MODEL_GATEWAY_API_KEY"] == "local-client-secret"
    assert values["MODEL_GATEWAY_BASE_URL"] == "http://127.0.0.1:2030/v1"
    assert "local-client-secret" not in output
    assert checks == [
        (tmp_path / "memgw-home", PROJECT_ROOT, 10.0),
    ]


def test_user_menu_uses_service_language_and_can_exit(
    tmp_path, monkeypatch, capsys
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    answers = iter(["0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert main([*args, "menu"]) == 0

    output = capsys.readouterr().out
    assert "本地记忆助手" in output
    assert "记忆服务" in output
    assert "模型服务" in output
    assert "connection" not in output
    assert "deployment" not in output


def test_user_menu_opens_independent_model_service_menu(
    tmp_path, monkeypatch
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    answers = iter(["2", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        "app.cli._find_modelgw",
        lambda project_root: Path("/fake/modelgw"),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "app.cli.subprocess.run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )

    assert main([*args, "menu"]) == 0
    assert calls == [["/fake/modelgw"]]


def test_stack_lifecycle_starts_model_first_and_stops_memory_first(
    tmp_path,
    monkeypatch,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    settings_path = cli_paths(tmp_path / "memgw-home").settings_env
    update_env_value(settings_path, "MODEL_GATEWAY_BASE_URL", "http://127.0.0.1:2030/v1")
    update_env_value(settings_path, "MODEL_GATEWAY_API_KEY", "backend-key")
    calls: list[str] = []
    monkeypatch.setattr("app.cli._find_modelgw", lambda project_root: Path("/fake/modelgw"))
    monkeypatch.setattr(
        "app.cli._run_modelgw",
        lambda modelgw, home, arguments, **kwargs: calls.append("model:" + arguments[0]) or 0,
    )
    monkeypatch.setattr(
        "app.cli._cmd_start",
        lambda args, paths, project_root: calls.append("memory:start") or 0,
    )
    monkeypatch.setattr(
        "app.cli._cmd_stop",
        lambda args, paths, project_root: calls.append("memory:stop") or 0,
    )

    assert main([*args, "stack", "start"]) == 0
    assert calls == ["model:start", "memory:start"]
    calls.clear()
    assert main([*args, "stack", "stop"]) == 0
    assert calls == ["memory:stop", "model:stop"]


def test_stack_install_rotates_and_syncs_backend_key_without_echo(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    model_home = tmp_path / "model-home"
    model_home.mkdir()
    (model_home / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "server": {"port": 2030},
                "routes": {"memory.embedding": {"targets": ["embedding"]}},
                "deployments": {
                    "embedding": {"embedding_space": "portable-space"}
                },
            }
        ),
        encoding="utf-8",
    )
    secret_inputs: list[str] = []
    monkeypatch.setattr(
        "app.cli._ensure_model_gateway_runtime",
        lambda args, project_root: Path("/fake/modelgw"),
    )
    monkeypatch.setattr(
        "app.cli._modelgw_json",
        lambda modelgw, home, arguments: [
            {"id": "memory-gateway", "kind": "backend", "secret_configured": True},
            {"id": "memory-console-admin", "kind": "admin", "secret_configured": True},
        ],
    )

    def fake_modelgw(modelgw, home, arguments, **kwargs):
        if kwargs.get("input_text"):
            secret_inputs.append(kwargs["input_text"].strip())
        return 0

    monkeypatch.setattr("app.cli._run_modelgw", fake_modelgw)

    assert (
        main(
            [
                *args,
                "stack",
                "install",
                "--model-gateway-home",
                str(model_home),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert len(secret_inputs) == 1
    assert values["MODEL_GATEWAY_API_KEY"] == secret_inputs[0]
    assert values["MODEL_GATEWAY_BASE_URL"] == "http://127.0.0.1:2030/v1"
    assert values["MODEL_GATEWAY_EMBEDDING_SPACE_ID"] == "portable-space"
    assert secret_inputs[0] not in output


def _install_stack_mocks(tmp_path, monkeypatch) -> Path:
    model_home = tmp_path / "model-home"
    model_home.mkdir()
    (model_home / "config.json").write_text(
        json.dumps({"schema_version": 1, "server": {"port": 2030}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.cli._ensure_model_gateway_runtime",
        lambda args, project_root: Path("/fake/modelgw"),
    )
    monkeypatch.setattr(
        "app.cli._modelgw_json",
        lambda modelgw, home, arguments: [
            {"id": "memory-gateway", "kind": "backend", "secret_configured": True},
            {"id": "memory-console-admin", "kind": "admin", "secret_configured": True},
        ],
    )
    monkeypatch.setattr(
        "app.cli._run_modelgw",
        lambda modelgw, home, arguments, **kwargs: 0,
    )
    return model_home


def test_stack_install_auto_generates_gateway_key_when_absent(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    model_home = _install_stack_mocks(tmp_path, monkeypatch)

    assert (
        main([*args, "stack", "install", "--model-gateway-home", str(model_home)])
        == 0
    )

    output = capsys.readouterr().out
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    gateway_key = values["GATEWAY_API_KEY"]
    # A real generated key, not a placeholder, and long enough to be secure.
    assert gateway_key and gateway_key != "change-me"
    assert len(gateway_key) >= 32
    # The client needs it, so it is shown exactly once on first generation.
    assert gateway_key in output


def test_stack_install_keeps_existing_gateway_key(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    settings_path = cli_paths(tmp_path / "memgw-home").settings_env
    update_env_value(settings_path, "GATEWAY_API_KEY", "already-configured-key")
    capsys.readouterr()
    model_home = _install_stack_mocks(tmp_path, monkeypatch)

    assert (
        main([*args, "stack", "install", "--model-gateway-home", str(model_home)])
        == 0
    )

    output = capsys.readouterr().out
    values = read_env_file(settings_path)
    # An existing client key is never rotated or echoed by install.
    assert values["GATEWAY_API_KEY"] == "already-configured-key"
    assert "already-configured-key" not in output


def test_stack_install_generates_admin_key_once_when_missing(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    model_home = tmp_path / "model-home"
    model_home.mkdir()
    (model_home / "config.json").write_text(
        json.dumps({"schema_version": 1, "server": {"port": 2030}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.cli._ensure_model_gateway_runtime",
        lambda args, project_root: Path("/fake/modelgw"),
    )
    monkeypatch.setattr(
        "app.cli._modelgw_json",
        lambda modelgw, home, arguments: [
            {"id": "memory-gateway", "kind": "backend", "secret_configured": True},
            {"id": "memory-console-admin", "kind": "admin", "secret_configured": False},
        ],
    )
    secret_calls: list[tuple[list[str], str]] = []

    def fake_modelgw(modelgw, home, arguments, **kwargs):
        if kwargs.get("input_text"):
            secret_calls.append((list(arguments), kwargs["input_text"].strip()))
        return 0

    monkeypatch.setattr("app.cli._run_modelgw", fake_modelgw)

    assert (
        main([*args, "stack", "install", "--model-gateway-home", str(model_home)])
        == 0
    )

    output = capsys.readouterr().out
    admin_calls = [
        secret for arguments, secret in secret_calls
        if arguments[:3] == ["secret", "set", "memory-console-admin"]
    ]
    assert len(admin_calls) == 1
    admin_key = admin_calls[0]
    assert len(admin_key) >= 32
    # Shown exactly once so the Web Console can be unlocked without the CLI.
    assert admin_key in output


def test_cli_adds_model_and_assigns_feature_route(tmp_path) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0

    assert (
        main(
            [
                *args,
                "model",
                "add",
                "upstream/example-chat",
                "--provider",
                "upstream",
                "--model",
                "example-chat",
                "--capability",
                "streaming",
                "--official-url",
                "https://provider.example/models/example-chat",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                *args,
                "route",
                "set",
                "memory.review",
                "upstream/example-chat",
                "deepseek/deepseek-v4-flash",
            ]
        )
        == 0
    )

    paths = cli_paths(tmp_path / "memgw-home")
    models = read_json(paths.models)["models"]
    routes = read_json(paths.routes)["routes"]
    assert any(item["id"] == "upstream/example-chat" for item in models)
    assert routes["memory.review"] == [
        "upstream/example-chat",
        "deepseek/deepseek-v4-flash",
    ]


def test_cli_route_accepts_mkd_shorthand(tmp_path) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0

    assert main([*args, "route", "set", "chat", "MKD"]) == 0

    routes = read_json(cli_paths(tmp_path / "memgw-home").routes)["routes"]
    assert routes["chat"] == [
        "mimo/mimo-v2.5-pro-ultraspeed",
        "kimi/kimi-k2.7-code",
        "deepseek/deepseek-v4-flash",
    ]


def test_cli_route_maps_deepseek_shorthand_to_pro_for_knowledge_pro(tmp_path) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0

    assert main([*args, "route", "set", "knowledge.pro", "D"]) == 0

    routes = read_json(cli_paths(tmp_path / "memgw-home").routes)["routes"]
    assert routes["knowledge.pro"] == ["deepseek/deepseek-v4-pro"]


def test_cli_adds_pricing_to_external_catalog(tmp_path) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0

    assert (
        main(
            [
                *args,
                "pricing",
                "add",
                "kimi/kimi-k2.7-code",
                "--billing-provider",
                "kimi",
                "--cache-hit",
                "1",
                "--cache-miss",
                "2",
                "--output",
                "3",
                "--source",
                "https://platform.kimi.com/docs/pricing/chat-k27-code",
                "--as-of",
                "2026-08-02",
                "--replace",
            ]
        )
        == 0
    )

    payload = read_json(cli_paths(tmp_path / "memgw-home").pricing)
    price = next(item for item in payload["models"] if item["key"] == "kimi:kimi-k2.7-code")
    assert price["input_cache_hit_per_million"] == "1"
    assert price["input_cache_miss_per_million"] == "2"
    assert price["output_per_million"] == "3"
    assert payload["as_of"] == "2026-08-02"


def test_settings_error_redaction_and_secret_name_suffixes() -> None:
    from app.cli_config import _is_secret_name
    from app.config import Settings, describe_settings_error

    try:
        Settings(
            _env_file=None,
            **{
                "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030",
                "GATEWAY_API_KEY": "gw-secret-value-1234567890",
                "EMBEDDING_API_KEY": "emb-secret-value-abcdefghij",
            },
        )
    except Exception as exc:
        text = describe_settings_error(exc)
    else:
        raise AssertionError("Settings validation should have failed")
    assert "gw-secret-value-1234567890" not in text
    assert "emb-secret-value-abcdefghij" not in text
    assert "必须同时配置" in text

    assert _is_secret_name("OPENAI_KEY")
    assert _is_secret_name("DASHSCOPE_PASSWORD")
    assert _is_secret_name("GATEWAY_API_KEY")
    assert not _is_secret_name("LOG_LEVEL")


def test_root_setup_returns_machine_readable_error_before_any_mutation() -> None:
    platform_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            str(platform_root / "scripts" / "setup.sh"),
            "--configure-only",
            "--config",
            str(platform_root / "examples" / "quickstart.example.json"),
            "--json",
        ],
        input="",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload == {
        "setup_verified": False,
        "error": {"step": "arguments", "exit_code": 2},
    }
    assert "provider API key is required" in result.stderr
