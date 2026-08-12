"""One-shot, offline initializer for the split-container Docker stack.

The initializer is the only container allowed to see both services' fresh
volumes.  It never prints generated credentials; the two user-facing values
are delivered as mode-0600 host files instead.
"""

from __future__ import annotations

import os
from pathlib import Path
import hmac
import secrets
import stat
import subprocess
import sys

from app.cli_config import cli_paths, read_env_file, update_env_value
from app.auth.tokens import AuthTokenStore
from model_gateway.config_store import gateway_paths, load_config, read_secrets


MEMORY_UID = 10001
MODEL_UID = 10002
MEMORY_DATA = Path("/memory-data")
MEMORY_SECRETS = Path("/memory-secrets")
MODEL_DATA = Path("/model-data")
MODEL_SECRETS = Path("/model-secrets")
CREDENTIALS = Path("/credentials")
MEMORY_MARKER = MEMORY_DATA / ".stack-installed-v2"
MODEL_MARKER = MODEL_DATA / ".stack-installed-v2"


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
        _secure_credentials()
        _validate_published_credentials()
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

    environment = dict(os.environ)
    environment.update(
        {
            "MEMGW_HOME": str(MEMORY_DATA / "config"),
            "MEMGW_SETTINGS_PATH": str(MEMORY_SECRETS / "settings.env"),
            "MEMGW_PROJECT_ROOT": "/app/services/memory-gateway",
            "MODEL_GATEWAY_HOME": str(MODEL_DATA),
            "MODEL_GATEWAY_SECRETS_PATH": str(MODEL_SECRETS / "secrets.env"),
        }
    )
    # Generated values must not enter container logs even temporarily.  The
    # command writes them straight into the two private stores; output is
    # discarded and errors are reported only by return code.
    result = subprocess.run(
        [
            "memgw",
            "--home",
            str(MEMORY_DATA / "config"),
            "--project-root",
            "/app/services/memory-gateway",
            "stack",
            "install",
            "--model-gateway-home",
            str(MODEL_DATA),
            # This one-shot initializer provisions the final auth.db and
            # host-mounted credential files after rewriting container paths.
            # Avoid minting a second source-layout token in config/auth.db.
            "--defer-credential-delivery",
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        print(
            f"离线初始化失败（exit={result.returncode}）；未输出任何密钥。",
            file=sys.stderr,
        )
        return result.returncode

    paths = cli_paths(MEMORY_DATA / "config")
    runtime_settings = {
        "MODEL_GATEWAY_BASE_URL": "http://model-gateway:2030/v1",
        "MODEL_GATEWAY_ALLOW_PRIVATE_HTTP": "true",
        # These values are consumed by the long-lived Memory container, where
        # its private data volume is mounted at /data (not /memory-data, the
        # initializer's cross-volume mount point).
        "DATABASE_PATH": "/data/memory.db",
        "KNOWLEDGE_DATABASE_PATH": "/data/knowledge.db",
        "AUTH_DATABASE_PATH": "/data/auth.db",
        "EVAL_DIR": "/data/eval",
        "UI_DIST_DIR": "/app/ui/dist",
    }
    for name, value in runtime_settings.items():
        update_env_value(paths.settings_env, name, value)
    current = read_env_file(paths.settings_env)
    if not current.get("GATEWAY_SIGNING_SECRET", "").strip():
        update_env_value(
            paths.settings_env,
            "GATEWAY_SIGNING_SECRET",
            secrets.token_urlsafe(48),
        )

    _provision_first_console_token(
        settings_path=paths.settings_env,
        auth_database_path=MEMORY_DATA / "auth.db",
        credential_path=CREDENTIALS / "gateway.key",
    )
    model_paths = gateway_paths(MODEL_DATA)
    config = load_config(model_paths.config)
    admin_client = config.clients.get("memory-console-admin")
    model_secrets = read_secrets(model_paths.secrets)
    admin_key = (
        model_secrets.get(admin_client.secret_ref, "").strip()
        if admin_client is not None
        else ""
    )
    if not admin_key:
        print("初始化未生成完整凭据；拒绝发布半成品标记。", file=sys.stderr)
        return 3

    _deliver_once(CREDENTIALS / "admin.key", admin_key)
    # settings.env.bak can contain live backend/gateway material and is not a
    # supported recovery mechanism; the portable backup intentionally excludes
    # secrets.  Remove this convenience copy before publishing the volumes.
    paths.settings_env.with_suffix(paths.settings_env.suffix + ".bak").unlink(
        missing_ok=True
    )

    transaction_id = secrets.token_hex(16)
    _write_marker(MEMORY_MARKER, transaction_id)
    _write_marker(MODEL_MARKER, transaction_id)
    _secure_tree(MEMORY_DATA, MEMORY_UID)
    _secure_tree(MEMORY_SECRETS, MEMORY_UID)
    _secure_tree(MODEL_DATA, MODEL_UID)
    _secure_tree(MODEL_SECRETS, MODEL_UID)
    _secure_credentials()
    print("离线初始化完成；访问凭据已写入宿主机私有文件。")
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


def _provision_first_console_token(
    *,
    settings_path: Path,
    auth_database_path: Path,
    credential_path: Path,
) -> None:
    """Provision the sole fresh-install console credential.

    Legacy volumes use ``migrate_legacy.py`` and intentionally retain their
    one-version all-scope key. This initializer handles only a fresh volume.
    """

    store = AuthTokenStore(auth_database_path)
    store.init_db()
    active = [record for record in store.list_tokens() if record.revoked_at is None]
    try:
        descriptor = _open_regular_no_follow(credential_path, os.O_RDONLY)
    except FileNotFoundError:
        descriptor = None
    if descriptor is not None:
        try:
            token = _read_credential_descriptor(descriptor)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        record = store.authenticate(token)
        if (
            record is None
            or record.name != "first-console"
            or record.user_id != "default"
            or record.role != "console"
            or len(active) != 1
            or active[0].token_id != record.token_id
        ):
            raise RuntimeError("initial console credential does not match auth database")
    else:
        if active:
            raise RuntimeError("fresh auth database already contains active tokens")
        created = store.create_token(
            name="first-console",
            user_id="default",
            role="console",
        )
        _deliver_once(credential_path, created.token)

    update_env_value(settings_path, "GATEWAY_LEGACY_API_KEY_ENABLED", "false")
    update_env_value(settings_path, "GATEWAY_API_KEY", None)


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


def _secure_credentials() -> None:
    uid = _bounded_id(os.getenv("HOST_UID", ""))
    gid = _bounded_id(os.getenv("HOST_GID", ""))
    directory = CREDENTIALS.lstat()
    if not stat.S_ISDIR(directory.st_mode):
        raise RuntimeError("credential directory is unavailable")
    CREDENTIALS.chmod(0o700)
    if uid is not None and gid is not None:
        os.chown(CREDENTIALS, uid, gid)
    for path in (CREDENTIALS / "gateway.key", CREDENTIALS / "admin.key"):
        try:
            descriptor = _open_regular_no_follow(path, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise RuntimeError(_missing_credential_message(path)) from exc
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size <= 0:
                raise RuntimeError(_missing_credential_message(path))
            os.fchmod(descriptor, 0o600)
            if uid is not None and gid is not None:
                os.fchown(descriptor, uid, gid)
        finally:
            os.close(descriptor)


def _missing_credential_message(path: Path) -> str:
    return (
        f"数据卷已初始化，但宿主 credentials/{path.name} 缺失。"
        "数据没有丢：把原安装目录中的 credentials/gateway.key 和 "
        "credentials/admin.key 放回安装目录的 credentials/ 后重跑同一条安装命令即可。"
        "两枚密钥都遗失时参见 docs/stack-operations.md 的密钥重设章节。"
    )


def _validate_published_credentials() -> None:
    uid = _bounded_id(os.getenv("HOST_UID", ""))
    gid = _bounded_id(os.getenv("HOST_GID", ""))
    directory = CREDENTIALS.lstat()
    if not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700:
        raise RuntimeError("published credential directory permissions are invalid")
    if uid is not None and gid is not None and (
        directory.st_uid != uid or directory.st_gid != gid
    ):
        raise RuntimeError("published credential directory ownership is invalid")
    for path in (CREDENTIALS / "gateway.key", CREDENTIALS / "admin.key"):
        try:
            descriptor = _open_regular_no_follow(path, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise RuntimeError(_missing_credential_message(path)) from exc
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


def _bounded_id(value: str) -> int | None:
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= 2_147_483_647 else None


def _installation_complete() -> bool:
    required = (
        MEMORY_SECRETS / "settings.env",
        MODEL_DATA / "config.json",
        MODEL_SECRETS / "secrets.env",
        CREDENTIALS / "gateway.key",
        CREDENTIALS / "admin.key",
    )
    return all(_is_nonempty_regular_no_follow(path) for path in required)


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
