from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from typing import Any, Mapping

from dotenv import dotenv_values

from model_gateway.models import GatewayConfig, validate_id


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayPaths:
    home: Path
    config: Path
    secrets: Path
    usage_db: Path
    state: Path
    log: Path


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
    return GatewayPaths(
        home=root,
        config=root / "config.json",
        secrets=root / "secrets.env",
        usage_db=root / "usage.db",
        state=root / "service-state.json",
        log=root / "model-gateway.log",
    )


def initialize(paths: GatewayPaths) -> dict[str, Any]:
    paths.home.mkdir(parents=True, exist_ok=True)
    _chmod(paths.home, 0o700)
    created: list[str] = []
    if not paths.config.exists():
        write_config(paths.config, GatewayConfig())
        created.append(paths.config.name)
    if not paths.secrets.exists():
        write_secrets(paths.secrets, {})
        created.append(paths.secrets.name)
    load_config(paths.config)
    return {"created": created, "home": str(paths.home)}


def load_config(path: Path) -> GatewayConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"无法安全读取配置文件：{path}: {type(exc).__name__}") from exc
    try:
        return GatewayConfig.model_validate(payload)
    except ValueError as exc:
        raise ConfigError(f"配置校验失败：{exc}") from exc


def write_config(path: Path, config: GatewayConfig | Mapping[str, Any]) -> None:
    validated = (
        config if isinstance(config, GatewayConfig) else GatewayConfig.model_validate(config)
    )
    # ``None`` is meaningful for models_endpoint (discovery unsupported), so
    # it must survive an edit/write/reload cycle.
    payload = validated.model_dump(mode="json", exclude_none=False)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", 0o600)


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
