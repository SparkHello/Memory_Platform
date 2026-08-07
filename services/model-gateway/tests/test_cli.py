from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import httpx
import pytest

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


def test_process_command_is_not_truncated_by_narrow_terminal(monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    marker = "x" * 150
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker]
    )
    try:
        command = cli_module._process_command(process.pid)
    finally:
        process.kill()
        process.wait()
    assert command is not None
    assert marker in command


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_cli_background_start_tracks_symlinked_home(tmp_path: Path, capsys) -> None:
    real_home = tmp_path / "real-gateway-home"
    real_home.mkdir()
    home = tmp_path / "gateway-home-link"
    home.symlink_to(real_home, target_is_directory=True)
    assert run_cli(home, "init") == 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    try:
        assert run_cli(home, "start", "--port", str(port)) == 0
        assert run_cli(home, "status") == 0
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        assert response.status_code == 200
    finally:
        run_cli(home, "stop", "--timeout", "5", "--force")

    assert run_cli(home, "status") == 1
    assert gateway_paths(home).state.exists() is False


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


def test_quickstart_non_interactive_builds_config_and_reads_key_from_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import io
    import sys

    home = tmp_path / "gateway-home"
    monkeypatch.setattr(sys, "stdin", io.StringIO("upstream-sensitive-token\n"))

    assert (
        main(
            [
                "--home",
                str(home),
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
        == 0
    )

    output = capsys.readouterr().out
    assert "upstream-sensitive-token" not in output

    paths = gateway_paths(home)
    config = load_config(paths.config)
    # One connection, one chat deployment, every chat route pointed at it, and
    # the memory-gateway backend client materialized for a standalone run.
    assert len(config.connections) == 1
    chat_deployments = [d for d in config.deployments.values() if d.kind == "chat"]
    assert len(chat_deployments) == 1
    assert config.routes["memory.chat"].targets == config.routes["knowledge.pro"].targets
    assert "memory-gateway" in config.clients

    secrets = read_secrets(paths.secrets)
    connection = next(iter(config.connections.values()))
    assert secrets[connection.auth.secret_ref] == "upstream-sensitive-token"
    # The generated backend client key is stored and non-empty, never printed.
    assert secrets[config.clients["memory-gateway"].secret_ref]


def test_quickstart_accepts_json_flag_after_subcommand(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # `--json` is a global flag, but automation (and the ai-install doc) naturally
    # appends it after the subcommand. The subparser mirrors it with SUPPRESS so
    # both `modelgw --json quickstart ...` and `modelgw quickstart ... --json`
    # emit parseable JSON. This locks the trailing position against regression.
    import io
    import sys

    home = tmp_path / "gateway-home"
    monkeypatch.setattr(sys, "stdin", io.StringIO("upstream-sensitive-token\n"))

    assert (
        main(
            [
                "--home",
                str(home),
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
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["connection_id"]
    assert len(payload["chat_routes"]) == 7
    assert "upstream-sensitive-token" not in json.dumps(payload)


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
