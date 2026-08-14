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
from model_gateway import process as process_module
from model_gateway.cli import main
from model_gateway.config_store import gateway_paths, load_config, read_secrets


STRONG_LOCAL_TOKEN = "local_sensitive_token_0123456789_ABCDEFG"
STRONG_DUPLICATE_TOKEN = "duplicate_sensitive_token_0123456789_XYZ"


def run_cli(home: Path, *arguments: str) -> int:
    return main(["--home", str(home), *arguments])


def test_cli_can_set_deployment_adapter_profile(tmp_path: Path, capsys) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    assert (
        run_cli(
            home,
            "connection",
            "add",
            "dashscope",
            "--vendor",
            "dashscope",
            "--base-url",
            "https://workspace.example/compatible-mode/v1",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "deployment",
            "add",
            "deepseek-v4-flash",
            "--connection",
            "dashscope",
            "--model",
            "deepseek-v4-flash",
            "--author",
            "deepseek",
            "--adapter-profile",
            "dashscope_deepseek_v4",
            "--tool-choice-with-reasoning",
            "none",
            "--capability",
            "reasoning",
        )
        == 0
    )

    deployment = load_config(home / "config.json").deployments[
        "deepseek-v4-flash"
    ]
    assert deployment.adapter_profile == "dashscope_deepseek_v4"
    assert deployment.tool_choice_with_reasoning == "none"
    capsys.readouterr()


def test_cli_derives_embedding_space_when_not_explicitly_overridden(
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    assert (
        run_cli(
            home,
            "connection",
            "add",
            "vector-channel",
            "--vendor",
            "vector-vendor",
            "--base-url",
            "https://vector.example/v1",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "deployment",
            "add",
            "embed-v4",
            "--connection",
            "vector-channel",
            "--model",
            "embed-v4",
            "--kind",
            "embedding",
            "--dimensions",
            "1024",
        )
        == 0
    )

    deployment = load_config(home / "config.json").deployments["embed-v4"]
    assert deployment.embedding_space.startswith("mgw-embedding-v1-1024-")
    capsys.readouterr()


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
            STRONG_LOCAL_TOKEN,
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
    assert STRONG_LOCAL_TOKEN not in output
    assert "upstream-sensitive-token" not in output
    paths = gateway_paths(home)
    config = load_config(paths.config)
    assert config.routes["memory.chat"].targets == ["deepseek-chat-official"]
    assert config.deployments["deepseek-chat-official"].reasoning_default == "enabled"
    assert config.deployments["deepseek-chat-official"].pricing == "deepseek-chat-2026-08"
    secrets = read_secrets(paths.secrets)
    assert secrets[config.clients["memory-gateway"].secret_ref] == STRONG_LOCAL_TOKEN


def test_cli_background_start_status_and_stop(tmp_path: Path, capsys) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    try:
        assert run_cli(home, "start", "--port", str(port)) == 0
        assert run_cli(home, "status") == 0
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            timeout=2,
            trust_env=False,
        )
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
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            timeout=2,
            trust_env=False,
        )
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
    assert (
        run_cli(
            home,
            "secret",
            "set",
            "memory-gateway",
            "--value",
            STRONG_DUPLICATE_TOKEN,
            "--no-check",
        )
        == 0
    )
    assert (
        run_cli(
            home,
            "secret",
            "set",
            "memory-console-admin",
            "--value",
            STRONG_DUPLICATE_TOKEN,
            "--no-check",
        )
        == 2
    )

    assert run_cli(home, "doctor") == 0

    output = capsys.readouterr()
    combined = output.out + output.err
    assert "密钥配置冲突" in combined
    assert STRONG_DUPLICATE_TOKEN not in combined


def test_client_secret_set_rejects_weak_v2_value(tmp_path: Path, capsys) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    assert run_cli(home, "client", "add", "memory-gateway") == 0

    assert (
        run_cli(
            home,
            "secret",
            "set",
            "memory-gateway",
            "--value",
            "short-password",
            "--no-check",
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "short-password" not in (captured.out + captured.err)
    assert read_secrets(gateway_paths(home).secrets) == {}


def test_doctor_warns_for_migrated_weak_client_and_rotation_clears_override(
    tmp_path: Path,
    capsys,
) -> None:
    from model_gateway.config_store import write_config, write_secrets

    home = tmp_path / "gateway-home"
    paths = gateway_paths(home)
    assert run_cli(home, "init") == 0
    legacy_payload = {
        "schema_version": 1,
        "clients": {
            "memory-gateway": {
                "kind": "backend",
                "secret_ref": "CLIENT_MEMORY_GATEWAY",
                "allowed_routes": ["memory.*"],
            }
        },
    }
    write_config(paths.config, legacy_payload)
    weak = "legacy-weak-value"
    write_secrets(paths.secrets, {"CLIENT_MEMORY_GATEWAY": weak})

    assert run_cli(home, "doctor") == 0
    doctor_output = capsys.readouterr().out
    assert "schema-v1 client 暂时使用旧弱密钥" in doctor_output
    assert "memory-gateway" in doctor_output
    assert weak not in doctor_output

    assert (
        run_cli(
            home,
            "secret",
            "set",
            "memory-gateway",
            "--value",
            STRONG_LOCAL_TOKEN,
            "--no-check",
        )
        == 0
    )
    config = load_config(paths.config)
    assert config.clients["memory-gateway"].allow_legacy_weak_secret is False
    assert read_secrets(paths.secrets)["CLIENT_MEMORY_GATEWAY"] == STRONG_LOCAL_TOKEN


def test_serve_container_network_requires_exact_explicit_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0
    captured: dict[str, object] = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert (
        run_cli(
            home,
            "serve",
            "--host",
            "0.0.0.0",
            "--container-network",
            "--no-access-log",
        )
        == 0
    )
    assert captured["host"] == "0.0.0.0"
    assert captured["access_log"] is False
    capsys.readouterr()


def test_non_loopback_serve_is_rejected_without_container_flag(
    tmp_path: Path,
    capsys,
) -> None:
    home = tmp_path / "gateway-home"
    assert run_cli(home, "init") == 0

    assert run_cli(home, "serve", "--host", "0.0.0.0") == 2
    assert run_cli(home, "serve", "--container-network") == 2
    assert (
        run_cli(
            home,
            "serve",
            "--host",
            "192.168.1.10",
            "--container-network",
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "--container-network" in captured.err or "回环地址" in captured.err


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
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(process_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(process_module, "_process_command", lambda pid: "unrelated.exe")

    assert process_module._state_process_matches(state, paths) is False

    monkeypatch.setattr(
        process_module,
        "_process_command",
        lambda pid: (
            f'python -m model_gateway.cli --home "{paths.home.resolve()}" serve'
        ),
    )
    assert process_module._state_process_matches(state, paths) is True
