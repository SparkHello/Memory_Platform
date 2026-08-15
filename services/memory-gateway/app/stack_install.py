from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlparse

from app.auth.tokens import AuthTokenStore
from app.cli_config import (
    CliPaths,
    effective_environment,
    ensure_initialized,
    is_placeholder_value,
    read_env_file,
    read_json,
    update_env_value,
)


StackInstallLayout = Literal["source", "docker"]
_MIN_CUSTOM_KEY_LENGTH = 16
_CUSTOM_KEY_VARIABLES = (
    "GATEWAY_API_KEY",
    "GATEWAY_SIGNING_SECRET",
)


@dataclass(frozen=True, slots=True)
class StackInstallDataPaths:
    memory_database: str
    knowledge_database: str
    auth_database: str
    auth_store: Path
    evaluation_directory: str
    ui_directory: str
    model_gateway_secrets: Path | None = None


@dataclass(frozen=True, slots=True)
class StackCredentialSink:
    gateway_path: Path
    admin_path: Path
    read: Callable[[Path], str]
    deliver: Callable[[Path, str], None]


@dataclass(frozen=True, slots=True)
class StackInstallResult:
    model_gateway_home: Path
    model_gateway_base_url: str
    console_credential_path: Path | None
    console_credential_generated: bool
    admin_credential_path: Path | None
    legacy_migration: bool
    existing_scoped_tokens: bool


class StackInstallCommandError(RuntimeError):
    def __init__(self, returncode: int) -> None:
        super().__init__(f"Model Gateway command failed with exit code {returncode}")
        self.returncode = int(returncode)


def read_private_credential(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"首次凭据文件缺失：{path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"首次凭据必须是普通文件且不能是符号链接：{path}")
    if metadata.st_size <= 0 or metadata.st_size > 16 * 1024:
        raise ValueError(f"首次凭据文件大小无效：{path}")
    if os.name == "posix" and hasattr(os, "geteuid"):
        if metadata.st_uid != os.geteuid():
            raise ValueError(f"首次凭据文件必须由当前用户持有：{path}")
    try:
        os.chmod(path, 0o600)
        value = path.read_text(encoding="ascii").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"首次凭据文件无法安全读取：{path}") from exc
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError(f"首次凭据文件内容无效：{path}")
    return value


def deliver_private_credential(path: Path, value: str) -> None:
    if (
        not value
        or len(value) > 16 * 1024
        or not value.isascii()
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError("拒绝写入格式无效的首次凭据")
    if path.exists() or path.is_symlink():
        current = read_private_credential(path)
        if not hmac.compare_digest(current.encode("ascii"), value.encode("ascii")):
            raise ValueError(f"首次凭据文件已存在且内容不同，拒绝覆盖：{path}")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        current = read_private_credential(path)
        if not hmac.compare_digest(current.encode("ascii"), value.encode("ascii")):
            raise ValueError(
                f"首次凭据文件在写入期间被占用且内容不同，拒绝覆盖：{path}"
            ) from None
        return
    created = True
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            descriptor = -1
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
        created = False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            path.unlink(missing_ok=True)


def _describe_weak_key(name: str, value: str) -> str:
    if any(character.isspace() for character in value):
        return f"{name} 不能包含空格、制表符或换行。"
    if len(value) < _MIN_CUSTOM_KEY_LENGTH:
        return (
            f"{name} 至少需要 {_MIN_CUSTOM_KEY_LENGTH} 个字符，"
            f"当前只有 {len(value)} 个。"
        )
    if len(set(value)) < 8:
        return f"{name} 里不同字符太少，请使用更随机的值。"
    return ""


def _validate_custom_keys(environment: Mapping[str, str]) -> None:
    for name in _CUSTOM_KEY_VARIABLES:
        value = environment.get(name, "").strip()
        if not value or is_placeholder_value(value):
            continue
        problem = _describe_weak_key(name, value)
        if problem:
            raise ValueError(f"{problem} 不设置该变量则自动生成一枚高强度密钥。")


def validate_stack_install_process_environment() -> None:
    forbidden = [
        name
        for name in (
            "GATEWAY_API_KEY",
            "GATEWAY_SIGNING_SECRET",
            "MODEL_GATEWAY_API_KEY",
            "MEMORY_CONSOLE_ADMIN_KEY",
        )
        if os.environ.get(name, "").strip()
    ]
    if forbidden:
        raise ValueError(
            "拒绝从进程环境读取首次访问凭据："
            + ", ".join(forbidden)
            + "。请移除这些环境变量；fresh install 会把随机凭据仅写入 0600 文件。"
        )


def _credential_path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _validate_first_console_credential(
    store: AuthTokenStore,
    credential_sink: StackCredentialSink,
    active_records: list[Any],
) -> bool:
    managed = [record for record in active_records if record.name == "first-console"]
    if not managed:
        return False
    if len(managed) != 1:
        raise ValueError("first-console 凭据状态不唯一，拒绝继续安装")
    token = credential_sink.read(credential_sink.gateway_path)
    authenticated = store.authenticate(token)
    if (
        authenticated is None
        or authenticated.token_id != managed[0].token_id
        or authenticated.user_id != "default"
        or authenticated.role != "console"
    ):
        raise ValueError("gateway credential 与 auth.db 中的 first-console 不匹配")
    return True


def _provision_console_credential(
    *,
    paths: CliPaths,
    auth_database_path: Path,
    credential_sink: StackCredentialSink,
    persisted_settings: dict[str, str],
) -> tuple[Path | None, bool]:
    legacy_value = persisted_settings.get("GATEWAY_API_KEY", "").strip()
    legacy_flag = persisted_settings.get(
        "GATEWAY_LEGACY_API_KEY_ENABLED", ""
    ).strip().lower()
    legacy_explicitly_disabled = legacy_flag in {"0", "false", "no", "off"}
    if (
        legacy_value
        and not is_placeholder_value(legacy_value)
        and not legacy_explicitly_disabled
    ):
        update_env_value(paths.settings_env, "GATEWAY_LEGACY_API_KEY_ENABLED", "true")
        return None, False

    store = AuthTokenStore(auth_database_path)
    store.init_db()
    active = [record for record in store.list_tokens() if record.revoked_at is None]
    if _validate_first_console_credential(store, credential_sink, active):
        update_env_value(paths.settings_env, "GATEWAY_API_KEY", None)
        update_env_value(paths.settings_env, "GATEWAY_LEGACY_API_KEY_ENABLED", "false")
        return credential_sink.gateway_path, False
    if active:
        update_env_value(paths.settings_env, "GATEWAY_API_KEY", None)
        update_env_value(paths.settings_env, "GATEWAY_LEGACY_API_KEY_ENABLED", "false")
        return None, False

    created = store.create_token(
        name="first-console",
        user_id="default",
        role="console",
    )
    try:
        credential_sink.deliver(credential_sink.gateway_path, created.token)
    except Exception:
        store.revoke_token(created.record.token_id)
        raise
    update_env_value(paths.settings_env, "GATEWAY_API_KEY", None)
    update_env_value(paths.settings_env, "GATEWAY_LEGACY_API_KEY_ENABLED", "false")
    return credential_sink.gateway_path, True


def _modelgw_base_command(modelgw: Path, home: Path) -> list[str]:
    return [str(modelgw), "--home", str(home)]


def _run_modelgw(
    modelgw: Path,
    home: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    result = subprocess.run(
        [*_modelgw_base_command(modelgw, home), *arguments],
        input=input_text,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    return int(result.returncode)


def _modelgw_json(
    modelgw: Path,
    home: Path,
    arguments: list[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> list[Any]:
    result = subprocess.run(
        [*_modelgw_base_command(modelgw, home), "--json", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Model Gateway 命令失败").strip())
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Model Gateway 返回了无效 JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("Model Gateway JSON 响应格式无效")
    return payload


def _read_model_gateway_config(home: Path) -> dict[str, Any]:
    config_path = home / "config.json"
    if not config_path.is_file():
        raise ValueError(f"Model Gateway 配置不存在：{config_path}")
    return read_json(config_path)


def _model_gateway_embedding_space(config: dict[str, Any]) -> str:
    routes = config.get("routes")
    deployments = config.get("deployments")
    if not isinstance(routes, dict) or not isinstance(deployments, dict):
        return ""
    route = routes.get("memory.embedding")
    if not isinstance(route, dict):
        return ""
    targets = route.get("targets")
    if not isinstance(targets, list) or not targets:
        return ""
    deployment = deployments.get(str(targets[0]))
    if not isinstance(deployment, dict):
        return ""
    return (
        str(deployment.get("embedding_space") or "")
        if isinstance(deployment, dict)
        else ""
    )


def apply_stack_install(
    *,
    layout: StackInstallLayout,
    paths: CliPaths,
    project_root: Path,
    modelgw: Path,
    model_gateway_home: Path,
    model_gateway_base_url: str,
    data_paths: StackInstallDataPaths,
    credential_sink: StackCredentialSink,
    keep_backend_key: bool,
) -> StackInstallResult:
    """Apply stack wiring without rendering output or starting either service."""

    if layout not in {"source", "docker"}:
        raise ValueError("stack install layout 必须是 source 或 docker")
    normalized_base_url = model_gateway_base_url.strip().rstrip("/")
    parsed_base_url = urlparse(normalized_base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
        raise ValueError("Model Gateway base URL 无效")
    required_data_paths = (
        data_paths.memory_database,
        data_paths.knowledge_database,
        data_paths.auth_database,
        str(data_paths.auth_store),
        data_paths.evaluation_directory,
    )
    if any(not value.strip() for value in required_data_paths):
        raise ValueError("stack install data paths 不能为空")
    if layout == "docker" and data_paths.model_gateway_secrets is None:
        raise ValueError("Docker stack install 必须显式提供 Model Gateway secret path")

    validate_stack_install_process_environment()
    ensure_initialized(paths, project_root)
    environment = effective_environment(paths, project_root)
    _validate_custom_keys(environment)
    persisted_settings = read_env_file(paths.settings_env)
    persisted_legacy = persisted_settings.get("GATEWAY_API_KEY", "").strip()
    legacy_flag = persisted_settings.get(
        "GATEWAY_LEGACY_API_KEY_ENABLED", ""
    ).strip().lower()
    legacy_migration = bool(
        persisted_legacy
        and not is_placeholder_value(persisted_legacy)
        and legacy_flag not in {"0", "false", "no", "off"}
    )
    if layout == "docker" and legacy_migration:
        raise ValueError("Docker fresh initializer 不接受 legacy gateway credential")

    active_records: list[Any] = []
    managed_console = False
    if not legacy_migration:
        access_store = AuthTokenStore(data_paths.auth_store)
        access_store.init_db()
        active_records = [
            record for record in access_store.list_tokens() if record.revoked_at is None
        ]
        managed_console = _validate_first_console_credential(
            access_store,
            credential_sink,
            active_records,
        )
        if layout == "docker" and active_records and not managed_console:
            raise ValueError(
                "Docker fresh initializer 发现非 first-console 的既有 active token"
            )
        if not active_records and _credential_path_present(
            credential_sink.gateway_path
        ):
            raise ValueError("fresh auth database 与既有 gateway credential 状态冲突")
        if managed_console and layout == "source":
            if not _credential_path_present(credential_sink.admin_path):
                raise ValueError("安全 scoped 安装缺少 admin.key；拒绝修改现有接线")
            credential_sink.read(credential_sink.admin_path)

    fresh_access_install = not legacy_migration and not active_records
    if (
        layout == "source"
        and fresh_access_install
        and _credential_path_present(credential_sink.admin_path)
    ):
        raise ValueError("fresh source 安装发现无法验证的既有 admin.key")

    model_environment: dict[str, str] | None = None
    if data_paths.model_gateway_secrets is not None:
        model_environment = dict(os.environ)
        model_environment["MODEL_GATEWAY_HOME"] = str(model_gateway_home)
        model_environment["MODEL_GATEWAY_SECRETS_PATH"] = str(
            data_paths.model_gateway_secrets
        )

    def run_modelgw(
        arguments: list[str],
        *,
        input_text: str | None = None,
    ) -> None:
        returncode = _run_modelgw(
            modelgw,
            model_gateway_home,
            arguments,
            input_text=input_text,
            environment=model_environment,
        )
        if returncode:
            raise StackInstallCommandError(returncode)

    run_modelgw(["init"])
    clients = _modelgw_json(
        modelgw,
        model_gateway_home,
        ["client", "list"],
        environment=model_environment,
    )
    client_by_id = {
        str(item.get("id") or ""): item
        for item in clients
        if isinstance(item, dict) and item.get("id")
    }
    backend = client_by_id.get("memory-gateway")
    backend_routes = (
        set(str(item) for item in backend.get("allowed_routes") or [])
        if isinstance(backend, dict)
        else set()
    )
    required_backend_routes = list(
        dict.fromkeys(
            environment.get(name, default).strip() or default
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
    if (
        not isinstance(backend, dict)
        or backend.get("kind") != "backend"
        or not backend.get("enabled", True)
        or backend_routes != set(required_backend_routes)
        or backend.get("allow_direct_deployments", False)
    ):
        arguments = ["client", "add", "memory-gateway", "--kind", "backend"]
        for route_id in required_backend_routes:
            arguments.extend(["--route", route_id])
        arguments.append("--replace")
        run_modelgw(arguments)

    admin = client_by_id.get("memory-console-admin")
    admin_path_present = _credential_path_present(credential_sink.admin_path)
    admin_needs_secret = (
        not isinstance(admin, dict)
        or not admin.get("secret_configured")
        or fresh_access_install
        or (layout == "docker" and not admin_path_present)
    )
    if (
        not isinstance(admin, dict)
        or admin.get("kind") != "admin"
        or not admin.get("enabled", True)
    ):
        run_modelgw(
            [
                "client",
                "add",
                "memory-console-admin",
                "--kind",
                "admin",
                "--route",
                "*",
                "--replace",
            ]
        )
        admin_needs_secret = True

    environment = effective_environment(paths, project_root)
    backend_key = environment.get("MODEL_GATEWAY_API_KEY", "").strip()
    if not keep_backend_key or not backend_key or is_placeholder_value(backend_key):
        backend_key = secrets.token_urlsafe(48)
    run_modelgw(
        ["secret", "set", "memory-gateway", "--stdin", "--no-check"],
        input_text=backend_key + "\n",
    )

    if admin_needs_secret:
        if admin_path_present:
            admin_key = credential_sink.read(credential_sink.admin_path)
            problem = _describe_weak_key("memory-console-admin", admin_key)
            if problem:
                raise ValueError(problem)
        else:
            admin_key = secrets.token_urlsafe(48)
            credential_sink.deliver(credential_sink.admin_path, admin_key)
            admin_path_present = True
        run_modelgw(
            [
                "secret",
                "set",
                "memory-console-admin",
                "--stdin",
                "--no-check",
            ],
            input_text=admin_key + "\n",
        )
    elif admin_path_present:
        credential_sink.read(credential_sink.admin_path)

    update_env_value(paths.settings_env, "MODEL_GATEWAY_BASE_URL", normalized_base_url)
    update_env_value(paths.settings_env, "MODEL_GATEWAY_API_KEY", backend_key)
    if layout == "docker":
        update_env_value(
            paths.settings_env,
            "MODEL_GATEWAY_ALLOW_PRIVATE_HTTP",
            "true",
        )
    for name, value in (
        ("DATABASE_PATH", data_paths.memory_database),
        ("KNOWLEDGE_DATABASE_PATH", data_paths.knowledge_database),
        ("AUTH_DATABASE_PATH", data_paths.auth_database),
        ("EVAL_DIR", data_paths.evaluation_directory),
        ("UI_DIST_DIR", data_paths.ui_directory),
    ):
        update_env_value(paths.settings_env, name, value or None)
    if legacy_migration:
        update_env_value(
            paths.settings_env,
            "GATEWAY_LEGACY_API_KEY_ENABLED",
            "true",
        )

    config = _read_model_gateway_config(model_gateway_home)
    embedding_space = _model_gateway_embedding_space(config)
    if embedding_space:
        update_env_value(
            paths.settings_env,
            "MODEL_GATEWAY_EMBEDDING_SPACE_ID",
            embedding_space,
        )

    console_path: Path | None = None
    console_generated = False
    if not legacy_migration:
        console_path, console_generated = _provision_console_credential(
            paths=paths,
            auth_database_path=data_paths.auth_store,
            credential_sink=credential_sink,
            persisted_settings=persisted_settings,
        )
    if console_path is not None and not admin_path_present:
        raise ValueError("安全 scoped 安装缺少 admin credential；拒绝报告安装完成")

    return StackInstallResult(
        model_gateway_home=model_gateway_home,
        model_gateway_base_url=normalized_base_url,
        console_credential_path=console_path,
        console_credential_generated=console_generated,
        admin_credential_path=(credential_sink.admin_path if admin_path_present else None),
        legacy_migration=legacy_migration,
        existing_scoped_tokens=bool(
            active_records and console_path is None and not legacy_migration
        ),
    )
