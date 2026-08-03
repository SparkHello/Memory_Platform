from __future__ import annotations

import json
from pathlib import Path
import socket

import httpx

from model_gateway import cli as cli_module
from model_gateway.cli import main
from model_gateway.config_store import gateway_paths, load_config, read_secrets


def run_cli(home: Path, *arguments: str) -> int:
    return main(["--home", str(home), *arguments])


def test_cli_builds_complete_config_without_echoing_secrets(
    tmp_path: Path, capsys
) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    assert run_cli(home, "client", "add", "memory-gateway", "--route", "memory.*") == 0
    assert (
        run_cli(
            home,
            "secret",
            "set",
            "memory-gateway",
            "--value",
            "local-sensitive-token",
            "--no-check",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "connection",
            "add",
            "deepseek-official",
            "--vendor",
            "deepseek",
            "--base-url",
            "https://api.deepseek.example/v1",
            "--adapter",
            "deepseek",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "secret",
            "set",
            "deepseek-official",
            "--value",
            "upstream-sensitive-token",
            "--no-check",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "deployment",
            "add",
            "deepseek-chat-official",
            "--connection",
            "deepseek-official",
            "--model",
            "deepseek-chat",
            "--author",
            "deepseek",
            "--capability",
            "tools",
            "--capability",
            "reasoning",
            "--reasoning-default",
            "enabled",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "pricing",
            "set",
            "deepseek-chat-2026-08",
            "--input",
            "1",
            "--cached-input",
            "0.1",
            "--output",
            "2",
            "--source-url",
            "https://api.deepseek.example/pricing",
            "--deployment",
            "deepseek-chat-official",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "route",
            "set",
            "memory.chat",
            "deepseek-chat-official",
            "--require",
            "tools",
            "--require",
            "reasoning",
        )
        == 0
    )
    assert run_cli(home, "doctor") == 0

    output = capsys.readouterr().out
    assert "local-sensitive-token" not in output
    assert "upstream-sensitive-token" not in output
    paths = gateway_paths(home)
    config = load_config(paths.config)
    assert config.routes["memory.chat"].targets == ["deepseek-chat-official"]
    assert config.deployments["deepseek-chat-official"].reasoning_default == "enabled"
    assert config.deployments["deepseek-chat-official"].pricing == "deepseek-chat-2026-08"
    secrets = read_secrets(paths.secrets)
    assert secrets[config.clients["memory-gateway"].secret_ref] == "local-sensitive-token"


def test_cli_background_start_status_and_stop(tmp_path: Path, capsys) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    try:
        assert run_cli(home, "start", "--port", str(port)) == 0
        assert run_cli(home, "status") == 0
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        run_cli(home, "stop", "--timeout", "5", "--force")

    assert run_cli(home, "status") == 1
    assert gateway_paths(home).state.exists() is False
    output = capsys.readouterr().out
    assert "后台启动" in output
    assert "已停止" in output


def test_schema_command_outputs_json(capsys) -> None:
    assert main(["schema"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "GatewayConfig"
    assert "connections" in payload["properties"]


def test_doctor_rejects_duplicate_client_secrets_without_echoing_them(
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    assert run_cli(home, "client", "add", "memory-gateway", "--kind", "backend") == 0
    assert run_cli(home, "client", "add", "memory-console-admin", "--kind", "admin") == 0
    for client_id in ("memory-gateway", "memory-console-admin"):
        assert (
            run_cli(
                home,
                "secret",
                "set",
                client_id,
                "--value",
                "duplicate-sensitive-token",
                "--no-check",
            )
            == 0
        )

    assert run_cli(home, "doctor") == 1

    output = capsys.readouterr()
    combined = output.out + output.err
    assert "client_secret_uniqueness" in combined
    assert "memory-console-admin" in combined
    assert "memory-gateway" in combined
    assert "duplicate-sensitive-token" not in combined


def test_cli_accepts_ordered_multi_tier_pricing(tmp_path: Path) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    assert (
        run_cli(
            home,
            "pricing",
            "set",
            "tiered-price",
            "--tier",
            '{"max_input_tokens":32000,"input":"1","output":"2"}',
            "--tier",
            '{"input":"3","output":"4"}',
            "--source-url",
            "https://vendor.example/pricing",
        )
        == 0
    )

    pricing = load_config(gateway_paths(home).config).pricing["tiered-price"]
    assert [tier.max_input_tokens for tier in pricing.tiers] == [32000, None]
    assert str(pricing.tiers[1].output) == "4"


def test_windows_managed_process_requires_command_identity(
    tmp_path: Path, monkeypatch
) -> None:
    paths = gateway_paths(tmp_path / "home")
    state = {"pid": 42, "home": str(paths.home.resolve())}
    monkeypatch.setattr(cli_module.os, "name", "nt")
    monkeypatch.setattr(cli_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli_module, "_process_command", lambda pid: "unrelated.exe")

    assert cli_module._state_process_matches(state, paths) is False

    monkeypatch.setattr(
        cli_module,
        "_process_command",
        lambda pid: (
            f'python -m model_gateway.cli --home "{paths.home.resolve()}" serve'
        ),
    )
    assert cli_module._state_process_matches(state, paths) is True
