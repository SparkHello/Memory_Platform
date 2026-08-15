from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
import webbrowser

import httpx

from app.auth.tokens import AUTH_ROLES, AuthTokenStore
from app.cli_config import (
    CliPaths,
    cli_paths,
    default_model_gateway_home,
    discover_project_root,
    effective_environment,
    ensure_initialized,
    initialize_cli,
    is_placeholder_value,
    is_secret_name,
    masked_environment,
    read_env_file,
    read_json,
    update_env_value,
    write_json_atomic,
)
from app.config import Settings, describe_settings_error
from app.stack_backup import (
    create_stack_backup,
    recover_interrupted_stack_restore,
    restore_stack_backup,
)
from app.stack_install import (
    StackCredentialSink,
    StackInstallCommandError,
    StackInstallDataPaths,
    apply_stack_install,
    deliver_private_credential as _deliver_private_credential,
    read_private_credential as _read_private_credential,
    validate_stack_install_process_environment,
)


VERSION = "0.5.1"
_SECRET_ALIASES = {
    "gateway": "GATEWAY_API_KEY",
    "signing": "GATEWAY_SIGNING_SECRET",
    "model-gateway": "MODEL_GATEWAY_API_KEY",
}
_REMOVED_DIRECT_SECRETS = {
    "mimo": "LLM_MIMO_API_KEY",
    "kimi": "LLM_KIMI_API_KEY",
    "deepseek": "LLM_DEEPSEEK_API_KEY",
    "upstream": "UPSTREAM_API_KEY",
    "embedding": "EMBEDDING_API_KEY",
}
# PATH 安装后仓库相对路径不可达，迁移说明必须使用绝对 URL。
MIGRATION_DOC_URL = (
    "https://github.com/SparkHello/Memory_Platform/blob/main/"
    "docs/migrate-to-model-gateway.md"
)
_DIRECT_PROVIDER_MIGRATION_MESSAGE = (
    "direct-provider 路径已移除。\n"
    "请使用 Model Gateway：\n"
    "  modelgw connection / deployment / route / pricing\n"
    "  或 Web Console「模型与路由」\n"
    f"迁移说明：{MIGRATION_DOC_URL}"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments and sys.stdin.isatty():
        arguments = ["menu"]
    args = parser.parse_args(arguments)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    paths = cli_paths(args.home)
    try:
        project_root = discover_project_root(args.project_root, paths=paths)
        return int(args.handler(args, paths, project_root) or 0)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"错误：{describe_settings_error(exc)}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memgw",
        description="memory-gateway 本地控制台",
    )
    parser.add_argument("--version", action="version", version=f"memgw {VERSION}")
    parser.add_argument("--home", default="", help="覆盖 memgw 用户配置目录")
    parser.add_argument("--project-root", default="", help="覆盖项目根目录")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="初始化用户配置")
    init_parser.add_argument(
        "--no-import-env",
        action="store_true",
        help="不从项目现有 .env 导入非占位配置",
    )
    init_parser.set_defaults(handler=_cmd_init)

    run_parser = subparsers.add_parser("run", help="前台运行服务")
    _add_run_options(run_parser)
    run_parser.set_defaults(handler=_cmd_run)

    start_parser = subparsers.add_parser("start", help="后台启动服务")
    _add_run_options(start_parser)
    start_parser.set_defaults(handler=_cmd_start)

    stop_parser = subparsers.add_parser("stop", help="停止 memgw 启动的后台服务")
    stop_parser.add_argument("--force", action="store_true", help="超时后强制终止")
    stop_parser.set_defaults(handler=_cmd_stop)

    restart_parser = subparsers.add_parser("restart", help="重启后台服务")
    _add_run_options(restart_parser)
    restart_parser.add_argument("--force", action="store_true")
    restart_parser.set_defaults(handler=_cmd_restart)

    status_parser = subparsers.add_parser("status", help="查看进程和健康状态")
    status_parser.set_defaults(handler=_cmd_status)

    logs_parser = subparsers.add_parser("logs", help="查看后台日志")
    logs_parser.add_argument("-n", "--lines", type=int, default=80)
    logs_parser.add_argument("-f", "--follow", action="store_true")
    logs_parser.set_defaults(handler=_cmd_logs)

    open_parser = subparsers.add_parser("open", help="在浏览器打开 Web 控制台")
    open_parser.set_defaults(handler=_cmd_open)

    doctor_parser = subparsers.add_parser("doctor", help="检查环境、配置和目录")
    doctor_parser.set_defaults(handler=_cmd_doctor)

    install_parser = subparsers.add_parser("install-path", help="把 memgw 安装到用户 PATH 目录")
    install_parser.add_argument("--force", action="store_true")
    install_parser.set_defaults(handler=_cmd_install_path)

    menu_parser = subparsers.add_parser("menu", help="打开交互式控制台")
    menu_parser.set_defaults(handler=_cmd_menu)

    _add_stack_commands(subparsers)
    _add_config_commands(subparsers)
    _add_secret_commands(subparsers)
    _add_token_commands(subparsers)
    _add_retired_direct_provider_command(subparsers, "model")
    _add_retired_direct_provider_command(subparsers, "route")
    _add_retired_direct_provider_command(subparsers, "pricing")
    return parser


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址；默认仅本机。局域网使用需显式传 --host 0.0.0.0",
    )
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true")


def _add_stack_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "stack",
        help="用一个入口安装、运行和迁移记忆服务与 Model Gateway",
    )
    commands = parser.add_subparsers(dest="stack_command", required=True)

    install = commands.add_parser("install", help="安装并连接双服务运行栈")
    install.add_argument(
        "--model-gateway-source",
        default="",
        help="Model Gateway 源码或发行包路径；默认发现相邻项目或已安装命令",
    )
    install.add_argument("--model-gateway-home", default="")
    install.add_argument(
        "--credential-dir",
        default="",
        help="首次 Console/admin 凭据的私有文件目录；默认用户配置目录/credentials",
    )
    install.add_argument(
        "--keep-backend-key",
        action="store_true",
        help="保留现有 backend key；默认安全轮换并同步两端",
    )
    install.add_argument("--start", action="store_true", help="安装完成后启动整个栈")
    install.set_defaults(handler=_cmd_stack_install)

    start = commands.add_parser("start", help="按 Model Gateway → My_Memory 顺序启动")
    _add_run_options(start)
    start.add_argument("--model-gateway-home", default="")
    start.set_defaults(handler=_cmd_stack_start)

    stop = commands.add_parser("stop", help="按 My_Memory → Model Gateway 顺序停止")
    stop.add_argument("--model-gateway-home", default="")
    stop.add_argument("--force", action="store_true")
    stop.set_defaults(handler=_cmd_stack_stop)

    restart = commands.add_parser("restart", help="重启整个栈")
    _add_run_options(restart)
    restart.add_argument("--model-gateway-home", default="")
    restart.add_argument("--force", action="store_true")
    restart.set_defaults(handler=_cmd_stack_restart)

    status = commands.add_parser("status", help="同时查看两个服务")
    status.add_argument("--model-gateway-home", default="")
    status.set_defaults(handler=_cmd_stack_status)

    doctor = commands.add_parser("doctor", help="同时检查两个服务和接线")
    doctor.add_argument("--model-gateway-home", default="")
    doctor.set_defaults(handler=_cmd_stack_doctor)

    backup = commands.add_parser("backup", help="创建不含明文密钥的便携备份")
    backup.add_argument("--output", default="")
    backup.add_argument("--model-gateway-home", default="")
    backup.add_argument("--force", action="store_true")
    backup.set_defaults(handler=_cmd_stack_backup)

    restore = commands.add_parser("restore", help="校验并恢复便携备份")
    restore.add_argument("archive")
    restore.add_argument("--model-gateway-source", default="")
    restore.add_argument("--model-gateway-home", default="")
    restore.add_argument("--start", action="store_true")
    restore.add_argument("--yes", action="store_true", help="确认停止服务并替换当前数据")
    restore.set_defaults(handler=_cmd_stack_restore)

    recover_restore = commands.add_parser(
        "recover-restore",
        help="离线回滚被断电或强制终止打断的整栈恢复",
    )
    recover_restore.add_argument("--model-gateway-home", default="")
    recover_restore.add_argument("--yes", action="store_true")
    recover_restore.set_defaults(handler=_cmd_stack_recover_restore)


def _add_config_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser("config", help="管理普通运行配置")
    commands = parser.add_subparsers(dest="config_command", required=True)
    show = commands.add_parser("show")
    show.set_defaults(handler=_cmd_config_show)
    set_parser = commands.add_parser("set")
    set_parser.add_argument("name")
    set_parser.add_argument("value")
    set_parser.set_defaults(handler=_cmd_config_set)
    unset = commands.add_parser("unset")
    unset.add_argument("name")
    unset.set_defaults(handler=_cmd_config_unset)


def _add_secret_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser("secret", help="安全地编辑 API Key")
    commands = parser.add_subparsers(dest="secret_command", required=True)
    list_parser = commands.add_parser("list")
    list_parser.set_defaults(handler=_cmd_secret_list)
    set_parser = commands.add_parser("set")
    # name 不做 argparse choices 硬限制：退役的 direct-provider 名需要在
    # handler 里打印迁移提示，而不是被 argparse 以 invalid choice 拒绝。
    set_parser.add_argument("name")
    set_parser.add_argument("--stdin", action="store_true", help="从标准输入读取密钥")
    set_parser.add_argument(
        "--no-check",
        action="store_true",
        help="保存后不自动检查 Model Gateway 连接（默认仅 GET /models，不发起推理）",
    )
    set_parser.set_defaults(handler=_cmd_secret_set)
    delete_parser = commands.add_parser("delete")
    delete_parser.add_argument("name")
    delete_parser.add_argument("--yes", action="store_true")
    delete_parser.set_defaults(handler=_cmd_secret_delete)


def _add_token_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "token",
        help="管理按设备和用途隔离的访问 token",
    )
    commands = parser.add_subparsers(dest="token_command", required=True)
    create = commands.add_parser("create", help="创建并仅显示一次新 token")
    create.add_argument("--name", required=True, help="设备或客户端名称")
    create.add_argument("--user", default="default", help="绑定的用户命名空间")
    create.add_argument("--role", required=True, choices=AUTH_ROLES)
    create.set_defaults(handler=_cmd_token_create)
    list_parser = commands.add_parser("list", help="列出 token 元数据，不显示密钥")
    list_parser.set_defaults(handler=_cmd_token_list)
    revoke = commands.add_parser("revoke", help="按 token id 立即撤销")
    revoke.add_argument("token_id")
    revoke.set_defaults(handler=_cmd_token_revoke)


def _add_retired_direct_provider_command(subparsers: Any, name: str) -> None:
    parser = subparsers.add_parser(
        name,
        help="已迁移至 modelgw（direct-provider 已移除）",
    )
    # 吞下任意尾随参数（含旧子命令和选项），始终打印迁移提示而不是
    # argparse 的 unrecognized arguments。
    parser.add_argument("legacy_args", nargs=argparse.REMAINDER)
    parser.set_defaults(handler=_cmd_direct_provider_removed)


def _cmd_direct_provider_removed(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args, paths, project_root
    print(_DIRECT_PROVIDER_MIGRATION_MESSAGE, file=sys.stderr)
    return 2


def _cmd_init(args: Any, paths: CliPaths, project_root: Path) -> int:
    result = initialize_cli(
        paths=paths,
        project_root=project_root,
        import_project_env=not args.no_import_env,
    )
    print(f"memgw 配置目录：{paths.home}")
    if result["created"]:
        print("已创建：" + ", ".join(result["created"]))
    if result["imported"]:
        print(f"已从项目 .env 导入 {len(result['imported'])} 项非占位配置。")
    print("项目中的 .env 未被修改。接下来可运行 `memgw stack install`。")
    return 0


def _stack_credential_directory(credential_dir: str, paths: CliPaths) -> Path:
    configured = credential_dir.strip()
    project_config = read_json(paths.project_file)
    remembered = str(project_config.get("credential_dir") or "").strip()
    selected_value = configured or remembered
    selected = (
        Path(selected_value).expanduser() if selected_value else paths.credentials
    )
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    selected = selected.absolute()
    try:
        metadata = selected.lstat()
    except FileNotFoundError:
        selected.mkdir(parents=True, mode=0o700)
        metadata = selected.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("credential-dir 必须是非符号链接的私有目录")
    if os.name == "posix" and hasattr(os, "geteuid"):
        if metadata.st_uid != os.geteuid():
            raise ValueError("credential-dir 必须由当前用户持有")
    try:
        os.chmod(selected, 0o700)
    except OSError as exc:
        raise ValueError("无法把 credential-dir 权限设为 0700") from exc
    if configured and remembered != str(selected):
        project_config["credential_dir"] = str(selected)
        write_json_atomic(paths.project_file, project_config)
    return selected


def _cmd_stack_install(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _stack_install(
        paths=paths,
        project_root=project_root,
        model_gateway_source=args.model_gateway_source,
        model_gateway_home=args.model_gateway_home,
        credential_dir=args.credential_dir,
        keep_backend_key=bool(args.keep_backend_key),
        start=bool(args.start),
    )


def _source_stack_data_paths(
    *,
    paths: CliPaths,
    project_root: Path,
    environment: Mapping[str, str],
) -> StackInstallDataPaths:
    auth_database = environment.get("AUTH_DATABASE_PATH", "").strip() or str(
        paths.home / "auth.db"
    )
    return StackInstallDataPaths(
        memory_database=environment.get("DATABASE_PATH", "").strip()
        or "data/memory.db",
        knowledge_database=environment.get("KNOWLEDGE_DATABASE_PATH", "").strip()
        or "data/knowledge.db",
        auth_database=auth_database,
        auth_store=_resolve_runtime_path(project_root, auth_database),
        evaluation_directory=environment.get("EVAL_DIR", "").strip() or "eval",
        ui_directory=environment.get("UI_DIST_DIR", "").strip(),
    )


def _stack_install(
    *,
    paths: CliPaths,
    project_root: Path,
    model_gateway_source: str,
    model_gateway_home: str,
    credential_dir: str,
    keep_backend_key: bool,
    start: bool,
) -> int:
    validate_stack_install_process_environment()
    ensure_initialized(paths, project_root)
    environment = effective_environment(paths, project_root)
    credential_directory = _stack_credential_directory(credential_dir, paths)
    credential_sink = StackCredentialSink(
        gateway_path=credential_directory / "gateway.key",
        admin_path=credential_directory / "admin.key",
        read=_read_private_credential,
        deliver=_deliver_private_credential,
    )
    modelgw = _ensure_model_gateway_runtime(
        project_root,
        model_gateway_source=model_gateway_source,
    )
    model_home = _resolve_model_gateway_home(model_gateway_home)
    config = _read_model_gateway_config(model_home) if (model_home / "config.json").is_file() else {}
    server = config.get("server") if isinstance(config.get("server"), dict) else {}
    port = int(server.get("port") or 2030)
    try:
        result = apply_stack_install(
            layout="source",
            paths=paths,
            project_root=project_root,
            modelgw=modelgw,
            model_gateway_home=model_home,
            model_gateway_base_url=f"http://127.0.0.1:{port}/v1",
            data_paths=_source_stack_data_paths(
                paths=paths,
                project_root=project_root,
                environment=environment,
            ),
            credential_sink=credential_sink,
            keep_backend_key=keep_backend_key,
        )
    except StackInstallCommandError as exc:
        print(
            f"Model Gateway 安装命令失败（exit={exc.returncode}）。",
            file=sys.stderr,
        )
        return exc.returncode

    memory_port = int(read_json(paths.project_file).get("port") or 2026)
    # 在容器里 uvicorn 固定绑 2026，宿主机映射到哪个端口只有 compose 知道。用户
    # 会照着这段日志填客户端地址（文档也让他们回来看日志找密钥），所以必须打印
    # 宿主机上真正能访问的端口，否则改过端口的人拿到的是一份连不上的地址。
    public_port = str(memory_port)
    declared_public = environment.get("MEMORY_PUBLIC_PORT", "").strip()
    if declared_public.isdigit():
        public_port = declared_public
    print("双服务运行栈已经安装并安全接线。")
    print(f"Model Gateway 配置：{result.model_gateway_home}")
    print("backend key 已在两端同步，值未显示，也未写入项目 .env。")
    print("")
    print("接入信息")
    print("-" * 36)
    print(f"Web Console            http://127.0.0.1:{public_port}/ui/")
    print(f"OpenAI 兼容 base URL   http://127.0.0.1:{public_port}/v1")
    print(f"MCP                    http://127.0.0.1:{public_port}/mcp")
    print(f"Model Gateway base URL {result.model_gateway_base_url}")
    print("                       ↑ 内部接线地址，不要填进客户端")
    if result.console_credential_path is not None:
        print("")
        action = "已生成" if result.console_credential_generated else "已校验"
        print(f"{action} scoped Console token；明文未显示：")
        print(f"  {result.console_credential_path}")
        print("聊天/MCP 客户端请分别用 `memgw token create --role chat|mcp` 创建。")
    elif result.legacy_migration:
        print("")
        print("检测到旧版 GATEWAY_API_KEY，已保留一个版本的 legacy 兼容；值未显示。")
        print("建议为设备创建 scoped token 后禁用 legacy 兼容。")
    elif result.existing_scoped_tokens:
        print("")
        print("已有用户管理的 scoped token，安装未额外生成 Console token。")
    if result.admin_credential_path is not None:
        print("Model Gateway admin key 明文未显示：")
        print(f"  {result.admin_credential_path}")
    if start:
        return _stack_restart(
            paths=paths,
            project_root=project_root,
            host="127.0.0.1",
            port=None,
            reload=False,
            force=False,
            model_gateway_home=str(model_home),
        )
    print("下一步：memgw stack restart")
    return 0


def _cmd_stack_start(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _stack_start(
        paths=paths,
        project_root=project_root,
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        model_gateway_home=args.model_gateway_home,
    )


def _stack_start(
    *,
    paths: CliPaths,
    project_root: Path,
    host: str,
    port: int | None,
    reload: bool,
    model_gateway_home: str,
) -> int:
    ensure_initialized(paths, project_root)
    environment = effective_environment(paths, project_root)
    settings = Settings(_env_file=None, **environment)
    if not settings.model_gateway_enabled:
        raise ValueError("尚未连接独立 Model Gateway；请先运行 `memgw stack install`")
    modelgw = _require_modelgw(project_root)
    model_result = _run_modelgw(
        modelgw,
        _resolve_model_gateway_home(model_gateway_home),
        ["start"],
    )
    if model_result:
        return model_result
    memory_result = _start_memory_service(
        paths=paths,
        project_root=project_root,
        host=host,
        port=port,
        reload=reload,
    )
    if not memory_result:
        print("Memory Stack 已启动。")
    return memory_result


def _cmd_stack_stop(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _stack_stop(
        paths=paths,
        project_root=project_root,
        force=bool(args.force),
        model_gateway_home=args.model_gateway_home,
    )


def _stack_stop(
    *,
    paths: CliPaths,
    project_root: Path,
    force: bool,
    model_gateway_home: str,
) -> int:
    memory_result = _stop_memory_service(paths=paths, force=force)
    modelgw = _find_modelgw(project_root)
    if modelgw is None:
        print("没有找到 modelgw；My_Memory 已按当前状态处理。", file=sys.stderr)
        return memory_result or 1
    model_result = _run_modelgw(
        modelgw,
        _resolve_model_gateway_home(model_gateway_home),
        ["stop", *(["--force"] if force else [])],
    )
    if not memory_result and not model_result:
        print("Memory Stack 已停止。")
    return memory_result or model_result


def _cmd_stack_restart(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _stack_restart(
        paths=paths,
        project_root=project_root,
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        force=bool(args.force),
        model_gateway_home=args.model_gateway_home,
    )


def _stack_restart(
    *,
    paths: CliPaths,
    project_root: Path,
    host: str,
    port: int | None,
    reload: bool,
    force: bool,
    model_gateway_home: str,
) -> int:
    stop_result = _stack_stop(
        paths=paths,
        project_root=project_root,
        force=force,
        model_gateway_home=model_gateway_home,
    )
    if stop_result:
        return stop_result
    return _stack_start(
        paths=paths,
        project_root=project_root,
        host=host,
        port=port,
        reload=reload,
        model_gateway_home=model_gateway_home,
    )


def _cmd_stack_status(args: Any, paths: CliPaths, project_root: Path) -> int:
    modelgw = _find_modelgw(project_root)
    print("Model Gateway", flush=True)
    print("-" * 36, flush=True)
    model_result = (
        _run_modelgw(modelgw, _resolve_model_gateway_home(args.model_gateway_home), ["status"])
        if modelgw is not None
        else 1
    )
    if modelgw is None:
        print("未安装或未找到 modelgw。")
    print("\nMy_Memory")
    print("-" * 36)
    memory_result = _cmd_status(args, paths, project_root)
    return model_result or memory_result


def _cmd_stack_doctor(args: Any, paths: CliPaths, project_root: Path) -> int:
    modelgw = _require_modelgw(project_root)
    print("Model Gateway 检查", flush=True)
    print("-" * 36, flush=True)
    model_result = _run_modelgw(
        modelgw,
        _resolve_model_gateway_home(args.model_gateway_home),
        ["doctor"],
    )
    print("\nMy_Memory 检查")
    print("-" * 36)
    memory_result = _doctor_memory(paths=paths, project_root=project_root)
    return model_result or memory_result


def _cmd_stack_backup(args: Any, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    settings = Settings(_env_file=None, **effective_environment(paths, project_root))
    memory_database = _resolve_runtime_path(project_root, settings.database_path)
    knowledge_database = _resolve_runtime_path(project_root, settings.knowledge_database_path)
    auth_database = _resolve_runtime_path(project_root, settings.auth_database_path)
    default_name = "memory-stack-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".zip"
    destination = Path(args.output).expanduser() if args.output else Path.cwd() / default_name
    result = create_stack_backup(
        destination=destination,
        paths=paths,
        memory_database=memory_database,
        knowledge_database=knowledge_database,
        auth_database=auth_database,
        model_gateway_home=_resolve_model_gateway_home(args.model_gateway_home),
        force=bool(args.force),
    )
    print(f"便携备份已创建：{result['archive']}")
    print(f"包含 {len(result['files'])} 个组件；API Key 未包含。")
    print("注意：记忆和知识正文为明文敏感数据，请妥善保存备份文件。")
    return 0


def _stop_stack_for_offline_restore(
    *,
    paths: CliPaths,
    project_root: Path,
    model_gateway_home: str,
) -> int:
    ensure_initialized(paths, project_root)
    modelgw = _find_modelgw(project_root)
    model_home = _resolve_model_gateway_home(model_gateway_home)
    memory_stop = _stop_memory_service(paths=paths, force=False)
    if memory_stop:
        return memory_stop
    if modelgw is not None:
        model_stop = _run_modelgw(
            modelgw,
            model_home,
            ["stop"],
        )
        if model_stop:
            return model_stop
    project_config = read_json(paths.project_file)
    memory_port = int(project_config.get("port") or 2026)
    if _health_ok(memory_port):
        raise ValueError(
            f"端口 {memory_port} 上仍有 My_Memory 服务运行；请先停止非 memgw 管理的进程"
        )
    if _model_gateway_health_ok(model_home):
        raise ValueError("Model Gateway 仍在运行；拒绝替换其配置和用量数据库")
    return 0


def _cmd_stack_recover_restore(
    args: Any,
    paths: CliPaths,
    project_root: Path,
) -> int:
    if not args.yes:
        raise ValueError("恢复中断回滚会替换当前文件；确认后请加 --yes")
    stopped = _stop_stack_for_offline_restore(
        paths=paths,
        project_root=project_root,
        model_gateway_home=args.model_gateway_home,
    )
    if stopped:
        return stopped
    settings = Settings(_env_file=None, **effective_environment(paths, project_root))
    result = recover_interrupted_stack_restore(
        paths=paths,
        memory_database=_resolve_runtime_path(project_root, settings.database_path),
        knowledge_database=_resolve_runtime_path(
            project_root,
            settings.knowledge_database_path,
        ),
        auth_database=_resolve_runtime_path(project_root, settings.auth_database_path),
        model_gateway_home=_resolve_model_gateway_home(args.model_gateway_home),
    )
    print(f"已回滚 {result['recovered_journals']} 个中断的整栈恢复 journal。")
    return 0


def _cmd_stack_restore(args: Any, paths: CliPaths, project_root: Path) -> int:
    if not args.yes:
        raise ValueError("恢复会替换当前数据库和配置；确认后请加 --yes")
    stopped = _stop_stack_for_offline_restore(
        paths=paths,
        project_root=project_root,
        model_gateway_home=args.model_gateway_home,
    )
    if stopped:
        return stopped

    settings = Settings(_env_file=None, **effective_environment(paths, project_root))
    result = restore_stack_backup(
        archive_path=Path(args.archive),
        paths=paths,
        memory_database=_resolve_runtime_path(project_root, settings.database_path),
        knowledge_database=_resolve_runtime_path(project_root, settings.knowledge_database_path),
        auth_database=_resolve_runtime_path(project_root, settings.auth_database_path),
        model_gateway_home=_resolve_model_gateway_home(args.model_gateway_home),
    )
    print(f"已恢复 {len(result['restored'])} 个组件。")
    print(f"原文件回滚副本：{result['rollback']}")

    install_result = _stack_install(
        paths=paths,
        project_root=project_root,
        model_gateway_source=args.model_gateway_source,
        model_gateway_home=args.model_gateway_home,
        credential_dir="",
        keep_backend_key=False,
        start=False,
    )
    if install_result:
        return install_result
    print("供应商 API Key 和首次凭据文件不在备份中；缺失时请重新配置。")
    if args.start:
        return _stack_start(
            paths=paths,
            project_root=project_root,
            host="127.0.0.1",
            port=None,
            reload=False,
            model_gateway_home=args.model_gateway_home,
        )
    return 0


def _cmd_run(args: Any, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    command, environment, _ = _server_command(
        paths=paths,
        project_root=project_root,
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
    )
    return subprocess.run(command, cwd=project_root, env=environment, check=False).returncode


def _cmd_start(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _start_memory_service(
        paths=paths,
        project_root=project_root,
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
    )


def _start_memory_service(
    *,
    paths: CliPaths,
    project_root: Path,
    host: str,
    port: int | None,
    reload: bool,
) -> int:
    ensure_initialized(paths, project_root)
    state = _read_state(paths)
    if state and _pid_running(int(state.get("pid", 0))) and _pid_matches_gateway(
        int(state.get("pid", 0))
    ):
        print(f"服务已经在运行，PID {state['pid']}。")
        return 0
    command, environment, port = _server_command(
        paths=paths,
        project_root=project_root,
        host=host,
        port=port,
        reload=reload,
    )
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    with paths.log.open("ab", buffering=0) as log_handle:
        popen_kwargs: dict[str, Any] = {
            "cwd": project_root,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **popen_kwargs)
    write_json_atomic(
        paths.state,
        {
            "version": 1,
            "pid": process.pid,
            "port": port,
            "project_root": str(project_root),
            "started_at": datetime.now(UTC).isoformat(),
            "command": command,
        },
        backup=False,
    )
    healthy = _wait_for_health(port, process, timeout_seconds=20)
    if healthy:
        print(f"服务已启动：PID {process.pid}，http://localhost:{port}/ui")
        return 0
    if process.poll() is not None:
        print(f"服务启动失败，退出码 {process.returncode}。日志：{paths.log}", file=sys.stderr)
        return 1
    print(f"服务进程已启动但健康检查仍未就绪。PID {process.pid}，日志：{paths.log}")
    return 0


def _cmd_stop(args: Any, paths: CliPaths, project_root: Path) -> int:
    del project_root
    return _stop_memory_service(paths=paths, force=bool(args.force))


def _stop_memory_service(*, paths: CliPaths, force: bool) -> int:
    state = _read_state(paths)
    if not state:
        print("没有 memgw 管理的后台服务记录。")
        return 0
    pid = int(state.get("pid", 0))
    if not _pid_running(pid):
        paths.state.unlink(missing_ok=True)
        print("后台服务已经停止；已清理过期状态。")
        return 0
    if not _pid_matches_gateway(pid) and not force:
        raise ValueError(
            f"PID {pid} 当前命令无法确认为 memory-gateway；拒绝终止。"
            "确认后可使用 --force。"
        )
    if os.name == "nt":
        # Detached console processes do not receive a graceful CTRL event.
        # Terminate the already identity-checked process tree explicitly.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while _pid_running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_running(pid):
        if not force:
            print("服务未在 10 秒内退出；可使用 `memgw stop --force`。", file=sys.stderr)
            return 1
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    paths.state.unlink(missing_ok=True)
    print("服务已停止。")
    return 0


def _cmd_restart(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _restart_memory_service(
        paths=paths,
        project_root=project_root,
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        force=bool(args.force),
    )


def _restart_memory_service(
    *,
    paths: CliPaths,
    project_root: Path,
    host: str,
    port: int | None,
    reload: bool,
    force: bool,
) -> int:
    result = _stop_memory_service(paths=paths, force=force)
    if result:
        return result
    return _start_memory_service(
        paths=paths,
        project_root=project_root,
        host=host,
        port=port,
        reload=reload,
    )


def _cmd_status(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args, project_root
    state = _read_state(paths)
    if not state:
        print("状态：未由 memgw 启动")
        return 1
    pid = int(state.get("pid", 0))
    port = int(state.get("port", 2026))
    running = _pid_running(pid) and _pid_matches_gateway(pid)
    healthy = _health_ok(port) if running else False
    print(f"状态：{'运行中' if running else '已停止'}")
    print(f"PID：{pid}")
    print(f"健康检查：{'正常' if healthy else '不可用'}")
    print(f"控制台：http://localhost:{port}/ui")
    print(f"日志：{paths.log}")
    return 0 if running and healthy else 1


def _cmd_logs(args: Any, paths: CliPaths, project_root: Path) -> int:
    del project_root
    return _show_logs(paths=paths, lines=args.lines, follow=bool(args.follow))


def _show_logs(*, paths: CliPaths, lines: int, follow: bool) -> int:
    if not paths.log.exists():
        print(f"日志尚不存在：{paths.log}")
        return 1
    _print_log_tail(paths.log, max(1, lines))
    if not follow:
        return 0
    with paths.log.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                print(line, end="")
            else:
                time.sleep(0.2)


def _cmd_open(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args, project_root
    return _open_console(paths=paths)


def _open_console(*, paths: CliPaths) -> int:
    state = _read_state(paths) or {}
    port = int(state.get("port", 2026))
    url = f"http://localhost:{port}/ui"
    if not webbrowser.open(url):
        print(url)
    return 0


def _cmd_config_show(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args
    ensure_initialized(paths, project_root)
    values = masked_environment(read_env_file(paths.settings_env))
    for name in sorted(values):
        print(f"{name}={values[name]}")
    return 0


def _cmd_config_set(args: Any, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    name = args.name.strip().upper()
    if is_secret_name(name):
        raise ValueError("密钥请使用 `memgw secret set`，避免出现在终端历史中")
    update_env_value(paths.settings_env, name, args.value)
    print(f"已设置 {name}。重启服务后生效。")
    return 0


def _cmd_config_unset(args: Any, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    name = args.name.strip().upper()
    if is_secret_name(name):
        raise ValueError("密钥请使用 `memgw secret delete`")
    update_env_value(paths.settings_env, name, None)
    print(f"已移除 {name}。")
    return 0


def _cmd_secret_list(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args
    ensure_initialized(paths, project_root)
    values = read_env_file(paths.settings_env)
    for alias, name in _SECRET_ALIASES.items():
        print(f"{alias:10} {'已配置' if values.get(name) else '未配置'}")
    return 0


def _reject_secret_name(name: str) -> int:
    """secret 名在 handler 内校验：退役 direct-provider 名给迁移提示，其余非法名给清单。"""
    if name in _REMOVED_DIRECT_SECRETS:
        print(_DIRECT_PROVIDER_MIGRATION_MESSAGE, file=sys.stderr)
    else:
        print(
            f"未知密钥：{name}。可用密钥：{', '.join(sorted(_SECRET_ALIASES))}。",
            file=sys.stderr,
        )
    return 2


def _cmd_secret_set(args: Any, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    variable = _SECRET_ALIASES.get(args.name)
    if variable is None:
        return _reject_secret_name(args.name)
    value = sys.stdin.read().strip() if args.stdin else getpass.getpass(f"{args.name} 密钥：")
    if not value:
        raise ValueError("密钥不能为空")
    if args.name in {"gateway", "signing"}:
        candidate = effective_environment(paths, project_root)
        candidate[variable] = value
        Settings(_env_file=None, **candidate)
    update_env_value(paths.settings_env, variable, value)
    print(f"已安全保存 {args.name} 密钥；不会写入项目 .env。")
    if args.name == "gateway":
        update_env_value(
            paths.settings_env,
            "GATEWAY_LEGACY_API_KEY_ENABLED",
            "true",
        )
        print("已显式启用一个版本的 legacy all-scope gateway key 兼容。")
        print("新设备优先使用 `memgw token create` 创建 scoped token。")
        return 0
    if args.name == "signing":
        print("signing 仅用于内部游标与预览签名，不能作为客户端访问 token。")
        return 0
    if args.name == "model-gateway":
        environment = effective_environment(paths, project_root)
        if not environment.get("MODEL_GATEWAY_BASE_URL", "").strip():
            update_env_value(
                paths.settings_env,
                "MODEL_GATEWAY_BASE_URL",
                "http://127.0.0.1:2030/v1",
            )
            print("已使用本机 Model Gateway 默认地址 http://127.0.0.1:2030/v1。")
        if args.no_check:
            return 0
        print("正在检查 Model Gateway（只读取 /models，不会发起付费推理）……")
        return _run_model_gateway_check(paths, project_root, timeout_seconds=10.0)


def _cmd_secret_delete(args: Any, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    variable = _SECRET_ALIASES.get(args.name)
    if variable is None:
        return _reject_secret_name(args.name)
    if not args.yes and not _confirm(f"确定移除 {args.name} 密钥？"):
        print("已取消。")
        return 0
    # Keep an explicit empty override so deleting a migrated secret cannot
    # silently reveal the older value still present in the untouched project
    # .env beneath this user-owned configuration layer.
    update_env_value(paths.settings_env, variable, "")
    if args.name == "gateway":
        update_env_value(
            paths.settings_env,
            "GATEWAY_LEGACY_API_KEY_ENABLED",
            "false",
        )
    if args.name == "model-gateway":
        # URL and client key are a required pair under the central-only runtime.
        update_env_value(paths.settings_env, "MODEL_GATEWAY_BASE_URL", "")
    print(f"已移除 {args.name} 密钥。")
    return 0


def _cli_auth_store(paths: CliPaths, project_root: Path) -> AuthTokenStore:
    ensure_initialized(paths, project_root)
    settings = Settings(
        _env_file=None,
        **effective_environment(paths, project_root),
    )
    database_path = _resolve_runtime_path(project_root, settings.auth_database_path)
    store = AuthTokenStore(database_path)
    store.init_db()
    return store


def _cmd_token_create(args: Any, paths: CliPaths, project_root: Path) -> int:
    return _create_scoped_token(
        paths=paths,
        project_root=project_root,
        name=args.name,
        user=args.user,
        role=args.role,
    )


def _create_scoped_token(
    *,
    paths: CliPaths,
    project_root: Path,
    name: str,
    user: str,
    role: str,
) -> int:
    created = _cli_auth_store(paths, project_root).create_token(
        name=name,
        user_id=user,
        role=role,
    )
    print("访问 token（仅显示这一次，请立即保存到对应设备）：")
    print(created.token)
    print(
        f"id={created.record.token_id} role={created.record.role} "
        f"user={created.record.user_id} name={created.record.name}"
    )
    return 0


def _cmd_token_list(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args
    records = _cli_auth_store(paths, project_root).list_tokens()
    if not records:
        print("尚无 scoped token。")
        return 0
    print("ID               ROLE     USER             STATUS    NAME")
    for record in records:
        status_label = "revoked" if record.revoked_at else "active"
        print(
            f"{record.token_id} {record.role:8} {record.user_id[:16]:16} "
            f"{status_label:9} {record.name}"
        )
    return 0


def _cmd_token_revoke(args: Any, paths: CliPaths, project_root: Path) -> int:
    token_id = args.token_id.strip().lower()
    if not _cli_auth_store(paths, project_root).revoke_token(token_id):
        print(f"未找到 active token：{token_id}", file=sys.stderr)
        return 1
    print(f"已撤销 token：{token_id}")
    return 0


def _run_model_gateway_check(
    paths: CliPaths,
    project_root: Path,
    *,
    timeout_seconds: float,
) -> int:
    environment = effective_environment(paths, project_root)
    settings = Settings(_env_file=None, **environment)
    if not settings.model_gateway_enabled:
        raise ValueError("Model Gateway 地址或客户端 API Key 未配置")
    url = f"{settings.model_gateway_base_url.rstrip('/')}/models"
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.model_gateway_api_key}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        print(
            f"[失败] 无法连接 Model Gateway：{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    if response.status_code != 200:
        print(
            f"[失败] Model Gateway 返回 HTTP {response.status_code}",
            file=sys.stderr,
        )
        return 1
    try:
        payload = response.json()
        models = payload.get("data", []) if isinstance(payload, dict) else []
        model_ids = [
            str(item.get("id") or "")
            for item in models
            if isinstance(item, dict) and item.get("id")
        ]
    except (json.JSONDecodeError, ValueError):
        print("[失败] Model Gateway /models 返回的 JSON 无效", file=sys.stderr)
        return 1
    print(
        f"[正常] Model Gateway 连接与鉴权正常；可用 route {len(model_ids)} 条。"
    )
    return 0


def _cmd_doctor(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args
    return _doctor_memory(paths=paths, project_root=project_root)


def _doctor_memory(*, paths: CliPaths, project_root: Path) -> int:
    ensure_initialized(paths, project_root)
    problems: list[str] = []
    python = _project_python(project_root)
    print(f"项目：{project_root}")
    print(f"配置：{paths.home}")
    print(f"Python：{python if python.exists() else '缺失'}")
    environment = effective_environment(paths, project_root)
    try:
        settings = Settings(_env_file=None, **environment)
    except Exception as exc:
        problems.append(f"运行配置无效：{describe_settings_error(exc)}")
        settings = None
    if settings is not None:
        memory_path = _resolve_runtime_path(project_root, settings.database_path)
        knowledge_path = _resolve_runtime_path(project_root, settings.knowledge_database_path)
        auth_path = _resolve_runtime_path(project_root, settings.auth_database_path)
        print(f"记忆库：{memory_path}（{'存在' if memory_path.exists() else '尚未创建'}）")
        print(f"知识库：{knowledge_path}（{'存在' if knowledge_path.exists() else '尚未创建'}）")
        print(f"凭证库：{auth_path}（{'存在' if auth_path.exists() else '尚未创建'}）")
        if len({memory_path, knowledge_path, auth_path}) != 3:
            problems.append(
                "DATABASE_PATH、KNOWLEDGE_DATABASE_PATH 与 AUTH_DATABASE_PATH 必须互不相同"
            )
        if settings.model_gateway_enabled:
            print(f"模型模式：Model Gateway（{settings.model_gateway_base_url}）")
            for route_name, alias in (
                ("chat", settings.model_gateway_chat_model),
                ("memory.extract", settings.model_gateway_memory_extract_model),
                ("memory.compact", settings.model_gateway_memory_compact_model),
                ("memory.core", settings.model_gateway_memory_core_model),
                ("memory.review", settings.model_gateway_memory_review_model),
                ("knowledge.fast", settings.model_gateway_knowledge_fast_model),
                ("knowledge.pro", settings.model_gateway_knowledge_pro_model),
                ("memory.embedding", settings.model_gateway_embedding_model),
            ):
                print(f"{route_name:18} -> {alias}")
            if not settings.model_gateway_embedding_space_id.strip():
                print("embedding space：未配置，将安全回退到关键词/FTS")
        else:
            problems.append(
                "未配置 Model Gateway；请运行 `memgw stack install` 或 "
                f"`memgw secret set model-gateway`（见 {MIGRATION_DOC_URL}）"
            )
    configured_secrets = sum(
        bool(environment.get(variable))
        and not is_placeholder_value(environment.get(variable, ""))
        for variable in _SECRET_ALIASES.values()
    )
    print(f"密钥：已配置 {configured_secrets}/{len(_SECRET_ALIASES)} 项（值已隐藏）")
    if not environment.get("GATEWAY_SIGNING_SECRET") or is_placeholder_value(
        environment.get("GATEWAY_SIGNING_SECRET", "")
    ):
        problems.append("GATEWAY_SIGNING_SECRET 尚未配置")
    if settings is not None:
        active_scoped_token = False
        auth_path = _resolve_runtime_path(project_root, settings.auth_database_path)
        if auth_path.exists():
            try:
                active_scoped_token = AuthTokenStore(auth_path).has_active_tokens()
            except (OSError, sqlite3.Error, ValueError):
                problems.append("AUTH_DATABASE_PATH 无法读取或 schema 不兼容")
        legacy_key_available = bool(environment.get("GATEWAY_API_KEY")) and not (
            is_placeholder_value(environment.get("GATEWAY_API_KEY", ""))
        )
        if not active_scoped_token and not (
            settings.gateway_legacy_api_key_enabled and legacy_key_available
        ):
            problems.append(
                "没有可用访问凭证；请运行 `memgw token create`，或启用并配置 legacy gateway key"
            )
    if problems:
        for problem in problems:
            print(f"问题：{problem}", file=sys.stderr)
        return 1
    print("检查通过。")
    return 0


def _cmd_install_path(args: Any, paths: CliPaths, project_root: Path) -> int:
    del paths
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Programs" / "memory-gateway" / "bin"
        base.mkdir(parents=True, exist_ok=True)
        launcher = base / "memgw.cmd"
        if launcher.exists() and not args.force:
            raise ValueError(f"启动器已存在：{launcher}；使用 --force 可替换")
        python = _project_python(project_root)
        launcher.write_text(
            f'@echo off\r\n"{python}" -m app.cli %*\r\n',
            encoding="utf-8",
        )
        print(f"已安装：{launcher}")
        print(f"如果该目录不在 PATH，请把它加入用户 PATH：{base}")
        return 0
    target = project_root / "scripts" / "memgw"
    if not target.exists():
        raise ValueError(f"项目启动器不存在：{target}")
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "memgw"
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() and launcher.resolve() == target.resolve():
            print(f"已经安装：{launcher}")
            return 0
        if not args.force:
            raise ValueError(f"{launcher} 已存在；使用 --force 可替换")
        launcher.unlink()
    launcher.symlink_to(target)
    print(f"已安装：{launcher} -> {target}")
    if str(bin_dir) not in os.getenv("PATH", "").split(os.pathsep):
        print('请把下面一行加入 ~/.zprofile，然后重新打开终端：')
        print('export PATH="$HOME/.local/bin:$PATH"')
    return 0


def _cmd_menu(args: Any, paths: CliPaths, project_root: Path) -> int:
    del args
    ensure_initialized(paths, project_root)
    while True:
        state = _read_state(paths) or {}
        running = _pid_running(int(state.get("pid", 0)))
        print("\n本地记忆助手")
        print("=" * 36)
        print(f"记忆服务：{'运行中' if running else '已停止'}")
        print(f"模型服务：{_model_service_status(paths, project_root)}")
        print()
        print("1. 启动或停止记忆服务")
        print("2. 设置模型渠道、模型和用途")
        print("3. 检查整个系统是否可用")
        print("4. 为设备创建最小权限 token")
        print("5. 查看最近日志")
        print("6. 打开记忆管理页面")
        print("7. 重启记忆服务")
        print("0. 退出")
        try:
            choice = input("请选择：").strip()
        except EOFError:
            print()
            return 0
        if choice == "0":
            return 0
        if choice == "1":
            if running:
                if _confirm("停止记忆服务？"):
                    _stop_memory_service(paths=paths, force=False)
            else:
                _start_memory_service(
                    paths=paths,
                    project_root=project_root,
                    host="127.0.0.1",
                    port=None,
                    reload=False,
                )
        elif choice == "2":
            modelgw = _find_modelgw(project_root)
            if modelgw is None:
                print("没有找到模型服务菜单。请确认相邻的 Model_Gateway 项目已经安装。")
            else:
                subprocess.run([str(modelgw)], check=False)
        elif choice == "3":
            modelgw = _find_modelgw(project_root)
            if modelgw is not None:
                subprocess.run([str(modelgw), "doctor"], check=False)
            else:
                print("[注意] 没有找到独立模型服务。")
            _doctor_memory(paths=paths, project_root=project_root)
        elif choice == "4":
            name = input("设备或客户端名称：").strip()
            role = input("用途（chat/mcp/console，默认 chat）：").strip() or "chat"
            user = input("用户命名空间（默认 default）：").strip() or "default"
            if not name or role not in AUTH_ROLES:
                print("名称不能为空，用途必须是 chat、mcp 或 console。")
            else:
                _create_scoped_token(
                    paths=paths,
                    project_root=project_root,
                    name=name,
                    role=role,
                    user=user,
                )
        elif choice == "5":
            _show_logs(paths=paths, lines=40, follow=False)
        elif choice == "6":
            _open_console(paths=paths)
        elif choice == "7":
            _restart_memory_service(
                paths=paths,
                project_root=project_root,
                host="127.0.0.1",
                port=None,
                reload=False,
                force=False,
            )
        else:
            print("没有这个选项，请输入菜单里的数字。")


def _find_modelgw(project_root: Path) -> Path | None:
    managed = (
        project_root / ".venv" / "Scripts" / "modelgw.exe"
        if os.name == "nt"
        else project_root / ".venv" / "bin" / "modelgw"
    )
    if managed.is_file():
        return managed
    installed = shutil.which("modelgw")
    if installed:
        return Path(installed)
    siblings = (
        project_root.parent / "Model_Gateway",
        project_root.parent / "model-gateway",
    )
    candidates = tuple(
        candidate
        for sibling in siblings
        for candidate in (
            sibling / ".venv" / "bin" / "modelgw",
            sibling / ".venv" / "Scripts" / "modelgw.exe",
            sibling / "scripts" / "modelgw",
        )
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _require_modelgw(project_root: Path) -> Path:
    modelgw = _find_modelgw(project_root)
    if modelgw is None:
        raise ValueError(
            "没有找到 Model Gateway 运行时；请运行 `memgw stack install "
            "--model-gateway-source /path/to/model-gateway`"
        )
    return modelgw


def _ensure_model_gateway_runtime(
    project_root: Path,
    *,
    model_gateway_source: str,
) -> Path:
    managed = (
        project_root / ".venv" / "Scripts" / "modelgw.exe"
        if os.name == "nt"
        else project_root / ".venv" / "bin" / "modelgw"
    )
    if managed.is_file():
        return managed

    explicit_source = model_gateway_source.strip()
    sibling_candidates = (
        project_root.parent / "Model_Gateway",
        project_root.parent / "model-gateway",
    )
    sibling = next(
        (
            candidate
            for candidate in sibling_candidates
            if (candidate / "pyproject.toml").is_file()
        ),
        sibling_candidates[0],
    )
    source = Path(explicit_source).expanduser() if explicit_source else sibling
    installable_source = source.is_file() or (
        source.is_dir() and (source / "pyproject.toml").is_file()
    )
    if installable_source:
        python = _project_python(project_root)
        if not python.is_file():
            raise ValueError(f"项目虚拟环境不存在：{python}")
        print(f"正在把 Model Gateway 安装到统一运行环境：{python.parent.parent}")
        result = subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", str(source.resolve())],
            check=False,
        )
        if result.returncode:
            raise ValueError("Model Gateway 运行时安装失败")
        if not managed.is_file():
            raise ValueError("安装完成但没有找到 modelgw 启动器")
        return managed

    installed = shutil.which("modelgw")
    if installed:
        return Path(installed)
    raise ValueError(
        "没有找到 Model Gateway 源码或已安装命令；请使用 "
        "`--model-gateway-source /path/to/model-gateway`"
    )


def _resolve_model_gateway_home(model_gateway_home: str) -> Path:
    value = model_gateway_home.strip()
    return Path(value).expanduser().resolve() if value else default_model_gateway_home()


def _modelgw_base_command(modelgw: Path, home: Path) -> list[str]:
    return [str(modelgw), "--home", str(home)]


def _run_modelgw(
    modelgw: Path,
    home: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
    quiet: bool = False,
) -> int:
    result = subprocess.run(
        [*_modelgw_base_command(modelgw, home), *arguments],
        input=input_text,
        text=True,
        capture_output=quiet,
        check=False,
    )
    if quiet and result.returncode:
        message = (result.stderr or result.stdout or "Model Gateway 命令失败").strip()
        print(message, file=sys.stderr)
    return int(result.returncode)


def _read_model_gateway_config(home: Path) -> dict[str, Any]:
    config_path = home / "config.json"
    if not config_path.is_file():
        raise ValueError(f"Model Gateway 配置不存在：{config_path}")
    return read_json(config_path)


def _model_gateway_health_ok(home: Path) -> bool:
    try:
        config = _read_model_gateway_config(home)
        server = config.get("server")
        port = int(server.get("port") or 2030) if isinstance(server, dict) else 2030
    except (OSError, ValueError):
        return False
    return _health_ok(port)


def _model_service_status(paths: CliPaths, project_root: Path) -> str:
    environment = effective_environment(paths, project_root)
    base_url = environment.get("MODEL_GATEWAY_BASE_URL", "").strip()
    if not base_url:
        return "尚未连接"
    parsed = urlparse(base_url)
    if not parsed.hostname:
        return "地址设置有误"
    root_path = parsed.path.rstrip("/")
    if root_path.endswith("/v1"):
        root_path = root_path[:-3]
    health_url = parsed._replace(path=root_path + "/health", query="", fragment="").geturl()
    try:
        response = httpx.get(health_url, timeout=0.8)
    except httpx.HTTPError:
        return "已经连接，但当前未运行"
    return "已连接并运行" if response.status_code == 200 else "已经连接，但当前不可用"


def _server_command(
    *,
    paths: CliPaths,
    project_root: Path,
    host: str,
    port: int | None,
    reload: bool,
) -> tuple[list[str], dict[str, str], int]:
    python = _project_python(project_root)
    if not python.exists():
        raise ValueError(f"项目虚拟环境不存在：{python}；请先创建 .venv")
    project_config = read_json(paths.project_file)
    port = int(port or project_config.get("port") or 2026)
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    command = [
        str(python),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload:
        command.append("--reload")
    # The service reads its private 0600 file itself. Passing only the path
    # prevents gateway/backend/provider/signing material from lingering in
    # uvicorn and worker process environments (and therefore /proc or process
    # inspection output). Non-secret operational overrides remain available.
    environment = {
        name: value
        for name, value in effective_environment(paths, project_root).items()
        if not is_secret_name(name)
    }
    environment["MEMGW_SETTINGS_PATH"] = str(paths.settings_env)
    if os.name == "nt":
        base_python = _windows_venv_base_python(python)
        if base_python != python.resolve():
            # Python 3.14's Windows venv launcher starts the base interpreter
            # as a child and exits.  Launch the interpreter directly so the
            # recorded PID remains the gateway PID, while preserving the venv.
            command[0] = str(base_python)
            environment["__PYVENV_LAUNCHER__"] = str(python.resolve())
    return command, environment, port


def _project_python(project_root: Path) -> Path:
    return (
        project_root / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else project_root / ".venv" / "bin" / "python"
    )


def _windows_venv_base_python(venv_python: Path) -> Path:
    configuration = venv_python.parent.parent / "pyvenv.cfg"
    try:
        values = {
            key.strip().lower(): value.strip()
            for line in configuration.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
    except OSError:
        return venv_python.resolve()
    candidates = []
    if values.get("executable"):
        candidates.append(Path(values["executable"]))
    if values.get("home"):
        candidates.append(Path(values["home"]) / "python.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return venv_python.resolve()


def _read_state(paths: CliPaths) -> dict[str, Any] | None:
    if not paths.state.exists():
        return None
    try:
        return read_json(paths.state)
    except ValueError:
        return None


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # signal 0 is CTRL_C_EVENT on Windows, so os.kill(pid, 0) is not a
        # harmless existence probe and can interrupt the managed process.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _pid_matches_gateway(pid: int) -> bool:
    if os.name == "nt":
        script = (
            '$p = Get-CimInstance Win32_Process -Filter "ProcessId = '
            f'{pid}"; if ($null -ne $p) {{ $p.CommandLine }}'
        )
        command = ""
        for executable in ("powershell.exe", "pwsh.exe"):
            try:
                result = subprocess.run(
                    [
                        executable,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0 and result.stdout.strip():
                command = result.stdout.lower()
                break
    else:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        command = result.stdout.lower()
    return "uvicorn" in command and "app.main:app" in command


def _health_ok(port: int) -> bool:
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            timeout=1,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _wait_for_health(port: int, process: subprocess.Popen[Any], *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if _health_ok(port):
            return True
        time.sleep(0.2)
    return False


def _print_log_tail(path: Path, lines: int) -> None:
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        print(line)


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _resolve_runtime_path(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()



if __name__ == "__main__":
    raise SystemExit(main())
