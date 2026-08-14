from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
from typing import Any, Mapping

from dotenv import dotenv_values


_PLACEHOLDERS = {
    "change-me",
    "your-api-key",
    "your-upstream-api-key",
    "replace-me",
}
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Direct-provider / local catalog env names retired after Model Gateway convergence.
_RETIRED_DIRECT_ENV_PREFIXES = (
    "UPSTREAM_",
    "LLM_MIMO_",
    "LLM_KIMI_",
    "LLM_DEEPSEEK_",
    "KNOWLEDGE_AGENT_MIMO_",
    "KNOWLEDGE_AGENT_KIMI_",
    "MODEL_CATALOG_",
    "MODEL_ROUTES_",
    "PRICING_CATALOG_",
)
_RETIRED_DIRECT_ENV_NAMES = frozenset(
    {
        "PROVIDERS_PATH",
        "ROUTES_PATH",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL",
        "LLM_PROVIDER_PRIORITY",
        "KNOWLEDGE_AGENT_PROVIDER_PRIORITY",
        "KNOWLEDGE_AGENT_BASE_URL",
        "KNOWLEDGE_AGENT_API_KEY",
        "KNOWLEDGE_AGENT_FLASH_MODEL",
        "KNOWLEDGE_AGENT_PRO_MODEL",
    }
)


@dataclass(frozen=True, slots=True)
class CliPaths:
    home: Path
    credentials: Path
    project_file: Path
    settings_env: Path
    # Legacy catalog paths kept for stack backup/restore of old installs only.
    models: Path
    routes: Path
    pricing: Path
    state: Path
    log: Path


def cli_paths(home: str | Path = "") -> CliPaths:
    root = Path(home).expanduser() if str(home).strip() else default_cli_home()
    settings_override = os.getenv("MEMGW_SETTINGS_PATH", "").strip()
    return CliPaths(
        home=root,
        credentials=root / "credentials",
        project_file=root / "project.json",
        settings_env=(
            Path(settings_override).expanduser()
            if settings_override
            else root / "settings.env"
        ),
        models=root / "models.json",
        routes=root / "routes.json",
        pricing=root / "pricing.json",
        state=root / "service-state.json",
        log=root / "memory-gateway.log",
    )


def default_cli_home() -> Path:
    override = os.getenv("MEMGW_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "memory-gateway"
    if os.name == "nt":
        base = os.getenv("APPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "memory-gateway"
    base = os.getenv("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "memory-gateway"


def discover_project_root(explicit: str | Path = "", *, paths: CliPaths | None = None) -> Path:
    if str(explicit).strip():
        return _validate_project_root(Path(explicit).expanduser())
    env_root = os.getenv("MEMGW_PROJECT_ROOT", "").strip()
    if env_root:
        return _validate_project_root(Path(env_root).expanduser())
    selected_paths = paths or cli_paths()
    if selected_paths.project_file.exists():
        payload = read_json(selected_paths.project_file)
        stored = payload.get("project_root")
        if isinstance(stored, str) and stored.strip():
            return _validate_project_root(Path(stored).expanduser())
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if _looks_like_project(candidate):
            return candidate.resolve()
    package_root = Path(__file__).resolve().parents[1]
    return _validate_project_root(package_root)


def initialize_cli(
    *,
    paths: CliPaths,
    project_root: Path,
    import_project_env: bool = True,
) -> dict[str, object]:
    paths.home.mkdir(parents=True, exist_ok=True)
    _chmod_private_directory(paths.home)
    write_json_atomic(
        paths.project_file,
        {"version": 1, "project_root": str(project_root.resolve()), "port": 2026},
        backup=False,
    )
    # Local model/routes/pricing catalogs are no longer seeded. Routing lives
    # in Model Gateway; leftover files from old installs remain for backup only.
    created: list[str] = []

    values = read_env_file(paths.settings_env)
    imported: list[str] = []
    if import_project_env:
        for name, value in read_env_file(project_root / ".env").items():
            if name in values or not value or _is_placeholder(value):
                continue
            if _is_retired_direct_env(name):
                continue
            values[name] = value
            imported.append(name)
    for name in list(values):
        if _is_retired_direct_env(name):
            values.pop(name, None)
    _migrate_security_settings(paths, values)
    write_env_atomic(paths.settings_env, values)
    return {"created": created, "imported": sorted(imported)}


def ensure_initialized(paths: CliPaths, project_root: Path) -> None:
    if paths.project_file.exists() and paths.settings_env.exists():
        values = read_env_file(paths.settings_env)
        changed = _migrate_security_settings(paths, values)
        for name in list(values):
            if _is_retired_direct_env(name):
                values.pop(name, None)
                changed = True
        if changed:
            write_env_atomic(paths.settings_env, values)
        return
    initialize_cli(paths=paths, project_root=project_root)


def _migrate_security_settings(
    paths: CliPaths,
    values: dict[str, str],
) -> bool:
    changed = False
    if not values.get("AUTH_DATABASE_PATH", "").strip():
        values["AUTH_DATABASE_PATH"] = str(paths.home / "auth.db")
        changed = True
    if "GATEWAY_SIGNING_SECRET" not in values:
        # Stable across restarts, never printed, and independent of every
        # client-facing access token. An explicit blank is preserved so an
        # operator can deliberately disable signed features.
        values["GATEWAY_SIGNING_SECRET"] = secrets.token_urlsafe(48)
        changed = True
    return changed


def _is_retired_direct_env(name: str) -> bool:
    upper = name.upper()
    if upper in _RETIRED_DIRECT_ENV_NAMES:
        return True
    return any(upper.startswith(prefix) for prefix in _RETIRED_DIRECT_ENV_PREFIXES)


def effective_environment(paths: CliPaths, project_root: Path) -> dict[str, str]:
    merged = dict(os.environ)
    project_values = {
        name: value
        for name, value in read_env_file(project_root / ".env").items()
        if not (_is_secret_name(name) and _is_placeholder(value))
        and not _is_retired_direct_env(name)
    }
    merged.update(project_values)
    settings_values = {
        name: value
        for name, value in read_env_file(paths.settings_env).items()
        if not _is_retired_direct_env(name)
    }
    merged.update(settings_values)
    merged["PYTHONUNBUFFERED"] = "1"
    # Drop any retired keys that still linger in the process environment.
    for name in list(merged):
        if _is_retired_direct_env(name):
            merged.pop(name, None)
    return merged


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed = dotenv_values(path)
    return {
        str(name): str(value)
        for name, value in parsed.items()
        if value is not None and _ENV_NAME_RE.fullmatch(str(name))
    }


def update_env_value(path: Path, name: str, value: str | None) -> None:
    normalized = name.strip().upper()
    if not _ENV_NAME_RE.fullmatch(normalized):
        raise ValueError("配置名必须由大写字母、数字和下划线组成")
    values = read_env_file(path)
    if value is None:
        values.pop(normalized, None)
    else:
        values[normalized] = value
    write_env_atomic(path, values)


def write_env_atomic(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Managed by memgw. Secrets are intentionally kept outside the repository.",
        "# Use `memgw config` and `memgw secret`; do not commit this file.",
        "",
    ]
    for name in sorted(values):
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"无效配置名：{name}")
        lines.append(f"{name}={_quote_env_value(str(values[name]))}")
    _write_text_atomic(path, "\n".join(lines) + "\n", backup=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return payload


def write_json_atomic(
    path: Path,
    payload: Mapping[str, Any],
    *,
    backup: bool = True,
) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_text_atomic(path, text, backup=backup)


def masked_environment(values: Mapping[str, str]) -> dict[str, str]:
    return {
        name: ("已配置（已隐藏）" if _is_secret_name(name) and value else value)
        for name, value in values.items()
    }


def is_secret_name(name: str) -> bool:
    return _is_secret_name(name)


def is_placeholder_value(value: str) -> bool:
    return _is_placeholder(value)


def _validate_project_root(path: Path) -> Path:
    resolved = path.resolve()
    if not _looks_like_project(resolved):
        raise ValueError(f"不是 memory-gateway 项目根目录：{resolved}")
    return resolved


def _looks_like_project(path: Path) -> bool:
    return (
        (path / "pyproject.toml").is_file()
        and (path / "app" / "main.py").is_file()
    )


def _write_text_atomic(path: Path, text: str, *, backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        shutil.copyfile(path, backup_path)
        os.chmod(backup_path, 0o600)
        _fsync_file(backup_path)
        _fsync_directory(path.parent)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _fsync_file(path: Path) -> None:
    # Windows rejects ``fsync`` on a descriptor opened read-only (EBADF).
    # These are files managed by this process, so reopen them read/write for
    # the durability barrier there while preserving the POSIX read-only path.
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in _PLACEHOLDERS or lowered.startswith("your-")


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(
        ("_API_KEY", "_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_CREDENTIALS")
    )


def _chmod_private_directory(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
