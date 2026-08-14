from __future__ import annotations

from contextlib import closing
import json
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zipfile

import pytest

import app.stack_backup as stack_backup_module
from app.auth.tokens import AuthTokenStore
from app.cli_config import (
    cli_paths,
    initialize_cli,
    read_env_file,
    update_env_value,
)
from app.schema_versions import (
    AUTH_SCHEMA_VERSION,
    KNOWLEDGE_SCHEMA_VERSION,
    MEMORY_SCHEMA_VERSION,
)
from app.stack_backup import (
    create_stack_backup as _create_stack_backup,
    restore_stack_backup as _restore_stack_backup,
    validate_stack_backup as _validate_stack_backup,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_stack_backup(**kwargs):
    kwargs.setdefault(
        "auth_database",
        Path(kwargs["memory_database"]).with_name("auth.db"),
    )
    return _create_stack_backup(**kwargs)


def restore_stack_backup(**kwargs):
    kwargs.setdefault(
        "auth_database",
        Path(kwargs["memory_database"]).with_name("auth.db"),
    )
    return _restore_stack_backup(**kwargs)


def validate_stack_backup(**kwargs):
    return _validate_stack_backup(**kwargs)


def _database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == "auth.db":
        # Real store schema (current version), not a handwritten replica: the
        # backup validator must accept exactly what production writes.
        store = AuthTokenStore(path)
        store.init_db()
        store.create_token(name="fixture", user_id="default", role="console")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))
        return
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))
        if path.name == "memory.db":
            connection.executescript(
                f"""
                CREATE TABLE memories (
                    id TEXT, user_id TEXT, content TEXT, type TEXT, archived INTEGER
                );
                CREATE TABLE memory_spaces (id TEXT, user_id TEXT, name TEXT);
                CREATE TABLE core_memory_sections (
                    id TEXT, user_id TEXT, section TEXT, content TEXT
                );
                PRAGMA user_version = {MEMORY_SCHEMA_VERSION};
                """
            )
        elif path.name == "knowledge.db":
            connection.executescript(
                f"""
                CREATE TABLE knowledge_documents (
                    id TEXT, user_id TEXT, title TEXT, status TEXT
                );
                CREATE TABLE knowledge_versions (
                    id TEXT, document_id TEXT, content TEXT, index_status TEXT
                );
                CREATE TABLE knowledge_chunks (
                    id TEXT, version_id TEXT, ordinal INTEGER, content TEXT
                );
                PRAGMA user_version = {KNOWLEDGE_SCHEMA_VERSION};
                """
            )
        elif path.name == "usage.db":
            connection.execute(
                """
                CREATE TABLE usage_events (
                    id TEXT, created_at TEXT, client_id TEXT, kind TEXT,
                    route_id TEXT, deployment_id TEXT, connection_id TEXT,
                    upstream_model TEXT, status_code INTEGER
                )
                """
            )


def _database_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection, connection:
        return str(connection.execute("SELECT value FROM sample").fetchone()[0])


def _leave_committed_wal(path: Path, value: str) -> None:
    """Simulate a stopped/crashed service whose committed WAL still exists."""

    script = (
        "import os, sqlite3, sys; "
        "connection = sqlite3.connect(sys.argv[1]); "
        "connection.execute('PRAGMA journal_mode = WAL'); "
        "connection.execute('PRAGMA wal_autocheckpoint = 0'); "
        "connection.execute('UPDATE sample SET value = ?', (sys.argv[2],)); "
        "connection.commit(); os._exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), value],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert path.with_name(path.name + "-wal").is_file()


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
    _database(memory_database.with_name("auth.db"), "auth-before")

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


def test_stack_backup_endpoint_streams_zip_and_declares_scope(
    client, auth_headers, monkeypatch, tmp_path: Path
) -> None:
    model_home = tmp_path / "endpoint-modelgw-home"
    model_home.mkdir()
    (model_home / "config.json").write_text(
        json.dumps({"schema_version": 1, "server": {"port": 2030}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_GATEWAY_HOME", str(model_home))

    response = client.post("/memories/stack-backup", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-backup-scope"] == "all-users"
    assert response.headers["cache-control"] == "no-store"
    import io

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 2
        assert manifest["components"]["auth_database"]["status"] == "present"


def test_validate_stack_backup_accepts_portable_archive(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    result = validate_stack_backup(archive_path=archive_path)

    assert result["ok"] is True
    assert result["restorable"] is True
    assert result["secrets_included"] is False
    assert result["version"] == 2
    assert result["restore_requires_stopped_services"] is True
    assert result["components"]["memory_database"]["status"] == "present"
    assert "active_memories" in result["stats"]


def test_validate_stack_backup_rejects_non_zip(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-backup.txt"
    junk.write_text("hello", encoding="utf-8")
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        validate_stack_backup(archive_path=junk)


def test_stack_backup_validate_endpoint_accepts_upload(
    client, auth_headers, tmp_path: Path
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

    response = client.post(
        "/memories/stack-backup/validate",
        headers=auth_headers,
        files={
            "file": (
                "memory-stack.zip",
                archive_path.read_bytes(),
                "application/zip",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["restorable"] is True
    assert payload["version"] == 2


def test_stack_backup_validate_endpoint_rejects_garbage(
    client, auth_headers
) -> None:
    response = client.post(
        "/memories/stack-backup/validate",
        headers=auth_headers,
        files={"file": ("bad.zip", b"not-a-zip", "application/zip")},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["code"] == "stack_backup_invalid"


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
        assert manifest["version"] == 2
        assert manifest["secrets_included"] is False
        assert manifest["components"]["memory_database"]["status"] == "present"
        assert manifest["components"]["knowledge_database"]["status"] == "present"
        assert manifest["components"]["auth_database"]["status"] == "present"
        assert manifest["components"]["model_gateway_config"]["status"] == "present"
        assert manifest["components"]["model_gateway_usage"]["status"] == "present"
        assert settings["ALLOW_SENSITIVE_EGRESS"] == "true"
        assert "GATEWAY_API_KEY" not in settings
        assert "MODEL_GATEWAY_API_KEY" not in settings
        for name in archive.namelist():
            payload = archive.read(name)
            assert b"never-export-this" not in payload
            assert b"never-export-backend" not in payload
            assert b"never-export-provider" not in payload


def test_stack_backup_accepts_model_config_override_without_model_home(
    tmp_path: Path,
) -> None:
    """Split Docker: Memory may only have a temp portable config, not Model volumes."""
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    override = tmp_path / "fetched-config.json"
    override.write_bytes((model_home / "config.json").read_bytes())
    archive_path = tmp_path / "portable-override.zip"

    result = create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=None,
        model_config_override=override,
    )

    assert result["secrets_included"] is False
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["components"]["model_gateway_config"]["status"] == "present"
        # usage.db is optional when Model home is not mounted
        assert manifest["components"]["model_gateway_usage"]["status"] == "absent"
        assert "model-gateway/config.json" in archive.namelist()
        assert "model-gateway/usage.db" not in archive.namelist()


def test_sqlite_staging_recovers_readonly_wal_copy_without_touching_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "readonly-source" / "memory.db"
    source.parent.mkdir()
    destination = tmp_path / "backup" / "memory.db"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE sample(value TEXT NOT NULL)")
        writer.execute("INSERT INTO sample(value) VALUES ('main-old')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE sample SET value='wal-new'")
        writer.commit()

        source_family = tuple(
            path
            for path in (
                source,
                source.with_name(source.name + "-wal"),
                source.with_name(source.name + "-shm"),
            )
            if path.exists()
        )
        before = {
            path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in source_family
        }
        direct = stack_backup_module._backup_sqlite_direct

        def fail_live_source_once(source_path: Path, destination_path: Path) -> None:
            if source_path == source:
                raise sqlite3.OperationalError("unable to open database file")
            direct(source_path, destination_path)

        monkeypatch.setattr(
            stack_backup_module,
            "_backup_sqlite_direct",
            fail_live_source_once,
        )
        staged: dict[str, Path] = {}
        stack_backup_module._stage_sqlite(
            source,
            destination,
            staged,
            "memory/memory.db",
        )

        assert staged == {"memory/memory.db": destination}
        with closing(sqlite3.connect(destination)) as recovered, recovered:
            assert recovered.execute("SELECT value FROM sample").fetchone()[0] == (
                "wal-new"
            )
            assert recovered.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert {
            path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in source_family
        } == before
        assert not list(destination.parent.glob(".memory.db.readonly-source-*"))
    finally:
        writer.close()


def test_backup_fsyncs_verified_archive_before_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    destination = tmp_path / "portable.zip"
    synced: set[Path] = set()
    real_fsync_file = stack_backup_module._fsync_file
    real_replace = stack_backup_module.os.replace

    def tracked_fsync(path: Path) -> None:
        synced.add(Path(path))
        real_fsync_file(path)

    def checked_replace(source, target) -> None:
        if Path(target) == destination:
            assert Path(source) in synced
        real_replace(source, target)

    monkeypatch.setattr(stack_backup_module, "_fsync_file", tracked_fsync)
    monkeypatch.setattr(stack_backup_module.os, "replace", checked_replace)

    create_stack_backup(
        destination=destination,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert destination.is_file()


def test_rollback_copy_is_fsynced_before_it_can_be_journaled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    root = tmp_path / "rollback"
    source.write_bytes(b"durable")
    synced_files: list[Path] = []
    synced_directories: list[Path] = []

    monkeypatch.setattr(
        stack_backup_module,
        "_fsync_file",
        lambda path: synced_files.append(Path(path)),
    )
    monkeypatch.setattr(
        stack_backup_module,
        "_fsync_directory",
        lambda path: synced_directories.append(Path(path)),
    )

    rollback = stack_backup_module._save_rollback(
        source,
        root,
        "memory/memory.db",
    )

    assert rollback is not None
    assert rollback.read_bytes() == b"durable"
    assert synced_files == [rollback]
    assert rollback.parent in synced_directories
    assert root in synced_directories


def test_normal_rollback_durably_removes_new_sqlite_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new-usage.db"
    with closing(sqlite3.connect(target)) as connection, connection:
        connection.execute("CREATE TABLE usage_events (id TEXT)")
    target.with_name(target.name + "-wal").write_bytes(b"stale")
    target.with_name(target.name + "-shm").write_bytes(b"stale")
    synced_directories: list[Path] = []
    real_fsync_directory = stack_backup_module._fsync_directory

    def tracked_fsync(path: Path) -> None:
        synced_directories.append(Path(path))
        real_fsync_directory(path)

    monkeypatch.setattr(stack_backup_module, "_fsync_directory", tracked_fsync)

    errors = stack_backup_module._rollback_modified_targets(
        [(target, None, stack_backup_module._validate_model_usage_database)]
    )

    assert errors == []
    assert not target.exists()
    assert not target.with_name(target.name + "-wal").exists()
    assert not target.with_name(target.name + "-shm").exists()
    assert tmp_path in synced_directories


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://user:secret@provider.example/v1",
        "https://provider.example/v1?token=secret",
        "https://provider.example/v1#secret",
    ],
)
def test_stack_backup_refuses_secret_bearing_url_values(
    tmp_path: Path,
    unsafe_url: str,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    update_env_value(paths.settings_env, "LEGACY_PROVIDER_URL", unsafe_url)

    with pytest.raises(ValueError, match="secrets_included=false"):
        create_stack_backup(
            destination=tmp_path / "portable.zip",
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert not (tmp_path / "portable.zip").exists()


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
    secret_rollback = Path(result["secret_rollback"])
    assert (rollback / "memory/memory.db").is_file()
    assert (rollback / "model-gateway/config.json").is_file()
    assert not (rollback / "memory/settings.env").exists()
    assert secret_rollback.parent.parent == paths.settings_env.parent
    if os.name == "posix":
        assert (secret_rollback / "settings.env").stat().st_mode & 0o777 == 0o600
    assert not paths.settings_env.with_suffix(".env.bak").exists()
    assert result["secrets_restored"] is False
    journal = json.loads((rollback / "restore-journal.json").read_text())
    assert journal["status"] == "complete"
    if os.name == "posix":
        assert (rollback / "restore-journal.json").stat().st_mode & 0o777 == 0o600
    assert "new-device-secret" not in json.dumps(journal)


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
    journals = list((paths.home / "restore-backups").glob("*/restore-journal.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text())["status"] == "rolled_back"


def test_stack_restore_rollback_preserves_committed_wal_pages(
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

    _leave_committed_wal(memory_database, "memory-wal-current")

    original_atomic_restore = stack_backup_module._atomic_restore
    call_count = 0

    def fail_second_replacement(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated restore failure after first database")
        return original_atomic_restore(*args, **kwargs)

    monkeypatch.setattr(
        stack_backup_module,
        "_atomic_restore",
        fail_second_replacement,
    )

    with pytest.raises(ValueError, match="已自动恢复原文件"):
        restore_stack_backup(
            archive_path=archive_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-wal-current"


def test_interrupted_restore_blocks_start_and_recovers_idempotently(
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

    original_atomic_restore = stack_backup_module._atomic_restore
    calls = 0

    def interrupt_after_first_replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_atomic_restore(*args, **kwargs)
        if calls == 1:
            raise KeyboardInterrupt("simulated hard interruption")
        return result

    monkeypatch.setattr(
        stack_backup_module,
        "_atomic_restore",
        interrupt_after_first_replace,
    )
    with pytest.raises(KeyboardInterrupt, match="hard interruption"):
        restore_stack_backup(
            archive_path=archive_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-before"
    assert _database_value(knowledge_database) == "knowledge-current"
    with pytest.raises(RuntimeError, match="拒绝启动"):
        stack_backup_module.assert_no_interrupted_stack_restore(paths.home)

    monkeypatch.setattr(
        stack_backup_module,
        "_atomic_restore",
        original_atomic_restore,
    )
    result = stack_backup_module.recover_interrupted_stack_restore(
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        auth_database=memory_database.with_name("auth.db"),
        model_gateway_home=model_home,
    )

    assert result == {"recovered_journals": 1}
    assert _database_value(memory_database) == "memory-current"
    assert _database_value(knowledge_database) == "knowledge-current"
    stack_backup_module.assert_no_interrupted_stack_restore(paths.home)
    assert stack_backup_module.recover_interrupted_stack_restore(
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        auth_database=memory_database.with_name("auth.db"),
        model_gateway_home=model_home,
    ) == {"recovered_journals": 0}
    journals = list((paths.home / "restore-backups").glob("*/restore-journal.json"))
    assert len(journals) == 1
    recovered_journal = json.loads(journals[0].read_text())
    assert recovered_journal["status"] == "rolled_back"
    assert recovered_journal["recovered_after_interruption"] is True


def test_stack_restore_checkpoints_and_discards_stale_sqlite_sidecars(
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

    _leave_committed_wal(memory_database, "after-backup")

    restore_stack_backup(
        archive_path=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert _database_value(memory_database) == "memory-before"
    assert not memory_database.with_name(memory_database.name + "-wal").exists()
    assert not memory_database.with_name(memory_database.name + "-shm").exists()
    assert not list(memory_database.parent.glob(".memory.db.*-wal"))
    assert not list(memory_database.parent.glob(".memory.db.*-shm"))


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


def test_stack_restore_rejects_component_database_swap_before_writing(
    tmp_path: Path,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    swapped_path = tmp_path / "swapped.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    with zipfile.ZipFile(archive_path) as archive:
        knowledge_payload = archive.read("memory/knowledge.db")
    _replace_archive_payload(
        archive_path,
        swapped_path,
        "memory/memory.db",
        knowledge_payload,
    )

    with pytest.raises(ValueError, match="Memory 数据库缺少必需 schema"):
        restore_stack_backup(
            archive_path=swapped_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-before"
    assert _database_value(knowledge_database) == "knowledge-before"
    assert not (paths.home / "restore-backups").exists()


def test_stack_backup_rejects_wrong_component_database_identity(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    knowledge_database.unlink()
    knowledge_database.write_bytes(memory_database.read_bytes())

    with pytest.raises(ValueError, match="Knowledge 数据库"):
        create_stack_backup(
            destination=tmp_path / "invalid-component.zip",
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert not (tmp_path / "invalid-component.zip").exists()


def test_stack_restore_rejects_future_memory_schema_before_writing(
    tmp_path: Path,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    future_path = tmp_path / "future.zip"
    future_database = tmp_path / "future-memory.db"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    with zipfile.ZipFile(archive_path) as archive:
        future_database.write_bytes(archive.read("memory/memory.db"))
    with closing(sqlite3.connect(future_database)) as connection, connection:
        connection.execute("PRAGMA user_version = 999")
    _replace_archive_payload(
        archive_path,
        future_path,
        "memory/memory.db",
        future_database.read_bytes(),
    )

    with pytest.raises(ValueError, match="更高版本"):
        restore_stack_backup(
            archive_path=future_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-before"


def _rewrite_auth_payload_version(
    tmp_path: Path, archive_path: Path, target_path: Path, version: int
) -> None:
    patched_database = tmp_path / f"auth-v{version}.db"
    with zipfile.ZipFile(archive_path) as archive:
        patched_database.write_bytes(archive.read("memory/auth.db"))
    with closing(sqlite3.connect(patched_database)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {version}")
    with closing(sqlite3.connect(patched_database)) as connection, connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _replace_archive_payload(
        archive_path,
        target_path,
        "memory/auth.db",
        patched_database.read_bytes(),
    )


def test_stack_restore_accepts_older_supported_auth_schema(tmp_path: Path) -> None:
    """v1 auth backups from older releases stay restorable; startup migrates them."""
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    old_path = tmp_path / "old-auth.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    _rewrite_auth_payload_version(tmp_path, archive_path, old_path, 1)

    result = restore_stack_backup(
        archive_path=old_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert "memory/auth.db" in result["restored"]
    auth_database = memory_database.with_name("auth.db")
    with closing(sqlite3.connect(auth_database)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    # The regular startup path upgrades the restored older database in place.
    AuthTokenStore(auth_database).init_db()
    with closing(sqlite3.connect(auth_database)) as connection, connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == AUTH_SCHEMA_VERSION
        )


def test_stack_restore_rejects_future_auth_schema_before_writing(
    tmp_path: Path,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "portable.zip"
    future_path = tmp_path / "future-auth.zip"
    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    _rewrite_auth_payload_version(
        tmp_path, archive_path, future_path, AUTH_SCHEMA_VERSION + 1
    )

    with pytest.raises(ValueError, match="更高版本"):
        restore_stack_backup(
            archive_path=future_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database.with_name("auth.db")) == "auth-before"


@pytest.mark.parametrize(
    ("missing_index", "label"),
    [
        (1, "Memory 数据库"),
        (2, "Knowledge 数据库"),
        (3, "Model Gateway 配置"),
    ],
)
def test_stack_backup_rejects_missing_required_components(
    tmp_path: Path, missing_index: int, label: str
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    required = [None, memory_database, knowledge_database, model_home / "config.json"]
    required[missing_index].unlink()

    with pytest.raises(ValueError, match=label):
        create_stack_backup(
            destination=tmp_path / "incomplete.zip",
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert not (tmp_path / "incomplete.zip").exists()


def test_stack_backup_records_optional_usage_as_absent(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    (model_home / "usage.db").unlink()
    archive_path = tmp_path / "portable.zip"

    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["components"]["model_gateway_usage"] == {
            "archive_path": "model-gateway/usage.db",
            "required": False,
            "status": "absent",
        }
        assert "model-gateway/usage.db" not in archive.namelist()


def test_stack_restore_rejects_insufficient_disk_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr(
        stack_backup_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=0),
    )

    with pytest.raises(ValueError, match="磁盘空间不足"):
        restore_stack_backup(
            archive_path=archive_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )

    assert _database_value(memory_database) == "memory-current"
    assert _database_value(knowledge_database) == "knowledge-current"
    assert not (paths.home / "restore-backups").exists()


def test_backup_and_restore_staging_always_uses_persistent_target_filesystems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    archive_path = tmp_path / "backups" / "portable.zip"
    real_temporary_directory = tempfile.TemporaryDirectory
    requested_dirs: list[Path | None] = []

    def tracked_temporary_directory(*args, **kwargs):
        raw_dir = kwargs.get("dir")
        requested_dirs.append(Path(raw_dir) if raw_dir is not None else None)
        return real_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(
        stack_backup_module.tempfile,
        "TemporaryDirectory",
        tracked_temporary_directory,
    )

    create_stack_backup(
        destination=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    restore_stack_backup(
        archive_path=archive_path,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert requested_dirs
    assert all(directory is not None for directory in requested_dirs)
    assert archive_path.parent in requested_dirs
    assert memory_database.parent in requested_dirs
    assert model_home in requested_dirs


def test_restore_preflights_each_target_filesystem_before_extraction(
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

    class Probe:
        def __init__(self, device: int) -> None:
            self.device = device

        def stat(self):
            return SimpleNamespace(st_dev=self.device)

    def fake_probe(path: Path):
        return Probe(2 if "modelgw-home" in str(path) else 1)

    def fake_disk_usage(probe: Probe):
        free = 0 if probe.device == 2 else 10 * 1024 * 1024 * 1024
        return SimpleNamespace(total=free, used=0, free=free)

    monkeypatch.setattr(stack_backup_module, "_existing_path", fake_probe)
    monkeypatch.setattr(stack_backup_module.shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(
        stack_backup_module,
        "_verified_payloads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("extraction must not begin before every volume passes")
        ),
    )

    with pytest.raises(ValueError, match="磁盘空间不足"):
        restore_stack_backup(
            archive_path=archive_path,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            model_gateway_home=model_home,
        )


def test_stack_restore_accepts_legacy_v1_archive(tmp_path: Path) -> None:
    paths, memory_database, knowledge_database, model_home = _fixture(tmp_path)
    current = tmp_path / "portable-v2.zip"
    legacy = tmp_path / "portable-v1.zip"
    create_stack_backup(
        destination=current,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )
    with zipfile.ZipFile(current) as source, zipfile.ZipFile(legacy, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["version"] = 1
        manifest.pop("components")
        target.writestr("manifest.json", json.dumps(manifest))
        for name in source.namelist():
            if name != "manifest.json":
                target.writestr(name, source.read(name))

    memory_database.unlink()
    _database(memory_database, "memory-current")
    restore_stack_backup(
        archive_path=legacy,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        model_gateway_home=model_home,
    )

    assert _database_value(memory_database) == "memory-before"


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
