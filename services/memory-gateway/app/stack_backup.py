"""Portable, secret-free backups for the complete local memory stack."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Callable
import zipfile

from app.cli_config import (
    CliPaths,
    is_secret_name,
    read_env_file,
    write_env_atomic,
)


STACK_BACKUP_VERSION = 1
_MAX_ARCHIVE_FILES = 16
_MAX_TOTAL_BYTES = 100 * 1024 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024
_DEVICE_LOCAL_SETTINGS = {
    "DATABASE_PATH",
    "KNOWLEDGE_DATABASE_PATH",
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
    "memory/settings.json",
    "memory/models.json",
    "memory/routes.json",
    "memory/pricing.json",
    "model-gateway/config.json",
    "model-gateway/usage.db",
}


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
    model_gateway_home: Path,
    force: bool = False,
) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise ValueError(f"备份文件已存在：{destination}；使用 --force 可替换")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="memgw-stack-backup-") as temporary_name:
        temporary = Path(temporary_name)
        staged: dict[str, Path] = {}
        _stage_sqlite(memory_database, temporary / "memory.db", staged, "memory/memory.db")
        _stage_sqlite(
            knowledge_database,
            temporary / "knowledge.db",
            staged,
            "memory/knowledge.db",
        )
        _stage_file(paths.models, staged, "memory/models.json")
        _stage_file(paths.routes, staged, "memory/routes.json")
        _stage_file(paths.pricing, staged, "memory/pricing.json")
        _stage_file(model_gateway_home / "config.json", staged, "model-gateway/config.json")
        _stage_sqlite(
            model_gateway_home / "usage.db",
            temporary / "usage.db",
            staged,
            "model-gateway/usage.db",
        )

        safe_settings = {
            name: value
            for name, value in read_env_file(paths.settings_env).items()
            if not is_secret_name(name) and name not in _DEVICE_LOCAL_SETTINGS
        }
        settings_path = temporary / "settings.json"
        settings_path.write_text(
            json.dumps(safe_settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staged["memory/settings.json"] = settings_path

        files = {
            archive_name: {
                "size": source.stat().st_size,
                "sha256": _file_hash(source),
            }
            for archive_name, source in sorted(staged.items())
        }
        manifest = {
            "format": "memory-stack-portable-backup",
            "version": STACK_BACKUP_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "secrets_included": False,
            "files": files,
            "restore_notes": [
                "API keys are intentionally excluded and must be re-entered on a new device.",
                "Memory and knowledge databases may contain sensitive plaintext.",
            ],
        }
        descriptor, temporary_archive_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary_archive = Path(temporary_archive_name)
        try:
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
            os.chmod(temporary_archive, 0o600)
            os.replace(temporary_archive, destination)
        finally:
            temporary_archive.unlink(missing_ok=True)
    return {
        "archive": str(destination),
        "files": sorted(files),
        "secrets_included": False,
    }


def restore_stack_backup(
    *,
    archive_path: Path,
    paths: CliPaths,
    memory_database: Path,
    knowledge_database: Path,
    model_gateway_home: Path,
) -> dict[str, Any]:
    archive_path = archive_path.expanduser().resolve()
    if not archive_path.is_file():
        raise ValueError(f"找不到备份文件：{archive_path}")

    with tempfile.TemporaryDirectory(prefix="memgw-stack-restore-") as staging_name:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _validated_manifest(archive)
            extracted = _verified_payloads(archive, manifest, Path(staging_name))

        targets: dict[str, tuple[Path, Callable[[Path], None] | None]] = {
            "memory/memory.db": (memory_database, _validate_sqlite),
            "memory/knowledge.db": (knowledge_database, _validate_sqlite),
            "memory/models.json": (paths.models, _validate_json_object),
            "memory/routes.json": (paths.routes, _validate_json_object),
            "memory/pricing.json": (paths.pricing, _validate_json_object),
            "model-gateway/config.json": (
                model_gateway_home / "config.json",
                _validate_json_object,
            ),
            "model-gateway/usage.db": (
                model_gateway_home / "usage.db",
                _validate_sqlite,
            ),
        }
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
        restored: list[str] = []
        for archive_name, (target, validator) in targets.items():
            payload = extracted.get(archive_name)
            if payload is None:
                continue
            _save_rollback(target, rollback_root, archive_name)
            _atomic_restore(target, payload, validator=validator)
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
            _save_rollback(paths.settings_env, rollback_root, "memory/settings.env")
            write_env_atomic(paths.settings_env, current_settings)
            restored.append("memory/settings.json")

    return {
        "archive": str(archive_path),
        "restored": sorted(restored),
        "rollback": str(rollback_root),
        "secrets_restored": False,
    }


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
    source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    _validate_sqlite(destination)
    staged[archive_name] = destination


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
    if manifest.get("version") != STACK_BACKUP_VERSION:
        raise ValueError("备份版本不受当前程序支持")
    if manifest.get("secrets_included") is not False:
        raise ValueError("拒绝恢复包含明文密钥的未知备份")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("备份 manifest 的 files 无效")
    if set(files) != set(names) - {"manifest.json"}:
        raise ValueError("备份 manifest 与实际文件不一致")
    return manifest


def _verified_payloads(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    staging: Path,
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


def _save_rollback(target: Path, root: Path, archive_name: str) -> None:
    if not target.is_file():
        return
    destination = root / archive_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, destination)
    os.chmod(destination, 0o600)


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
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_sqlite(path: Path) -> None:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise ValueError(f"SQLite 快照校验失败：{path.name}")


def _validate_json_object(path: Path) -> None:
    payload = _json_object(path.read_bytes(), path.name)
    if not payload:
        raise ValueError(f"JSON 配置不能为空：{path.name}")


def _json_object(payload: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 文件无效：{name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 文件必须是对象：{name}")
    return value


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
