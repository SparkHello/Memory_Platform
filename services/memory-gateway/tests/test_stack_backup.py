from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import zipfile

import pytest

import app.stack_backup as stack_backup_module
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


def _replace_archive_payload(
    source_path: Path,
    destination_path: Path,
    archive_name: str,
    payload: bytes,
) -> None:
    with zipfile.ZipFile(source_path) as source:
        manifest = json.loads(source.read("manifest.json"))
        manifest["files"][archive_name] = {
            "size": len(payload),
            "sha256": sha256(payload).hexdigest(),
        }
        with zipfile.ZipFile(destination_path, "w") as destination:
            destination.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False),
            )
            for name in source.namelist():
                if name == "manifest.json":
                    continue
                destination.writestr(
                    name,
                    payload if name == archive_name else source.read(name),
                )


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


def test_stack_restore_failure_rolls_back_every_modified_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    _database(memory_database, "memory-current")
    _database(knowledge_database, "knowledge-current")
    current_model_config = {"schema_version": 1, "server": {"port": 9999}}
    (model_home / "config.json").write_text(
        json.dumps(current_model_config),
        encoding="utf-8",
    )

    original_atomic_restore = stack_backup_module._atomic_restore
    call_count = 0

    def fail_second_replacement(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated restore failure")
        return original_atomic_restore(*args, **kwargs)

    monkeypatch.setattr(
        stack_backup_module,
        "_atomic_restore",
        fail_second_replacement,
    )

    with pytest.raises(ValueError, match="已自动恢复原文件") as error:
        restore_stack_backup(
            archive_path=archive_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-current"
    assert _database_value(knowledge_database) == "knowledge-current"
    assert json.loads((model_home / "config.json").read_text(encoding="utf-8")) == (
        current_model_config
    )
    assert "restore-backups" in str(error.value)


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


def test_stack_restore_rejects_schema_invalid_config_before_writing(
    tmp_path: Path,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    invalid = tmp_path / "invalid-config.zip"
    _replace_archive_payload(
        archive_path,
        invalid,
        "model-gateway/config.json",
        b'{"schema_version":1,"server":{"port":0}}',
    )

    with pytest.raises(ValueError, match="schema"):
        restore_stack_backup(
            archive_path=invalid,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-before"
    assert _database_value(knowledge_database) == "knowledge-before"
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
