from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from app.cli_config import (
    cli_paths,
    initialize_cli,
    read_env_file,
    update_env_value,
)
from app.stack_backup import create_stack_backup, restore_stack_backup


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))


def _database_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM sample").fetchone()[0])


def _fixture(tmp_path: Path):
    paths = cli_paths(tmp_path / "memgw-home")
    initialize_cli(paths=paths, project_root=PROJECT_ROOT, import_project_env=False)
    update_env_value(paths.settings_env, "GATEWAY_API_KEY", "never-export-this")
    update_env_value(paths.settings_env, "MODEL_GATEWAY_API_KEY", "never-export-backend")
    update_env_value(paths.settings_env, "ALLOW_SENSITIVE_EGRESS", "true")

    memory_database = tmp_path / "runtime" / "memory.db"
    knowledge_database = tmp_path / "runtime" / "knowledge.db"
    _database(memory_database, "memory-before")
    _database(knowledge_database, "knowledge-before")

    model_home = tmp_path / "modelgw-home"
    model_home.mkdir()
    (model_home / "config.json").write_text(
        json.dumps({"schema_version": 1, "server": {"port": 2030}}),
        encoding="utf-8",
    )
    (model_home / "secrets.env").write_text(
        "UPSTREAM_TEST=never-export-provider\n",
        encoding="utf-8",
    )
    _database(model_home / "usage.db", "usage-before")
    return paths, memory_database, knowledge_database, model_home


def test_stack_backup_is_portable_and_excludes_all_secrets(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"

    result = create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert result["secrets_included"] is False
    with zipfile.ZipFile(archive_path) as archive:
        assert "model-gateway/secrets.env" not in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        settings = json.loads(archive.read("memory/settings.json"))
        assert manifest["secrets_included"] is False
        assert settings["ALLOW_SENSITIVE_EGRESS"] == "true"
        assert "GATEWAY_API_KEY" not in settings
        assert "MODEL_GATEWAY_API_KEY" not in settings
        for name in archive.namelist():
            payload = archive.read(name)
            assert b"never-export-this" not in payload
            assert b"never-export-backend" not in payload
            assert b"never-export-provider" not in payload


def test_stack_restore_verifies_then_restores_with_rollback(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    memory_database.unlink()
    knowledge_database.unlink()
    _database(memory_database, "memory-after")
    _database(knowledge_database, "knowledge-after")
    update_env_value(paths.settings_env, "ALLOW_SENSITIVE_EGRESS", "false")
    update_env_value(paths.settings_env, "GATEWAY_API_KEY", "new-device-secret")
    (model_home / "config.json").write_text(
        json.dumps({"schema_version": 1, "server": {"port": 9999}}),
        encoding="utf-8",
    )

    result = restore_stack_backup(
        archive_path=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert _database_value(memory_database) == "memory-before"
    assert _database_value(knowledge_database) == "knowledge-before"
    values = read_env_file(paths.settings_env)
    assert values["ALLOW_SENSITIVE_EGRESS"] == "true"
    assert values["GATEWAY_API_KEY"] == "new-device-secret"
    assert json.loads((model_home / "config.json").read_text())["server"]["port"] == 2030
    rollback = Path(result["rollback"])
    assert (rollback / "memory/memory.db").is_file()
    assert (rollback / "model-gateway/config.json").is_file()
    assert result["secrets_restored"] is False


def test_stack_restore_rejects_tampering_before_writing(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "memory/settings.json":
                payload = b'{"ALLOW_SENSITIVE_EGRESS":"false"}\n'
            target.writestr(name, payload)

    with pytest.raises(ValueError, match="校验失败"):
        restore_stack_backup(
            archive_path=tampered,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-before"
    assert not (paths.home / "restore-backups").exists()


def test_stack_restore_rejects_oversized_entry_before_writing(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(oversized, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "memory/memory.db":
                payload = payload + b" " * (8 * 1024 * 1024)
            target.writestr(name, payload)

    with pytest.raises(ValueError, match="校验失败"):
        restore_stack_backup(
            archive_path=oversized,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-before"
    assert not (paths.home / "restore-backups").exists()
