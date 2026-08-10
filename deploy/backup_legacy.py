"""Create a current portable backup from a stopped legacy all-in-one volume.

The legacy volume is mounted read-only at ``/legacy``.  Installations that
predate scoped device tokens do not have ``auth.db``; in that case a valid
empty auth database is created only in a private sibling stage under the host
backup directory so backup v2 remains complete without modifying the old
volume.
"""

from __future__ import annotations

import os
from dataclasses import replace
import importlib.util
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys

from app.auth.tokens import AuthTokenStore
from app.cli_config import cli_paths, read_env_file
from app.stack_backup import create_stack_backup


LEGACY = Path("/legacy")
LEGACY_MEMORY = LEGACY / "memory-gateway"
LEGACY_MODEL = LEGACY / "model-gateway"
BACKUP_DIRECTORY = Path("/backup")
_BACKUP_NAME = re.compile(r"^pre-upgrade-[A-Za-z0-9_.-]+\.zip$")


def _require_regular(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        raise RuntimeError(f"legacy backup source is not a regular file: {path}")


def _legacy_path(raw: str) -> Path | None:
    value = raw.strip()
    if not value:
        return None
    pure = PurePosixPath(value)
    if not pure.is_absolute() or pure.parts[:2] != ("/", "data") or ".." in pure.parts:
        raise RuntimeError("legacy AUTH_DATABASE_PATH is outside /data")
    candidate = LEGACY.joinpath(*pure.parts[2:])
    resolved = candidate.resolve(strict=False)
    if resolved != LEGACY and LEGACY not in resolved.parents:
        raise RuntimeError("legacy AUTH_DATABASE_PATH escapes the source volume")
    return candidate


def _legacy_runtime_path(settings: dict[str, str], key: str, default: str) -> Path:
    candidate = _legacy_path(settings.get(key, default))
    if candidate is None:
        raise RuntimeError(f"legacy {key} is empty")
    _require_regular(candidate)
    return candidate


def _select_auth_database(settings: dict[str, str], scratch_auth: Path) -> Path:
    raw = settings.get("AUTH_DATABASE_PATH", "").strip()
    # Relative source-tree defaults were never stored in the legacy volume;
    # treat them as absent instead of pretending they name persistent auth.
    configured = _legacy_path(raw) if raw.startswith("/") else None
    default = LEGACY_MEMORY / "data" / "auth.db"
    candidate = configured if configured is not None else default
    if candidate.exists() or candidate.is_symlink():
        _require_regular(candidate)
        return candidate
    scratch_auth.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    AuthTokenStore(scratch_auth).init_db()
    return scratch_auth


def _load_migration_helpers():
    """Reuse the migrator's audited, read-only SQLite snapshot primitive."""

    source = Path(__file__).resolve().with_name("migrate_legacy.py")
    _require_regular(source)
    specification = importlib.util.spec_from_file_location(
        "memory_platform_legacy_snapshot",
        source,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("legacy SQLite snapshot helper is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _stage_read_only_sources(
    *,
    memory_database: Path,
    knowledge_database: Path,
    auth_database: Path,
    model_gateway_home: Path,
    stage_root: Path,
) -> tuple[Path, Path, Path, Path]:
    """Normalize SQLite main/WAL/journal files without writing the old volume.

    SQLite databases whose persisted journal mode is WAL may need a writable
    shared-memory file even for a ``mode=ro`` backup.  The legacy volume is an
    immutable rollback anchor, so copy the stable main/sidecar set into the
    private scratch filesystem, recover it there, and point the portable
    backup builder only at those self-contained copies.
    """

    stage_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    migration = _load_migration_helpers()
    staged_memory = stage_root / "memory.db"
    staged_knowledge = stage_root / "knowledge.db"
    staged_auth = stage_root / "auth.db"
    staged_model = stage_root / "model-gateway"
    staged_model.mkdir(mode=0o700)
    migration._copy_sqlite(memory_database, staged_memory)
    migration._copy_sqlite(knowledge_database, staged_knowledge)
    migration._copy_sqlite(auth_database, staged_auth)
    migration._copy_file(
        model_gateway_home / "config.json",
        staged_model / "config.json",
        required=True,
    )
    usage = model_gateway_home / "usage.db"
    if usage.exists() or usage.is_symlink():
        migration._copy_sqlite(usage, staged_model / "usage.db")
    return staged_memory, staged_knowledge, staged_auth, staged_model


def _source_bytes(paths: tuple[Path, ...]) -> int:
    total = 0
    for path in paths:
        for candidate in (
            path,
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-journal"),
        ):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            _require_regular(candidate)
            total += candidate.stat().st_size
    return total


def _ensure_stage_space(parent: Path, payload_bytes: int) -> None:
    # The normalized source generation coexists briefly with the portable
    # builder's own snapshot and archive. Keep a conservative fourth copy plus
    # metadata reserve available before writing the first staging byte.
    required = payload_bytes * 4 + 64 * 1024 * 1024
    if shutil.disk_usage(parent).free < required:
        raise RuntimeError("legacy backup staging filesystem has insufficient space")


def main() -> int:
    os.umask(0o077)
    if len(sys.argv) != 4 or not _BACKUP_NAME.fullmatch(sys.argv[1]):
        raise RuntimeError("invalid legacy backup name")
    backup_name = sys.argv[1]
    try:
        host_uid = int(sys.argv[2])
        host_gid = int(sys.argv[3])
    except ValueError as error:
        raise RuntimeError("invalid host uid/gid") from error
    if host_uid < 0 or host_gid < 0 or host_uid > 2**31 - 1 or host_gid > 2**31 - 1:
        raise RuntimeError("host uid/gid is outside the supported range")
    settings_path = LEGACY_MEMORY / "settings.env"
    for path in (settings_path, LEGACY_MODEL / "config.json"):
        _require_regular(path)
    settings = read_env_file(settings_path)
    memory_database = _legacy_runtime_path(
        settings,
        "DATABASE_PATH",
        "/data/memory-gateway/data/memory.db",
    )
    knowledge_database = _legacy_runtime_path(
        settings,
        "KNOWLEDGE_DATABASE_PATH",
        "/data/memory-gateway/data/knowledge.db",
    )
    configured_auth = _legacy_path(settings.get("AUTH_DATABASE_PATH", "").strip())
    default_auth = LEGACY_MEMORY / "data" / "auth.db"
    source_auth = configured_auth if configured_auth is not None else default_auth
    source_databases = [memory_database, knowledge_database]
    if source_auth.exists() or source_auth.is_symlink():
        source_databases.append(source_auth)
    usage_database = LEGACY_MODEL / "usage.db"
    if usage_database.exists() or usage_database.is_symlink():
        source_databases.append(usage_database)
    # Keep this helper side-effect free even when tests or an embedding caller
    # invoke ``main`` in-process.  ``cli_paths(explicit_home)`` intentionally
    # honours a runtime MEMGW_SETTINGS_PATH override, but a legacy migration
    # must use the settings file inside the explicitly mounted read-only source
    # volume.  Pin that path on the immutable value object instead of mutating
    # process-wide environment variables that could leak into later work.
    paths = replace(cli_paths(LEGACY_MEMORY), settings_env=settings_path)
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if BACKUP_DIRECTORY.is_symlink() or not BACKUP_DIRECTORY.is_dir():
        raise RuntimeError("legacy backup destination is unsafe")
    _ensure_stage_space(BACKUP_DIRECTORY, _source_bytes(tuple(source_databases)))
    destination = BACKUP_DIRECTORY / backup_name
    work = BACKUP_DIRECTORY / f".{backup_name}.source"
    work.mkdir(mode=0o700, exist_ok=False)
    try:
        auth_database = _select_auth_database(settings, work / "auth-source.db")
        (
            memory_database,
            knowledge_database,
            auth_database,
            model_gateway_home,
        ) = _stage_read_only_sources(
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            auth_database=auth_database,
            model_gateway_home=LEGACY_MODEL,
            stage_root=work / "portable-source",
        )
        create_stack_backup(
            destination=destination,
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            auth_database=auth_database,
            model_gateway_home=model_gateway_home,
            force=False,
        )
    finally:
        shutil.rmtree(work)
        descriptor = os.open(
            BACKUP_DIRECTORY,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.chown(destination, host_uid, host_gid)
    os.chmod(destination, 0o600)
    with destination.open("rb") as archive:
        os.fsync(archive.fileno())
    descriptor = os.open(
        BACKUP_DIRECTORY,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
