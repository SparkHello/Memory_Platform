from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
import sys

import httpx
import pytest

from app.auth.tokens import AuthTokenStore
from app.cli import (
    _REMOVED_DIRECT_SECRETS,
    MIGRATION_DOC_URL,
    _server_command,
    build_parser,
    main,
)
from app.cli_config import cli_paths, read_env_file, update_env_value


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_stack_install_secret_environment(monkeypatch) -> None:
    for name in (
        "GATEWAY_API_KEY",
        "GATEWAY_SIGNING_SECRET",
        "MODEL_GATEWAY_API_KEY",
        "MODEL_GATEWAY_BASE_URL",
        "MEMORY_CONSOLE_ADMIN_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


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
    assert values["AUTH_DATABASE_PATH"] == str(paths.home / "auth.db")
    assert len(values["GATEWAY_SIGNING_SECRET"]) >= 32
    assert values.get("GATEWAY_API_KEY") != "change-me"
    # Direct-provider catalog keys must not be re-seeded for new installs.
    assert "MODEL_CATALOG_PATH" not in values
    assert "UPSTREAM_API_KEY" not in values


def test_existing_cli_home_migrates_missing_auth_settings_once(tmp_path) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    paths = cli_paths(tmp_path / "memgw-home")
    update_env_value(paths.settings_env, "AUTH_DATABASE_PATH", None)
    update_env_value(paths.settings_env, "GATEWAY_SIGNING_SECRET", None)

    assert main([*args, "token", "list"]) == 0
    migrated = read_env_file(paths.settings_env)
    first_secret = migrated["GATEWAY_SIGNING_SECRET"]
    assert migrated["AUTH_DATABASE_PATH"] == str(paths.home / "auth.db")
    assert len(first_secret) >= 32

    assert main([*args, "token", "list"]) == 0
    assert read_env_file(paths.settings_env)["GATEWAY_SIGNING_SECRET"] == first_secret


def test_cli_sets_secrets_without_echoing_them(tmp_path, monkeypatch, capsys) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("super-secret-value\n"))

    assert main([*args, "secret", "set", "gateway", "--stdin"]) == 0

    output = capsys.readouterr().out
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert values["GATEWAY_API_KEY"] == "super-secret-value"
    assert values["GATEWAY_LEGACY_API_KEY_ENABLED"] == "true"
    assert "super-secret-value" not in output


def test_cli_deleting_legacy_gateway_key_disables_compatibility(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("super-secret-value\n"))
    assert main([*args, "secret", "set", "gateway", "--stdin"]) == 0
    capsys.readouterr()

    assert main([*args, "secret", "delete", "gateway", "--yes"]) == 0

    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert values["GATEWAY_API_KEY"] == ""
    assert values["GATEWAY_LEGACY_API_KEY_ENABLED"] == "false"


def test_cli_creates_lists_and_revokes_scoped_token_without_persisting_secret(
    tmp_path,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()

    assert main(
        [
            *args,
            "token",
            "create",
            "--name",
            "Family browser",
            "--user",
            "alice",
            "--role",
            "console",
        ]
    ) == 0
    create_output = capsys.readouterr().out
    token = next(line for line in create_output.splitlines() if line.startswith("mgw_"))
    token_id = token.split("_", 2)[1]
    secret = token.split("_", 2)[2]
    paths = cli_paths(tmp_path / "memgw-home")
    assert token.encode() not in (paths.home / "auth.db").read_bytes()
    assert secret.encode() not in (paths.home / "auth.db").read_bytes()

    assert main([*args, "token", "list"]) == 0
    list_output = capsys.readouterr().out
    assert token_id in list_output
    assert "console" in list_output
    assert "Family browser" in list_output
    assert token not in list_output
    assert secret not in list_output

    assert main([*args, "token", "revoke", token_id]) == 0
    assert token_id in capsys.readouterr().out
    assert main([*args, "token", "list"]) == 0
    assert "revoked" in capsys.readouterr().out


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


@pytest.mark.parametrize("name", sorted(_REMOVED_DIRECT_SECRETS))
def test_secret_set_removed_direct_names_print_migration_hint(
    tmp_path,
    monkeypatch,
    capsys,
    name,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("removed-direct-secret\n"))

    assert main([*args, "secret", "set", name, "--stdin"]) == 2

    captured = capsys.readouterr()
    assert "direct-provider 路径已移除" in captured.err
    assert MIGRATION_DOC_URL in captured.err
    assert "removed-direct-secret" not in captured.out + captured.err
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert _REMOVED_DIRECT_SECRETS[name] not in values


@pytest.mark.parametrize("name", sorted(_REMOVED_DIRECT_SECRETS))
def test_secret_delete_removed_direct_names_print_migration_hint(
    tmp_path,
    capsys,
    name,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()

    assert main([*args, "secret", "delete", name, "--yes"]) == 2

    captured = capsys.readouterr()
    assert "direct-provider 路径已移除" in captured.err
    assert MIGRATION_DOC_URL in captured.err


def test_secret_set_unknown_name_explains_valid_choices(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO("whatever\n"))

    assert main([*args, "secret", "set", "bogus", "--stdin"]) == 2

    error = capsys.readouterr().err
    assert "未知密钥" in error
    assert "bogus" in error
    for legal in ("gateway", "signing", "model-gateway"):
        assert legal in error


def test_secret_set_signing_still_saved_without_migration_hint(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    secret = "pytest-only-signing-secret-32-bytes-minimum"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))

    assert main([*args, "secret", "set", "signing", "--stdin"]) == 0

    captured = capsys.readouterr()
    assert "direct-provider" not in captured.err
    assert secret not in captured.out
    values = read_env_file(cli_paths(tmp_path / "memgw-home").settings_env)
    assert values["GATEWAY_SIGNING_SECRET"] == secret


@pytest.mark.parametrize(
    "retired_argv",
    (
        ["model"],
        ["model", "list"],
        ["model", "add", "upstream/example-chat", "--capability", "streaming"],
        ["route"],
        ["route", "set", "chat", "MKD"],
        ["pricing"],
        ["pricing", "add", "kimi/kimi-k2.7-code", "--cache-hit", "1"],
    ),
)
def test_retired_direct_provider_commands_always_print_migration_hint(
    tmp_path,
    capsys,
    retired_argv,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()

    assert main([*args, *retired_argv]) == 2

    captured = capsys.readouterr()
    assert "direct-provider 路径已移除" in captured.err
    assert MIGRATION_DOC_URL in captured.err
    assert "unrecognized arguments" not in captured.err


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
    assert calls == [[str(Path("/fake/modelgw"))]]


def test_user_menu_creates_scoped_device_token_instead_of_legacy_key(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    answers = iter(["4", "Family phone", "chat", "alice", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert main([*args, "menu"]) == 0

    output = capsys.readouterr().out
    token = next(line for line in output.splitlines() if line.startswith("mgw_"))
    paths = cli_paths(tmp_path / "memgw-home")
    record = AuthTokenStore(paths.home / "auth.db").authenticate(token)
    assert record is not None
    assert (record.name, record.user_id, record.role) == (
        "Family phone",
        "alice",
        "chat",
    )
    assert "GATEWAY_API_KEY" not in read_env_file(paths.settings_env)


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
        "app.cli._start_memory_service",
        lambda **kwargs: calls.append("memory:start") or 0,
    )
    monkeypatch.setattr(
        "app.cli._stop_memory_service",
        lambda **kwargs: calls.append("memory:stop") or 0,
    )

    assert main([*args, "stack", "start"]) == 0
    assert calls == ["model:start", "memory:start"]
    calls.clear()
    assert main([*args, "stack", "stop"]) == 0
    assert calls == ["memory:stop", "model:stop"]


@pytest.mark.skipif(os.name != "nt", reason="Windows venv process regression")
def test_windows_background_start_tracks_the_gateway_process(tmp_path) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    try:
        assert main([*args, "start", "--port", str(port)]) == 0
        assert main([*args, "status"]) == 0
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            timeout=2,
            trust_env=False,
        )
        assert response.status_code == 200
    finally:
        main([*args, "stop", "--force"])

    assert main([*args, "status"]) == 1


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
    modelgw_calls: list[list[str]] = []
    monkeypatch.setattr(
        "app.cli._ensure_model_gateway_runtime",
        lambda *args, **kwargs: Path("/fake/modelgw"),
    )
    monkeypatch.setattr(
        "app.cli._modelgw_json",
        lambda modelgw, home, arguments: [
            {"id": "memory-gateway", "kind": "backend", "secret_configured": True},
            {"id": "memory-console-admin", "kind": "admin", "secret_configured": True},
        ],
    )

    def fake_modelgw(modelgw, home, arguments, **kwargs):
        modelgw_calls.append(list(arguments))
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
    assert len(secret_inputs) == 2
    assert values["MODEL_GATEWAY_API_KEY"] == secret_inputs[0]
    assert values["MODEL_GATEWAY_BASE_URL"] == "http://127.0.0.1:2030/v1"
    assert values["MODEL_GATEWAY_EMBEDDING_SPACE_ID"] == "portable-space"
    assert all(secret not in output for secret in secret_inputs)
    backend_call = next(
        call
        for call in modelgw_calls
        if call[:4] == ["client", "add", "memory-gateway", "--kind"]
    )
    configured_routes = {
        backend_call[index + 1]
        for index, value in enumerate(backend_call[:-1])
        if value == "--route"
    }
    assert configured_routes == {
        "memory.chat",
        "memory.extract",
        "memory.compact",
        "memory.core",
        "memory.review",
        "knowledge.fast",
        "knowledge.pro",
        "memory.embedding",
    }
    assert "memory.*" not in backend_call
    assert "knowledge.*" not in backend_call


def _install_stack_mocks(tmp_path, monkeypatch) -> Path:
    model_home = tmp_path / "model-home"
    model_home.mkdir()
    (model_home / "config.json").write_text(
        json.dumps({"schema_version": 1, "server": {"port": 2030}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.cli._ensure_model_gateway_runtime",
        lambda *args, **kwargs: Path("/fake/modelgw"),
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


def test_stack_install_provisions_scoped_console_credential_without_echo(
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
    paths = cli_paths(tmp_path / "memgw-home")
    values = read_env_file(paths.settings_env)
    credential_path = paths.credentials / "gateway.key"
    token = credential_path.read_text(encoding="ascii").strip()
    record = AuthTokenStore(paths.home / "auth.db").authenticate(token)

    assert "GATEWAY_API_KEY" not in values
    assert values["GATEWAY_LEGACY_API_KEY_ENABLED"] == "false"
    assert record is not None
    assert (record.name, record.user_id, record.role) == (
        "first-console",
        "default",
        "console",
    )
    if os.name == "posix":
        assert paths.credentials.stat().st_mode & 0o777 == 0o700
        assert credential_path.stat().st_mode & 0o777 == 0o600
    assert token not in output
    assert str(credential_path) in output


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
    assert values["GATEWAY_LEGACY_API_KEY_ENABLED"] == "true"
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
        lambda *args, **kwargs: Path("/fake/modelgw"),
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
    admin_path = cli_paths(tmp_path / "memgw-home").credentials / "admin.key"
    assert admin_path.read_text(encoding="ascii").strip() == admin_key
    if os.name == "posix":
        assert admin_path.stat().st_mode & 0o777 == 0o600
    assert admin_key not in output
    assert str(admin_path) in output


def test_stack_install_supports_private_custom_credential_directory(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    model_home = _install_stack_mocks(tmp_path, monkeypatch)
    credential_dir = tmp_path / "host-private-credentials"

    assert (
        main(
            [
                *args,
                "stack",
                "install",
                "--model-gateway-home",
                str(model_home),
                "--credential-dir",
                str(credential_dir),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    first_console = (credential_dir / "gateway.key").read_text(encoding="ascii")
    for name in ("gateway.key", "admin.key"):
        credential = credential_dir / name
        value = credential.read_text(encoding="ascii").strip()
        assert value
        if os.name == "posix":
            assert credential.stat().st_mode & 0o777 == 0o600
        assert value not in output
        assert str(credential) in output

    # The non-secret path is remembered so a safe rerun does not silently look
    # in the default directory and report the managed credential as missing.
    assert (
        main([*args, "stack", "install", "--model-gateway-home", str(model_home)])
        == 0
    )
    assert (credential_dir / "gateway.key").read_text(encoding="ascii") == first_console


def test_stack_install_rerun_fails_closed_before_mutation_when_credential_missing(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    model_home = _install_stack_mocks(tmp_path, monkeypatch)
    command = [*args, "stack", "install", "--model-gateway-home", str(model_home)]
    assert main(command) == 0
    paths = cli_paths(tmp_path / "memgw-home")
    (paths.credentials / "gateway.key").unlink()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "app.cli._run_modelgw",
        lambda modelgw, home, arguments, **kwargs: calls.append(list(arguments)) or 0,
    )

    assert main(command) == 2

    assert calls == []
    assert "gateway.key" in capsys.readouterr().err


def test_stack_install_rejects_symlink_credential_directory(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    capsys.readouterr()
    model_home = _install_stack_mocks(tmp_path, monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "credentials-link"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise

    assert (
        main(
            [
                *args,
                "stack",
                "install",
                "--model-gateway-home",
                str(model_home),
                "--credential-dir",
                str(linked),
            ]
        )
        == 2
    )

    assert "符号链接" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name",
    (
        "GATEWAY_API_KEY",
        "GATEWAY_SIGNING_SECRET",
        "MODEL_GATEWAY_API_KEY",
        "MEMORY_CONSOLE_ADMIN_KEY",
    ),
)
def test_stack_install_rejects_first_access_credentials_from_environment(
    tmp_path,
    monkeypatch,
    capsys,
    name,
) -> None:
    args = _base_args(tmp_path)
    secret = "environment-secret-must-not-be-read-0123456789"
    monkeypatch.setenv(name, secret)

    assert main([*args, "stack", "install"]) == 2

    error = capsys.readouterr().err
    assert name in error
    assert "拒绝从进程环境读取" in error
    assert secret not in error
    assert not (tmp_path / "memgw-home").exists()


def test_source_runtime_defaults_to_loopback_and_lan_is_explicit() -> None:
    parser = build_parser()
    start = parser.parse_args(["stack", "start"])
    explicit_lan = parser.parse_args(
        ["stack", "restart", "--host", "0.0.0.0"]
    )

    assert start.host == "127.0.0.1"
    assert explicit_lan.host == "0.0.0.0"


def test_server_command_exports_settings_path_but_not_secret_values(
    tmp_path,
    monkeypatch,
) -> None:
    args = _base_args(tmp_path)
    assert main([*args, "init", "--no-import-env"]) == 0
    paths = cli_paths(tmp_path / "memgw-home")
    # _server_command requires the project venv to exist; stub it so the test
    # does not depend on a real .venv in the checkout (CI has none).
    fake_python = tmp_path / "project-venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.touch()
    monkeypatch.setattr("app.cli._project_python", lambda _root: fake_python)
    synthetic = {
        "GATEWAY_API_KEY": "synthetic-legacy-secret",
        "GATEWAY_SIGNING_SECRET": "synthetic-signing-secret-at-least-32-bytes",
        "MODEL_GATEWAY_API_KEY": "synthetic-backend-secret",
    }
    for name, value in synthetic.items():
        update_env_value(paths.settings_env, name, value)
        monkeypatch.setenv(name, "environment-copy-must-be-removed")

    _, environment, _ = _server_command(
        paths=paths,
        project_root=PROJECT_ROOT,
        host="0.0.0.0",
        port=None,
        reload=False,
    )

    assert environment["MEMGW_SETTINGS_PATH"] == str(paths.settings_env)
    for name, value in synthetic.items():
        assert name not in environment
        assert value not in environment.values()


def test_settings_error_redaction_and_secret_name_suffixes() -> None:
    from app.cli_config import _is_secret_name
    from app.config import Settings, describe_settings_error

    try:
        Settings(
            _env_file=None,
            **{
                "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030",
                "GATEWAY_API_KEY": "gw-secret-value-1234567890",
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


@pytest.mark.skipif(os.name == "nt", reason="root source setup is a POSIX script")
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


@pytest.mark.skipif(os.name == "nt", reason="root source setup is a POSIX script")
def test_root_setup_rejects_access_secrets_from_environment_before_bootstrap() -> None:
    platform_root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    environment["MODEL_GATEWAY_API_KEY"] = "must-not-enter-installer-environment"

    result = subprocess.run(
        [str(platform_root / "scripts" / "setup.sh"), "--install-only"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 2
    assert "MODEL_GATEWAY_API_KEY" in result.stderr
    assert "must-not-enter-installer-environment" not in result.stderr
    assert "准备运行环境" not in result.stdout
