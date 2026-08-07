from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

import httpx
from pydantic import ValidationError

from model_gateway.config_store import (
    ConfigError,
    GatewayPaths,
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    set_secret,
    write_config,
)
from model_gateway.models import (
    AuthConfig,
    BillingPlan,
    Capabilities,
    ClientConfig,
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
    PricingConfig,
    PricingTier,
    RequestTransform,
    RouteConfig,
    ServerConfig,
    validate_id,
)
from model_gateway.pricing_research import (
    PricingResearchCallError,
    PricingResearchOutcome,
    ResearchCallMetadata,
    research_pricing,
)
from model_gateway.routing import RouteTarget
from model_gateway.usage import UsageCapture, UsageStore


class CLIError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProbeResult:
    connection_id: str
    status: str
    detail: str
    model_ids: frozenset[str] = frozenset()
    deployment_id: str = ""
    live: bool = False
    level: str = ""

    @property
    def ok(self) -> bool:
        if self.level:
            return self.level == "ok"
        return self.status in {"connected", "available", "connected_unlisted", "live_ok"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modelgw",
        description="本地模型连接、部署与功能路由控制台。",
    )
    parser.add_argument(
        "--home",
        default="",
        metavar="DIR",
        help="配置目录；默认使用当前系统的用户配置目录",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出适合脚本消费的结果",
    )
    parser.add_argument("--version", action="version", version="modelgw 0.1.0")
    commands = parser.add_subparsers(dest="command")

    menu_parser = commands.add_parser(
        "menu",
        help="打开面向日常使用的交互式终端菜单",
    )
    menu_parser.set_defaults(handler=_cmd_menu)

    init_parser = commands.add_parser("init", help="初始化用户配置目录")
    init_parser.set_defaults(handler=_cmd_init)

    quickstart_parser = commands.add_parser(
        "quickstart",
        help="一步配置一个渠道、聊天模型和全部用途，并连接记忆服务",
    )
    quickstart_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="不提问；用下面的参数配置，API Key 从标准输入读取一行",
    )
    # --json is also a global flag; accepting it here too lets automation append
    # it after the subcommand (the natural form) without an argparse error.
    # SUPPRESS keeps the global value intact when this positional copy is absent.
    quickstart_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="以 JSON 输出结果（也可放在 quickstart 之前作为全局参数）",
    )
    quickstart_parser.add_argument("--channel", default="", help="渠道英文简称，例如 deepseek")
    quickstart_parser.add_argument("--base-url", default="", help="官方 OpenAI 兼容 API 地址（HTTPS）")
    quickstart_parser.add_argument("--chat-model", default="", help="供应商页面显示的精确聊天模型 ID")
    quickstart_parser.add_argument(
        "--adapter", default="generic", choices=["generic", "kimi", "deepseek", "mimo"]
    )
    quickstart_parser.add_argument(
        "--plan",
        default="payg",
        choices=[
            "payg",
            "subscription",
            "free_tier",
            "token_plan",
            "coding_plan",
            "direct_tool_only",
            "custom",
        ],
    )
    quickstart_parser.add_argument("--chat-author", default="", help="聊天模型作者简称，默认用渠道名")
    quickstart_parser.add_argument(
        "--chat-capability",
        action="append",
        default=[],
        choices=[
            "tools",
            "parallel_tools",
            "reasoning",
            "multimodal_input",
            "json_object",
            "json_schema",
        ],
        help="聊天模型能力；可重复",
    )
    quickstart_parser.add_argument(
        "--reasoning-default",
        default="inherit",
        choices=["inherit", "enabled", "disabled"],
    )
    quickstart_parser.add_argument("--embedding-model", default="", help="可选：向量模型 ID")
    quickstart_parser.add_argument("--embedding-dimensions", type=int, help="向量维度")
    quickstart_parser.add_argument("--embedding-space", default="", help="向量空间名称")
    quickstart_parser.add_argument("--embedding-author", default="", help="向量模型作者简称")
    quickstart_parser.add_argument(
        "--no-connect-memory",
        action="store_true",
        help="只配置模型服务，不自动连接记忆服务",
    )
    quickstart_parser.add_argument(
        "--no-start",
        action="store_true",
        help="配置完成后不自动启动模型服务",
    )
    quickstart_parser.add_argument("--memgw", default="", help="记忆服务 memgw 启动器路径")
    quickstart_parser.set_defaults(handler=_cmd_quickstart)

    serve_parser = commands.add_parser(
        "serve",
        aliases=["run"],
        help="在前台启动本地网关（run 为别名）",
    )
    serve_parser.add_argument("--host", help="覆盖监听地址（仅允许回环地址）")
    serve_parser.add_argument("--port", type=int, help="覆盖监听端口")
    serve_parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    serve_parser.add_argument("--no-access-log", action="store_true")
    serve_parser.set_defaults(handler=_cmd_serve)

    start_parser = commands.add_parser("start", help="在后台启动本地网关")
    start_parser.add_argument("--host", help="覆盖监听地址（仅允许回环地址）")
    start_parser.add_argument("--port", type=int, help="覆盖监听端口")
    start_parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    start_parser.add_argument("--access-log", action="store_true")
    start_parser.set_defaults(handler=_cmd_start)

    stop_parser = commands.add_parser("stop", help="停止由 modelgw start 管理的后台网关")
    stop_parser.add_argument("--timeout", type=float, default=10.0)
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="正常停止超时后强制终止已验证身份的网关进程",
    )
    stop_parser.set_defaults(handler=_cmd_stop)

    status_parser = commands.add_parser("status", help="查看后台进程与 HTTP 健康状态")
    status_parser.set_defaults(handler=_cmd_status)

    logs_parser = commands.add_parser("logs", help="查看后台网关日志")
    logs_parser.add_argument("--lines", type=int, default=100)
    logs_parser.add_argument("--follow", "-f", action="store_true")
    logs_parser.set_defaults(handler=_cmd_logs)

    doctor_parser = commands.add_parser("doctor", help="检查本地配置、安全权限与依赖")
    doctor_parser.set_defaults(handler=_cmd_doctor)

    secret_parser = commands.add_parser("secret", help="管理仓库外密钥")
    secret_commands = secret_parser.add_subparsers(dest="secret_command", required=True)
    secret_set = secret_commands.add_parser("set", help="安全写入密钥")
    secret_set.add_argument("name", help="connection/client ID 或 secret_ref")
    value_source = secret_set.add_mutually_exclusive_group()
    value_source.add_argument(
        "--value",
        help="自动化用途；更推荐无回显提示或 --stdin，避免进入 shell 历史",
    )
    value_source.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取一行密钥",
    )
    secret_set.add_argument(
        "--no-check",
        action="store_true",
        help="保存后不执行免费的 GET /models 检查",
    )
    secret_set.add_argument(
        "--live",
        action="store_true",
        help="除免费检查外发送最小真实请求；可能产生费用",
    )
    secret_set.add_argument(
        "--as-interactive",
        action="store_true",
        help="仅用于交互式客户端套餐检查；不会把连接开放给 backend",
    )
    secret_set.set_defaults(handler=_cmd_secret_set)
    secret_list = secret_commands.add_parser("list", help="只列出密钥名称和引用，不显示值")
    secret_list.set_defaults(handler=_cmd_secret_list)
    secret_delete = secret_commands.add_parser("delete", help="删除一个密钥")
    secret_delete.add_argument("name", help="connection/client ID 或 secret_ref")
    secret_delete.set_defaults(handler=_cmd_secret_delete)

    client_parser = commands.add_parser("client", help="管理调用本地网关的客户端")
    client_commands = client_parser.add_subparsers(dest="client_command", required=True)
    client_add = client_commands.add_parser("add", help="新增或替换客户端")
    client_add.add_argument("client_id")
    client_add.add_argument(
        "--kind",
        default="backend",
        choices=["backend", "interactive", "admin"],
    )
    client_add.add_argument("--secret-name", help="密钥引用名称；省略时自动生成安全名称")
    client_add.add_argument(
        "--route",
        action="append",
        default=[],
        help="允许的 route glob；可重复或用逗号分隔，默认 *",
    )
    client_add.add_argument("--allow-direct-deployments", action="store_true")
    client_add.add_argument("--disabled", action="store_true")
    client_add.add_argument(
        "--set-secret",
        action="store_true",
        help="添加后立即通过无回显提示设置本地客户端密钥",
    )
    client_add.add_argument("--replace", action="store_true")
    client_add.set_defaults(handler=_cmd_client_add)
    client_list = client_commands.add_parser("list", help="列出客户端")
    client_list.set_defaults(handler=_cmd_client_list)
    client_remove = client_commands.add_parser("remove", help="删除客户端配置（不删除密钥）")
    client_remove.add_argument("client_id")
    client_remove.set_defaults(handler=_cmd_client_remove)

    connection_parser = commands.add_parser("connection", help="管理供应商连接")
    connection_commands = connection_parser.add_subparsers(
        dest="connection_command", required=True
    )
    connection_add = connection_commands.add_parser("add", help="新增或替换连接")
    connection_add.add_argument("connection_id")
    connection_add.add_argument(
        "--vendor",
        "--channel-operator",
        dest="channel_operator",
        required=True,
        help="实际接入渠道/运营方，例如 deepseek、siliconflow、dashscope",
    )
    connection_add.add_argument("--base-url", required=True)
    connection_add.add_argument("--secret-name", help="上游 API Key 的 secret_ref")
    connection_add.add_argument(
        "--auth-type", default="bearer", choices=["bearer", "x-api-key"]
    )
    connection_add.add_argument(
        "--adapter", default="generic", choices=["generic", "kimi", "deepseek", "mimo"]
    )
    connection_add.add_argument(
        "--plan",
        dest="billing_type",
        default="payg",
        choices=[
            "payg",
            "subscription",
            "free_tier",
            "token_plan",
            "coding_plan",
            "direct_tool_only",
            "custom",
        ],
    )
    connection_add.add_argument("--plan-name", default="default")
    connection_add.add_argument(
        "--scope",
        choices=["backend_allowed", "interactive_only", "disabled"],
        help="省略时普通连接为 backend_allowed，Token/Coding Plan 为 interactive_only",
    )
    models_endpoint = connection_add.add_mutually_exclusive_group()
    models_endpoint.add_argument("--models-endpoint", default="/models")
    models_endpoint.add_argument(
        "--no-models-endpoint", action="store_true", help="该渠道不提供模型列表接口"
    )
    connection_add.add_argument("--chat-endpoint", default="/chat/completions")
    connection_add.add_argument("--embeddings-endpoint", default="/embeddings")
    connection_add.add_argument(
        "--forward-header", action="append", default=[], help="允许转发的客户端 Header；可重复"
    )
    connection_add.add_argument("--timeout", type=float, default=300.0)
    connection_add.add_argument("--cooldown", type=float, default=300.0)
    connection_add.add_argument("--disabled", action="store_true")
    connection_add.add_argument("--replace", action="store_true")
    connection_add.set_defaults(handler=_cmd_connection_add)
    connection_list = connection_commands.add_parser("list", help="列出连接")
    connection_list.set_defaults(handler=_cmd_connection_list)
    connection_remove = connection_commands.add_parser(
        "remove", help="删除没有 deployment 引用的连接（不删除密钥）"
    )
    connection_remove.add_argument("connection_id")
    connection_remove.set_defaults(handler=_cmd_connection_remove)
    connection_check = connection_commands.add_parser("check", help="检查一个或全部连接")
    connection_check.add_argument("connection_ids", nargs="*")
    connection_check.add_argument(
        "--live", action="store_true", help="发送最小真实请求；可能产生费用"
    )
    connection_check.add_argument(
        "--as-interactive",
        action="store_true",
        help="以 interactive 用途检查受限套餐；不会改变 connection 配置",
    )
    connection_check.set_defaults(handler=_cmd_connection_check)

    deployment_parser = commands.add_parser("deployment", help="管理精确上游模型部署")
    deployment_commands = deployment_parser.add_subparsers(
        dest="deployment_command", required=True
    )
    deployment_add = deployment_commands.add_parser("add", help="新增或替换 deployment")
    deployment_add.add_argument("deployment_id")
    deployment_add.add_argument("--connection", required=True)
    deployment_add.add_argument("--model", "--upstream-model", dest="upstream_model", required=True)
    deployment_add.add_argument("--kind", default="chat", choices=["chat", "embedding"])
    deployment_add.add_argument("--author", help="模型作者；省略时使用 connection 的渠道名")
    deployment_add.add_argument("--family", default="")
    deployment_add.add_argument(
        "--reasoning-default",
        default="inherit",
        choices=["inherit", "enabled", "disabled"],
        help="客户端未显式指定 reasoning 时的 deployment 默认值",
    )
    deployment_add.add_argument(
        "--capability",
        action="append",
        default=[],
        choices=[
            "tools",
            "parallel_tools",
            "reasoning",
            "multimodal_input",
            "json_object",
            "json_schema",
        ],
    )
    deployment_add.add_argument("--no-streaming", action="store_true")
    deployment_add.add_argument("--dimensions", type=int)
    deployment_add.add_argument("--embedding-space", default="")
    deployment_add.add_argument("--pricing", help="引用已确认的 pricing ID")
    deployment_add.add_argument(
        "--remove", action="append", default=[], help="移除一个非核心请求参数；可重复"
    )
    deployment_add.add_argument(
        "--set-if-missing",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help="仅在缺失时设置 provider 参数",
    )
    deployment_add.add_argument(
        "--force-param",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help="强制设置 provider 参数",
    )
    deployment_add.add_argument("--disabled", action="store_true")
    deployment_add.add_argument("--replace", action="store_true")
    deployment_add.set_defaults(handler=_cmd_deployment_add)
    deployment_list = deployment_commands.add_parser("list", help="列出 deployments")
    deployment_list.set_defaults(handler=_cmd_deployment_list)
    deployment_remove = deployment_commands.add_parser(
        "remove", help="删除没有 route 引用的 deployment"
    )
    deployment_remove.add_argument("deployment_id")
    deployment_remove.set_defaults(handler=_cmd_deployment_remove)

    route_parser = commands.add_parser("route", help="管理功能模型路由")
    route_commands = route_parser.add_subparsers(dest="route_command", required=True)
    route_set = route_commands.add_parser("set", help="设置有序 fallback deployment 列表")
    route_set.add_argument("route_id")
    route_set.add_argument("targets", nargs="+", help="按优先级排列；可用空格或逗号分隔")
    route_set.add_argument("--kind", choices=["chat", "embedding"], help="省略时从 deployment 推断")
    route_set.add_argument(
        "--require",
        action="append",
        default=[],
        choices=[
            "streaming",
            "tools",
            "parallel_tools",
            "reasoning",
            "multimodal_input",
            "json_object",
            "json_schema",
        ],
        help="所有目标必须满足的能力；可重复",
    )
    route_set.add_argument("--max-attempts", type=int, default=3)
    route_set.add_argument("--disabled", action="store_true")
    route_set.set_defaults(handler=_cmd_route_set)
    route_list = route_commands.add_parser("list", help="列出功能路由")
    route_list.set_defaults(handler=_cmd_route_list)
    route_remove = route_commands.add_parser("remove", help="删除功能路由")
    route_remove.add_argument("route_id")
    route_remove.set_defaults(handler=_cmd_route_remove)

    pricing_parser = commands.add_parser("pricing", help="管理官方价格快照")
    pricing_commands = pricing_parser.add_subparsers(dest="pricing_command", required=True)
    pricing_set = pricing_commands.add_parser("set", help="新增或替换一个价格快照")
    pricing_set.add_argument("pricing_id")
    pricing_set.add_argument(
        "--mode",
        default="per_token",
        choices=["per_token", "subscription", "free_tier", "custom", "unknown"],
    )
    pricing_set.add_argument("--currency", default="USD")
    pricing_set.add_argument("--unit-tokens", type=int, default=1_000_000)
    pricing_set.add_argument("--input", help="每 unit_tokens 的输入价格")
    pricing_set.add_argument(
        "--cached-input", "--cached", dest="cached_input", help="缓存输入价格"
    )
    pricing_set.add_argument("--output", help="输出价格")
    pricing_set.add_argument(
        "--max-input-tokens", type=int, help="此 tier 的输入 Token 上限；省略表示无上限"
    )
    pricing_set.add_argument(
        "--tier",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            "多分档价格；可重复，例如 "
            "'{\"max_input_tokens\":32000,\"input\":\"1\",\"output\":\"2\"}'"
        ),
    )
    pricing_set.add_argument("--source-url", "--source", dest="source_url", default="")
    pricing_set.add_argument(
        "--checked-at",
        default="",
        help="人工核对时间；默认记录当前 UTC 日期",
    )
    pricing_set.add_argument("--effective-from", default="")
    pricing_set.add_argument("--notes", default="")
    pricing_set.add_argument(
        "--deployment",
        action="append",
        default=[],
        help="同时把该快照绑定到 deployment；可重复",
    )
    pricing_set.set_defaults(handler=_cmd_pricing_set)
    pricing_research = pricing_commands.add_parser(
        "research",
        help="从明确指定的官方 HTTPS 页面生成价格候选；默认不写配置",
    )
    pricing_research.add_argument("target_deployment", help="需要核价的精确 deployment")
    pricing_research.add_argument(
        "--source-url",
        required=True,
        help="该目标 connection/channel 的官方 HTTPS 价格页面",
    )
    pricing_research.add_argument(
        "--research-deployment",
        required=True,
        help="显式用于提取候选的 backend_allowed chat deployment",
    )
    pricing_research.add_argument(
        "--official-host",
        default="",
        help=(
            "当官方文档域与 connection API 域不同，明确确认 source URL 的 hostname；"
            "不得用于第三方聚合页"
        ),
    )
    pricing_research.add_argument(
        "--pricing-id",
        default="",
        help="应用时保存的 pricing ID；省略时从目标 deployment 与当前日期生成",
    )
    pricing_research.add_argument(
        "--apply",
        action="store_true",
        help="确认候选后写入 pricing 并绑定目标 deployment",
    )
    pricing_research.add_argument(
        "--yes",
        action="store_true",
        help="与 --apply 一起使用，跳过交互式精确确认",
    )
    pricing_research.add_argument(
        "--replace",
        action="store_true",
        help="与 --apply 一起使用，替换同名 pricing 快照",
    )
    pricing_research.set_defaults(handler=_cmd_pricing_research)
    pricing_list = pricing_commands.add_parser("list", help="列出价格快照")
    pricing_list.set_defaults(handler=_cmd_pricing_list)
    pricing_remove = pricing_commands.add_parser(
        "remove", help="删除没有 deployment 引用的价格快照"
    )
    pricing_remove.add_argument("pricing_id")
    pricing_remove.set_defaults(handler=_cmd_pricing_remove)

    check_parser = commands.add_parser("check", help="检查全部或指定连接")
    check_parser.add_argument("scope", nargs="?", default="all", choices=["all"])
    check_parser.add_argument(
        "--connection", action="append", default=[], dest="connection_ids"
    )
    check_parser.add_argument(
        "--live", action="store_true", help="发送最小真实请求；可能产生费用"
    )
    check_parser.add_argument(
        "--as-interactive",
        action="store_true",
        help="以 interactive 用途检查受限套餐；不会改变 connection 配置",
    )
    check_parser.set_defaults(handler=_cmd_connection_check)

    usage_parser = commands.add_parser("usage", help="查看不含正文的本地用量")
    usage_commands = usage_parser.add_subparsers(dest="usage_command", required=True)
    usage_summary = usage_commands.add_parser("summary", help="汇总最近用量")
    usage_summary.add_argument("--days", type=int, default=30)
    usage_summary.set_defaults(handler=_cmd_usage_summary)

    install_parser = commands.add_parser("install-path", help="把 modelgw 安装到用户 PATH 目录")
    install_parser.add_argument("--target-dir", help="目标目录，默认 ~/.local/bin")
    install_parser.add_argument("--force", action="store_true", help="替换已有的 modelgw 启动器")
    install_parser.set_defaults(handler=_cmd_install_path)

    schema_parser = commands.add_parser("schema", help="打印当前 GatewayConfig JSON Schema")
    schema_parser.set_defaults(handler=_cmd_schema)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arguments)
    if not getattr(args, "command", None):
        if sys.stdin.isatty():
            args.handler = _cmd_menu
        else:
            parser.print_help()
            return 0
    try:
        result = args.handler(args)
        return int(result or 0)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except (CLIError, ConfigError, ValidationError, ValueError) as exc:
        print(f"[错误] {_clean_error(exc)}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2


def _cmd_menu(args: argparse.Namespace) -> int:
    if args.json:
        raise CLIError("交互式菜单不能与 --json 一起使用")
    from model_gateway.user_console import run_user_console

    return run_user_console(args)


def _cmd_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    result = initialize(paths)
    payload = {
        "home": str(paths.home),
        "config": str(paths.config),
        "secrets": str(paths.secrets),
        "usage_db": str(paths.usage_db),
        "created": result["created"],
    }
    if args.json:
        _json(payload)
    else:
        if result["created"]:
            print("已初始化 Model Gateway：" + ", ".join(result["created"]))
        else:
            print("Model Gateway 已初始化；现有配置未被覆盖。")
        print(f"配置目录：{paths.home}")
        print(f"配置文件：{paths.config}")
        print(f"密钥文件：{paths.secrets}（不会写入项目目录）")
    return 0


def _cmd_quickstart(args: argparse.Namespace) -> int:
    from model_gateway.quickstart import QuickstartError, QuickstartSpec, apply_quickstart
    from model_gateway.user_console import _find_memgw, _run_memgw

    paths = _paths(args)
    initialize(paths)

    if args.non_interactive:
        api_key = sys.stdin.readline().rstrip("\r\n")
        spec = QuickstartSpec(
            channel_operator=args.channel,
            base_url=args.base_url,
            chat_model=args.chat_model,
            api_key=api_key,
            adapter=args.adapter,
            plan=args.plan,
            chat_author=args.chat_author,
            chat_capabilities=tuple(dict.fromkeys(args.chat_capability)),
            reasoning_default=args.reasoning_default,
            embedding_model=args.embedding_model,
            embedding_dimensions=args.embedding_dimensions,
            embedding_space=args.embedding_space,
            embedding_author=args.embedding_author,
            connect_memory=not args.no_connect_memory,
        )
    else:
        if args.json:
            raise CLIError("交互式 quickstart 不能与 --json 一起使用；自动化请加 --non-interactive")
        spec = _quickstart_prompt(args)

    try:
        result = apply_quickstart(paths, spec)
    except QuickstartError as exc:
        raise CLIError(str(exc)) from exc

    # Wire the memory service unless the caller opted out. The generated client
    # key is what memgw must present as MODEL_GATEWAY_API_KEY; both sides keep
    # independent secret files and the value is never echoed.
    memgw_wired = False
    if spec.connect_memory:
        memgw = Path(args.memgw).expanduser() if args.memgw else _find_memgw()
        if memgw is None or not Path(memgw).is_file():
            result.warnings.append(
                "没有找到记忆服务 memgw；模型服务已配置，可稍后用 memgw stack install 连接。"
            )
        else:
            base_url = _server_url(load_config(paths.config).server) + "/v1"
            if _run_memgw(memgw, ["config", "set", "MODEL_GATEWAY_BASE_URL", base_url]) != 0:
                result.warnings.append("记忆服务没有接受模型服务地址。")
            elif _run_memgw(
                memgw,
                ["secret", "set", "model-gateway", "--stdin", "--no-check"],
                input_text=result.memory_client_key + "\n",
            ) != 0:
                result.warnings.append("记忆服务没有保存 backend key；可稍后重试。")
            else:
                memgw_wired = True
                if result.embedding_deployment_id:
                    _run_memgw(
                        memgw,
                        ["config", "set", "MODEL_GATEWAY_EMBEDDING_SPACE_ID", result.embedding_space],
                    )
                    _run_memgw(
                        memgw,
                        ["config", "set", "EMBEDDING_DIMENSIONS", str(result.embedding_dimensions)],
                    )

    started = False
    if not args.no_start:
        start_args = argparse.Namespace(
            home=getattr(args, "home", ""),
            json=False,
            host=None,
            port=None,
            log_level="info",
            access_log=False,
        )
        if _cmd_start(start_args) == 0:
            started = True
        if memgw_wired:
            _run_memgw(
                Path(args.memgw).expanduser() if args.memgw else _find_memgw(),
                ["restart"],
            )

    payload = {
        "connection_id": result.connection_id,
        "chat_deployment_id": result.chat_deployment_id,
        "chat_routes": list(result.chat_routes),
        "embedding_deployment_id": result.embedding_deployment_id,
        "memgw_wired": memgw_wired,
        "started": started,
        "warnings": result.warnings,
    }
    if args.json:
        _json(payload)
    else:
        print("")
        print("模型服务已配置")
        print("-" * 36)
        print(f"渠道：{spec.channel_operator}  ·  聊天模型：{spec.chat_model}")
        print(f"已安排全部 {len(result.chat_routes)} 项文字用途使用该模型。")
        if result.embedding_deployment_id:
            print(f"向量模型：{spec.embedding_model}（{result.embedding_dimensions} 维）")
        if memgw_wired:
            print("已连接记忆服务；两端使用独立密钥文件，密钥未显示。")
        elif spec.connect_memory:
            print("模型服务已就绪，但尚未连接记忆服务（见下方提示）。")
        for warning in result.warnings:
            print(f"注意：{warning}")
        if started:
            print("模型服务已启动。")
    return 0


def _quickstart_prompt(args: argparse.Namespace) -> Any:
    from model_gateway.quickstart import QuickstartSpec

    print("一步完成首次配置：一个渠道、一个聊天模型、全部文字用途。")
    channel = (args.channel or input("渠道英文简称（例如 deepseek）：").strip()).strip()
    base_url = (args.base_url or input("官方 OpenAI 兼容 API 地址（HTTPS）：").strip()).strip()
    chat_model = (
        args.chat_model or input("供应商页面显示的精确聊天模型 ID：").strip()
    ).strip()
    api_key = getpass.getpass("该渠道的 API Key（输入时不会显示）：").strip()
    embedding_model = args.embedding_model
    embedding_dimensions = args.embedding_dimensions
    embedding_space = args.embedding_space
    if not embedding_model and input("要现在配置向量（语义搜索）模型吗？[y/N] ").strip().lower() in {
        "y",
        "yes",
        "是",
    }:
        embedding_model = input("向量模型 ID：").strip()
        if embedding_model:
            raw_dimensions = input("向量维度（例如 1024）：").strip()
            if not raw_dimensions.isdecimal() or int(raw_dimensions) < 1:
                raise CLIError("向量维度必须是正整数")
            embedding_dimensions = int(raw_dimensions)
            embedding_space = input("向量空间名称（换模型或维度时必须换名）：").strip()
    return QuickstartSpec(
        channel_operator=channel,
        base_url=base_url,
        chat_model=chat_model,
        api_key=api_key,
        adapter=args.adapter,
        plan=args.plan,
        chat_author=args.chat_author,
        chat_capabilities=tuple(dict.fromkeys(args.chat_capability)),
        reasoning_default=args.reasoning_default,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        embedding_space=embedding_space,
        embedding_author=args.embedding_author,
        connect_memory=not args.no_connect_memory,
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    paths = _paths(args)
    initialize(paths)
    config = load_config(paths.config)
    server = ServerConfig(
        host=args.host if args.host is not None else config.server.host,
        port=args.port if args.port is not None else config.server.port,
        body_limit_bytes=config.server.body_limit_bytes,
    )
    if args.json:
        raise CLIError("serve 不能与 --json 一起使用")
    from model_gateway.service import create_app
    import uvicorn

    print(f"Model Gateway 正在启动：http://{server.host}:{server.port}")
    print(f"配置目录：{paths.home}")
    app = create_app(paths=paths)
    uvicorn.run(
        app,
        host=server.host,
        port=server.port,
        log_level=args.log_level,
        access_log=not args.no_access_log,
    )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    paths = _paths(args)
    initialize(paths)
    config = load_config(paths.config)
    server = ServerConfig(
        host=args.host or config.server.host,
        port=args.port or config.server.port,
        body_limit_bytes=config.server.body_limit_bytes,
    )
    existing = _read_state(paths)
    if existing and _state_process_matches(existing, paths):
        payload = {
            "status": "already_running",
            "pid": int(existing["pid"]),
            "url": str(existing.get("url") or _server_url(server)),
        }
        if args.json:
            _json(payload)
        else:
            print(f"Model Gateway 已在运行（PID {payload['pid']}）：{payload['url']}")
        return 0

    url = _server_url(server)
    if _gateway_responding(url):
        raise CLIError(
            f"{url} 已有网关响应，但不是当前状态文件管理的进程；"
            "为避免误杀，请手工确认该进程"
        )
    command = [
        sys.executable,
        "-m",
        "model_gateway.cli",
        "--home",
        str(paths.home.resolve()),
        "serve",
        "--host",
        server.host,
        "--port",
        str(server.port),
        "--log-level",
        args.log_level,
    ]
    if not args.access_log:
        command.append("--no-access-log")
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    with paths.log.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
            close_fds=True,
        )
    state = {
        "schema_version": 1,
        "pid": process.pid,
        "started_at": datetime.now(UTC).isoformat(),
        "url": url,
        "home": str(paths.home.resolve()),
        "python": str(Path(sys.executable).resolve()),
    }
    _write_state(paths, state)

    deadline = time.monotonic() + 8.0
    healthy = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            paths.state.unlink(missing_ok=True)
            raise CLIError(
                f"后台网关启动失败（exit={process.returncode}）；请查看 {paths.log}"
            )
        if _gateway_responding(url):
            healthy = True
            break
        time.sleep(0.1)
    payload = {
        "status": "running" if healthy else "starting",
        "pid": process.pid,
        "url": url,
        "log": str(paths.log),
    }
    if args.json:
        _json(payload)
    else:
        print(f"Model Gateway 已后台启动（PID {process.pid}）：{url}")
        print(f"日志：{paths.log}")
        if not healthy:
            print("健康端点尚未就绪；请运行 modelgw status 再检查。")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    if args.timeout < 0 or args.timeout > 60:
        raise CLIError("--timeout 必须在 0 到 60 秒之间")
    paths = _paths(args)
    state = _read_state(paths)
    if not state or not _state_process_matches(state, paths):
        payload = {"status": "not_running"}
        if args.json:
            _json(payload)
        else:
            print("Model Gateway 没有由当前配置目录管理的后台进程。")
        return 0
    pid = int(state["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        if not args.force:
            raise CLIError(
                f"PID {pid} 在 {args.timeout:g} 秒内未退出；确认强制终止请加 --force"
            )
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    paths.state.unlink(missing_ok=True)
    payload = {"status": "stopped", "pid": pid}
    if args.json:
        _json(payload)
    else:
        print(f"Model Gateway 已停止（PID {pid}）。")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = _read_state(paths)
    managed = bool(state and _state_process_matches(state, paths))
    url = str(state.get("url") or "") if state else ""
    healthy = bool(url and _gateway_responding(url))
    payload = {
        "status": "running" if managed and healthy else "starting" if managed else "stopped",
        "managed_process": managed,
        "http_healthy": healthy,
        "pid": int(state["pid"]) if state and str(state.get("pid", "")).isdigit() else None,
        "url": url,
        "state_file": str(paths.state),
        "log": str(paths.log),
    }
    if args.json:
        _json(payload)
    else:
        if managed:
            label = "正常" if healthy else "进程存在，HTTP 尚未就绪"
            print(f"Model Gateway：{label}（PID {payload['pid']}）")
            print(f"地址：{url}")
        else:
            print("Model Gateway：未运行（或状态文件已失效）")
        print(f"日志：{paths.log}")
    return 0 if managed and healthy else 1


def _cmd_logs(args: argparse.Namespace) -> int:
    if args.lines < 0 or args.lines > 100_000:
        raise CLIError("--lines 必须在 0 到 100000 之间")
    paths = _paths(args)
    if not paths.log.exists():
        raise CLIError(f"日志文件不存在：{paths.log}")
    with paths.log.open("r", encoding="utf-8", errors="replace") as handle:
        recent = deque(handle, maxlen=args.lines) if args.lines else deque()
        for line in recent:
            print(line, end="")
        if not args.follow:
            return 0
        while True:
            line = handle.readline()
            if line:
                print(line, end="", flush=True)
            else:
                time.sleep(0.5)


def _cmd_doctor(args: argparse.Namespace) -> int:
    paths = _paths(args)
    checks: list[dict[str, str]] = []
    config: GatewayConfig | None = None
    if not paths.home.exists():
        checks.append(_check("home", "error", f"配置目录不存在：{paths.home}；先运行 modelgw init"))
    else:
        checks.append(_check("home", "ok", str(paths.home)))
    try:
        config = load_config(paths.config)
        checks.append(_check("config", "ok", "JSON 与配置关系校验正常"))
    except (ConfigError, ValueError) as exc:
        checks.append(_check("config", "error", _clean_error(exc)))

    secret_values = read_secrets(paths.secrets)
    if paths.secrets.exists():
        checks.append(_check("secrets", "ok", f"已保存 {len(secret_values)} 个密钥（值不显示）"))
    else:
        checks.append(_check("secrets", "error", f"密钥文件不存在：{paths.secrets}"))

    if os.name != "nt":
        _append_mode_check(checks, paths.home, expected=0o700, label="home_permissions")
        _append_mode_check(checks, paths.config, expected=0o600, label="config_permissions")
        _append_mode_check(checks, paths.secrets, expected=0o600, label="secret_permissions")

    if config is not None:
        references = _secret_references(config)
        missing = sorted(reference for reference in references if not secret_values.get(reference))
        if missing:
            checks.append(
                _check(
                    "secret_references",
                    "warning",
                    "缺少：" + ", ".join(missing),
                )
            )
        else:
            checks.append(_check("secret_references", "ok", "所有已引用密钥均已配置"))
        client_ids_by_secret: dict[str, list[str]] = {}
        for client_id, client in config.clients.items():
            value = secret_values.get(client.secret_ref, "")
            if value:
                client_ids_by_secret.setdefault(value, []).append(client_id)
        duplicate_clients = [
            sorted(client_ids)
            for client_ids in client_ids_by_secret.values()
            if len(client_ids) > 1
        ]
        if duplicate_clients:
            checks.append(
                _check(
                    "client_secret_uniqueness",
                    "error",
                    "以下 client 复用了同一密钥："
                    + "; ".join(", ".join(group) for group in duplicate_clients),
                )
            )
        else:
            checks.append(
                _check(
                    "client_secret_uniqueness",
                    "ok",
                    "每个已配置 client 使用独立密钥",
                )
            )
        if not config.routes:
            checks.append(_check("routes", "warning", "尚未配置功能路由"))
        else:
            checks.append(_check("routes", "ok", f"{len(config.routes)} 条 route"))
        unpriced = sorted(
            deployment_id
            for deployment_id, deployment in config.deployments.items()
            if deployment.enabled and deployment.pricing is None
        )
        if unpriced:
            checks.append(
                _check(
                    "pricing",
                    "warning",
                    "以下 deployment 尚无官方价格快照：" + ", ".join(unpriced),
                )
            )
        elif config.deployments:
            checks.append(_check("pricing", "ok", "所有已启用 deployment 均已绑定价格快照"))

    try:
        import uvicorn  # noqa: F401

        checks.append(_check("runtime", "ok", f"Python {sys.version_info.major}.{sys.version_info.minor}"))
    except ImportError:
        checks.append(_check("runtime", "error", "缺少 uvicorn；重新安装项目依赖"))

    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    if args.json:
        _json({"ok": errors == 0, "errors": errors, "warnings": warnings, "checks": checks})
    else:
        labels = {"ok": "正常", "warning": "警告", "error": "错误"}
        for item in checks:
            print(f"[{labels[item['status']]}] {item['name']}: {item['detail']}")
        print(f"检查完成：{errors} 个错误，{warnings} 个警告。")
    return 1 if errors else 0


def _cmd_secret_set(args: argparse.Namespace) -> int:
    paths = _paths(args)
    initialize(paths)
    config = load_config(paths.config)
    secret_ref = _resolve_secret_ref(config, args.name)
    if args.value is not None:
        value = args.value
    elif args.stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        value = getpass.getpass(f"{secret_ref} API Key：")
    if not value:
        raise CLIError("密钥不能为空")
    set_secret(paths.secrets, secret_ref, value)
    payload: dict[str, Any] = {"saved": True, "secret_ref": secret_ref, "checks": []}
    connections = [
        connection_id
        for connection_id, connection in config.connections.items()
        if connection.auth.secret_ref == secret_ref
    ]
    if not args.json:
        print(f"已安全保存 {secret_ref}；密钥值不会回显，也不会写入项目 .env。")
        if connections and not args.no_check:
            if args.live:
                print("正在执行显式请求的真实最小检查；可能产生费用……")
            else:
                print("正在检查连接（只读取 /models，不发送付费推理）……")
    if not args.no_check and connections:
        results = asyncio.run(
            _run_probes(
                config=config,
                secret_values=read_secrets(paths.secrets),
                connection_ids=connections,
                live=args.live,
                client_kind="interactive" if args.as_interactive else "backend",
            )
        )
        payload["checks"] = [_probe_dict(result) for result in results]
    if args.json:
        _json(payload)
    else:
        if connections and not args.no_check:
            _print_probe_results(results)
        elif not connections:
            print("该 secret_ref 尚未被 connection 引用，已跳过连接检查。")
    return 0


def _cmd_secret_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    initialize(paths)
    config = load_config(paths.config)
    secret_values = read_secrets(paths.secrets)
    references = _secret_references(config)
    names = sorted(set(secret_values) | set(references))
    records = [
        {
            "secret_ref": name,
            "used_by": ", ".join(references.get(name, [])) or "-",
            "configured": bool(secret_values.get(name)),
        }
        for name in names
    ]
    if args.json:
        _json(records)
    else:
        _table(["SECRET_REF", "USED_BY", "STATUS"], [
            [
                record["secret_ref"],
                record["used_by"],
                "configured" if record["configured"] else "missing",
            ]
            for record in records
        ])
        if not records:
            print("尚未保存任何密钥。")
        else:
            print(f"共 {len(records)} 个密钥引用；值已隐藏。")
    return 0


def _cmd_secret_delete(args: argparse.Namespace) -> int:
    paths = _paths(args)
    initialize(paths)
    config = load_config(paths.config)
    secret_ref = _resolve_secret_ref(config, args.name)
    existed = secret_ref in read_secrets(paths.secrets)
    set_secret(paths.secrets, secret_ref, None)
    payload = {"deleted": existed, "secret_ref": secret_ref}
    if args.json:
        _json(payload)
    else:
        print(
            f"已删除 {secret_ref}；相关连接会显示为未配置。"
            if existed
            else f"{secret_ref} 原本就没有保存。"
        )
    return 0


def _cmd_client_add(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    client_id = validate_id(args.client_id, "client")
    if client_id in config.clients and not args.replace:
        raise CLIError(f"client 已存在：{client_id}；如需替换请加 --replace")
    secret_ref = validate_id(
        args.secret_name or _default_secret_ref("CLIENT", client_id), "secret_ref"
    )
    routes = _split_values(args.route) or ["*"]
    client = ClientConfig(
        kind=args.kind,
        secret_ref=secret_ref,
        allowed_routes=routes,
        allow_direct_deployments=args.allow_direct_deployments,
        enabled=not args.disabled,
    )
    _replace_config_item(paths, config, "clients", client_id, client)
    if args.set_secret:
        value = getpass.getpass(f"{secret_ref} 本地客户端 API Key：")
        if not value:
            raise CLIError("客户端密钥不能为空；client 配置已保存，可稍后运行 secret set")
        set_secret(paths.secrets, secret_ref, value)
    payload = {
        "client_id": client_id,
        "secret_ref": secret_ref,
        "secret_configured": bool(read_secrets(paths.secrets).get(secret_ref)),
    }
    if args.json:
        _json(payload)
    else:
        print(f"已保存 client：{client_id}（{client.kind}）")
        print(f"密钥引用：{secret_ref}（值不回显）")
        if not payload["secret_configured"]:
            print(f"下一步：modelgw secret set {client_id}")
    return 0


def _cmd_client_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    secret_values = read_secrets(paths.secrets)
    records = [
        {
            "id": client_id,
            "kind": client.kind,
            "enabled": client.enabled,
            "allowed_routes": client.allowed_routes,
            "allow_direct_deployments": client.allow_direct_deployments,
            "secret_ref": client.secret_ref,
            "secret_configured": bool(secret_values.get(client.secret_ref)),
        }
        for client_id, client in sorted(config.clients.items())
    ]
    if args.json:
        _json(records)
    else:
        _table(
            ["CLIENT", "KIND", "ROUTES", "DIRECT", "SECRET", "ENABLED"],
            [
                [
                    item["id"],
                    item["kind"],
                    ",".join(item["allowed_routes"]),
                    _yes_no(item["allow_direct_deployments"]),
                    "configured" if item["secret_configured"] else "missing",
                    _yes_no(item["enabled"]),
                ]
                for item in records
            ],
        )
        if not records:
            print("尚未配置 client。")
    return 0


def _cmd_client_remove(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    client_id = validate_id(args.client_id, "client")
    _remove_config_item(paths, config, "clients", client_id)
    return _print_removed(args, "client", client_id)


def _cmd_connection_add(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    connection_id = validate_id(args.connection_id, "connection")
    if connection_id in config.connections and not args.replace:
        raise CLIError(f"connection 已存在：{connection_id}；如需替换请加 --replace")
    secret_ref = validate_id(
        args.secret_name or _default_secret_ref("CONNECTION", connection_id),
        "secret_ref",
    )
    restricted = args.billing_type in {
        "token_plan",
        "coding_plan",
        "direct_tool_only",
    }
    scope = args.scope or ("interactive_only" if restricted else "backend_allowed")
    connection = ConnectionConfig(
        channel_operator=args.channel_operator,
        adapter=args.adapter,
        base_url=args.base_url,
        auth=AuthConfig(type=args.auth_type, secret_ref=secret_ref),
        billing_plan=BillingPlan(type=args.billing_type, name=args.plan_name),
        usage_scope=scope,
        models_endpoint=None if args.no_models_endpoint else args.models_endpoint,
        chat_endpoint=args.chat_endpoint,
        embeddings_endpoint=args.embeddings_endpoint,
        forward_headers=_split_values(args.forward_header),
        timeout_seconds=args.timeout,
        rate_limit_cooldown_seconds=args.cooldown,
        enabled=not args.disabled,
    )
    _replace_config_item(paths, config, "connections", connection_id, connection)
    payload = {
        "connection_id": connection_id,
        "channel_operator": connection.channel_operator,
        "secret_ref": secret_ref,
        "secret_configured": bool(read_secrets(paths.secrets).get(secret_ref)),
    }
    if args.json:
        _json(payload)
    else:
        print(f"已保存 connection：{connection_id}")
        print(f"渠道：{connection.channel_operator}  Base URL：{connection.base_url}")
        print(f"使用范围：{connection.usage_scope}  密钥引用：{secret_ref}")
        if not payload["secret_configured"]:
            print(f"下一步：modelgw secret set {connection_id}")
    return 0


def _cmd_connection_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    secret_values = read_secrets(paths.secrets)
    records = [
        {
            "id": connection_id,
            "channel_operator": connection.channel_operator,
            "base_url": connection.base_url,
            "adapter": connection.adapter,
            "billing_plan": connection.billing_plan.model_dump(mode="json"),
            "usage_scope": connection.usage_scope,
            "models_endpoint": connection.models_endpoint,
            "secret_ref": connection.auth.secret_ref,
            "secret_configured": bool(secret_values.get(connection.auth.secret_ref)),
            "enabled": connection.enabled,
        }
        for connection_id, connection in sorted(config.connections.items())
    ]
    if args.json:
        _json(records)
    else:
        _table(
            ["CONNECTION", "CHANNEL", "PLAN", "SCOPE", "SECRET", "ENABLED", "BASE_URL"],
            [
                [
                    item["id"],
                    item["channel_operator"],
                    item["billing_plan"]["type"],
                    item["usage_scope"],
                    "configured" if item["secret_configured"] else "missing",
                    _yes_no(item["enabled"]),
                    item["base_url"],
                ]
                for item in records
            ],
        )
        if not records:
            print("尚未配置 connection。")
    return 0


def _cmd_connection_remove(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    connection_id = validate_id(args.connection_id, "connection")
    users = sorted(
        deployment_id
        for deployment_id, deployment in config.deployments.items()
        if deployment.connection == connection_id
    )
    if users:
        raise CLIError(
            "connection 仍被 deployment 引用：" + ", ".join(users)
        )
    _remove_config_item(paths, config, "connections", connection_id)
    return _print_removed(args, "connection", connection_id)


def _cmd_connection_check(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    requested = list(getattr(args, "connection_ids", []) or [])
    connection_ids = requested or list(config.connections)
    unknown = sorted(set(connection_ids) - set(config.connections))
    if unknown:
        raise CLIError("未知 connection：" + ", ".join(unknown))
    live = bool(getattr(args, "live", False))
    if not args.json:
        if live:
            print("正在执行真实最小请求；这些请求可能产生费用……")
        else:
            print("正在读取 provider /models；不会发送推理请求……")
    results = asyncio.run(
        _run_probes(
            config=config,
            secret_values=read_secrets(paths.secrets),
            connection_ids=connection_ids,
            live=live,
            client_kind=(
                "interactive" if getattr(args, "as_interactive", False) else "backend"
            ),
        )
    )
    summary = _probe_summary(results)
    if args.json:
        _json({"live": live, "summary": summary, "results": [_probe_dict(item) for item in results]})
    else:
        _print_probe_results(results)
    return 1 if summary["failed"] else 0


def _cmd_deployment_add(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    deployment_id = validate_id(args.deployment_id, "deployment")
    if deployment_id in config.deployments and not args.replace:
        raise CLIError(f"deployment 已存在：{deployment_id}；如需替换请加 --replace")
    connection = config.connections.get(args.connection)
    if connection is None:
        raise CLIError(f"未知 connection：{args.connection}")
    enabled_capabilities = set(args.capability)
    if "parallel_tools" in enabled_capabilities:
        enabled_capabilities.add("tools")
    capabilities = Capabilities(
        streaming=args.kind == "chat" and not args.no_streaming,
        tools="tools" in enabled_capabilities,
        parallel_tools="parallel_tools" in enabled_capabilities,
        reasoning="reasoning" in enabled_capabilities,
        multimodal_input="multimodal_input" in enabled_capabilities,
        json_object="json_object" in enabled_capabilities,
        json_schema="json_schema" in enabled_capabilities,
    )
    transform = RequestTransform(
        remove=_split_values(args.remove),
        set_if_missing=_parse_json_assignments(args.set_if_missing),
        force=_parse_json_assignments(args.force_param),
    )
    deployment = DeploymentConfig(
        connection=args.connection,
        upstream_model=args.upstream_model,
        model_author=args.author or connection.channel_operator,
        model_family=args.family,
        kind=args.kind,
        reasoning_default=args.reasoning_default,
        capabilities=capabilities,
        request_transform=transform,
        dimensions=args.dimensions,
        embedding_space=args.embedding_space,
        pricing=args.pricing,
        enabled=not args.disabled,
    )
    _replace_config_item(paths, config, "deployments", deployment_id, deployment)
    payload = {"deployment_id": deployment_id, **deployment.model_dump(mode="json")}
    if args.json:
        _json(payload)
    else:
        print(f"已保存 deployment：{deployment_id}")
        print(
            f"{deployment.connection} / {deployment.upstream_model} / {deployment.kind}"
        )
    return 0


def _cmd_deployment_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    records = []
    for deployment_id, deployment in sorted(config.deployments.items()):
        capabilities = [
            name
            for name, enabled in deployment.capabilities.model_dump().items()
            if enabled
        ]
        records.append(
            {
                "id": deployment_id,
                **deployment.model_dump(mode="json"),
                "enabled_capabilities": capabilities,
            }
        )
    if args.json:
        _json(records)
    else:
        _table(
            [
                "DEPLOYMENT",
                "CONNECTION",
                "UPSTREAM_MODEL",
                "KIND",
                "REASONING_DEFAULT",
                "CAPABILITIES",
                "ENABLED",
            ],
            [
                [
                    item["id"],
                    item["connection"],
                    item["upstream_model"],
                    item["kind"],
                    item["reasoning_default"],
                    ",".join(item["enabled_capabilities"]) or "-",
                    _yes_no(item["enabled"]),
                ]
                for item in records
            ],
        )
        if not records:
            print("尚未配置 deployment。")
    return 0


def _cmd_deployment_remove(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    deployment_id = validate_id(args.deployment_id, "deployment")
    users = sorted(
        route_id
        for route_id, route in config.routes.items()
        if deployment_id in route.targets
    )
    if users:
        raise CLIError("deployment 仍被 route 引用：" + ", ".join(users))
    _remove_config_item(paths, config, "deployments", deployment_id)
    return _print_removed(args, "deployment", deployment_id)


def _cmd_route_set(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    route_id = validate_id(args.route_id, "route")
    targets = _split_values(args.targets)
    if not targets:
        raise CLIError("route 至少需要一个 deployment")
    missing = [target for target in targets if target not in config.deployments]
    if missing:
        raise CLIError("未知 deployment：" + ", ".join(missing))
    kinds = {config.deployments[target].kind for target in targets}
    if len(kinds) != 1:
        raise CLIError("同一 route 的 deployments 必须具有相同 kind")
    inferred_kind = next(iter(kinds))
    if args.kind and args.kind != inferred_kind:
        raise CLIError(f"指定 kind={args.kind}，但 deployment kind={inferred_kind}")
    route = RouteConfig(
        kind=args.kind or inferred_kind,
        targets=targets,
        required_capabilities=list(dict.fromkeys(args.require)),
        max_attempts=args.max_attempts,
        enabled=not args.disabled,
    )
    _replace_config_item(paths, config, "routes", route_id, route)
    payload = {"route_id": route_id, **route.model_dump(mode="json")}
    if args.json:
        _json(payload)
    else:
        print(f"已保存 route：{route_id} ({route.kind})")
        print("优先级：" + " → ".join(route.targets))
    return 0


def _cmd_route_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    records = [
        {"id": route_id, **route.model_dump(mode="json")}
        for route_id, route in sorted(config.routes.items())
    ]
    if args.json:
        _json(records)
    else:
        _table(
            ["ROUTE", "KIND", "ORDERED_TARGETS", "REQUIRES", "ATTEMPTS", "ENABLED"],
            [
                [
                    item["id"],
                    item["kind"],
                    " > ".join(item["targets"]),
                    ",".join(item["required_capabilities"]) or "-",
                    item["max_attempts"],
                    _yes_no(item["enabled"]),
                ]
                for item in records
            ],
        )
        if not records:
            print("尚未配置 route。")
    return 0


def _cmd_route_remove(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    route_id = validate_id(args.route_id, "route")
    _remove_config_item(paths, config, "routes", route_id)
    return _print_removed(args, "route", route_id)


def _cmd_pricing_set(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    pricing_id = validate_id(args.pricing_id, "pricing")
    rate_values = (args.input, args.cached_input, args.output)
    tiers: list[PricingTier] = []
    if args.tier and (any(value is not None for value in rate_values) or args.max_input_tokens):
        raise CLIError("--tier 不能与 --input/--cached-input/--output/--max-input-tokens 混用")
    if args.tier:
        for raw_tier in args.tier:
            try:
                value = json.loads(raw_tier)
            except json.JSONDecodeError as exc:
                raise CLIError(f"--tier 不是合法 JSON：{exc.msg}") from exc
            if not isinstance(value, dict):
                raise CLIError("--tier JSON 顶层必须是对象")
            tiers.append(PricingTier.model_validate(value))
    elif any(value is not None for value in rate_values):
        tiers.append(
            PricingTier(
                max_input_tokens=args.max_input_tokens,
                input=args.input,
                cached_input=args.cached_input,
                output=args.output,
            )
        )
    pricing = PricingConfig(
        mode=args.mode,
        currency=args.currency,
        unit_tokens=args.unit_tokens,
        tiers=tiers,
        source_url=args.source_url,
        effective_from=args.effective_from,
        checked_at=args.checked_at or datetime.now(UTC).date().isoformat(),
        notes=args.notes,
    )
    deployment_ids = _split_values(args.deployment)
    unknown = [item for item in deployment_ids if item not in config.deployments]
    if unknown:
        raise CLIError("未知 deployment：" + ", ".join(unknown))

    payload = config.model_dump(mode="python")
    pricing_records = dict(payload["pricing"])
    pricing_records[pricing_id] = pricing.model_dump(mode="python")
    payload["pricing"] = pricing_records
    if deployment_ids:
        deployments = dict(payload["deployments"])
        for deployment_id in deployment_ids:
            deployment = dict(deployments[deployment_id])
            deployment["pricing"] = pricing_id
            deployments[deployment_id] = deployment
        payload["deployments"] = deployments
    validated = GatewayConfig.model_validate(payload)
    write_config(paths.config, validated)

    record = {"pricing_id": pricing_id, **pricing.model_dump(mode="json")}
    record["bound_deployments"] = deployment_ids
    if args.json:
        _json(record)
    else:
        print(f"已保存 pricing：{pricing_id}（{pricing.mode}，{pricing.currency}）")
        if len(pricing.tiers) == 1:
            tier = pricing.tiers[0]
            print(
                f"每 {pricing.unit_tokens} tokens：input={tier.input}，"
                f"cached_input={tier.cached_input}，output={tier.output}"
            )
        elif pricing.tiers:
            print(f"已记录 {len(pricing.tiers)} 个按输入 Token 上限递增的价格分档。")
        print(f"官方来源：{pricing.source_url or '未提供'}")
        print(f"核对时间：{pricing.checked_at}")
        if deployment_ids:
            print("已绑定：" + ", ".join(deployment_ids))
    return 0


def _cmd_pricing_research(args: argparse.Namespace) -> int:
    if args.yes and not args.apply:
        raise CLIError("--yes 只能与 --apply 一起使用；默认研究不会写配置")
    if args.replace and not args.apply:
        raise CLIError("--replace 只能与 --apply 一起使用")
    if args.json and args.apply and not args.yes:
        raise CLIError("--json --apply 必须同时使用 --yes，避免交互提示破坏 JSON 输出")
    paths = _paths(args)
    config = _load_initialized(paths)
    target_id = validate_id(args.target_deployment, "deployment")
    research_id = validate_id(args.research_deployment, "deployment")
    try:
        outcome = asyncio.run(
            research_pricing(
                config=config,
                secrets=read_secrets(paths.secrets),
                target_deployment_id=target_id,
                research_deployment_id=research_id,
                source_url=args.source_url,
                confirmed_official_host=args.official_host,
            )
        )
    except PricingResearchCallError as exc:
        _record_pricing_research_usage(
            paths=paths,
            config=config,
            research_deployment_id=research_id,
            metadata=exc.metadata,
        )
        raise
    if outcome.research_call is not None:
        _record_pricing_research_usage(
            paths=paths,
            config=config,
            research_deployment_id=research_id,
            metadata=outcome.research_call,
        )
    pricing_id = (
        validate_id(args.pricing_id, "pricing")
        if args.pricing_id
        else _default_research_pricing_id(target_id)
    )
    applied = False
    preview_printed = False
    if args.apply:
        if outcome.status != "candidate" or outcome.pricing is None:
            raise CLIError("研究结果为 unknown，没有可安全应用的价格候选")
        if pricing_id in config.pricing and not args.replace:
            raise CLIError(f"pricing 已存在：{pricing_id}；确认替换请加 --replace")
        if not args.yes:
            phrase = f"APPLY {pricing_id} TO {target_id}"
            _print_pricing_research(
                {
                    **outcome.as_dict(),
                    "pricing_id": pricing_id,
                    "applied": False,
                }
            )
            preview_printed = True
            print("请逐项核对官方来源、币种、单位和分档。")
            try:
                confirmation = input(f"确认应用请输入 `{phrase}`：").strip()
            except EOFError as exc:
                raise CLIError("未收到应用确认，配置保持不变") from exc
            if confirmation != phrase:
                raise CLIError("确认文本不匹配，配置保持不变")
        _apply_researched_pricing(
            paths=paths,
            config=config,
            pricing_id=pricing_id,
            target_deployment_id=target_id,
            outcome=outcome,
        )
        applied = True

    record = {
        **outcome.as_dict(),
        "pricing_id": pricing_id,
        "applied": applied,
    }
    if args.json:
        _json(record)
    elif preview_printed and applied:
        print(f"已保存 pricing：{pricing_id}，并绑定到 {target_id}。")
    else:
        _print_pricing_research(record)
    return 0


def _apply_researched_pricing(
    *,
    paths: GatewayPaths,
    config: GatewayConfig,
    pricing_id: str,
    target_deployment_id: str,
    outcome: PricingResearchOutcome,
) -> None:
    if outcome.pricing is None:
        raise CLIError("研究结果没有 PricingConfig 候选")
    # Validate the exact candidate again together with the entire relationship
    # graph immediately before the sole mutating operation.
    pricing = PricingConfig.model_validate(outcome.pricing.model_dump(mode="python"))
    payload = config.model_dump(mode="python")
    pricing_records = dict(payload["pricing"])
    pricing_records[pricing_id] = pricing.model_dump(mode="python")
    payload["pricing"] = pricing_records
    deployments = dict(payload["deployments"])
    target = dict(deployments[target_deployment_id])
    target["pricing"] = pricing_id
    deployments[target_deployment_id] = target
    payload["deployments"] = deployments
    validated = GatewayConfig.model_validate(payload)
    write_config(paths.config, validated)


def _record_pricing_research_usage(
    *,
    paths: GatewayPaths,
    config: GatewayConfig,
    research_deployment_id: str,
    metadata: ResearchCallMetadata,
) -> None:
    deployment = config.deployments.get(research_deployment_id)
    if deployment is None:
        return
    connection = config.connections.get(deployment.connection)
    if connection is None:
        return
    target = RouteTarget(
        route_id="pricing.research",
        deployment_id=research_deployment_id,
        deployment=deployment,
        connection_id=deployment.connection,
        connection=connection,
    )
    # Feed UsageCapture a synthetic metadata-only envelope. The source page,
    # request messages and model answer never enter UsageStore's API.
    capture = UsageCapture()
    capture.from_non_stream(
        json.dumps(
            {
                "usage": metadata.usage,
                "model": metadata.response_model,
                "request_id": metadata.request_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )
    pricing_id = deployment.pricing or ""
    pricing = config.pricing.get(pricing_id) if pricing_id else None
    store = UsageStore(paths.usage_db)
    store.init_db()
    store.record(
        client_id="modelgw-pricing-research",
        kind="chat",
        route_id="pricing.research",
        target=target,
        status_code=metadata.status_code,
        latency_ms=metadata.latency_ms,
        attempts=1,
        complete=200 <= metadata.status_code < 300,
        capture=capture,
        pricing_id=pricing_id,
        pricing=pricing,
    )


def _default_research_pricing_id(target_deployment_id: str) -> str:
    date = datetime.now(UTC).date().isoformat()
    candidate = f"{target_deployment_id}-{date}"
    if len(candidate) <= 120:
        return candidate
    digest = sha256(target_deployment_id.encode("utf-8")).hexdigest()[:8]
    return f"{target_deployment_id[:90]}-{digest}-{date}"


def _print_pricing_research(record: Mapping[str, Any]) -> None:
    print(
        f"价格研究：{record['target_deployment']} / "
        f"{record['status']} / source={record['source_host']}"
    )
    candidate = record.get("candidate")
    if isinstance(candidate, dict):
        print(
            f"候选：mode={candidate['mode']}，currency={candidate['currency']}，"
            f"unit_tokens={candidate['unit_tokens']}"
        )
        for index, tier in enumerate(candidate.get("tiers", []), start=1):
            print(
                f"tier {index}: max_input_tokens={tier.get('max_input_tokens')}，"
                f"input={tier.get('input')}，cached_input={tier.get('cached_input')}，"
                f"output={tier.get('output')}"
            )
        for quote in record.get("evidence", []):
            print(f"证据：{quote}")
    elif record.get("reason"):
        print(f"原因：{record['reason']}")
    if record.get("applied"):
        print(
            f"已保存 pricing：{record['pricing_id']}，并绑定到 "
            f"{record['target_deployment']}。"
        )
    else:
        print("仅显示候选，配置未修改；核对后可加 --apply 并明确确认。")


def _cmd_pricing_list(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    bindings: dict[str, list[str]] = {}
    for deployment_id, deployment in config.deployments.items():
        if deployment.pricing:
            bindings.setdefault(deployment.pricing, []).append(deployment_id)
    records: list[dict[str, Any]] = []
    for pricing_id, pricing in sorted(config.pricing.items()):
        record = {"id": pricing_id, **pricing.model_dump(mode="json")}
        record["deployments"] = bindings.get(pricing_id, [])
        records.append(record)
    if args.json:
        _json(records)
    else:
        rows: list[list[Any]] = []
        for record in records:
            tier = record["tiers"][0] if record["tiers"] else {}
            rates = (
                "/".join(
                    str(tier[name]) if tier.get(name) is not None else "-"
                    for name in ("input", "cached_input", "output")
                )
                if len(record["tiers"]) <= 1
                else f"{len(record['tiers'])} tiers"
            )
            rows.append(
                [
                    record["id"],
                    record["mode"],
                    record["currency"],
                    rates,
                    record["unit_tokens"],
                    record["checked_at"] or "-",
                    ",".join(record["deployments"]) or "-",
                    record["source_url"] or "-",
                ]
            )
        _table(
            ["PRICING", "MODE", "CCY", "INPUT/CACHED/OUTPUT", "UNIT", "CHECKED", "DEPLOYMENTS", "SOURCE"],
            rows,
        )
        if not records:
            print("尚未配置 pricing。")
    return 0


def _cmd_pricing_remove(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _load_initialized(paths)
    pricing_id = validate_id(args.pricing_id, "pricing")
    users = sorted(
        deployment_id
        for deployment_id, deployment in config.deployments.items()
        if deployment.pricing == pricing_id
    )
    if users:
        raise CLIError("pricing 仍被 deployment 引用：" + ", ".join(users))
    _remove_config_item(paths, config, "pricing", pricing_id)
    return _print_removed(args, "pricing", pricing_id)


def _cmd_usage_summary(args: argparse.Namespace) -> int:
    if args.days < 1:
        raise CLIError("--days 必须大于等于 1")
    paths = _paths(args)
    initialize(paths)
    store = UsageStore(paths.usage_db)
    store.init_db()
    summary = store.summary(days=args.days)
    if args.json:
        _json(summary)
    else:
        print(
            f"最近 {summary['days']} 天：{summary['calls']} 次调用，"
            f"{summary['complete_calls']} 次完整；"
            f"输入 {summary['input_tokens']}，输出 {summary['output_tokens']}，"
            f"总计 {summary['total_tokens']} tokens。"
        )
        costs = summary.get("estimated_costs", {})
        if costs:
            print(
                "可计费金额："
                + "，".join(f"{currency} {amount}" for currency, amount in costs.items())
            )
        incomplete_cost_calls = int(summary.get("incomplete_cost_calls", 0))
        if incomplete_cost_calls:
            print(f"费用不完整：{incomplete_cost_calls} 次调用缺少明确 usage 或价格。")
        _table(
            [
                "DEPLOYMENT",
                "CONNECTION",
                "CHANNEL",
                "AUTHOR",
                "MODEL",
                "CALLS",
                "TOKENS",
            ],
            [
                [
                    row["deployment_id"] or "-",
                    row["connection_id"] or "-",
                    row["channel_operator"] or "-",
                    row["model_author"] or "-",
                    row["upstream_model"] or "-",
                    row["calls"],
                    row["total_tokens"],
                ]
                for row in summary["deployments"]
            ],
        )
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    del args
    _json(GatewayConfig.model_json_schema())
    return 0


def _cmd_install_path(args: argparse.Namespace) -> int:
    if os.name == "nt":
        default_target = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Programs" / "model-gateway" / "bin"
        target_dir = Path(args.target_dir).expanduser() if args.target_dir else default_target
        launcher = target_dir / "modelgw.cmd"
        content = f'@echo off\r\n"{sys.executable}" -m model_gateway.cli %*\r\n'
        _install_launcher_file(launcher, content, force=args.force)
    else:
        target_dir = Path(args.target_dir).expanduser() if args.target_dir else Path.home() / ".local" / "bin"
        launcher = target_dir / "modelgw"
        source_script = Path(__file__).resolve().parent.parent / "scripts" / "modelgw"
        if source_script.exists():
            _install_symlink(launcher, source_script, force=args.force)
        else:
            content = f'#!/bin/sh\nexec "{sys.executable}" -m model_gateway.cli "$@"\n'
            _install_launcher_file(launcher, content, force=args.force, mode=0o755)
    in_path = _directory_in_path(target_dir)
    payload = {"launcher": str(launcher), "directory": str(target_dir), "in_path": in_path}
    if args.json:
        _json(payload)
    else:
        print(f"已安装：{launcher}")
        if in_path:
            print("该目录已在 PATH 中；现在可从任意目录运行 modelgw。")
        elif os.name == "nt":
            print(f"请把此目录加入用户 PATH：{target_dir}")
        else:
            print("请把下面一行加入 ~/.zshrc（macOS 默认 shell）：")
            print(f'export PATH="{target_dir}:$PATH"')
    return 0


def _paths(args: argparse.Namespace) -> GatewayPaths:
    return gateway_paths(args.home)


def _server_url(server: ServerConfig) -> str:
    host = f"[{server.host}]" if ":" in server.host else server.host
    return f"http://{host}:{server.port}"


def _gateway_responding(url: str) -> bool:
    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=0.5)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") in {"ok", "warning"}


def _read_state(paths: GatewayPaths) -> dict[str, Any] | None:
    if not paths.state.exists():
        return None
    try:
        payload = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return payload


def _write_state(paths: GatewayPaths, payload: Mapping[str, Any]) -> None:
    paths.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.state.with_name(f".{paths.state.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, paths.state)
    finally:
        temporary.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.name != "nt":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if result.returncode != 0 or result.stdout.strip().startswith("Z"):
            return False
    return True


def _state_process_matches(state: Mapping[str, Any], paths: GatewayPaths) -> bool:
    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not _pid_alive(pid):
        return False
    expected_home = str(paths.home.resolve())
    if str(state.get("home") or "") != expected_home:
        return False
    command = _process_command(pid)
    if command is None:
        return False
    return (
        "model_gateway.cli" in command
        and "serve" in command
        and expected_home in command
    )


def _process_command(pid: int) -> str | None:
    if os.name == "nt":
        script = (
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = "
            f"{pid}\"; if ($null -ne $p) {{ $p.CommandLine }}"
        )
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
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            command = result.stdout.strip()
            if result.returncode == 0 and command:
                return command
        return None
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command = result.stdout.strip()
    return command if result.returncode == 0 and command else None


def _load_initialized(paths: GatewayPaths) -> GatewayConfig:
    initialize(paths)
    return load_config(paths.config)


def _replace_config_item(
    paths: GatewayPaths,
    config: GatewayConfig,
    collection_name: str,
    item_id: str,
    item: Any,
) -> GatewayConfig:
    payload = config.model_dump(mode="python")
    collection = dict(payload[collection_name])
    collection[item_id] = item.model_dump(mode="python")
    payload[collection_name] = collection
    validated = GatewayConfig.model_validate(payload)
    write_config(paths.config, validated)
    return validated


def _remove_config_item(
    paths: GatewayPaths,
    config: GatewayConfig,
    collection_name: str,
    item_id: str,
) -> GatewayConfig:
    payload = config.model_dump(mode="python")
    collection = dict(payload[collection_name])
    if item_id not in collection:
        raise CLIError(f"{collection_name} 中不存在：{item_id}")
    del collection[item_id]
    payload[collection_name] = collection
    validated = GatewayConfig.model_validate(payload)
    write_config(paths.config, validated)
    return validated


def _print_removed(args: argparse.Namespace, kind: str, item_id: str) -> int:
    payload = {"removed": True, "kind": kind, "id": item_id}
    if args.json:
        _json(payload)
    else:
        print(f"已删除 {kind}：{item_id}。关联密钥未删除，可用 modelgw secret delete 单独清理。")
    return 0


def _resolve_secret_ref(config: GatewayConfig, name: str) -> str:
    normalized = name.strip()
    if normalized in config.connections:
        return config.connections[normalized].auth.secret_ref
    if normalized in config.clients:
        return config.clients[normalized].secret_ref
    return validate_id(normalized, "secret_ref")


def _default_secret_ref(prefix: str, item_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", item_id).strip("_").upper()
    value = f"{prefix}_{slug}_API_KEY"
    if len(value) <= 120:
        return value
    digest = sha256(item_id.encode("utf-8")).hexdigest()[:8].upper()
    return f"{value[:111]}_{digest}"


def _secret_references(config: GatewayConfig) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    for client_id, client in config.clients.items():
        references.setdefault(client.secret_ref, []).append(f"client:{client_id}")
    for connection_id, connection in config.connections.items():
        references.setdefault(connection.auth.secret_ref, []).append(
            f"connection:{connection_id}"
        )
    return references


async def _run_probes(
    *,
    config: GatewayConfig,
    secret_values: Mapping[str, str],
    connection_ids: Iterable[str],
    live: bool,
    client_kind: str = "backend",
) -> list[ProbeResult]:
    """Run the bundled, policy-aware health checker.

    Health checking must fail closed if that module is unavailable. Falling
    back to the old ad-hoc probe would bypass workload policy and redirect
    protections around restricted credentials.
    """
    external = await _try_external_health(
        config=config,
        secret_values=secret_values,
        connection_ids=list(connection_ids),
        live=live,
        client_kind=client_kind,
    )
    if external is None:
        raise CLIError("健康检查模块不可用；请重新安装 Model Gateway")
    return external


async def _try_external_health(
    *,
    config: GatewayConfig,
    secret_values: Mapping[str, str],
    connection_ids: list[str],
    live: bool,
    client_kind: str,
) -> list[ProbeResult] | None:
    try:
        from model_gateway.health import check_health
    except (ImportError, AttributeError):
        return None
    reports = await asyncio.gather(
        *[
            check_health(
                config=config,
                secrets=secret_values,
                connection_id=connection_id,
                live=live,
                client_kind=client_kind,
            )
            for connection_id in connection_ids
        ]
    )
    converted: list[ProbeResult] = []
    for report in reports:
        for connection in report.connections:
            if not connection.deployments:
                converted.append(
                    ProbeResult(
                        connection_id=connection.connection_id,
                        status=connection.status,
                        detail=connection.detail,
                        live=live,
                        level=connection.level,
                    )
                )
                continue
            for deployment in connection.deployments:
                converted.append(
                    ProbeResult(
                        connection_id=connection.connection_id,
                        deployment_id=deployment.deployment_id,
                        status=deployment.status,
                        detail=deployment.detail,
                        live=live,
                        level=deployment.level,
                    )
                )
    return converted


async def _probe_connection(
    *,
    connection_id: str,
    config: GatewayConfig,
    secret_values: Mapping[str, str],
    live: bool,
) -> list[ProbeResult]:
    connection = config.connections[connection_id]
    if not connection.enabled:
        return [ProbeResult(connection_id, "disabled", "connection 已禁用")]
    secret = secret_values.get(connection.auth.secret_ref, "")
    if not secret:
        return [ProbeResult(connection_id, "not_configured", "缺少上游 API Key")]
    headers = _provider_auth_headers(connection.auth.type, secret)
    timeout = httpx.Timeout(
        connect=min(connection.timeout_seconds, 15.0),
        read=min(connection.timeout_seconds, 30.0),
        write=min(connection.timeout_seconds, 30.0),
        pool=min(connection.timeout_seconds, 15.0),
    )
    model_ids: frozenset[str] = frozenset()
    results: list[ProbeResult] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        if connection.models_endpoint is None:
            results.append(
                ProbeResult(connection_id, "unsupported", "未配置 models_endpoint，无法免费探测")
            )
        else:
            url = f"{connection.base_url}{connection.models_endpoint}"
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                results.append(
                    ProbeResult(connection_id, "network_error", f"网络连接失败：{type(exc).__name__}")
                )
            else:
                if response.is_success:
                    model_ids = _extract_model_ids(response)
                    results.append(
                        ProbeResult(
                            connection_id,
                            "connected",
                            f"连接与鉴权正常；模型列表 {len(model_ids)} 项",
                            model_ids,
                        )
                    )
                elif response.status_code in {401, 403}:
                    results.append(
                        ProbeResult(connection_id, "auth_failed", f"鉴权失败（HTTP {response.status_code}）")
                    )
                else:
                    results.append(
                        ProbeResult(connection_id, "http_error", f"GET /models 返回 HTTP {response.status_code}")
                    )

        deployments = [
            (deployment_id, deployment)
            for deployment_id, deployment in config.deployments.items()
            if deployment.connection == connection_id and deployment.enabled
        ]
        if model_ids:
            for deployment_id, deployment in deployments:
                if deployment.upstream_model in model_ids:
                    continue
                results.append(
                    ProbeResult(
                        connection_id,
                        "connected_unlisted",
                        "连接与鉴权正常，但模型未出现在 /models；不能据此判定已下线",
                        model_ids,
                        deployment_id=deployment_id,
                    )
                )
        if live:
            for deployment_id, deployment in deployments:
                endpoint = (
                    connection.chat_endpoint
                    if deployment.kind == "chat"
                    else connection.embeddings_endpoint
                )
                payload: dict[str, Any]
                if deployment.kind == "chat":
                    payload = {
                        "model": deployment.upstream_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "stream": False,
                    }
                else:
                    payload = {"model": deployment.upstream_model, "input": ["ping"]}
                    if deployment.dimensions is not None:
                        payload["dimensions"] = deployment.dimensions
                try:
                    response = await client.post(
                        f"{connection.base_url}{endpoint}",
                        headers={**headers, "content-type": "application/json"},
                        json=payload,
                    )
                except httpx.HTTPError as exc:
                    results.append(
                        ProbeResult(
                            connection_id,
                            "live_failed",
                            f"真实请求网络失败：{type(exc).__name__}",
                            deployment_id=deployment_id,
                            live=True,
                        )
                    )
                else:
                    results.append(
                        ProbeResult(
                            connection_id,
                            "live_ok" if response.is_success else "live_failed",
                            (
                                "真实最小请求成功"
                                if response.is_success
                                else f"真实请求返回 HTTP {response.status_code}"
                            ),
                            deployment_id=deployment_id,
                            live=True,
                        )
                    )
    return results


def _provider_auth_headers(auth_type: str, secret: str) -> dict[str, str]:
    if auth_type == "x-api-key":
        return {"X-Api-Key": secret, "Accept": "application/json"}
    return {"Authorization": f"Bearer {secret}", "Accept": "application/json"}


def _extract_model_ids(response: httpx.Response) -> frozenset[str]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return frozenset()
    values: Any = payload.get("data") if isinstance(payload, dict) else None
    if values is None and isinstance(payload, dict):
        values = payload.get("models")
    if not isinstance(values, list):
        return frozenset()
    model_ids: set[str] = set()
    for item in values:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
            if isinstance(value, str) and value.strip():
                model_ids.add(value.strip())
    return frozenset(model_ids)


def _probe_dict(result: ProbeResult) -> dict[str, Any]:
    return {
        "connection_id": result.connection_id,
        "deployment_id": result.deployment_id,
        "status": result.status,
        "level": result.level,
        "detail": result.detail,
        "live": result.live,
        "model_ids": sorted(result.model_ids),
    }


def _probe_summary(results: Sequence[ProbeResult]) -> dict[str, int]:
    failures = {
        "auth_failed",
        "network_error",
        "http_error",
        "live_failed",
    }
    failed = sum(
        item.level == "error" if item.level else item.status in failures
        for item in results
    )
    warnings = sum(
        item.level == "warning"
        if item.level
        else item.status in {"connected_unlisted", "unsupported", "disabled"}
        for item in results
    )
    return {
        "total": len(results),
        "ok": sum(item.ok for item in results),
        "failed": failed,
        "not_configured": sum(item.status == "not_configured" for item in results),
        "warnings": warnings,
        "skipped": sum(
            item.level == "skipped" and item.status != "not_configured"
            for item in results
        ),
    }


def _print_probe_results(results: Sequence[ProbeResult]) -> None:
    labels = {
        "connected": "正常",
        "available": "正常",
        "connected_unverified": "警告",
        "connected_unlisted": "警告",
        "live_ok": "正常",
        "not_configured": "未配置",
        "disabled": "跳过",
        "unsupported": "警告",
        "check_unsupported": "警告",
        "policy_blocked": "跳过",
        "rate_limited": "警告",
        "provider_error": "失败",
        "model_not_found": "失败",
        "dimension_mismatch": "失败",
        "auth_failed": "失败",
        "network_error": "失败",
        "http_error": "失败",
        "live_failed": "失败",
    }
    for result in results:
        target = result.connection_id
        if result.deployment_id:
            target += f"/{result.deployment_id}"
        phase = "（真实请求）" if result.live else ""
        level_label = {
            "ok": "正常",
            "warning": "警告",
            "error": "失败",
            "skipped": "跳过",
        }.get(result.level)
        status_label = labels.get(result.status)
        if result.status in {"not_configured", "disabled", "policy_blocked"}:
            level_label = status_label
        print(f"[{level_label or status_label or '未知'}] {target}{phase}: {result.detail}")
    summary = _probe_summary(results)
    print(
        f"检查完成：{summary['ok']} 个正常，{summary['failed']} 个失败，"
        f"{summary['not_configured']} 个未配置，{summary['warnings']} 个警告，"
        f"{summary['skipped']} 个跳过。"
    )


def _parse_json_assignments(values: Iterable[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for assignment in values:
        if "=" not in assignment:
            raise CLIError(f"参数必须使用 KEY=JSON：{assignment}")
        key, raw = assignment.split("=", 1)
        key = key.strip()
        if not key:
            raise CLIError(f"参数名不能为空：{assignment}")
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CLIError(f"{key} 的值不是合法 JSON：{exc.msg}") from exc
    return parsed


def _split_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for part in value.split(","):
            normalized = part.strip()
            if normalized and normalized not in result:
                result.append(normalized)
    return result


def _append_mode_check(
    checks: list[dict[str, str]], path: Path, *, expected: int, label: str
) -> None:
    if not path.exists():
        return
    actual = stat.S_IMODE(path.stat().st_mode)
    status = "ok" if actual == expected else "warning"
    detail = f"{path}: {actual:04o}" if status == "ok" else f"{path}: {actual:04o}，建议 {expected:04o}"
    checks.append(_check(label, status, detail))


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _install_symlink(launcher: Path, source: Path, *, force: bool) -> None:
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher.is_symlink() and launcher.resolve() == source.resolve():
        return
    if launcher.exists() or launcher.is_symlink():
        if not force:
            raise CLIError(f"目标已存在：{launcher}；确认替换请加 --force")
        if launcher.is_dir():
            raise CLIError(f"目标是目录，拒绝替换：{launcher}")
        launcher.unlink()
    launcher.symlink_to(source)


def _install_launcher_file(
    launcher: Path, content: str, *, force: bool, mode: int = 0o600
) -> None:
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher.exists() and not force:
        raise CLIError(f"目标已存在：{launcher}；确认替换请加 --force")
    if launcher.exists() and launcher.is_dir():
        raise CLIError(f"目标是目录，拒绝替换：{launcher}")
    temporary = launcher.with_name(f".{launcher.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, mode)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)


def _directory_in_path(directory: Path) -> bool:
    target = os.path.normcase(str(directory.resolve()))
    for value in os.getenv("PATH", "").split(os.pathsep):
        if not value:
            continue
        try:
            if os.path.normcase(str(Path(value).expanduser().resolve())) == target:
                return True
        except OSError:
            continue
    return False


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        return
    string_rows = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in string_rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row, strict=True)]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    print("  ".join("-" * width for width in widths))
    for row in string_rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _clean_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return " ".join(text.split()) if text else type(exc).__name__


if __name__ == "__main__":
    raise SystemExit(main())
