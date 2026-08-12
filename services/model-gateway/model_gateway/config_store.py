from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from typing import Any, Iterator, Mapping
from uuid import uuid4

from dotenv import dotenv_values

from model_gateway.models import GatewayConfig, validate_id
from model_gateway.storage import ensure_write_capacity


class ConfigError(ValueError):
    pass


class ConfigConflict(ConfigError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayPaths:
    home: Path
    config: Path
    secrets: Path
    usage_db: Path
    state: Path
    log: Path
    control_lock: Path
    journal: Path


def default_home() -> Path:
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


def gateway_paths(home: str | Path = "") -> GatewayPaths:
    root = Path(home).expanduser() if str(home).strip() else default_home()
    secrets_override = os.getenv("MODEL_GATEWAY_SECRETS_PATH", "").strip()
    return GatewayPaths(
        home=root,
        config=root / "config.json",
        secrets=(
            Path(secrets_override).expanduser()
            if secrets_override
            else root / "secrets.env"
        ),
        usage_db=root / "usage.db",
        state=root / "service-state.json",
        log=root / "model-gateway.log",
        control_lock=root / ".control-plane.lock",
        journal=root / ".control-plane-journal.json",
    )


def initialize(paths: GatewayPaths) -> dict[str, Any]:
    with control_plane_lock(paths):
        _recover_control_plane_unlocked(paths)
        paths.home.mkdir(parents=True, exist_ok=True)
        _chmod(paths.home, 0o700)
        created: list[str] = []
        if not paths.config.exists():
            _write_config_unlocked(paths.config, GatewayConfig())
            created.append(paths.config.name)
        if not paths.secrets.exists():
            _write_secrets_unlocked(paths.secrets, {})
            created.append(paths.secrets.name)
        load_config(paths.config)
    return {"created": created, "home": str(paths.home)}


def load_config(path: Path) -> GatewayConfig:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"无法安全读取配置文件：{path}: {type(exc).__name__}") from exc
    try:
        config = GatewayConfig.model_validate(payload)
        config._source_revision = sha256(raw).hexdigest()
        return config
    except ValueError as exc:
        raise ConfigError(f"配置校验失败：{exc}") from exc


def write_config(path: Path, config: GatewayConfig | Mapping[str, Any]) -> None:
    _write_config_unlocked(path, config)


def _write_config_unlocked(
    path: Path, config: GatewayConfig | Mapping[str, Any]
) -> None:
    validated = (
        config if isinstance(config, GatewayConfig) else GatewayConfig.model_validate(config)
    )
    # ``None`` is meaningful for models_endpoint (discovery unsupported), so
    # it must survive an edit/write/reload cycle.
    payload = validated.model_dump(mode="json", exclude_none=False)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, content, 0o600)
    if isinstance(validated, GatewayConfig):
        validated._source_revision = sha256(content.encode("utf-8")).hexdigest()


def read_secrets(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        # API keys are opaque bytes-as-text. ``python-dotenv`` interpolation
        # would silently rewrite a legitimate value containing ``${NAME}`` and
        # could pull unrelated process environment data into the snapshot.
        values = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(
            f"无法安全读取密钥文件：{path}: {type(exc).__name__}"
        ) from exc
    return {
        str(key): str(value)
        for key, value in values.items()
        if value is not None and str(key).strip()
    }


def write_secrets(path: Path, values: Mapping[str, str]) -> None:
    _write_secrets_unlocked(path, values)


def _write_secrets_unlocked(path: Path, values: Mapping[str, str]) -> None:
    lines = [
        "# Managed by modelgw. Never commit this file.",
        "",
    ]
    for name in sorted(values):
        try:
            validate_id(name, "secret_ref")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        secret_value = str(values[name])
        if any(character in secret_value for character in "\r\n\x00"):
            raise ConfigError(f"密钥 {name} 不能包含换行或 NUL 字符")
        lines.append(f"{name}={_quote_env(secret_value)}")
    # A deleted credential must not survive in a convenience backup.
    path.with_suffix(path.suffix + ".bak").unlink(missing_ok=True)
    _atomic_write(path, "\n".join(lines) + "\n", 0o600, backup=False)


def set_secret(path: Path, name: str, value: str | None) -> None:
    values = read_secrets(path)
    if value is None:
        values.pop(name, None)
    else:
        values[name] = value
    write_secrets(path, values)


def configuration_revision(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return sha256(b"").hexdigest()


def source_revision(config: GatewayConfig, path: Path | None = None) -> str:
    revision = getattr(config, "_source_revision", "")
    if revision:
        return revision
    if path is not None:
        return configuration_revision(path)
    raise ConfigError("配置对象缺少来源 revision")


@dataclass(frozen=True, slots=True)
class ControlPlaneCommit:
    revision: str
    config: GatewayConfig
    secrets: dict[str, str]


@contextmanager
def control_plane_lock(paths: GatewayPaths) -> Iterator[None]:
    """Serialize every control-plane writer across CLI and web processes."""

    paths.home.mkdir(parents=True, exist_ok=True)
    _chmod(paths.home, 0o700)
    with paths.control_lock.open("a+b") as handle:
        _chmod(paths.control_lock, 0o600)
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def recover_control_plane(paths: GatewayPaths) -> bool:
    with control_plane_lock(paths):
        return _recover_control_plane_unlocked(paths)


def commit_control_plane(
    paths: GatewayPaths,
    *,
    expected_revision: str,
    config: GatewayConfig | Mapping[str, Any] | None = None,
    secret_updates: Mapping[str, str | None] | None = None,
    _crash_after: str = "",
) -> ControlPlaneCommit:
    """Crash-recoverable secret-first/config-last control-plane commit.

    The journal contains only phases and backup paths, never secret values.
    ``_crash_after`` is an in-process fault-injection hook used solely by tests.
    """

    if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise ConfigError("expected_revision 格式无效")
    updates = dict(secret_updates or {})
    with control_plane_lock(paths):
        _recover_control_plane_unlocked(paths)
        current_revision = configuration_revision(paths.config)
        if expected_revision != current_revision:
            raise ConfigConflict("配置已经被其他进程修改")
        current_config = load_config(paths.config)
        candidate_config = (
            current_config
            if config is None
            else (
                config
                if isinstance(config, GatewayConfig)
                else GatewayConfig.model_validate(config)
            )
        )
        current_secrets = read_secrets(paths.secrets)
        candidate_secrets = dict(current_secrets)
        for name, value in updates.items():
            validate_id(name, "secret_ref")
            if value is None:
                candidate_secrets.pop(name, None)
            else:
                candidate_secrets[name] = value
        # Recheck value-domain isolation inside the lock. A concurrent writer
        # must not be able to race two individually valid client/provider
        # updates into an ambiguous credential snapshot.
        from model_gateway.auth import validate_secret_domains

        validate_secret_domains(
            config=candidate_config,
            secrets=candidate_secrets,
        )

        # The transaction needs room for durable before-images, candidate
        # temporary files and its journal.  Refuse before creating any of them
        # when doing so would consume the emergency reserve.
        ensure_write_capacity(
            (paths.config, paths.secrets),
            candidate_config.server,
            expected_write_bytes=_control_plane_expected_write_bytes(
                paths=paths,
                candidate_config=candidate_config,
                candidate_secrets=candidate_secrets,
                config_changed=config is not None,
                secrets_changed=bool(updates),
            ),
        )

        transaction_id = uuid4().hex
        journal: dict[str, Any] | None = None
        try:
            journal = _prepare_journal(
                paths,
                transaction_id=transaction_id,
                config_changed=config is not None,
                secrets_changed=bool(updates),
            )
            _write_journal(paths, journal)
            _maybe_crash(_crash_after, "prepared")
            if updates:
                _write_secrets_unlocked(paths.secrets, candidate_secrets)
                journal["phase"] = "secret_applied"
                _write_journal(paths, journal)
                _maybe_crash(_crash_after, "secret_applied")
            if config is not None:
                _write_config_unlocked(paths.config, candidate_config)
                journal["phase"] = "config_applied"
                _write_journal(paths, journal)
                _maybe_crash(_crash_after, "config_applied")
            # This is the durable commit point.  Both candidate files were
            # fsynced by their atomic writers before this phase is published.
            # Recovery must therefore preserve them and only finish deleting
            # before-images, even if the process dies partway through cleanup.
            journal["phase"] = "committed"
            _write_journal(paths, journal)
            _maybe_crash(_crash_after, "committed")
        except Exception:
            if paths.journal.exists():
                _recover_control_plane_unlocked(paths)
            elif journal is not None:
                _discard_uncommitted_backups(paths, journal)
            raise
        assert journal is not None
        _finish_journal(paths, journal, _crash_after=_crash_after)
        committed = load_config(paths.config)
        return ControlPlaneCommit(
            revision=configuration_revision(paths.config),
            config=committed,
            secrets=read_secrets(paths.secrets),
        )


def _control_plane_expected_write_bytes(
    *,
    paths: GatewayPaths,
    candidate_config: GatewayConfig,
    candidate_secrets: Mapping[str, str],
    config_changed: bool,
    secrets_changed: bool,
) -> int:
    expected = 64 * 1024  # bounded journal, directory metadata and fsync slack
    if config_changed:
        candidate = json.dumps(
            candidate_config.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        expected += len(candidate) + _safe_file_size(paths.config)
    if secrets_changed:
        candidate_size = sum(
            len(name.encode("utf-8")) + len(str(value).encode("utf-8")) + 8
            for name, value in candidate_secrets.items()
        )
        expected += candidate_size + _safe_file_size(paths.secrets) + 256
    return expected


def _safe_file_size(path: Path) -> int:
    try:
        return max(0, int(path.stat().st_size))
    except OSError:
        return 0


class _SimulatedCrash(BaseException):
    pass


def _maybe_crash(selected: str, phase: str) -> None:
    if selected == phase:
        raise _SimulatedCrash(phase)


def _prepare_journal(
    paths: GatewayPaths,
    *,
    transaction_id: str,
    config_changed: bool,
    secrets_changed: bool,
) -> dict[str, Any]:
    journal: dict[str, Any] = {
        "version": 1,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "config_changed": config_changed,
        "secrets_changed": secrets_changed,
        "config_existed": paths.config.exists(),
        "secrets_existed": paths.secrets.exists(),
        "config_backup": "",
        "secrets_backup": "",
    }
    try:
        if config_changed and paths.config.exists():
            backup = paths.config.parent / f".{paths.config.name}.txn-{transaction_id}.before"
            journal["config_backup"] = str(backup)
            _copy_durable(paths.config, backup, 0o600)
        if secrets_changed and paths.secrets.exists():
            backup = paths.secrets.parent / f".{paths.secrets.name}.txn-{transaction_id}.before"
            journal["secrets_backup"] = str(backup)
            _copy_durable(paths.secrets, backup, 0o600)
    except Exception:
        _discard_uncommitted_backups(paths, journal)
        raise
    return journal


def _discard_uncommitted_backups(
    paths: GatewayPaths,
    journal: Mapping[str, Any],
) -> None:
    transaction_id = str(journal.get("transaction_id", ""))
    for name, target in (
        ("config_backup", paths.config),
        ("secrets_backup", paths.secrets),
    ):
        value = str(journal.get(name, ""))
        if not value:
            continue
        backup = Path(value)
        if backup.parent != target.parent or backup.name != (
            f".{target.name}.txn-{transaction_id}.before"
        ):
            continue
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            # No live file has been replaced at this phase.  Preserve the
            # original storage exception; a same-directory before-image is a
            # harmless orphan and contains no value absent from the live file.
            pass


def _write_journal(paths: GatewayPaths, journal: Mapping[str, Any]) -> None:
    _atomic_write(
        paths.journal,
        json.dumps(dict(journal), ensure_ascii=False, sort_keys=True) + "\n",
        0o600,
        backup=False,
    )


def _recover_control_plane_unlocked(
    paths: GatewayPaths,
    *,
    _crash_after: str = "",
) -> bool:
    if not paths.journal.exists():
        return False
    try:
        journal = json.loads(paths.journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("控制面恢复日志损坏，拒绝继续写入") from exc
    if not isinstance(journal, dict) or journal.get("version") != 1:
        raise ConfigError("控制面恢复日志版本无效，拒绝继续写入")
    transaction_id = str(journal.get("transaction_id", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise ConfigError("控制面恢复日志 transaction_id 无效")
    phase = journal.get("phase")
    if phase not in {
        "prepared",
        "secret_applied",
        "config_applied",
        "committed",
    }:
        raise ConfigError("控制面恢复日志 phase 无效")
    if phase == "committed":
        # The new config/secret pair is already durable.  Missing before-images
        # are expected when a crash interrupted cleanup, so never reinterpret a
        # committed transaction as an incomplete rollback.
        _finish_journal(paths, journal)
        return True
    for label, target in (("config", paths.config), ("secrets", paths.secrets)):
        if not bool(journal.get(f"{label}_changed")):
            continue
        existed = bool(journal.get(f"{label}_existed"))
        backup_value = str(journal.get(f"{label}_backup", ""))
        if existed:
            backup = Path(backup_value)
            expected_name = f".{target.name}.txn-{transaction_id}.before"
            if backup.parent != target.parent or backup.name != expected_name:
                raise ConfigError("控制面恢复日志包含无效备份路径")
            if not backup.exists():
                raise ConfigError("控制面恢复所需备份不存在，拒绝继续写入")
            # Keep the only before-image until every rollback target is durable
            # and cleanup begins.  Consuming it with os.replace() makes recovery
            # non-idempotent: a second crash after restoring just one file would
            # leave the journal present but its required backup missing.
            _restore_before_image(backup, target)
        else:
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)
        _maybe_crash(_crash_after, f"{label}_rolled_back")
    _finish_journal(paths, journal)
    return True


def _finish_journal(
    paths: GatewayPaths,
    journal: Mapping[str, Any],
    *,
    _crash_after: str = "",
) -> None:
    transaction_id = str(journal.get("transaction_id", ""))
    for name, target in (
        ("config_backup", paths.config),
        ("secrets_backup", paths.secrets),
    ):
        value = str(journal.get(name, ""))
        if not value:
            continue
        backup = Path(value)
        expected_name = f".{target.name}.txn-{transaction_id}.before"
        if backup.parent != target.parent or backup.name != expected_name:
            raise ConfigError("控制面恢复日志包含无效备份路径")
        backup.unlink(missing_ok=True)
        # ``MODEL_GATEWAY_SECRETS_PATH`` may place the secret store on a
        # separate volume from Gateway Home.  Persist each before-image
        # deletion on its own filesystem before the journal is removed;
        # otherwise a power loss can resurrect a secret-bearing orphan after
        # recovery has already forgotten the transaction.
        _fsync_directory(backup.parent)
        _maybe_crash(_crash_after, f"{name}_deleted")
    paths.journal.unlink(missing_ok=True)
    _fsync_directory(paths.home)


def _copy_durable(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    _chmod(destination, mode)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(destination.parent)


def _restore_before_image(source: Path, target: Path) -> None:
    """Atomically restore a before-image without consuming it.

    The journal remains the authority until cleanup completes.  Preserving the
    before-image lets a later process repeat the rollback after a power loss at
    any target boundary.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.rollback-",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination, source.open("rb") as handle:
            shutil.copyfileobj(handle, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigManager:
    """Hot-reloading last-known-good config and secret snapshot."""

    def __init__(self, paths: GatewayPaths):
        self.paths = paths
        self._lock = threading.Lock()
        self._config: GatewayConfig | None = None
        self._secrets: dict[str, str] = {}
        self._revision: tuple[int, int] = (-1, -1)
        self._last_reload_error = ""

    def snapshot(self) -> tuple[GatewayConfig, dict[str, str]]:
        # Readers join the same cross-process lock so they cannot observe the
        # deliberate secret-first/config-last intermediate state.
        with control_plane_lock(self.paths):
            _recover_control_plane_unlocked(self.paths)
            revision = (_mtime_ns(self.paths.config), _mtime_ns(self.paths.secrets))
            with self._lock:
                if self._config is None or revision != self._revision:
                    try:
                        config = load_config(self.paths.config)
                        secrets = read_secrets(self.paths.secrets)
                    except Exception as exc:
                        self._last_reload_error = f"{type(exc).__name__}: {exc}"
                        self._revision = revision
                        if self._config is not None:
                            return self._config, dict(self._secrets)
                        raise
                    self._config = config
                    self._secrets = secrets
                    self._revision = revision
                    self._last_reload_error = ""
                return self._config, dict(self._secrets)

    async def snapshot_async(self) -> tuple[GatewayConfig, dict[str, str]]:
        """Event-loop-safe snapshot.

        ``snapshot()`` takes a cross-process file lock and may re-read config
        files from disk; both can block for tens of milliseconds under
        contention, which would stall every in-flight request when called on
        the event loop. Async handlers must use this wrapper.
        """
        return await asyncio.to_thread(self.snapshot)

    def force_reload(self) -> tuple[GatewayConfig, dict[str, str]]:
        with self._lock:
            self._config = None
            self._revision = (-1, -1)
        return self.snapshot()

    @property
    def last_reload_error(self) -> str:
        with self._lock:
            return self._last_reload_error


def _atomic_write(path: Path, content: str, mode: int, *, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    backup_path = path.with_suffix(path.suffix + ".bak")
    if backup and path.exists():
        shutil.copyfile(path, backup_path)
        _chmod(backup_path, mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
