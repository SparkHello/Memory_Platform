"""Offline, allow-list migration from the legacy all-in-one Docker volume.

The source is mounted read-only at ``/legacy``.  Only documented configuration,
SQLite and evaluation paths are copied into the four split volumes.  The
migrator has no network and never prints credential values.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile

from app.cli_config import read_env_file, write_env_atomic
from app.auth.tokens import AuthTokenStore
from app.usage.pricing import load_pricing_catalog
from model_gateway.config_store import load_config, read_secrets, write_config


MEMORY_UID = 10001
MODEL_UID = 10002
LEGACY = Path("/legacy")
MEMORY_DATA = Path("/memory-data")
MEMORY_SECRETS = Path("/memory-secrets")
MODEL_DATA = Path("/model-data")
MODEL_SECRETS = Path("/model-secrets")
CREDENTIALS = Path("/credentials")
MEMORY_MARKER = MEMORY_DATA / ".stack-installed-v2"
MODEL_MARKER = MODEL_DATA / ".stack-installed-v2"
# Match init_stack: .txt is preferred (macOS Keynote steals .key).
GATEWAY_CREDENTIAL_NAMES = ("gateway.txt", "gateway.key")
ADMIN_CREDENTIAL_NAMES = ("admin.txt", "admin.key")


def main() -> int:
    os.umask(0o077)
    legacy_memory = LEGACY / "memory-gateway"
    legacy_model = LEGACY / "model-gateway"
    _require_private_source(legacy_memory)
    _require_private_source(legacy_model)
    if MEMORY_MARKER.exists() or MODEL_MARKER.exists():
        raise RuntimeError("split destination is already initialized")
    for destination in (MEMORY_DATA, MEMORY_SECRETS, MODEL_DATA, MODEL_SECRETS):
        _require_empty_destination(destination)
    CREDENTIALS.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.chmod(0o700)

    source_settings = legacy_memory / "settings.env"
    source_model_config = legacy_model / "config.json"
    source_model_secrets = legacy_model / "secrets.env"
    for required in (source_settings, source_model_config, source_model_secrets):
        _require_regular(required)

    settings = read_env_file(source_settings)
    source_memory_db = _legacy_runtime_path(
        settings.get("DATABASE_PATH", "/data/memory-gateway/data/memory.db")
    )
    source_knowledge_db = _legacy_runtime_path(
        settings.get(
            "KNOWLEDGE_DATABASE_PATH",
            "/data/memory-gateway/data/knowledge.db",
        )
    )
    _copy_sqlite(source_memory_db, MEMORY_DATA / "memory.db")
    _copy_sqlite(source_knowledge_db, MEMORY_DATA / "knowledge.db")
    source_auth = _optional_legacy_runtime_path(settings.get("AUTH_DATABASE_PATH", ""))
    if source_auth is not None and source_auth.is_file():
        _copy_sqlite(source_auth, MEMORY_DATA / "auth.db")
    else:
        AuthTokenStore(MEMORY_DATA / "auth.db").init_db()

    memory_config = MEMORY_DATA / "config"
    memory_config.mkdir(parents=True, mode=0o700, exist_ok=False)
    # project.json is required; local models/routes catalogs are legacy backup
    # only (routing lives in Model Gateway). pricing.json still backs local
    # usage ledger display for known historical model IDs.
    _copy_file(legacy_memory / "project.json", memory_config / "project.json", required=True)
    for filename in ("models.json", "routes.json", "pricing.json"):
        _copy_file(legacy_memory / filename, memory_config / filename, required=False)
    pricing_path = memory_config / "pricing.json"
    if pricing_path.is_file():
        load_pricing_catalog(pricing_path)

    legacy_eval = legacy_memory / "eval"
    if legacy_eval.exists():
        _copy_tree(legacy_eval, MEMORY_DATA / "eval")

    migrated_settings = dict(settings)
    for name in list(migrated_settings):
        if (
            name.endswith("_API_KEY")
            and name not in {"GATEWAY_API_KEY", "MODEL_GATEWAY_API_KEY"}
        ) or name == "MEMORY_CONSOLE_ADMIN_KEY":
            migrated_settings.pop(name, None)
    # Drop retired direct-provider / local catalog route keys.  *_API_KEY
    # variants are already removed by the suffix sweep above.
    for retired in (
        "PROVIDERS_PATH",
        "ROUTES_PATH",
        "MODEL_CATALOG_PATH",
        "MODEL_ROUTES_PATH",
        "PRICING_CATALOG_PATH",
        "UPSTREAM_BASE_URL",
        "UPSTREAM_MODEL",
        "LLM_PROVIDER_PRIORITY",
        "LLM_RATE_LIMIT_COOLDOWN_SECONDS",
        "LLM_MIMO_BASE_URL",
        "LLM_MIMO_MODEL",
        "LLM_KIMI_BASE_URL",
        "LLM_KIMI_MODEL",
        "LLM_DEEPSEEK_BASE_URL",
        "LLM_DEEPSEEK_FLASH_MODEL",
        "LLM_DEEPSEEK_PRO_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "KNOWLEDGE_AGENT_PROVIDER_PRIORITY",
        "KNOWLEDGE_AGENT_BASE_URL",
        "KNOWLEDGE_AGENT_FLASH_MODEL",
        "KNOWLEDGE_AGENT_PRO_MODEL",
        "KNOWLEDGE_AGENT_MIMO_BASE_URL",
        "KNOWLEDGE_AGENT_MIMO_MODEL",
        "KNOWLEDGE_AGENT_KIMI_BASE_URL",
        "KNOWLEDGE_AGENT_KIMI_MODEL",
        "KNOWLEDGE_AGENT_RATE_LIMIT_COOLDOWN_SECONDS",
    ):
        migrated_settings.pop(retired, None)
    migrated_settings.update(
        {
            "DATABASE_PATH": "/data/memory.db",
            "KNOWLEDGE_DATABASE_PATH": "/data/knowledge.db",
            "AUTH_DATABASE_PATH": "/data/auth.db",
            "EVAL_DIR": "/data/eval",
            "UI_DIST_DIR": "/app/ui/dist",
            "MODEL_GATEWAY_BASE_URL": "http://model-gateway:2030/v1",
            "MODEL_GATEWAY_ALLOW_PRIVATE_HTTP": "true",
        }
    )
    if not migrated_settings.get("GATEWAY_SIGNING_SECRET", "").strip():
        migrated_settings["GATEWAY_SIGNING_SECRET"] = secrets.token_urlsafe(48)
    migrated_settings.setdefault("GATEWAY_LEGACY_API_KEY_ENABLED", "true")
    migrated_settings.pop("LEGACY_GATEWAY_KEY_ENABLED", None)
    # The legacy token is a short migration bridge, not a multi-user bearer.
    # Namespace selection is now fixed by each scoped token.
    migrated_settings["GATEWAY_ALLOW_USER_ID_HEADER"] = "false"
    settings_destination = MEMORY_SECRETS / "settings.env"
    write_env_atomic(settings_destination, migrated_settings)
    settings_destination.with_suffix(".env.bak").unlink(missing_ok=True)

    _copy_file(source_model_config, MODEL_DATA / "config.json", required=True)
    _copy_file(source_model_secrets, MODEL_SECRETS / "secrets.env", required=True)
    source_usage = legacy_model / "usage.db"
    if source_usage.is_file():
        _copy_sqlite(source_usage, MODEL_DATA / "usage.db")

    config = load_config(MODEL_DATA / "config.json")
    exact_backend_routes = list(
        dict.fromkeys(
            migrated_settings.get(name, default).strip() or default
            for name, default in (
                ("MODEL_GATEWAY_CHAT_MODEL", "memory.chat"),
                ("MODEL_GATEWAY_MEMORY_EXTRACT_MODEL", "memory.extract"),
                ("MODEL_GATEWAY_MEMORY_COMPACT_MODEL", "memory.compact"),
                ("MODEL_GATEWAY_MEMORY_CORE_MODEL", "memory.core"),
                ("MODEL_GATEWAY_MEMORY_REVIEW_MODEL", "memory.review"),
                ("MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL", "knowledge.fast"),
                ("MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL", "knowledge.pro"),
                ("MODEL_GATEWAY_EMBEDDING_MODEL", "memory.embedding"),
            )
        )
    )
    config_payload = config.model_dump(mode="python", exclude_none=False)
    backend_payload = config_payload.get("clients", {}).get("memory-gateway")
    if not isinstance(backend_payload, dict):
        raise RuntimeError("legacy model config lacks required backend client")
    backend_payload["allowed_routes"] = exact_backend_routes
    backend_payload["allow_direct_deployments"] = False
    write_config(MODEL_DATA / "config.json", config_payload)
    config = load_config(MODEL_DATA / "config.json")
    model_secrets = read_secrets(MODEL_SECRETS / "secrets.env")
    backend = config.clients.get("memory-gateway")
    admin = config.clients.get("memory-console-admin")
    if backend is None or admin is None:
        raise RuntimeError("legacy model config lacks required local clients")
    backend_key = model_secrets.get(backend.secret_ref, "").strip()
    memory_backend_key = migrated_settings.get("MODEL_GATEWAY_API_KEY", "").strip()
    gateway_key = migrated_settings.get("GATEWAY_API_KEY", "").strip()
    admin_key = model_secrets.get(admin.secret_ref, "").strip()
    if not all((backend_key, memory_backend_key, gateway_key, admin_key)):
        raise RuntimeError("legacy credential set is incomplete")
    if not hmac.compare_digest(backend_key.encode(), memory_backend_key.encode()):
        raise RuntimeError("legacy backend credential wiring is inconsistent")

    _deliver_once(CREDENTIALS / GATEWAY_CREDENTIAL_NAMES[0], gateway_key)
    _deliver_once(CREDENTIALS / ADMIN_CREDENTIAL_NAMES[0], admin_key)
    transaction = secrets.token_hex(16)
    _write_marker(MEMORY_MARKER, transaction)
    _write_marker(MODEL_MARKER, transaction)
    _secure_tree(MEMORY_DATA, MEMORY_UID)
    _secure_tree(MEMORY_SECRETS, MEMORY_UID)
    _secure_tree(MODEL_DATA, MODEL_UID)
    _secure_tree(MODEL_SECRETS, MODEL_UID)
    _secure_credentials()
    _fsync_directory(MEMORY_DATA)
    _fsync_directory(MODEL_DATA)
    print("旧单卷已离线迁移并校验；原卷保持只读且未删除。")
    return 0


def _legacy_runtime_path(value: str) -> Path:
    normalized = PurePosixPath(value.strip())
    if not normalized.is_absolute() or ".." in normalized.parts:
        raise RuntimeError("legacy database path is not a safe absolute path")
    try:
        relative = normalized.relative_to(PurePosixPath("/data"))
    except ValueError as exc:
        raise RuntimeError("legacy database is outside the Docker data volume") from exc
    candidate = LEGACY.joinpath(*relative.parts)
    _require_regular(candidate)
    return candidate


def _optional_legacy_runtime_path(value: str) -> Path | None:
    return _legacy_runtime_path(value) if value.strip() else None


def _require_private_source(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("legacy service directory is missing or unsafe")


def _require_regular(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("required legacy file is missing or unsafe")


def _require_empty_destination(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(path.iterdir()):
        raise RuntimeError("split destination volume is not empty")
    path.chmod(0o700)


def _copy_file(source: Path, destination: Path, *, required: bool) -> None:
    if not source.exists() and not required:
        return
    _require_regular(source)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    snapshot = _sqlite_snapshot_manifest(source)
    snapshot_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.source-",
            dir=destination.parent,
        )
    )
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.destination-",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        staged_source = snapshot_directory / source.name
        for source_file, metadata in snapshot:
            suffix = source_file.name.removeprefix(source.name)
            _copy_sqlite_snapshot_file(
                source_file,
                staged_source.with_name(staged_source.name + suffix),
                metadata,
            )
        _validate_sqlite_snapshot_unchanged(source, snapshot)

        # Open the private copy read-write so SQLite can safely recover a hot
        # rollback journal or rebuild/checkpoint a WAL index.  The legacy
        # volume remains read-only and byte-for-byte untouched.  In contrast,
        # immutable=1 on the legacy main file silently ignores committed WAL.
        with sqlite3.connect(staged_source, timeout=0.0) as reader:
            with sqlite3.connect(temporary) as writer:
                reader.backup(writer)
                result = writer.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError("migrated SQLite quick_check failed")
        _validate_sqlite_snapshot_unchanged(source, snapshot)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
            _fsync_directory(temporary.parent)
        shutil.rmtree(snapshot_directory)


def _sqlite_snapshot_manifest(source: Path) -> list[tuple[Path, os.stat_result]]:
    """Capture the quiescent main DB and recovery sidecars without following links."""

    files: list[tuple[Path, os.stat_result]] = []
    for candidate, required in (
        (source, True),
        (source.with_name(source.name + "-wal"), False),
        (source.with_name(source.name + "-journal"), False),
    ):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if required:
                raise RuntimeError("required legacy SQLite file is missing") from None
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("legacy SQLite file or sidecar is unsafe")
        files.append((candidate, metadata))
    return files


def _copy_sqlite_snapshot_file(
    source: Path,
    destination: Path,
    expected: os.stat_result,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeError("legacy SQLite snapshot cannot be opened safely") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as reader:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not _same_sqlite_file(
                opened, expected
            ):
                raise RuntimeError("legacy SQLite changed before migration snapshot")
            with destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if not _same_sqlite_file(os.fstat(descriptor), expected):
                raise RuntimeError("legacy SQLite changed during migration snapshot")
        destination.chmod(0o600)
    finally:
        os.close(descriptor)


def _validate_sqlite_snapshot_unchanged(
    source: Path,
    snapshot: list[tuple[Path, os.stat_result]],
) -> None:
    expected = {path.name: metadata for path, metadata in snapshot}
    for suffix in ("", "-wal", "-journal"):
        candidate = source.with_name(source.name + suffix)
        metadata = expected.get(candidate.name)
        try:
            current = candidate.lstat()
        except FileNotFoundError:
            if metadata is not None:
                raise RuntimeError("legacy SQLite changed during migration snapshot") from None
            continue
        if metadata is None or not stat.S_ISREG(current.st_mode):
            raise RuntimeError("legacy SQLite changed during migration snapshot")
        if not _same_sqlite_file(current, metadata):
            raise RuntimeError("legacy SQLite changed during migration snapshot")


def _same_sqlite_file(current: os.stat_result, expected: os.stat_result) -> bool:
    return (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) == (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
    )


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("legacy evaluation directory is unsafe")
    for root, directories, files in os.walk(source, followlinks=False):
        current = Path(root)
        if current.is_symlink():
            raise RuntimeError("legacy evaluation tree contains a symlink")
        relative = current.relative_to(source)
        target = destination / relative
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.chmod(0o700)
        for name in [*directories, *files]:
            if (current / name).is_symlink():
                raise RuntimeError("legacy evaluation tree contains a symlink")
        for filename in files:
            _copy_file(current / filename, target / filename, required=True)


def _deliver_once(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = _open_regular_no_follow(path, flags, mode=0o600)
    except FileExistsError:
        descriptor = _open_regular_no_follow(path, os.O_RDONLY)
        try:
            existing = _read_credential_descriptor(descriptor)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        if not hmac.compare_digest(existing.encode(), value.encode()):
            raise RuntimeError("credential destination already contains another value") from None
        return
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o600)
    _fsync_directory_no_follow(path.parent)


def _write_marker(path: Path, transaction: str) -> None:
    if len(transaction) != 32 or any(character not in "0123456789abcdef" for character in transaction):
        raise RuntimeError("invalid migration transaction")
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        with temporary.open("x", encoding="ascii") as handle:
            handle.write(transaction + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _secure_tree(root: Path, owner: int) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        path = Path(current)
        if path.is_symlink():
            raise RuntimeError("destination contains a symlink")
        os.chown(path, owner, owner)
        path.chmod(0o700)
        for name in [*directories, *files]:
            child = path / name
            if child.is_symlink():
                raise RuntimeError("destination contains a symlink")
            os.chown(child, owner, owner)
            child.chmod(0o700 if child.is_dir() else 0o600)


def _is_nonempty_regular_no_follow(path: Path) -> bool:
    try:
        descriptor = _open_regular_no_follow(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        return os.fstat(descriptor).st_size > 0
    finally:
        os.close(descriptor)


def _resolve_credential_path(names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = CREDENTIALS / name
        if _is_nonempty_regular_no_follow(path):
            return path
    return None


def _secure_credentials() -> None:
    uid = _bounded_id(os.getenv("HOST_UID", ""))
    gid = _bounded_id(os.getenv("HOST_GID", ""))
    directory = CREDENTIALS.lstat()
    if not stat.S_ISDIR(directory.st_mode):
        raise RuntimeError("credential directory is unavailable")
    CREDENTIALS.chmod(0o700)
    if uid is not None and gid is not None:
        os.chown(CREDENTIALS, uid, gid)
    for names in (GATEWAY_CREDENTIAL_NAMES, ADMIN_CREDENTIAL_NAMES):
        if _resolve_credential_path(names) is None:
            raise RuntimeError("published credential is missing")
        for name in names:
            path = CREDENTIALS / name
            if not _is_nonempty_regular_no_follow(path):
                continue
            descriptor = _open_regular_no_follow(path, os.O_RDONLY)
            try:
                os.fchmod(descriptor, 0o600)
                if uid is not None and gid is not None:
                    os.fchown(descriptor, uid, gid)
            finally:
                os.close(descriptor)


def _bounded_id(value: str) -> int | None:
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= 2_147_483_647 else None


_MAX_CREDENTIAL_BYTES = 512


def _open_regular_no_follow(path: Path, flags: int, *, mode: int = 0o600) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise RuntimeError("credential no-follow open is unavailable")
    open_flags = flags | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, open_flags, mode)
    except FileExistsError:
        raise
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError("credential path is unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        linked = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_dev != linked.st_dev
            or metadata.st_ino != linked.st_ino
        ):
            raise RuntimeError("credential path is unsafe")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_credential_descriptor(descriptor: int) -> str:
    raw = os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1)
    if (
        not raw
        or len(raw) > _MAX_CREDENTIAL_BYTES
        or not raw.endswith(b"\n")
        or b"\n" in raw[:-1]
        or b"\r" in raw
    ):
        raise RuntimeError("credential file has invalid format")
    try:
        return raw[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("credential file has invalid format") from exc


def _fsync_directory_no_follow(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("credential directory is unsafe")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not reflect paths, parsed environment values or Pydantic input.
        print(f"旧单卷迁移失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
