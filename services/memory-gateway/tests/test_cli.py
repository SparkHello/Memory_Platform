from __future__ import annotations

import io
from pathlib import Path
import sys

from app.cli import main
from app.cli_config import cli_paths, read_env_file, read_json
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
