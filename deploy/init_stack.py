"""One-shot, offline initializer for the split-container Docker stack.

The initializer is the only container allowed to see both services' fresh
volumes.  It never prints generated credentials; the two user-facing values
are delivered as mode-0600 host files instead.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import hmac
import secrets
import stat
import sys

from app.stack_install import (
    StackCredentialSink,
    StackInstallCommandError,
    StackInstallDataPaths,
    apply_stack_install,
)
from app.cli_config import cli_paths


MEMORY_UID = 10001
MODEL_UID = 10002
MEMORY_DATA = Path("/memory-data")
MEMORY_SECRETS = Path("/memory-secrets")
MODEL_DATA = Path("/model-data")
MODEL_SECRETS = Path("/model-secrets")
CREDENTIALS = Path("/credentials")
MEMORY_MARKER = MEMORY_DATA / ".stack-installed-v2"
MODEL_MARKER = MODEL_DATA / ".stack-installed-v2"
# Prefer .txt so macOS Finder does not open credentials as Keynote presentations
# (UTI com.apple.iwork.keynote.sffkey). Legacy .key remains accepted for upgrades.
GATEWAY_CREDENTIAL_NAMES = ("gateway.txt", "gateway.key")
ADMIN_CREDENTIAL_NAMES = ("admin.txt", "admin.key")
MODELGW = Path("/opt/venv/bin/modelgw")
PROJECT_ROOT = Path("/app/services/memory-gateway")


def main() -> int:
    memory_ready = MEMORY_MARKER.is_file()
    model_ready = MODEL_MARKER.is_file()
    if memory_ready != model_ready:
        if not _installation_complete():
            print("初始化标记不一致；拒绝猜测或覆盖现有卷。", file=sys.stderr)
            return 2
        transaction_id = (
            MEMORY_MARKER.read_text(encoding="ascii").strip()
            if memory_ready
            else MODEL_MARKER.read_text(encoding="ascii").strip()
        )
        _write_marker(MEMORY_MARKER, transaction_id)
        _write_marker(MODEL_MARKER, transaction_id)
        memory_ready = model_ready = True
    if memory_ready:
        if (
            MEMORY_MARKER.read_text(encoding="ascii").strip()
            != MODEL_MARKER.read_text(encoding="ascii").strip()
        ):
            print("初始化标记事务不一致；拒绝启动。", file=sys.stderr)
            return 2
        _secure_tree(MEMORY_DATA, MEMORY_UID)
        _secure_tree(MEMORY_SECRETS, MEMORY_UID)
        _secure_tree(MODEL_DATA, MODEL_UID)
        _secure_tree(MODEL_SECRETS, MODEL_UID)
        missing = _secure_credentials(require_complete=False)
        missing.extend(_validate_published_credentials(require_complete=False))
        if missing:
            _warn_missing_published_credentials(sorted(set(missing)))
        return 0

    for directory in (
        MEMORY_DATA,
        MEMORY_SECRETS,
        MODEL_DATA,
        MODEL_SECRETS,
        CREDENTIALS,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    paths = replace(
        cli_paths(MEMORY_DATA / "config"),
        settings_env=MEMORY_SECRETS / "settings.env",
    )
    credential_sink = StackCredentialSink(
        gateway_path=_credential_write_path(GATEWAY_CREDENTIAL_NAMES),
        admin_path=_credential_write_path(ADMIN_CREDENTIAL_NAMES),
        read=_read_credential_path,
        deliver=_deliver_once,
    )
    try:
        apply_stack_install(
            layout="docker",
            paths=paths,
            project_root=PROJECT_ROOT,
            modelgw=MODELGW,
            model_gateway_home=MODEL_DATA,
            model_gateway_base_url="http://model-gateway:2030/v1",
            data_paths=StackInstallDataPaths(
                memory_database="/data/memory.db",
                knowledge_database="/data/knowledge.db",
                auth_database="/data/auth.db",
                auth_store=MEMORY_DATA / "auth.db",
                evaluation_directory="/data/eval",
                ui_directory="/app/ui/dist",
                model_gateway_secrets=MODEL_SECRETS / "secrets.env",
            ),
            credential_sink=credential_sink,
            keep_backend_key=False,
        )
    except StackInstallCommandError as exc:
        print(
            f"离线初始化失败（exit={exc.returncode}）；未输出任何密钥。",
            file=sys.stderr,
        )
        return exc.returncode

    # settings.env.bak can contain live backend/gateway material and is not a
    # supported recovery mechanism; the portable backup intentionally excludes
    # secrets.  Remove this convenience copy before publishing the volumes.
    paths.settings_env.with_suffix(paths.settings_env.suffix + ".bak").unlink(
        missing_ok=True
    )

    _secure_tree(MEMORY_DATA, MEMORY_UID)
    _secure_tree(MEMORY_SECRETS, MEMORY_UID)
    _secure_tree(MODEL_DATA, MODEL_UID)
    _secure_tree(MODEL_SECRETS, MODEL_UID)
    _secure_credentials()
    _validate_published_credentials()
    transaction_id = secrets.token_hex(16)
    _write_marker(MEMORY_MARKER, transaction_id)
    _write_marker(MODEL_MARKER, transaction_id)
    print(
        "离线初始化完成；访问凭据已写入 Compose 工作目录下的 "
        "credentials/gateway.txt 与 credentials/admin.txt。"
    )
    return 0


def _deliver_once(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = _open_regular_no_follow(path, flags, mode=0o600)
    except FileExistsError:
        # A crash may happen after one credential was delivered but before the
        # cross-volume completion markers were written. Accept only an exact
        # byte-for-byte match; never overwrite an unrelated existing file.
        descriptor = _open_regular_no_follow(path, os.O_RDONLY)
        try:
            existing = _read_credential_descriptor(descriptor)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        if not hmac.compare_digest(existing.encode(), value.encode()):
            raise RuntimeError("credential destination is not empty") from None
        return
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o600)
    _fsync_directory_no_follow(path.parent)


def _read_credential_path(path: Path) -> str:
    descriptor = _open_regular_no_follow(path, os.O_RDONLY)
    try:
        value = _read_credential_descriptor(descriptor)
        os.fchmod(descriptor, 0o600)
        return value
    finally:
        os.close(descriptor)


def _secure_tree(root: Path, owner: int) -> None:
    root.chmod(0o700)
    os.chown(root, owner, owner)
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        if current.is_symlink():
            raise RuntimeError("refusing symlink in service volume")
        os.chown(current, owner, owner)
        current.chmod(0o700)
        for name in [*names, *files]:
            path = current / name
            if path.is_symlink():
                raise RuntimeError("refusing symlink in service volume")
            os.chown(path, owner, owner)
            path.chmod(0o700 if path.is_dir() else 0o600)


def _credential_write_path(names: tuple[str, ...]) -> Path:
    """Preferred new path (.txt first). Does not require the file to exist."""
    return CREDENTIALS / names[0]


def _resolve_credential_path(names: tuple[str, ...]) -> Path | None:
    """First non-empty regular file among preferred and legacy names."""
    for name in names:
        path = CREDENTIALS / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        descriptor = _open_regular_no_follow(path, os.O_RDONLY)
        try:
            if os.fstat(descriptor).st_size <= 0:
                raise RuntimeError("credential file has invalid format")
        finally:
            os.close(descriptor)
        return path
    return None


def _secure_credentials(*, require_complete: bool = True) -> list[str]:
    uid = _bounded_id(os.getenv("HOST_UID", ""))
    gid = _bounded_id(os.getenv("HOST_GID", ""))
    directory = CREDENTIALS.lstat()
    if not stat.S_ISDIR(directory.st_mode):
        raise RuntimeError("credential directory is unavailable")
    CREDENTIALS.chmod(0o700)
    if uid is not None and gid is not None:
        os.chown(CREDENTIALS, uid, gid)
    missing: list[str] = []
    for names, label in (
        (GATEWAY_CREDENTIAL_NAMES, "gateway"),
        (ADMIN_CREDENTIAL_NAMES, "admin"),
    ):
        path = _resolve_credential_path(names)
        if path is None:
            if require_complete:
                raise RuntimeError(_missing_credential_message(label))
            missing.append(label)
            continue
        # Harden every present alias (.txt and legacy .key) so neither stays world-readable.
        for name in names:
            candidate = CREDENTIALS / name
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            descriptor = _open_regular_no_follow(candidate, os.O_RDONLY)
            try:
                if os.fstat(descriptor).st_size <= 0:
                    raise RuntimeError("credential file has invalid format")
                os.fchmod(descriptor, 0o600)
                if uid is not None and gid is not None:
                    os.fchown(descriptor, uid, gid)
            finally:
                os.close(descriptor)
        _ = path  # ensure resolve succeeded
    return missing


def _missing_credential_message(label: str) -> str:
    return (
        f"数据卷已初始化，但宿主 credentials/ 缺少 {label} 凭据文件"
        f"（优先 {label}.txt，兼容旧版 {label}.key）。"
        "数据没有丢：把原安装目录中的 credentials/gateway.txt（或 gateway.key）和 "
        "credentials/admin.txt（或 admin.key）放回安装目录的 credentials/ 后重跑同一条安装命令即可。"
        "两枚密钥都遗失时参见 docs/stack-operations.md 的密钥重设章节。"
    )


def _validate_published_credentials(*, require_complete: bool = True) -> list[str]:
    uid = _bounded_id(os.getenv("HOST_UID", ""))
    gid = _bounded_id(os.getenv("HOST_GID", ""))
    directory = CREDENTIALS.lstat()
    if not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700:
        raise RuntimeError("published credential directory permissions are invalid")
    if uid is not None and gid is not None and (
        directory.st_uid != uid or directory.st_gid != gid
    ):
        raise RuntimeError("published credential directory ownership is invalid")
    missing: list[str] = []
    for names, label in (
        (GATEWAY_CREDENTIAL_NAMES, "gateway"),
        (ADMIN_CREDENTIAL_NAMES, "admin"),
    ):
        path = _resolve_credential_path(names)
        if path is None:
            if require_complete:
                raise RuntimeError(_missing_credential_message(label))
            missing.append(label)
            continue
        descriptor = _open_regular_no_follow(path, os.O_RDONLY)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size <= 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise RuntimeError("published credential permissions are invalid")
            if uid is not None and gid is not None and (
                metadata.st_uid != uid or metadata.st_gid != gid
            ):
                raise RuntimeError("published credential ownership is invalid")
        finally:
            os.close(descriptor)
    return missing


def _warn_missing_published_credentials(missing: list[str]) -> None:
    warning = {
        "level": "warning",
        "code": "host_credential_delivery_missing",
        "missing": [f"{label}.txt" for label in missing],
        "message": "初始化 marker 已完成；内部凭据保持有效，服务将继续启动。",
        "reset_hint": (
            "参见 docs/stack-operations.md 的“安装目录丢失但数据卷仍在”："
            "Console 用 stack-maintenance token create --role console 重建，"
            "admin 用 modelgw secret set memory-console-admin --stdin 重设。"
        ),
    }
    print(
        json.dumps(warning, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
    )


def _bounded_id(value: str) -> int | None:
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= 2_147_483_647 else None


def _installation_complete() -> bool:
    required_paths = (
        MEMORY_SECRETS / "settings.env",
        MODEL_DATA / "config.json",
        MODEL_SECRETS / "secrets.env",
    )
    return all(_is_nonempty_regular_no_follow(path) for path in required_paths)


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


def _is_nonempty_regular_no_follow(path: Path) -> bool:
    try:
        descriptor = _open_regular_no_follow(path, os.O_RDONLY)
    except (FileNotFoundError, RuntimeError):
        return False
    try:
        return os.fstat(descriptor).st_size > 0
    finally:
        os.close(descriptor)


def _fsync_directory_no_follow(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(path: Path, transaction_id: str) -> None:
    if not transaction_id or any(character not in "0123456789abcdef" for character in transaction_id):
        raise RuntimeError("invalid initialization transaction")
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        with temporary.open("w", encoding="ascii") as handle:
            handle.write(transaction_id)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Exception messages may embed an operator-supplied path or malformed
        # candidate. Keep daemon logs useful without reflecting secret inputs.
        print(f"初始化失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
