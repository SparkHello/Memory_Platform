"""Portable, secret-free backups for the complete local memory stack."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Callable
from urllib.parse import urlsplit
import zipfile

from app.cli_config import (
    CliPaths,
    is_secret_name,
    read_env_file,
    write_env_atomic,
)
from app.schema_versions import (
    AUTH_SCHEMA_VERSION,
    KNOWLEDGE_SCHEMA_VERSION,
    MEMORY_SCHEMA_VERSION,
)


STACK_BACKUP_VERSION = 2
_SUPPORTED_STACK_BACKUP_VERSIONS = {1, STACK_BACKUP_VERSION}
_MAX_ARCHIVE_FILES = 16
_MAX_TOTAL_BYTES = 100 * 1024 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024
_DEVICE_LOCAL_SETTINGS = {
    "DATABASE_PATH",
    "KNOWLEDGE_DATABASE_PATH",
    "AUTH_DATABASE_PATH",
    "EVAL_DIR",
    "UI_DIST_DIR",
    "MODEL_CATALOG_PATH",
    "MODEL_ROUTES_PATH",
    "PRICING_CATALOG_PATH",
    "PROVIDERS_PATH",
    "ROUTES_PATH",
}
_PORTABLE_FILES = {
    "memory/memory.db",
    "memory/knowledge.db",
    "memory/auth.db",
    "memory/settings.json",
    "memory/models.json",
    "memory/routes.json",
    "memory/pricing.json",
    "model-gateway/config.json",
    "model-gateway/usage.db",
}
_V2_COMPONENTS = {
    "memory_database": ("memory/memory.db", True),
    "knowledge_database": ("memory/knowledge.db", True),
    "auth_database": ("memory/auth.db", True),
    "memory_settings": ("memory/settings.json", True),
    "memory_models": ("memory/models.json", False),
    "memory_routes": ("memory/routes.json", False),
    "memory_pricing": ("memory/pricing.json", False),
    "model_gateway_config": ("model-gateway/config.json", True),
    "model_gateway_usage": ("model-gateway/usage.db", False),
}
# Latest schema versions come from the shared single source of truth so this
# module cannot silently fall behind a store migration. Backups written by any
# older release (down to version 1 / pre-versioned 0) stay restorable.
_SUPPORTED_MEMORY_SCHEMA_VERSION = MEMORY_SCHEMA_VERSION
_SUPPORTED_KNOWLEDGE_SCHEMA_VERSION = KNOWLEDGE_SCHEMA_VERSION
_SUPPORTED_AUTH_SCHEMA_VERSION = AUTH_SCHEMA_VERSION


def default_model_gateway_home() -> Path:
    override = os.getenv("MODEL_GATEWAY_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "model-gateway"
    if os.name == "nt":
        base = os.getenv("APPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "model-gateway"
    base = os.getenv("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "model-gateway"


def create_stack_backup(
    *,
    destination: Path,
    paths: CliPaths,
    memory_database: Path,
    knowledge_database: Path,
    auth_database: Path,
    model_gateway_home: Path | None = None,
    model_config_override: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create a portable stack zip.

    Prefer a local ``model_gateway_home`` (CLI / co-located install). When the
    Model volume is not mounted (split Docker), callers may pass a temporary
    ``model_config_override`` obtained via the Model admin portable export.
    Provider secrets are never included.
    """
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise ValueError(f"备份文件已存在：{destination}；使用 --force 可替换")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _require_file(memory_database, "Memory 数据库")
    _require_file(knowledge_database, "Knowledge 数据库")
    _require_file(auth_database, "Auth 数据库")
    model_config_path = (
        Path(model_config_override).expanduser()
        if model_config_override is not None
        else (
            Path(model_gateway_home).expanduser() / "config.json"
            if model_gateway_home is not None
            else None
        )
    )
    if model_config_path is None:
        raise ValueError(
            "缺少 Model Gateway 配置：请提供 MODEL_GATEWAY_HOME，"
            "或通过 admin 便携导出传入 model_config_override"
        )
    _require_file(model_config_path, "Model Gateway 配置")
    model_home_for_estimate = (
        Path(model_gateway_home).expanduser()
        if model_gateway_home is not None
        else model_config_path.parent
    )
    estimated_payload_bytes = _estimated_backup_payload_bytes(
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        auth_database=auth_database,
        model_gateway_home=model_home_for_estimate,
    )
    _ensure_backup_space(destination.parent, estimated_payload_bytes)

    targets = _restore_targets(
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        auth_database=auth_database,
        model_gateway_home=model_home_for_estimate,
    )
    files: dict[str, dict[str, Any]] = {}
    descriptor, temporary_archive_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary_archive = Path(temporary_archive_name)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".memgw-stack-backup-stage-",
            dir=destination.parent,
        ) as temporary_name:
            os.chmod(temporary_name, 0o700)
            temporary = Path(temporary_name)
            staged: dict[str, Path] = {}
            _stage_sqlite(memory_database, temporary / "memory.db", staged, "memory/memory.db")
            _stage_sqlite(
                knowledge_database,
                temporary / "knowledge.db",
                staged,
                "memory/knowledge.db",
            )
            _stage_sqlite(auth_database, temporary / "auth.db", staged, "memory/auth.db")
            _stage_file(paths.models, staged, "memory/models.json")
            _stage_file(paths.routes, staged, "memory/routes.json")
            _stage_file(paths.pricing, staged, "memory/pricing.json")
            # Copy override into the staging tree so a temp file can be unlinked
            # after packaging without holding an external path open.
            staged_model_config = temporary / "model-config.json"
            shutil.copy2(model_config_path, staged_model_config)
            os.chmod(staged_model_config, 0o600)
            staged["model-gateway/config.json"] = staged_model_config
            if model_gateway_home is not None:
                _stage_sqlite(
                    Path(model_gateway_home).expanduser() / "usage.db",
                    temporary / "usage.db",
                    staged,
                    "model-gateway/usage.db",
                )

            safe_settings: dict[str, str] = {}
            for name, value in read_env_file(paths.settings_env).items():
                if is_secret_name(name) or name in _DEVICE_LOCAL_SETTINGS:
                    continue
                if not _portable_setting_value_is_secret_safe(value):
                    raise ValueError(
                        f"便携配置 {name} 的 URL 含凭据、query 或 fragment；"
                        "拒绝生成 secrets_included=false 备份"
                    )
                safe_settings[name] = value
            settings_path = temporary / "settings.json"
            settings_path.write_text(
                json.dumps(safe_settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(settings_path, 0o600)
            staged["memory/settings.json"] = settings_path

            # Validate component identity before packaging, then validate the
            # independently reopened archive once more below.
            _validate_restore_payloads(staged, targets)
            files = {
                archive_name: {
                    "size": source.stat().st_size,
                    "sha256": _file_hash(source),
                }
                for archive_name, source in sorted(staged.items())
            }
            components = {
                component: {
                    "archive_path": archive_name,
                    "required": required,
                    "status": "present" if archive_name in staged else "absent",
                }
                for component, (archive_name, required) in _V2_COMPONENTS.items()
            }
            manifest = {
                "format": "memory-stack-portable-backup",
                "version": STACK_BACKUP_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "secrets_included": False,
                "components": components,
                "files": files,
                "restore_notes": [
                    "API keys are intentionally excluded and must be re-entered on a new device.",
                    "Memory and knowledge databases may contain sensitive plaintext.",
                ],
            }
            with zipfile.ZipFile(
                temporary_archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                )
                for archive_name, source in sorted(staged.items()):
                    archive.write(source, archive_name)

        # The first uncompressed staging generation is gone before the
        # verification extraction starts, bounding peak space on the backup
        # filesystem to roughly archive + one uncompressed generation.
        with tempfile.TemporaryDirectory(
            prefix=".memgw-stack-backup-verify-",
            dir=destination.parent,
        ) as verification_name:
            os.chmod(verification_name, 0o700)
            with zipfile.ZipFile(temporary_archive) as archive:
                verified_manifest = _validated_manifest(archive)
                verified_payloads = _verified_payloads(
                    archive,
                    verified_manifest,
                    Path(verification_name),
                )
            _validate_restore_payloads(verified_payloads, targets)
        os.chmod(temporary_archive, 0o600)
        _fsync_file(temporary_archive)
        os.replace(temporary_archive, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary_archive.unlink(missing_ok=True)
    return {
        "archive": str(destination),
        "files": sorted(files),
        "secrets_included": False,
    }


def validate_stack_backup(*, archive_path: Path) -> dict[str, Any]:
    """Dry-run validation of a portable stack zip.

    Opens the archive, verifies the manifest, hashes, SQLite integrity and
    schema ranges, then returns a non-sensitive summary. Never writes to
    production database paths or settings.
    """
    archive_path = archive_path.expanduser().resolve()
    if not archive_path.is_file():
        raise ValueError(f"找不到备份文件：{archive_path}")

    with tempfile.TemporaryDirectory(prefix=".memgw-stack-backup-validate-") as temporary_name:
        os.chmod(temporary_name, 0o700)
        staging = Path(temporary_name)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _validated_manifest(archive)
            extracted = _verified_payloads(archive, manifest, staging)
        # Reuse the same payload validators as restore, with dummy target paths
        # (validators only inspect the staged payload file).
        dummy = Path(temporary_name)
        validation_targets: dict[str, tuple[Path, Callable[[Path], None] | None]] = {
            "memory/memory.db": (dummy, _validate_memory_database),
            "memory/knowledge.db": (dummy, _validate_knowledge_database),
            "memory/auth.db": (dummy, _validate_auth_database),
            "memory/models.json": (dummy, _validate_json_object),
            "memory/routes.json": (dummy, _validate_json_object),
            "memory/pricing.json": (dummy, _validate_json_object),
            "model-gateway/config.json": (dummy, _validate_json_object),
            "model-gateway/usage.db": (dummy, _validate_model_usage_database),
        }
        _validate_restore_payloads(extracted, validation_targets)

        file_rows: list[dict[str, Any]] = []
        files_meta = manifest.get("files")
        if isinstance(files_meta, dict):
            for name, meta in sorted(files_meta.items()):
                size = meta.get("size") if isinstance(meta, dict) else None
                file_rows.append(
                    {
                        "path": name,
                        "size_bytes": size if isinstance(size, int) and not isinstance(size, bool) else None,
                    }
                )

        components_out: dict[str, Any] = {}
        raw_components = manifest.get("components")
        if isinstance(raw_components, dict):
            for key, meta in raw_components.items():
                if isinstance(meta, dict):
                    components_out[key] = {
                        "status": meta.get("status"),
                        "required": meta.get("required"),
                    }
                else:
                    components_out[key] = {"status": "unknown"}

        stats = _stack_backup_content_stats(extracted)

    return {
        "ok": True,
        "restorable": True,
        "format": manifest.get("format"),
        "version": manifest.get("version"),
        "secrets_included": False,
        "components": components_out,
        "files": file_rows,
        "stats": stats,
        "restore_requires_stopped_services": True,
        "message": (
            "备份校验通过。Console 不会在线替换运行中的数据库；"
            "请先停止服务，再用 CLI 或维护容器执行恢复。"
        ),
    }


def _stack_backup_content_stats(extracted: dict[str, Path]) -> dict[str, Any]:
    """Non-sensitive aggregate counts for UI validation feedback."""
    stats: dict[str, Any] = {}
    memory_db = extracted.get("memory/memory.db")
    if memory_db is not None:
        stats.update(_sqlite_scalar_stats(
            memory_db,
            {
                "memory_users": "SELECT COUNT(DISTINCT user_id) FROM memories",
                "active_memories": (
                    "SELECT COUNT(*) FROM memories "
                    "WHERE COALESCE(archived, 0) = 0"
                ),
                "deleted_memories": (
                    "SELECT COUNT(*) FROM memories "
                    "WHERE COALESCE(archived, 0) != 0"
                ),
            },
        ))
    knowledge_db = extracted.get("memory/knowledge.db")
    if knowledge_db is not None:
        stats.update(_sqlite_scalar_stats(
            knowledge_db,
            {
                "knowledge_documents": (
                    "SELECT COUNT(*) FROM knowledge_documents"
                ),
            },
        ))
    auth_db = extracted.get("memory/auth.db")
    if auth_db is not None:
        stats.update(_sqlite_scalar_stats(
            auth_db,
            {
                "auth_token_hashes": "SELECT COUNT(*) FROM auth_tokens",
            },
        ))
    return stats


def _sqlite_scalar_stats(
    path: Path,
    queries: dict[str, str],
) -> dict[str, int | None]:
    result: dict[str, int | None] = {key: None for key in queries}
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
    except sqlite3.Error:
        return result
    try:
        for key, sql in queries.items():
            try:
                row = connection.execute(sql).fetchone()
                result[key] = int(row[0]) if row and row[0] is not None else 0
            except sqlite3.Error:
                result[key] = None
    finally:
        connection.close()
    return result


def restore_stack_backup(
    *,
    archive_path: Path,
    paths: CliPaths,
    memory_database: Path,
    knowledge_database: Path,
    auth_database: Path,
    model_gateway_home: Path,
) -> dict[str, Any]:
    archive_path = archive_path.expanduser().resolve()
    if not archive_path.is_file():
        raise ValueError(f"找不到备份文件：{archive_path}")

    targets = _restore_targets(
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        auth_database=auth_database,
        model_gateway_home=model_gateway_home,
    )
    recover_interrupted_stack_restores(paths=paths, targets=targets)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _validated_manifest(archive)
    _ensure_restore_space(
        manifest,
        targets,
        settings_target=paths.settings_env,
        rollback_parent=paths.home / "restore-backups",
    )

    with ExitStack() as staging_stack:
        staging_roots = _restore_staging_roots(
            staging_stack,
            manifest=manifest,
            targets=targets,
            settings_target=paths.settings_env,
        )
        with zipfile.ZipFile(archive_path) as archive:
            # Re-read the manifest from the same opened archive used for the
            # payload stream. Any replacement between the preflight and this
            # point is therefore detected by the exact hash/size validation.
            active_manifest = _validated_manifest(archive)
            if active_manifest != manifest:
                raise ValueError("备份包在恢复准备期间发生变化")
            extracted = _verified_payloads(
                archive,
                active_manifest,
                staging_roots,
            )

        _validate_restore_payloads(extracted, targets)
        rollback_parent = paths.home / "restore-backups"
        rollback_parent.mkdir(parents=True, exist_ok=True)
        rollback_root = Path(
            tempfile.mkdtemp(
                prefix=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-"),
                dir=rollback_parent,
            )
        )
        os.chmod(rollback_root, 0o700)
        _fsync_directory(rollback_parent)
        secret_rollback_parent = paths.settings_env.parent / ".restore-backups"
        secret_rollback_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(secret_rollback_parent, 0o700)
        _fsync_directory(secret_rollback_parent.parent)
        secret_rollback_root = secret_rollback_parent / rollback_root.name
        secret_rollback_root.mkdir(mode=0o700, exist_ok=False)
        _fsync_directory(secret_rollback_parent)
        journal_path = rollback_root / "restore-journal.json"
        journal: dict[str, Any] = {
            "format": "memory-stack-restore-journal",
            "version": 1,
            "archive_sha256": _file_hash(archive_path),
            "status": "prepared",
            "operations": [],
        }
        _write_journal(journal_path, journal)
        restored: list[str] = []
        modified: list[
            tuple[Path, Path | None, Callable[[Path], None] | None]
        ] = []
        try:
            for archive_name, (target, validator) in targets.items():
                payload = extracted.get(archive_name)
                if payload is None:
                    continue
                # A stopped SQLite service can still leave committed pages in
                # ``-wal``.  Checkpoint before taking the rollback copy so it
                # is self-contained, and remove exact sidecars before the main
                # file is atomically replaced.  Otherwise an old WAL can be
                # replayed against the newly restored database.
                if target.suffix.lower() == ".db":
                    _prepare_sqlite_target_for_replace(target)
                rollback_payload = _save_rollback(target, rollback_root, archive_name)
                # Record the original state before replacement. If a filesystem
                # failure is raised immediately after os.replace(), the target
                # must still participate in the reverse rollback pass.
                modified.append((target, rollback_payload, validator))
                journal["operations"].append(
                    {
                        "archive_path": archive_name,
                        "state": "replacing",
                        "had_original": rollback_payload is not None,
                    }
                )
                _write_journal(journal_path, journal)
                _atomic_restore(target, payload, validator=validator)
                journal["operations"][-1]["state"] = "replaced"
                _write_journal(journal_path, journal)
                restored.append(archive_name)

            settings_payload = extracted.get("memory/settings.json")
            if settings_payload is not None:
                safe_settings = _json_object(
                    settings_payload.read_bytes(), "memory/settings.json"
                )
                current_settings = read_env_file(paths.settings_env)
                for raw_name, raw_value in safe_settings.items():
                    name = str(raw_name).strip().upper()
                    if is_secret_name(name) or name in _DEVICE_LOCAL_SETTINGS:
                        continue
                    if not isinstance(raw_value, str):
                        raise ValueError(f"便携配置 {name} 必须是字符串")
                    current_settings[name] = raw_value
                settings_rollback = _save_rollback(
                    paths.settings_env,
                    secret_rollback_root,
                    "settings.env",
                )
                modified.append((paths.settings_env, settings_rollback, None))
                journal["operations"].append(
                    {
                        "archive_path": "memory/settings.json",
                        "state": "replacing",
                        "had_original": settings_rollback is not None,
                    }
                )
                _write_journal(journal_path, journal)
                write_env_atomic(paths.settings_env, current_settings)
                # write_env_atomic's convenience .bak would duplicate live
                # access/backend secrets outside the journaled rollback path.
                # The explicit 0600 copy above is the sole rollback source.
                paths.settings_env.with_suffix(
                    paths.settings_env.suffix + ".bak"
                ).unlink(missing_ok=True)
                _fsync_directory(paths.settings_env.parent)
                journal["operations"][-1]["state"] = "replaced"
                _write_journal(journal_path, journal)
                restored.append("memory/settings.json")
        except Exception as exc:
            rollback_errors = _rollback_modified_targets(modified)
            paths.settings_env.with_suffix(
                paths.settings_env.suffix + ".bak"
            ).unlink(missing_ok=True)
            _fsync_directory(paths.settings_env.parent)
            journal["status"] = (
                "rollback_incomplete" if rollback_errors else "rolled_back"
            )
            journal["error_type"] = type(exc).__name__
            _write_journal(journal_path, journal)
            rollback_detail = (
                "自动回滚未完整完成：" + ", ".join(rollback_errors)
                if rollback_errors
                else "已自动恢复原文件"
            )
            raise ValueError(
                "整栈恢复失败；"
                f"{rollback_detail}；回滚副本位于 {rollback_root}；"
                f"原始错误类型：{type(exc).__name__}"
            ) from exc
        journal["status"] = "complete"
        _write_journal(journal_path, journal)

    return {
        "archive": str(archive_path),
        "restored": sorted(restored),
        "rollback": str(rollback_root),
        "secret_rollback": str(secret_rollback_root),
        "secrets_restored": False,
    }


def incomplete_restore_journals(home: Path) -> list[Path]:
    """Return pending journals without exposing their contents or data paths."""

    root = Path(home).expanduser() / "restore-backups"
    if not root.is_dir():
        return []
    pending: list[Path] = []
    for journal_path in sorted(root.glob("*/restore-journal.json")):
        try:
            payload = _json_object(journal_path.read_bytes(), journal_path.name)
            status_value = payload.get("status")
        except (OSError, ValueError):
            pending.append(journal_path)
            continue
        if status_value not in {"complete", "rolled_back"}:
            pending.append(journal_path)
    return pending


def assert_no_interrupted_stack_restore(home: Path) -> None:
    if incomplete_restore_journals(home):
        raise RuntimeError(
            "检测到未完成的整栈恢复；服务已拒绝启动。"
            "请在离线维护环境运行 memgw stack recover-restore --yes"
        )


def recover_interrupted_stack_restores(
    *,
    paths: CliPaths,
    targets: dict[str, tuple[Path, Callable[[Path], None] | None]],
) -> dict[str, int]:
    """Idempotently roll back every journal left by a hard interruption."""

    recovered = 0
    for journal_path in incomplete_restore_journals(paths.home):
        try:
            journal = _json_object(journal_path.read_bytes(), journal_path.name)
        except (OSError, ValueError) as exc:
            raise ValueError("恢复 journal 损坏，拒绝自动处理") from exc
        if journal.get("format") != "memory-stack-restore-journal":
            raise ValueError("恢复 journal 格式无效，拒绝自动处理")
        operations = journal.get("operations")
        if not isinstance(operations, list):
            raise ValueError("恢复 journal 操作列表无效")
        rollback_root = journal_path.parent
        secret_root = (
            paths.settings_env.parent / ".restore-backups" / rollback_root.name
        )
        for operation in reversed(operations):
            if not isinstance(operation, dict):
                raise ValueError("恢复 journal 操作无效")
            archive_name = operation.get("archive_path")
            had_original = operation.get("had_original")
            if not isinstance(archive_name, str) or not isinstance(had_original, bool):
                raise ValueError("恢复 journal 缺少原文件状态，拒绝猜测")
            if archive_name == "memory/settings.json":
                target = paths.settings_env
                validator = None
                rollback_payload = secret_root / "settings.env"
            else:
                resolved = targets.get(archive_name)
                if resolved is None:
                    raise ValueError("恢复 journal 包含未知组件")
                target, validator = resolved
                rollback_payload = rollback_root / archive_name
            if had_original:
                if not rollback_payload.is_file():
                    raise ValueError("恢复 journal 的回滚副本缺失")
                _atomic_restore(target, rollback_payload, validator=validator)
            else:
                _remove_restored_target_durably(target)
            operation["state"] = "rolled_back"
            _write_journal(journal_path, journal)
        paths.settings_env.with_suffix(
            paths.settings_env.suffix + ".bak"
        ).unlink(missing_ok=True)
        _fsync_directory(paths.settings_env.parent)
        journal["status"] = "rolled_back"
        journal["recovered_after_interruption"] = True
        _write_journal(journal_path, journal)
        recovered += 1
    return {"recovered_journals": recovered}


def recover_interrupted_stack_restore(
    *,
    paths: CliPaths,
    memory_database: Path,
    knowledge_database: Path,
    auth_database: Path,
    model_gateway_home: Path,
) -> dict[str, int]:
    return recover_interrupted_stack_restores(
        paths=paths,
        targets=_restore_targets(
            paths=paths,
            memory_database=memory_database,
            knowledge_database=knowledge_database,
            auth_database=auth_database,
            model_gateway_home=model_gateway_home,
        ),
    )


def _validate_restore_payloads(
    extracted: dict[str, Path],
    targets: dict[str, tuple[Path, Callable[[Path], None] | None]],
) -> None:
    for archive_name, (_, validator) in targets.items():
        payload = extracted.get(archive_name)
        if payload is None or validator is None:
            continue
        validator(payload)
    settings_payload = extracted.get("memory/settings.json")
    if settings_payload is not None:
        _json_object(settings_payload.read_bytes(), "memory/settings.json")
    # Legacy memory-side model/pricing catalog files may still appear in older
    # backups. They are no longer runtime truth; only require valid JSON if present.
    model_gateway_config = extracted.get("model-gateway/config.json")
    if model_gateway_config is not None:
        _validate_model_gateway_config(model_gateway_config)


def _restore_targets(
    *,
    paths: CliPaths,
    memory_database: Path,
    knowledge_database: Path,
    auth_database: Path,
    model_gateway_home: Path,
) -> dict[str, tuple[Path, Callable[[Path], None] | None]]:
    return {
        "memory/memory.db": (memory_database, _validate_memory_database),
        "memory/knowledge.db": (knowledge_database, _validate_knowledge_database),
        "memory/auth.db": (auth_database, _validate_auth_database),
        "memory/models.json": (paths.models, _validate_json_object),
        "memory/routes.json": (paths.routes, _validate_json_object),
        "memory/pricing.json": (paths.pricing, _validate_json_object),
        "model-gateway/config.json": (
            model_gateway_home / "config.json",
            _validate_json_object,
        ),
        "model-gateway/usage.db": (
            model_gateway_home / "usage.db",
            _validate_model_usage_database,
        ),
    }


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label}缺失，拒绝创建不完整备份：{path}")


def _stage_file(source: Path, staged: dict[str, Path], archive_name: str) -> None:
    if source.is_file():
        staged[archive_name] = source


def _stage_sqlite(
    source: Path,
    destination: Path,
    staged: dict[str, Path],
    archive_name: str,
) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _backup_sqlite_direct(source, destination)
    except sqlite3.OperationalError:
        # A stopped WAL database on a read-only Docker volume may have no
        # writable -shm file. SQLite then cannot open even a mode=ro
        # connection. Recover a byte-stable copy on the writable backup volume
        # instead; never create or modify sidecars beside the live database.
        _remove_sqlite_family(destination)
        _backup_sqlite_from_stable_snapshot(source, destination)
    _validate_sqlite(destination)
    staged[archive_name] = destination


def _backup_sqlite_direct(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(
        f"{source.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _backup_sqlite_from_stable_snapshot(source: Path, destination: Path) -> None:
    snapshot = _sqlite_source_snapshot(source)
    with tempfile.TemporaryDirectory(
        prefix=f".{source.name}.readonly-source-",
        dir=destination.parent,
    ) as temporary_name:
        os.chmod(temporary_name, 0o700)
        staged_source = Path(temporary_name) / source.name
        for source_path, metadata in snapshot.items():
            suffix = source_path.name.removeprefix(source.name)
            _copy_sqlite_snapshot_file(
                source_path,
                staged_source.with_name(staged_source.name + suffix),
                metadata,
            )
        _assert_sqlite_snapshot_unchanged(source, snapshot)
        _backup_sqlite_direct(staged_source, destination)
        _assert_sqlite_snapshot_unchanged(source, snapshot)


def _sqlite_source_snapshot(source: Path) -> dict[Path, os.stat_result]:
    snapshot: dict[Path, os.stat_result] = {}
    for suffix, required in (("", True), ("-wal", False), ("-journal", False)):
        candidate = source.with_name(source.name + suffix)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if required:
                raise ValueError("SQLite source is missing") from None
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("SQLite source or recovery sidecar is unsafe")
        snapshot[candidate] = metadata
    return snapshot


def _copy_sqlite_snapshot_file(
    source: Path,
    destination: Path,
    expected: os.stat_result,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        if not _same_file_state(opened, expected):
            raise ValueError("SQLite source changed before snapshot")
        with os.fdopen(descriptor, "rb", closefd=False) as reader, destination.open(
            "xb"
        ) as writer:
            shutil.copyfileobj(reader, writer, length=_STREAM_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        if not _same_file_state(os.fstat(descriptor), expected):
            raise ValueError("SQLite source changed during snapshot")
        destination.chmod(0o600)
    finally:
        os.close(descriptor)


def _assert_sqlite_snapshot_unchanged(
    source: Path,
    expected: dict[Path, os.stat_result],
) -> None:
    for suffix in ("", "-wal", "-journal"):
        candidate = source.with_name(source.name + suffix)
        original = expected.get(candidate)
        try:
            current = candidate.lstat()
        except FileNotFoundError:
            current = None
        if original is None:
            if current is not None:
                raise ValueError("SQLite recovery sidecar appeared during snapshot")
        elif current is None or not _same_file_state(current, original):
            raise ValueError("SQLite source changed during snapshot")


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _remove_sqlite_family(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _validated_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(names) != len(set(names)):
        raise ValueError("备份包包含重复文件名")
    if len(names) > _MAX_ARCHIVE_FILES:
        raise ValueError("备份包文件数量超过限制")
    unexpected = set(names) - ({"manifest.json"} | _PORTABLE_FILES)
    if unexpected:
        raise ValueError("备份包包含不受支持的文件：" + ", ".join(sorted(unexpected)))
    if sum(item.file_size for item in infos) > _MAX_TOTAL_BYTES:
        raise ValueError("备份包解压后大小超过限制")
    if "manifest.json" not in names:
        raise ValueError("备份包缺少 manifest.json")
    manifest = _json_object(archive.read("manifest.json"), "manifest.json")
    if manifest.get("format") != "memory-stack-portable-backup":
        raise ValueError("不是 Memory Stack 便携备份")
    version = manifest.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in _SUPPORTED_STACK_BACKUP_VERSIONS
    ):
        raise ValueError("备份版本不受当前程序支持")
    if manifest.get("secrets_included") is not False:
        raise ValueError("拒绝恢复包含明文密钥的未知备份")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("备份 manifest 的 files 无效")
    if set(files) != set(names) - {"manifest.json"}:
        raise ValueError("备份 manifest 与实际文件不一致")
    if version == STACK_BACKUP_VERSION:
        _validate_v2_components(manifest, files)
    return manifest


def _validate_v2_components(
    manifest: dict[str, Any], files: dict[str, Any]
) -> None:
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != set(_V2_COMPONENTS):
        raise ValueError("备份 manifest 的 components 无效")
    for component, (archive_name, required) in _V2_COMPONENTS.items():
        metadata = components.get(component)
        if not isinstance(metadata, dict):
            raise ValueError(f"备份组件记录无效：{component}")
        status = metadata.get("status")
        if (
            metadata.get("archive_path") != archive_name
            or metadata.get("required") is not required
            or status not in {"present", "absent"}
            or (status == "present") != (archive_name in files)
            or (required and status != "present")
        ):
            raise ValueError(f"备份组件状态无效：{component}")


def _verified_payloads(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    staging: Path | dict[str, Path],
) -> dict[str, Path]:
    payloads: dict[str, Path] = {}
    files = manifest["files"]
    total_bytes = 0
    for archive_name, metadata in files.items():
        if archive_name not in _PORTABLE_FILES or not isinstance(metadata, dict):
            raise ValueError("备份 manifest 包含无效文件记录")
        expected_size = metadata.get("size")
        expected_hash = metadata.get("sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or not isinstance(expected_hash, str)
        ):
            raise ValueError(f"备份文件校验失败：{archive_name}")
        if isinstance(staging, dict):
            staging_root = staging.get(archive_name)
            if staging_root is None:
                raise ValueError("备份组件缺少安全恢复暂存目标")
            staged = staging_root / archive_name.replace("/", "__")
        else:
            staged = staging / archive_name
        staged.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256()
        actual_size = 0
        with archive.open(archive_name, "r") as source, staged.open("wb") as target:
            while True:
                chunk = source.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                actual_size += len(chunk)
                total_bytes += len(chunk)
                if actual_size > expected_size or total_bytes > _MAX_TOTAL_BYTES:
                    raise ValueError(f"备份文件校验失败：{archive_name}")
                digest.update(chunk)
                target.write(chunk)
        os.chmod(staged, 0o600)
        if actual_size != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError(f"备份文件校验失败：{archive_name}")
        payloads[archive_name] = staged
    return payloads


def _save_rollback(target: Path, root: Path, archive_name: str) -> Path | None:
    if not target.is_file():
        return None
    destination = root / archive_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, destination)
    os.chmod(destination, 0o600)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    if destination.parent != root:
        _fsync_directory(root)
    return destination


def _rollback_modified_targets(
    modified: list[tuple[Path, Path | None, Callable[[Path], None] | None]],
) -> list[str]:
    errors: list[str] = []
    for target, rollback_payload, validator in reversed(modified):
        try:
            if rollback_payload is None:
                _remove_restored_target_durably(target)
            else:
                _atomic_restore(target, rollback_payload, validator=validator)
        except Exception as exc:
            errors.append(f"{target.name}({type(exc).__name__})")
    return errors


def _remove_restored_target_durably(target: Path) -> None:
    """Remove a newly created restore target without leaving replayable state."""

    if target.suffix.lower() == ".db":
        _prepare_sqlite_target_for_replace(target)
    target.unlink(missing_ok=True)
    _fsync_directory(target.parent)


def _atomic_restore(
    target: Path,
    payload: Path,
    *,
    validator: Callable[[Path], None] | None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle, payload.open("rb") as source:
            shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if validator is not None:
            validator(temporary)
        if target.suffix.lower() == ".db":
            _prepare_sqlite_target_for_replace(target)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_sqlite(path: Path) -> None:
    # Validation must never create a ``.<database>.<random>-wal/-shm`` beside
    # an atomic replacement candidate.  The candidate is an immutable,
    # self-contained snapshot at this point.
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise ValueError(f"SQLite 快照校验失败：{path.name}")


def _validate_memory_database(path: Path) -> None:
    _validate_sqlite_component(
        path,
        component="Memory",
        required_columns={
            "memories": {"id", "user_id", "content", "type", "archived"},
            "memory_spaces": {"id", "user_id", "name"},
            "core_memory_sections": {"id", "user_id", "section", "content"},
        },
        minimum_user_version=0,
        maximum_user_version=_SUPPORTED_MEMORY_SCHEMA_VERSION,
    )


def _validate_knowledge_database(path: Path) -> None:
    _validate_sqlite_component(
        path,
        component="Knowledge",
        required_columns={
            "knowledge_documents": {"id", "user_id", "title", "status"},
            "knowledge_versions": {"id", "document_id", "content", "index_status"},
            "knowledge_chunks": {"id", "version_id", "ordinal", "content"},
        },
        minimum_user_version=0,
        maximum_user_version=_SUPPORTED_KNOWLEDGE_SCHEMA_VERSION,
    )


def _validate_auth_database(path: Path) -> None:
    _validate_sqlite_component(
        path,
        component="Auth",
        required_columns={
            "auth_tokens": {
                "token_hash",
                "name",
                "user_id",
                "role",
                "created_at",
                "revoked_at",
            },
        },
        minimum_user_version=1,
        maximum_user_version=_SUPPORTED_AUTH_SCHEMA_VERSION,
    )


def _validate_model_usage_database(path: Path) -> None:
    _validate_sqlite_component(
        path,
        component="Model usage",
        required_columns={
            "usage_events": {
                "id",
                "created_at",
                "client_id",
                "kind",
                "route_id",
                "deployment_id",
                "connection_id",
                "upstream_model",
                "status_code",
            },
        },
    )


def _validate_sqlite_component(
    path: Path,
    *,
    component: str,
    required_columns: dict[str, set[str]],
    minimum_user_version: int | None = None,
    maximum_user_version: int | None = None,
) -> None:
    """Validate integrity plus a component-specific, downgrade-safe identity."""

    _validate_sqlite(path)
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if minimum_user_version is not None and version < minimum_user_version:
            raise ValueError(f"{component} 数据库 schema 版本过旧或未初始化")
        if maximum_user_version is not None and version > maximum_user_version:
            raise ValueError(f"{component} 数据库来自更高版本，拒绝降级恢复")
        for table, expected in required_columns.items():
            rows = connection.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
            actual = {str(row[1]) for row in rows}
            if not expected.issubset(actual):
                raise ValueError(f"{component} 数据库缺少必需 schema")
    finally:
        connection.close()


def _prepare_sqlite_target_for_replace(path: Path) -> None:
    """Make an existing SQLite target self-contained before replacement.

    Restore is documented as an offline operation.  A busy checkpoint means
    that contract is not met, so fail closed instead of deleting a live WAL.
    """

    if path.is_file():
        connection = sqlite3.connect(path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()
        if checkpoint and int(checkpoint[0]) != 0:
            raise ValueError(f"SQLite 仍被占用，拒绝恢复：{path.name}")
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _validate_json_object(path: Path) -> None:
    payload = _json_object(path.read_bytes(), path.name)
    if not payload:
        raise ValueError(f"JSON 配置不能为空：{path.name}")


def _validate_model_gateway_config(path: Path) -> None:
    try:
        from model_gateway.models import GatewayConfig

        GatewayConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except ImportError as exc:
        raise ValueError("当前环境缺少 Model Gateway，无法校验其恢复配置") from exc
    except Exception as exc:
        raise ValueError("Model Gateway 配置未通过完整 schema 校验") from exc


def _json_object(payload: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 文件无效：{name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 文件必须是对象：{name}")
    return value


def _portable_setting_value_is_secret_safe(value: str) -> bool:
    if "://" not in value:
        return True
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _estimated_backup_payload_bytes(
    *,
    paths: CliPaths,
    memory_database: Path,
    knowledge_database: Path,
    auth_database: Path,
    model_gateway_home: Path,
) -> int:
    total = 0
    for source in (
        memory_database,
        knowledge_database,
        auth_database,
        model_gateway_home / "usage.db",
    ):
        if not source.is_file():
            continue
        total += source.stat().st_size
        wal = source.with_name(source.name + "-wal")
        if wal.is_file():
            total += wal.stat().st_size
    for source in (
        paths.models,
        paths.routes,
        paths.pricing,
        paths.settings_env,
        model_gateway_home / "config.json",
    ):
        if source.is_file():
            total += source.stat().st_size
    if total > _MAX_TOTAL_BYTES:
        raise ValueError("备份组件总大小超过便携格式限制")
    return total


def _ensure_backup_space(parent: Path, payload_bytes: int) -> None:
    # Snapshot generation and archive verification are sequential, so the
    # peak is one uncompressed generation plus the completed archive. Deflate
    # can grow incompressible data slightly; retain a 1% margin and metadata
    # reserve rather than assuming compression will save space.
    archive_upper_bound = payload_bytes + max(1024 * 1024, payload_bytes // 100)
    required = payload_bytes + archive_upper_bound + max(
        16 * 1024 * 1024,
        payload_bytes // 10,
    )
    probe = _existing_path(parent)
    if shutil.disk_usage(probe).free < required:
        raise ValueError("备份目标可用磁盘空间不足，拒绝开始备份")


def _archive_target(
    archive_name: str,
    *,
    targets: dict[str, tuple[Path, Callable[[Path], None] | None]],
    settings_target: Path,
) -> Path:
    if archive_name == "memory/settings.json":
        return settings_target
    resolved = targets.get(archive_name)
    if resolved is None:
        raise ValueError("备份 manifest 包含未知恢复组件")
    return resolved[0]


def _restore_staging_roots(
    stack: ExitStack,
    *,
    manifest: dict[str, Any],
    targets: dict[str, tuple[Path, Callable[[Path], None] | None]],
    settings_target: Path,
) -> dict[str, Path]:
    by_parent: dict[str, Path] = {}
    roots: dict[str, Path] = {}
    for archive_name in manifest["files"]:
        target = _archive_target(
            archive_name,
            targets=targets,
            settings_target=settings_target,
        )
        parent = target.expanduser().resolve(strict=False).parent
        parent.mkdir(parents=True, exist_ok=True)
        key = str(parent)
        root = by_parent.get(key)
        if root is None:
            temporary_name = stack.enter_context(
                tempfile.TemporaryDirectory(
                    prefix=".memgw-stack-restore-stage-",
                    dir=parent,
                )
            )
            os.chmod(temporary_name, 0o700)
            root = Path(temporary_name)
            by_parent[key] = root
        roots[archive_name] = root
    return roots


def _ensure_restore_space(
    manifest: dict[str, Any],
    targets: dict[str, tuple[Path, Callable[[Path], None] | None]],
    *,
    settings_target: Path,
    rollback_parent: Path,
) -> None:
    # Stage each component beside its target, while non-secret rollback copies
    # deliberately remain under the Memory home and the settings rollback stays
    # on the secret volume. Account for each real filesystem independently.
    requirements: dict[int, dict[str, Any]] = {}

    def add(path: Path, *, bytes_required: int, atomic_candidate: int = 0) -> None:
        probe = _existing_path(path)
        device = int(probe.stat().st_dev)
        bucket = requirements.setdefault(
            device,
            {"probe": probe, "bytes": 0, "largest_atomic": 0},
        )
        bucket["bytes"] += max(0, int(bytes_required))
        bucket["largest_atomic"] = max(
            int(bucket["largest_atomic"]),
            max(0, int(atomic_candidate)),
        )

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("备份 manifest 的 files 无效")
    for archive_name, metadata in files.items():
        if not isinstance(metadata, dict) or not isinstance(metadata.get("size"), int):
            raise ValueError("备份 manifest 文件大小无效")
        incoming = int(metadata["size"])
        target = _archive_target(
            archive_name,
            targets=targets,
            settings_target=settings_target,
        )
        add(target.parent, bytes_required=incoming, atomic_candidate=incoming)
        if target.is_file():
            rollback_location = (
                settings_target.parent
                if archive_name == "memory/settings.json"
                else rollback_parent
            )
            add(rollback_location, bytes_required=target.stat().st_size)

    for bucket in requirements.values():
        incoming_and_rollback = int(bucket["bytes"])
        required = (
            incoming_and_rollback
            + int(bucket["largest_atomic"])
            + max(16 * 1024 * 1024, incoming_and_rollback // 10)
        )
        if shutil.disk_usage(bucket["probe"]).free < required:
            raise ValueError("可用磁盘空间不足，拒绝开始恢复")


def _existing_path(path: Path) -> Path:
    probe = path.expanduser().resolve(strict=False)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _write_journal(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
