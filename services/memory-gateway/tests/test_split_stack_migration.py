from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.cli_config import read_env_file, write_env_atomic
from model_gateway.config_store import load_config, write_config, write_secrets
from model_gateway.models import GatewayConfig


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="split migration helpers run inside the Linux migration container",
)


def _load_migrator():
    script = Path(__file__).resolve().parents[3] / "deploy" / "migrate_legacy.py"
    spec = importlib.util.spec_from_file_location("split_stack_migrator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sqlite(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE canary(value TEXT NOT NULL)")
        connection.execute("INSERT INTO canary(value) VALUES (?)", (value,))


def _legacy_fixture(root: Path) -> None:
    memory = root / "memory-gateway"
    model = root / "model-gateway"
    memory.mkdir(parents=True)
    model.mkdir(parents=True)
    _sqlite(memory / "data" / "memory.db", "memory-canary")
    _sqlite(memory / "data" / "knowledge.db", "knowledge-canary")
    write_env_atomic(
        memory / "settings.env",
        {
            "DATABASE_PATH": "/data/memory-gateway/data/memory.db",
            "KNOWLEDGE_DATABASE_PATH": "/data/memory-gateway/data/knowledge.db",
            "GATEWAY_API_KEY": "legacy-gateway-test-value",
            "MODEL_GATEWAY_API_KEY": "legacy-backend-test-value",
            "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030/v1",
            "MEMORY_CONSOLE_ADMIN_KEY": "legacy-admin-env-residue",
        },
    )
    (memory / "project.json").write_text(
        '{"version":1,"project_root":"/app/services/memory-gateway","port":2026}\n',
        encoding="utf-8",
    )
    (memory / "eval").mkdir()
    (memory / "eval" / "canary.txt").write_text("synthetic", encoding="utf-8")

    config = GatewayConfig.model_validate(
        {
            "clients": {
                "memory-gateway": {
                    "kind": "backend",
                    "secret_ref": "CLIENT_MEMORY_GATEWAY",
                    "allowed_routes": ["memory.*", "knowledge.*"],
                },
                "memory-console-admin": {
                    "kind": "admin",
                    "secret_ref": "CLIENT_MEMORY_CONSOLE_ADMIN",
                    "allowed_routes": ["*"],
                },
            }
        }
    )
    write_config(model / "config.json", config)
    write_secrets(
        model / "secrets.env",
        {
            "CLIENT_MEMORY_GATEWAY": "legacy-backend-test-value",
            "CLIENT_MEMORY_CONSOLE_ADMIN": "legacy-admin-test-value",
        },
    )
    _sqlite(model / "usage.db", "usage-canary")


def test_legacy_volume_migrates_only_allowlisted_state(tmp_path, monkeypatch, capsys):
    module = _load_migrator()
    legacy = tmp_path / "legacy"
    _legacy_fixture(legacy)
    (legacy / "unrelated-secret.txt").write_text("must-not-copy", encoding="utf-8")
    roots = {
        "LEGACY": legacy,
        "MEMORY_DATA": tmp_path / "memory-data",
        "MEMORY_SECRETS": tmp_path / "memory-secrets",
        "MODEL_DATA": tmp_path / "model-data",
        "MODEL_SECRETS": tmp_path / "model-secrets",
        "CREDENTIALS": tmp_path / "credentials",
    }
    for name, value in roots.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, "MEMORY_MARKER", roots["MEMORY_DATA"] / ".stack-installed-v2")
    monkeypatch.setattr(module, "MODEL_MARKER", roots["MODEL_DATA"] / ".stack-installed-v2")
    monkeypatch.setattr(module.os, "chown", lambda *_args: None)

    assert module.main() == 0
    output = capsys.readouterr()
    assert "legacy-gateway-test-value" not in output.out + output.err
    assert "legacy-admin-test-value" not in output.out + output.err
    assert not (roots["MEMORY_DATA"] / "unrelated-secret.txt").exists()
    assert (legacy / "unrelated-secret.txt").read_text() == "must-not-copy"
    assert (roots["MEMORY_SECRETS"] / "settings.env").stat().st_mode & 0o777 == 0o600
    migrated_settings = read_env_file(roots["MEMORY_SECRETS"] / "settings.env")
    assert migrated_settings["GATEWAY_LEGACY_API_KEY_ENABLED"] == "true"
    assert migrated_settings["GATEWAY_ALLOW_USER_ID_HEADER"] == "false"
    assert migrated_settings["MODEL_GATEWAY_ALLOW_PRIVATE_HTTP"] == "true"
    assert migrated_settings["MODEL_GATEWAY_BASE_URL"] == "http://model-gateway:2030/v1"
    assert "LLM_DEEPSEEK_API_KEY" not in migrated_settings
    assert "MEMORY_CONSOLE_ADMIN_KEY" not in migrated_settings
    assert "LEGACY_GATEWAY_KEY_ENABLED" not in migrated_settings
    assert (roots["MODEL_SECRETS"] / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert (roots["CREDENTIALS"] / "gateway.txt").read_text().strip() == "legacy-gateway-test-value"
    assert (roots["CREDENTIALS"] / "admin.txt").read_text().strip() == "legacy-admin-test-value"
    assert not (roots["CREDENTIALS"] / "gateway.key").exists()
    assert not (roots["CREDENTIALS"] / "admin.key").exists()
    with sqlite3.connect(roots["MEMORY_DATA"] / "memory.db") as connection:
        assert connection.execute("SELECT value FROM canary").fetchone()[0] == "memory-canary"
    with sqlite3.connect(roots["MODEL_DATA"] / "usage.db") as connection:
        assert connection.execute("SELECT value FROM canary").fetchone()[0] == "usage-canary"
    migrated_model_config = load_config(roots["MODEL_DATA"] / "config.json")
    backend = migrated_model_config.clients["memory-gateway"]
    assert set(backend.allowed_routes) == {
        "memory.chat",
        "memory.extract",
        "memory.compact",
        "memory.core",
        "memory.review",
        "knowledge.fast",
        "knowledge.pro",
        "memory.embedding",
    }
    assert backend.allow_direct_deployments is False
    assert (roots["MEMORY_DATA"] / ".stack-installed-v2").read_text() == (
        roots["MODEL_DATA"] / ".stack-installed-v2"
    ).read_text()


def test_legacy_migration_rejects_symlinked_required_file(tmp_path, monkeypatch, capsys):
    module = _load_migrator()
    legacy = tmp_path / "legacy"
    _legacy_fixture(legacy)
    real_settings = legacy / "memory-gateway" / "settings-real.env"
    (legacy / "memory-gateway" / "settings.env").replace(real_settings)
    (legacy / "memory-gateway" / "settings.env").symlink_to(real_settings)
    for name in ("MEMORY_DATA", "MEMORY_SECRETS", "MODEL_DATA", "MODEL_SECRETS", "CREDENTIALS"):
        monkeypatch.setattr(module, name, tmp_path / name.lower())
    monkeypatch.setattr(module, "LEGACY", legacy)
    monkeypatch.setattr(module, "MEMORY_MARKER", module.MEMORY_DATA / ".stack-installed-v2")
    monkeypatch.setattr(module, "MODEL_MARKER", module.MODEL_DATA / ".stack-installed-v2")
    monkeypatch.setattr(module.os, "chown", lambda *_args: None)

    try:
        module.main()
    except RuntimeError:
        pass
    else:
        raise AssertionError("symlinked settings must be rejected")
    assert "legacy-gateway-test-value" not in capsys.readouterr().out


def test_legacy_migrator_rejects_symlinked_or_hardlinked_credential(tmp_path):
    module = _load_migrator()
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("synthetic-outside-value\n", encoding="ascii")
    outside.chmod(0o640)
    symlink = credential_directory / "gateway.key"
    symlink.symlink_to(outside)

    with pytest.raises(RuntimeError, match="unsafe"):
        module._deliver_once(symlink, "synthetic-outside-value")
    assert outside.read_text(encoding="ascii") == "synthetic-outside-value\n"
    assert outside.stat().st_mode & 0o777 == 0o640

    symlink.unlink()
    hardlink = credential_directory / "gateway.key"
    hardlink.hardlink_to(outside)
    with pytest.raises(RuntimeError, match="unsafe"):
        module._deliver_once(hardlink, "synthetic-outside-value")
    assert outside.read_text(encoding="ascii") == "synthetic-outside-value\n"
    assert outside.stat().st_mode & 0o777 == 0o640


def test_sqlite_migration_includes_committed_wal_without_touching_source(
    tmp_path: Path,
) -> None:
    module = _load_migrator()
    source = tmp_path / "legacy" / "memory.db"
    source.parent.mkdir()
    destination = tmp_path / "split" / "memory.db"

    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE canary(value TEXT NOT NULL)")
        writer.execute("INSERT INTO canary(value) VALUES ('main-old')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE canary SET value='wal-new'")
        writer.commit()

        wal = source.with_name(source.name + "-wal")
        shm = source.with_name(source.name + "-shm")
        assert wal.stat().st_size > 0
        with sqlite3.connect(
            f"file:{source.as_posix()}?mode=ro&immutable=1",
            uri=True,
        ) as main_only:
            assert main_only.execute("SELECT value FROM canary").fetchone()[0] == (
                "main-old"
            )
        assert writer.execute("SELECT value FROM canary").fetchone()[0] == "wal-new"

        source_paths = tuple(path for path in (source, wal, shm) if path.exists())
        for path in source_paths:
            path.chmod(0o444)
        source.parent.chmod(0o555)
        try:
            source_bytes = {path: path.read_bytes() for path in source_paths}
            source_metadata = {
                path: (
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in source_paths
            }

            module._copy_sqlite(source, destination)

            assert {path: path.read_bytes() for path in source_bytes} == source_bytes
            assert {
                path: (
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in source_bytes
            } == source_metadata
        finally:
            source.parent.chmod(0o700)
            for path in source_paths:
                path.chmod(0o600)

        with sqlite3.connect(destination) as migrated:
            assert migrated.execute("SELECT value FROM canary").fetchone()[0] == (
                "wal-new"
            )
            assert migrated.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not list(destination.parent.glob(f".{destination.name}.destination-*"))
    finally:
        writer.close()


def test_sqlite_migration_rejects_symlinked_wal_sidecar(tmp_path: Path) -> None:
    module = _load_migrator()
    source = tmp_path / "legacy.db"
    _sqlite(source, "main-value")
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic-sidecar")
    source.with_name(source.name + "-wal").symlink_to(outside)
    destination = tmp_path / "split" / "memory.db"

    with pytest.raises(RuntimeError, match="sidecar is unsafe"):
        module._copy_sqlite(source, destination)

    assert outside.read_bytes() == b"synthetic-sidecar"
    assert not destination.exists()
