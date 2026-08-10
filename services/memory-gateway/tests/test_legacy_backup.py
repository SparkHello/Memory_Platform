from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    path = ROOT / "deploy" / "backup_legacy.py"
    spec = importlib.util.spec_from_file_location("backup_legacy_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_legacy(tmp_path: Path):
    legacy = tmp_path / "legacy"
    memory = legacy / "memory-gateway"
    model = legacy / "model-gateway"
    (memory / "data").mkdir(parents=True)
    model.mkdir(parents=True)
    (memory / "settings.env").write_text(
        "DATABASE_PATH=/data/memory-gateway/data/memory.db\n"
        "KNOWLEDGE_DATABASE_PATH=/data/memory-gateway/data/knowledge.db\n",
        encoding="utf-8",
    )
    (memory / "data" / "memory.db").write_bytes(b"synthetic")
    (memory / "data" / "knowledge.db").write_bytes(b"synthetic")
    (model / "config.json").write_text("{}\n", encoding="utf-8")
    return legacy, memory, model


def _keep_test_sources(**kwargs):
    return (
        kwargs["memory_database"],
        kwargs["knowledge_database"],
        kwargs["auth_database"],
        kwargs["model_gateway_home"],
    )


def test_legacy_backup_uses_scratch_auth_without_modifying_old_volume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    legacy, memory, model = _synthetic_legacy(tmp_path)
    backup = tmp_path / "backup"
    captured: dict[str, object] = {}
    environment_before = dict(os.environ)
    monkeypatch.setattr(module, "LEGACY", legacy)
    monkeypatch.setattr(module, "LEGACY_MEMORY", memory)
    monkeypatch.setattr(module, "LEGACY_MODEL", model)
    monkeypatch.setattr(module, "BACKUP_DIRECTORY", backup)
    monkeypatch.setattr(module, "_stage_read_only_sources", _keep_test_sources)

    def fake_create_stack_backup(**kwargs) -> None:
        destination = Path(kwargs["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic-backup")
        captured.update(
            kwargs=kwargs,
            auth=str(kwargs["auth_database"]),
        )

    monkeypatch.setattr(module, "create_stack_backup", fake_create_stack_backup)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "backup_legacy.py",
            "pre-upgrade-test.zip",
            str(module.os.getuid()),
            str(module.os.getgid()),
        ],
    )

    assert module.main() == 0
    assert str(captured["auth"]).startswith(str(backup))
    assert dict(os.environ) == environment_before
    assert not (memory / "data" / "auth.db").exists()
    assert list(legacy.rglob("*"))


def test_legacy_backup_prefers_existing_auth_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    legacy, memory, model = _synthetic_legacy(tmp_path)
    existing = memory / "data" / "auth.db"
    existing.write_bytes(b"existing-auth")
    with (memory / "settings.env").open("a", encoding="utf-8") as settings:
        settings.write("AUTH_DATABASE_PATH=/data/memory-gateway/data/auth.db\n")
    captured: dict[str, str] = {}
    environment_before = dict(os.environ)
    monkeypatch.setattr(module, "LEGACY", legacy)
    monkeypatch.setattr(module, "LEGACY_MEMORY", memory)
    monkeypatch.setattr(module, "LEGACY_MODEL", model)
    monkeypatch.setattr(module, "BACKUP_DIRECTORY", tmp_path / "backup")
    monkeypatch.setattr(module, "_stage_read_only_sources", _keep_test_sources)

    def fake_create_stack_backup(**kwargs) -> None:
        destination = Path(kwargs["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic-backup")
        captured.update(auth=str(kwargs["auth_database"]))

    monkeypatch.setattr(module, "create_stack_backup", fake_create_stack_backup)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "backup_legacy.py",
            "pre-upgrade-test.zip",
            str(module.os.getuid()),
            str(module.os.getgid()),
        ],
    )

    assert module.main() == 0
    assert captured["auth"] == str(existing)
    assert dict(os.environ) == environment_before
    assert existing.read_bytes() == b"existing-auth"


def _sqlite(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE canary(value TEXT NOT NULL)")
        connection.execute("INSERT INTO canary(value) VALUES (?)", (value,))


def test_legacy_backup_stages_committed_wal_without_touching_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    legacy = tmp_path / "legacy"
    memory_home = legacy / "memory-gateway"
    model_home = legacy / "model-gateway"
    memory_home.mkdir(parents=True)
    model_home.mkdir(parents=True)
    memory_db = memory_home / "memory.db"
    knowledge_db = memory_home / "knowledge.db"
    auth_db = memory_home / "auth.db"
    usage_db = model_home / "usage.db"
    _sqlite(knowledge_db, "knowledge")
    _sqlite(auth_db, "auth")
    _sqlite(usage_db, "usage")
    (model_home / "config.json").write_text("{}\n", encoding="utf-8")

    writer = sqlite3.connect(memory_db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE canary(value TEXT NOT NULL)")
        writer.execute("INSERT INTO canary(value) VALUES ('main-old')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE canary SET value='wal-new'")
        writer.commit()
        wal = memory_db.with_name(memory_db.name + "-wal")
        shm = memory_db.with_name(memory_db.name + "-shm")
        source_paths = tuple(path for path in (memory_db, wal, shm) if path.exists())
        for path in source_paths:
            path.chmod(0o444)
        memory_home.chmod(0o555)
        source_bytes = {path: path.read_bytes() for path in source_paths}
        source_metadata = {
            path: (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
            for path in source_paths
        }
        scratch = tmp_path / "scratch" / "portable-source"
        try:
            staged_memory, staged_knowledge, staged_auth, staged_model = (
                module._stage_read_only_sources(
                    memory_database=memory_db,
                    knowledge_database=knowledge_db,
                    auth_database=auth_db,
                    model_gateway_home=model_home,
                    stage_root=scratch,
                )
            )
            assert {path: path.read_bytes() for path in source_paths} == source_bytes
            assert {
                path: (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
                for path in source_paths
            } == source_metadata
        finally:
            memory_home.chmod(0o700)
            for path in source_paths:
                path.chmod(0o600)

        with sqlite3.connect(staged_memory) as connection:
            assert connection.execute("SELECT value FROM canary").fetchone()[0] == "wal-new"
        with sqlite3.connect(staged_knowledge) as connection:
            assert connection.execute("SELECT value FROM canary").fetchone()[0] == "knowledge"
        with sqlite3.connect(staged_auth) as connection:
            assert connection.execute("SELECT value FROM canary").fetchone()[0] == "auth"
        with sqlite3.connect(staged_model / "usage.db") as connection:
            assert connection.execute("SELECT value FROM canary").fetchone()[0] == "usage"
    finally:
        writer.close()
