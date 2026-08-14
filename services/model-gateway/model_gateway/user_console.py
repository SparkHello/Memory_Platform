from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
from hashlib import sha256
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from typing import Any, Iterable
from urllib.parse import urlparse

from pydantic import ValidationError

from model_gateway.config_store import (
    ConfigError,
    GatewayPaths,
    commit_control_plane,
    initialize,
    load_config,
    read_secrets,
    source_revision,
)
from model_gateway.models import (
    AuthConfig,
    BillingPlan,
    Capabilities,
    ClientConfig,
    ConnectionConfig,
    DeploymentConfig,
    derive_embedding_space,
    GatewayConfig,
    PricingConfig,
    PricingTier,
    RequestTransform,
    RouteConfig,
)


PURPOSES: tuple[tuple[str, str, str], ...] = (
    ("memory.chat", "日常聊天", "chat"),
    ("memory.extract", "从对话中整理长期记忆", "chat"),
    ("memory.compact", "压缩较早的对话上下文", "chat"),
    ("memory.core", "整理核心记忆", "chat"),
    ("memory.review", "检查和改进记忆", "chat"),
    ("knowledge.fast", "快速查找知识资料", "chat"),
    ("knowledge.pro", "处理复杂的知识检索", "chat"),
    ("memory.embedding", "语义搜索（向量模型）", "embedding"),
)
CHAT_PURPOSES = tuple(item[0] for item in PURPOSES if item[2] == "chat")
PURPOSE_LABELS = {route_id: label for route_id, label, _ in PURPOSES}


def run_user_console(args: argparse.Namespace) -> int:
    # Imported lazily by cli.py, after that module has finished defining all
    # command handlers. This keeps the interactive product layer separate from
    # the stable automation-oriented command surface.
    from model_gateway import cli as cli_module

    paths = cli_module._paths(args)
    initialize(paths)
    while True:
        config = load_config(paths.config)
        running = _gateway_healthy(paths, config)
        print("\n本地模型服务")
        print("=" * 36)
        print(
            f"服务：{'运行中' if running else '已停止'}  ·  "
            f"渠道：{len(config.connections)}  ·  模型：{len(config.deployments)}  ·  "
            f"用途：{len(config.routes)}/{len(PURPOSES)}"
        )
        print(f"建议：{_next_step(config, paths, running)}")
        print()
        print("1. 添加渠道和模型")
        print("2. 安排每项用途使用哪个模型")
        print("3. 连接到记忆服务")
        print("4. 启动或停止模型服务")
        print("5. 检查渠道是否可用（不产生推理费用）")
        print("6. 查看目前的设置")
        print("7. 查看用量与记录官方价格")
        print("8. 检查问题或查看日志")
        print("0. 退出")
        try:
            choice = input("请选择：").strip()
        except EOFError:
            print()
            return 0
        if choice == "0":
            return 0
        try:
            if choice == "1":
                _add_channel_and_model(paths)
            elif choice == "2":
                _assign_purpose(paths)
            elif choice == "3":
                _connect_memory_service(paths, args)
            elif choice == "4":
                _toggle_service(paths, args)
            elif choice == "5":
                _check_channels(paths, args)
            elif choice == "6":
                _show_settings(paths)
            elif choice == "7":
                _usage_and_prices(paths, args)
            elif choice == "8":
                _diagnostics(paths, args)
            else:
                print("没有这个选项，请输入菜单里的数字。")
        except (ConfigError, ValidationError, ValueError, OSError) as exc:
            print(f"\n没有完成：{cli_module._clean_error(exc)}")


def _next_step(
    config: GatewayConfig,
    paths: GatewayPaths,
    running: bool,
) -> str:
    if not config.connections:
        return "先添加你购买 API 的渠道和一个模型。"
    secrets_by_name = read_secrets(paths.secrets)
    if any(
        not secrets_by_name.get(connection.auth.secret_ref)
        for connection in config.connections.values()
    ):
        return "有渠道还没有填写 API Key，请在“添加渠道和模型”中补充。"
    if not config.deployments:
        return "渠道已经添加，下一步添加它提供的模型。"
    missing = [route_id for route_id in CHAT_PURPOSES if route_id not in config.routes]
    if missing:
        return "给模型安排用途；第一次可以让同一个聊天模型承担全部文字工作。"
    if "memory-gateway" not in config.clients:
        return "用途已经就绪，选择“连接到记忆服务”。"
    if not running:
        return "设置已经就绪，可以启动模型服务。"
    return "可以正常使用；新增第二个模型后可把它排在备用位置。"


def _add_channel_and_model(paths: GatewayPaths) -> None:
    config = load_config(paths.config)
    connection_id, connection, new_connection, secret_value = _choose_connection(
        config,
        paths,
    )
    print("\n添加这个渠道提供的模型")
    upstream_model = _required(
        "供应商页面显示的模型 ID（必须完全一致，例如 deepseek-chat）："
    )
    kind_choice = _prompt("这是哪类模型？1=聊天模型，2=向量模型 [1]：", "1")
    if kind_choice not in {"1", "2"}:
        raise ValueError("模型类型只能选择 1 或 2")
    kind = "embedding" if kind_choice == "2" else "chat"
    author = _prompt("模型作者的英文简称（不确定请保留 unknown）[unknown]：", "unknown")
    capabilities = Capabilities(streaming=kind == "chat")
    reasoning_default = "inherit"
    dimensions: int | None = None
    embedding_space = ""
    if kind == "chat":
        print("模型支持哪些能力？可多选，用空格分开；不确定可直接回车。")
        print("  1 工具调用  2 深度思考  3 图片输入  4 JSON  5 JSON Schema")
        selected = set(input("能力：").split())
        unknown = selected - {"1", "2", "3", "4", "5"}
        if unknown:
            raise ValueError("无法识别的能力编号：" + ", ".join(sorted(unknown)))
        capabilities = Capabilities(
            streaming=True,
            tools="1" in selected,
            reasoning="2" in selected,
            multimodal_input="3" in selected,
            json_object="4" in selected or "5" in selected,
            json_schema="5" in selected,
        )
        if capabilities.reasoning and _confirm("用户没有特别指定时，默认开启深度思考？", False):
            reasoning_default = "enabled"
    else:
        dimensions = _positive_int(_required("向量维度（例如 1024）："), "向量维度")
        embedding_space = derive_embedding_space(
            connection,
            upstream_model,
            dimensions,
        )
        print(f"已自动生成向量空间 ID：{embedding_space}")

    deployment_id = _unique_id(
        _slug_id(f"{connection_id}-{upstream_model}"),
        config.deployments,
    )
    deployment = DeploymentConfig(
        connection=connection_id,
        upstream_model=upstream_model,
        model_author=author,
        kind=kind,
        reasoning_default=reasoning_default,
        capabilities=capabilities,
        request_transform=RequestTransform(),
        dimensions=dimensions,
        embedding_space=embedding_space,
    )
    payload = config.model_dump(mode="python")
    if new_connection:
        payload["connections"] = {
            **payload["connections"],
            connection_id: connection.model_dump(mode="python"),
        }
    payload["deployments"] = {
        **payload["deployments"],
        deployment_id: deployment.model_dump(mode="python"),
    }
    updated = GatewayConfig.model_validate(payload)

    print("\n准备保存：")
    print(f"  渠道：{connection.channel_operator}  {connection.base_url}")
    print(f"  模型：{upstream_model}（{'聊天' if kind == 'chat' else '向量'}）")
    if not _confirm("确认保存？", True):
        print("已取消，没有修改设置。")
        return
    commit_control_plane(
        paths,
        expected_revision=source_revision(config, paths.config),
        config=updated,
        secret_updates=(
            {connection.auth.secret_ref: secret_value}
            if secret_value is not None
            else {}
        ),
    )
    print("已保存渠道和模型，API Key 不会显示在列表或日志中。")

    if kind == "chat":
        answer = _prompt(
            "现在把它用于哪些工作？1=全部文字工作，2=只用于日常聊天，3=稍后安排 [1]：",
            "1",
        )
        if answer == "1":
            _assign_deployment_to_routes(paths, deployment_id, CHAT_PURPOSES, confirm_overwrite=True)
        elif answer == "2":
            _assign_deployment_to_routes(
                paths,
                deployment_id,
                ("memory.chat",),
                confirm_overwrite=True,
            )
        elif answer != "3":
            print("没有识别该选择；模型已保存，可稍后在“安排用途”中设置。")
    else:
        _assign_deployment_to_routes(
            paths,
            deployment_id,
            ("memory.embedding",),
            confirm_overwrite=True,
        )


def _choose_connection(
    config: GatewayConfig,
    paths: GatewayPaths,
) -> tuple[str, ConnectionConfig, bool, str | None]:
    connections = list(sorted(config.connections.items()))
    if connections:
        print("\n选择已有渠道，或添加新渠道：")
        for index, (_, connection) in enumerate(connections, start=1):
            print(f"  {index}. {connection.channel_operator} · {connection.base_url}")
        print(f"  {len(connections) + 1}. 添加新渠道")
        raw = _prompt("选择：", str(len(connections) + 1))
        if not raw.isdecimal() or not 1 <= int(raw) <= len(connections) + 1:
            raise ValueError("渠道编号超出范围")
        if int(raw) <= len(connections):
            connection_id, connection = connections[int(raw) - 1]
            values = read_secrets(paths.secrets)
            secret_value: str | None = None
            if not values.get(connection.auth.secret_ref):
                secret_value = getpass.getpass("该渠道的 API Key（输入时不会显示）：").strip()
                if not secret_value:
                    raise ValueError("API Key 不能为空")
            elif _confirm("该渠道已有 API Key，要更换吗？", False):
                secret_value = getpass.getpass("新的 API Key（输入时不会显示）：").strip()
                if not secret_value:
                    raise ValueError("API Key 不能为空")
            return connection_id, connection, False, secret_value

    print("\n添加一个 API 渠道")
    print("渠道是你实际购买或领取 API Key 的地方，不一定是模型作者。")
    operator = _required("渠道英文简称（例如 deepseek、siliconflow、dashscope）：").lower()
    base_url = _required("官方 OpenAI-compatible API 地址（必须是 HTTPS）：")
    plan_choice = _prompt(
        "你的套餐：1=按量付费，2=订阅，3=免费额度，4=其他 [1]：",
        "1",
    )
    plan_types = {"1": "payg", "2": "subscription", "3": "free_tier", "4": "custom"}
    if plan_choice not in plan_types:
        raise ValueError("套餐只能选择 1、2、3 或 4")
    print("接口兼容方式：1=标准 OpenAI，2=Kimi，3=DeepSeek，4=MiMo，5=百炼 Qwen")
    adapter_choice = _prompt("选择 [1]：", "1")
    adapters = {
        "1": "generic",
        "2": "kimi",
        "3": "deepseek",
        "4": "mimo",
        "5": "dashscope_openai",
    }
    if adapter_choice not in adapters:
        raise ValueError("兼容方式只能选择 1、2、3、4 或 5")
    connection_id = _unique_id(_slug_id(f"{operator}-account"), config.connections)
    from model_gateway import cli as cli_module

    secret_ref = cli_module._default_secret_ref("CONNECTION", connection_id)
    connection = ConnectionConfig(
        channel_operator=operator,
        adapter=adapters[adapter_choice],
        base_url=base_url,
        auth=AuthConfig(type="bearer", secret_ref=secret_ref),
        billing_plan=BillingPlan(type=plan_types[plan_choice], name="default"),
        usage_scope="backend_allowed",
    )
    secret_value = getpass.getpass("该渠道的 API Key（输入时不会显示）：").strip()
    if not secret_value:
        raise ValueError("API Key 不能为空")
    return connection_id, connection, True, secret_value


def _assign_purpose(paths: GatewayPaths) -> None:
    config = load_config(paths.config)
    if not config.deployments:
        print("还没有模型，请先选择“添加渠道和模型”。")
        return
    print("\n模型用途")
    for index, (route_id, label, _) in enumerate(PURPOSES, start=1):
        route = config.routes.get(route_id)
        current = _route_model_names(config, route.targets) if route else "尚未安排"
        print(f"  {index}. {label}：{current}")
    print("  9. 让一个聊天模型承担全部文字工作")
    raw = input("选择要调整的用途（0 返回）：").strip()
    if raw == "0" or not raw:
        return
    if raw == "9":
        deployment_id = _choose_deployments(config, "chat", one_only=True)[0]
        _assign_deployment_to_routes(paths, deployment_id, CHAT_PURPOSES, confirm_overwrite=True)
        return
    if not raw.isdecimal() or not 1 <= int(raw) <= len(PURPOSES):
        raise ValueError("用途编号超出范围")
    route_id, label, kind = PURPOSES[int(raw) - 1]
    targets = _choose_deployments(config, kind, one_only=False)
    print(f"将“{label}”依次使用：{_route_model_names(config, targets)}")
    if not _confirm("确认保存？", True):
        print("已取消。")
        return
    _write_route(paths, route_id, targets)
    print(f"已更新“{label}”。")


def _choose_deployments(
    config: GatewayConfig,
    kind: str | None,
    *,
    one_only: bool,
) -> list[str]:
    candidates = [
        (deployment_id, deployment)
        for deployment_id, deployment in sorted(config.deployments.items())
        if (kind is None or deployment.kind == kind) and deployment.enabled
    ]
    if not candidates:
        label = "模型" if kind is None else "聊天模型" if kind == "chat" else "向量模型"
        raise ValueError("没有可用的" + label)
    print("\n可选模型：")
    for index, (_, deployment) in enumerate(candidates, start=1):
        connection = config.connections[deployment.connection]
        print(
            f"  {index}. {deployment.upstream_model} · "
            f"渠道 {connection.channel_operator}"
        )
    prompt = "选择模型编号：" if one_only else "按优先顺序输入编号，用空格分开："
    values = input(prompt).split()
    if not values:
        raise ValueError("至少需要选择一个模型")
    if one_only and len(values) != 1:
        raise ValueError("这里只能选择一个模型")
    indices: list[int] = []
    for value in values:
        if not value.isdecimal() or not 1 <= int(value) <= len(candidates):
            raise ValueError(f"模型编号无效：{value}")
        indices.append(int(value) - 1)
    if len(indices) != len(set(indices)):
        raise ValueError("不能重复选择同一个模型")
    return [candidates[index][0] for index in indices]


def _assign_deployment_to_routes(
    paths: GatewayPaths,
    deployment_id: str,
    route_ids: Iterable[str],
    *,
    confirm_overwrite: bool,
) -> None:
    config = load_config(paths.config)
    selected = tuple(route_ids)
    replacing = [route_id for route_id in selected if route_id in config.routes]
    if replacing and confirm_overwrite:
        labels = "、".join(PURPOSE_LABELS[item] for item in replacing)
        if not _confirm(f"这会替换已有安排：{labels}。继续？", False):
            print("模型已经保存，原有用途安排保持不变。")
            return
    payload = config.model_dump(mode="python")
    routes = dict(payload["routes"])
    kind = config.deployments[deployment_id].kind
    for route_id in selected:
        routes[route_id] = RouteConfig(
            kind=kind,
            targets=[deployment_id],
            max_attempts=1,
        ).model_dump(mode="python")
    payload["routes"] = routes
    commit_control_plane(
        paths,
        expected_revision=source_revision(config, paths.config),
        config=GatewayConfig.model_validate(payload),
    )
    print("已安排用途：" + "、".join(PURPOSE_LABELS[item] for item in selected))


def _write_route(paths: GatewayPaths, route_id: str, targets: list[str]) -> None:
    config = load_config(paths.config)
    payload = config.model_dump(mode="python")
    routes = dict(payload["routes"])
    routes[route_id] = RouteConfig(
        kind=config.deployments[targets[0]].kind,
        targets=targets,
        max_attempts=min(3, len(targets)),
        fallback_scope="any_channel" if len(targets) > 1 else "none",
    ).model_dump(mode="python")
    payload["routes"] = routes
    commit_control_plane(
        paths,
        expected_revision=source_revision(config, paths.config),
        config=GatewayConfig.model_validate(payload),
    )


def _connect_memory_service(paths: GatewayPaths, args: argparse.Namespace) -> None:
    from model_gateway import cli as cli_module

    config = load_config(paths.config)
    missing = [route_id for route_id in CHAT_PURPOSES if route_id not in config.routes]
    if missing:
        print("还不能连接：以下用途尚未安排模型：")
        for route_id in missing:
            print(f"  - {PURPOSE_LABELS[route_id]}")
        print("请先选择“安排每项用途使用哪个模型”。")
        return

    memgw = _find_memgw()
    if memgw is None:
        entered = input("没有自动找到记忆项目，请输入 My_Memory 文件夹路径（回车取消）：").strip()
        if not entered:
            return
        candidate = Path(entered).expanduser() / "scripts" / "memgw"
        if not candidate.is_file():
            raise ValueError(f"没有找到记忆服务启动器：{candidate}")
        memgw = candidate

    payload = config.model_dump(mode="python")
    clients = dict(payload["clients"])
    existing = config.clients.get("memory-gateway")
    secret_ref = (
        existing.secret_ref
        if existing is not None
        else cli_module._default_secret_ref("CLIENT", "memory-gateway")
    )
    clients["memory-gateway"] = ClientConfig(
        kind="backend",
        secret_ref=secret_ref,
        allowed_routes=["memory.*", "knowledge.*"],
        allow_direct_deployments=False,
        allow_legacy_weak_secret=(
            existing.allow_legacy_weak_secret if existing is not None else False
        ),
    ).model_dump(mode="python")
    payload["clients"] = clients
    updated = GatewayConfig.model_validate(payload)
    secret_values = read_secrets(paths.secrets)
    client_key = secret_values.get(secret_ref) or secrets.token_urlsafe(32)
    commit_control_plane(
        paths,
        expected_revision=source_revision(config, paths.config),
        config=updated,
        secret_updates={secret_ref: client_key},
    )

    if not _gateway_healthy(paths, updated):
        print("正在启动模型服务……")
        start_args = _handler_args(args, host=None, port=None, log_level="info", access_log=False)
        result = cli_module._cmd_start(start_args)
        if result != 0 or not _gateway_healthy(paths, updated):
            raise ValueError("模型服务尚未就绪，请先在主菜单检查问题")

    base_url = cli_module._server_url(updated.server) + "/v1"
    if _run_memgw(memgw, ["config", "set", "MODEL_GATEWAY_BASE_URL", base_url]) != 0:
        raise ValueError("记忆服务没有接受模型服务地址")
    if _run_memgw(
        memgw,
        ["secret", "set", "model-gateway", "--stdin"],
        input_text=client_key + "\n",
    ) != 0:
        raise ValueError("记忆服务没有通过连接检查；密钥已经安全保存，可检查日志后重试")

    embedding_route = updated.routes.get("memory.embedding")
    if embedding_route:
        deployment = updated.deployments[embedding_route.targets[0]]
        _run_memgw(
            memgw,
            ["config", "set", "MODEL_GATEWAY_EMBEDDING_SPACE_ID", deployment.embedding_space],
        )
        _run_memgw(
            memgw,
            ["config", "set", "EMBEDDING_DIMENSIONS", str(deployment.dimensions)],
        )
    print("模型服务已经连接到记忆服务。两边使用独立密钥文件，密钥不会显示。")
    if _confirm("现在重启记忆服务，让新设置立即生效？", True):
        if _run_memgw(memgw, ["restart"]) != 0:
            print("记忆服务重启没有完成；设置已经保存，可稍后从 memgw 菜单启动。")


def _toggle_service(paths: GatewayPaths, args: argparse.Namespace) -> None:
    from model_gateway import cli as cli_module

    config = load_config(paths.config)
    if _gateway_managed(paths):
        if _confirm("停止模型服务？", False):
            cli_module._cmd_stop(_handler_args(args, timeout=10.0, force=False))
    else:
        cli_module._cmd_start(
            _handler_args(args, host=None, port=None, log_level="info", access_log=False)
        )
    if _gateway_healthy(paths, config):
        print("模型服务可以正常访问。")


def _check_channels(paths: GatewayPaths, args: argparse.Namespace) -> None:
    from model_gateway import cli as cli_module

    config = load_config(paths.config)
    if not config.connections:
        print("还没有渠道，请先添加渠道和模型。")
        return
    print("正在读取各渠道的模型列表；不会发送聊天或向量请求。")
    results = cli_module.asyncio.run(
        cli_module._run_probes(
            config=config,
            secret_values=read_secrets(paths.secrets),
            connection_ids=list(config.connections),
            live=False,
            client_kind="backend",
        )
    )
    labels = {
        "available": "可用",
        "connected": "已连接",
        "connected_unlisted": "已连接，模型列表中未找到已填模型",
        "connected_unverified": "已连接，但无法识别模型列表",
        "check_unsupported": "渠道不提供免费检查",
        "not_configured": "缺少 API Key",
        "policy_blocked": "套餐不允许记忆服务后台使用",
        "auth_failed": "API Key 无效或无权限",
        "network_error": "网络连接失败",
        "provider_error": "渠道返回错误",
    }
    for result in results:
        connection = config.connections[result.connection_id]
        mark = "正常" if result.ok else "注意"
        print(
            f"[{mark}] {connection.channel_operator}："
            f"{labels.get(result.status, result.status)}"
        )


def _show_settings(paths: GatewayPaths) -> None:
    config = load_config(paths.config)
    secret_values = read_secrets(paths.secrets)
    print("\n渠道")
    if not config.connections:
        print("  尚未添加")
    for connection_id, connection in sorted(config.connections.items()):
        key_state = "API Key 已保存" if secret_values.get(connection.auth.secret_ref) else "缺少 API Key"
        print(f"  {connection.channel_operator} · {connection.base_url} · {key_state}")
        models = [
            deployment
            for deployment in config.deployments.values()
            if deployment.connection == connection_id
        ]
        for deployment in models:
            label = "聊天" if deployment.kind == "chat" else f"向量 {deployment.dimensions} 维"
            print(f"    - {deployment.upstream_model}（{label}）")
    print("\n用途")
    for route_id, label, _ in PURPOSES:
        route = config.routes.get(route_id)
        current = _route_model_names(config, route.targets) if route else "尚未安排"
        print(f"  {label}：{current}")


def _usage_and_prices(paths: GatewayPaths, args: argparse.Namespace) -> None:
    from model_gateway import cli as cli_module

    while True:
        print("\n用量与价格")
        print("1. 查看最近 30 天用量")
        print("2. 查看已经记录的官方价格")
        print("3. 为一个模型记录官方价格")
        print("0. 返回")
        choice = input("选择：").strip()
        if choice == "0" or not choice:
            return
        if choice == "1":
            cli_module._cmd_usage_summary(_handler_args(args, days=30))
        elif choice == "2":
            _show_prices(paths)
        elif choice == "3":
            _record_price(paths)
        else:
            print("没有这个选项。")


def _show_prices(paths: GatewayPaths) -> None:
    config = load_config(paths.config)
    if not config.pricing:
        print("尚未记录价格。没有明确官方价格时，费用会保持未知，不会显示成免费。")
        return
    for pricing_id, price in sorted(config.pricing.items()):
        deployments = [
            item.upstream_model
            for item in config.deployments.values()
            if item.pricing == pricing_id
        ]
        rates = price.tiers[0] if len(price.tiers) == 1 else None
        if rates is None:
            rate_text = f"{len(price.tiers)} 个价格分档"
        else:
            rate_text = (
                f"输入 {rates.input} / 缓存输入 {rates.cached_input} / 输出 {rates.output}"
            )
        print(
            f"  {', '.join(deployments) or pricing_id}：{price.currency}，"
            f"每 {price.unit_tokens} tokens，{rate_text}"
        )
        print(f"    官方来源：{price.source_url or '未填写'}")


def _record_price(paths: GatewayPaths) -> None:
    config = load_config(paths.config)
    deployment_id = _choose_deployments(config, None, one_only=True)[0]
    deployment = config.deployments[deployment_id]
    source_url = _required("该渠道的官方价格页面（HTTPS）：")
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("官方价格页面必须是完整的 HTTPS 地址")
    currency = _prompt("币种代码 [USD]：", "USD").upper()
    unit_tokens = _positive_int(_prompt("价格对应多少 tokens [1000000]：", "1000000"), "Token 单位")
    print("不知道的单价请直接回车，不要用相似模型或搜索摘要猜测。")
    input_price = input("输入价格：").strip() or None
    cached_price = input("缓存输入价格：").strip() or None
    output_price = input("输出价格：").strip() or None
    tier = PricingTier(input=input_price, cached_input=cached_price, output=output_price)
    pricing = PricingConfig(
        mode="per_token",
        currency=currency,
        unit_tokens=unit_tokens,
        tiers=[tier],
        source_url=source_url,
        checked_at=datetime.now(UTC).date().isoformat(),
    )
    pricing_id = _unique_id(
        _slug_id(f"{deployment_id}-{pricing.checked_at}"),
        config.pricing,
    )
    print(
        f"将为 {deployment.upstream_model} 记录 {currency} 价格，"
        f"官方来源为 {parsed.hostname}。"
    )
    if not _confirm("已亲自核对官方页面，确认保存？", False):
        print("已取消。")
        return
    payload = config.model_dump(mode="python")
    payload["pricing"] = {
        **payload["pricing"],
        pricing_id: pricing.model_dump(mode="python"),
    }
    deployments = dict(payload["deployments"])
    updated_deployment = dict(deployments[deployment_id])
    updated_deployment["pricing"] = pricing_id
    deployments[deployment_id] = updated_deployment
    payload["deployments"] = deployments
    commit_control_plane(
        paths,
        expected_revision=source_revision(config, paths.config),
        config=GatewayConfig.model_validate(payload),
    )
    print("官方价格已经保存；过去的用量价格快照不会被改写。")


def _diagnostics(paths: GatewayPaths, args: argparse.Namespace) -> None:
    from model_gateway import cli as cli_module

    print("\n1. 自动检查设置  2. 查看最近 60 行日志  0. 返回")
    choice = input("选择：").strip()
    if choice == "1":
        cli_module._cmd_doctor(_handler_args(args))
    elif choice == "2":
        if not paths.log.exists():
            print("还没有日志；服务至少启动过一次后才会生成。")
        else:
            cli_module._cmd_logs(_handler_args(args, lines=60, follow=False))


def _gateway_managed(paths: GatewayPaths) -> bool:
    from model_gateway import process as process_module

    state = process_module._read_state(paths)
    return bool(state and process_module._state_process_matches(state, paths))


def _gateway_healthy(paths: GatewayPaths, config: GatewayConfig) -> bool:
    from model_gateway import cli as cli_module
    from model_gateway import process as process_module

    if not _gateway_managed(paths):
        return False
    return process_module._gateway_responding(cli_module._server_url(config.server))


def _find_memgw() -> Path | None:
    installed = shutil.which("memgw")
    if installed:
        return Path(installed)
    project_root = Path(__file__).resolve().parents[1]
    sibling_names = ("memory-gateway", "My_Memory")
    return next(
        (
            sibling
            for name in sibling_names
            if (sibling := project_root.parent / name / "scripts" / "memgw").is_file()
        ),
        None,
    )


def _run_memgw(
    command: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
    quiet: bool = False,
) -> int:
    result = subprocess.run(
        [str(command), *arguments],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if not quiet and result.stdout.strip():
        print(result.stdout.strip())
    if not quiet and result.stderr.strip():
        print(result.stderr.strip())
    return result.returncode


def _route_model_names(config: GatewayConfig, targets: Iterable[str]) -> str:
    names = [config.deployments[target].upstream_model for target in targets]
    return " → ".join(names)


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", value.lower()).strip("-._:")
    if not normalized:
        normalized = "item"
    if len(normalized) > 120:
        digest = sha256(normalized.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized[:110].rstrip('-._:')}-{digest}"
    return normalized


def _unique_id(candidate: str, records: Any) -> str:
    if candidate not in records:
        return candidate
    index = 2
    while True:
        suffix = f"-{index}"
        alternate = candidate[: 120 - len(suffix)].rstrip("-._:") + suffix
        if alternate not in records:
            return alternate
        index += 1


def _handler_args(base: argparse.Namespace, **values: Any) -> argparse.Namespace:
    return argparse.Namespace(
        home=getattr(base, "home", ""),
        json=False,
        **values,
    )


def _required(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        raise ValueError("这一项不能为空")
    return value


def _prompt(prompt: str, default: str) -> str:
    return input(prompt).strip() or default


def _confirm(prompt: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    value = input(f"{prompt} {suffix} ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "是"}


def _positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{label}必须是正整数")
    return parsed
    commit_control_plane,
