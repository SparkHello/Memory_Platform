"""Cheap live probes that infer which chat capabilities a model accepts.

Results are **advisory**: a 200 means the provider accepted the request shape;
it does not prove production-quality support. Unchecked capabilities in the
console mean the router will not send that feature to this deployment.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Mapping

import httpx

from model_gateway.auth import provider_secret_header_value
from model_gateway.models import (
    AuthConfig,
    Capabilities,
    ConnectionConfig,
    DeploymentConfig,
    PricingConfig,
    ServerConfig,
)
from model_gateway.proxy import prepare_payload
from model_gateway.routing import RouteTarget
from model_gateway.upstream_executor import UpstreamExecutor
from model_gateway.usage import UsageMetadata, UsageStore


ProbeName = Literal[
    "chat",
    "streaming",
    "tools",
    "reasoning",
    "json_object",
]
DEFAULT_PROBES: tuple[ProbeName, ...] = (
    "chat",
    "streaming",
    "tools",
    "reasoning",
    "json_object",
)

# Keep probes tiny: one short completion each.
_PROBE_MAX_TOKENS = 8
_PROBE_TIMEOUT = 25.0


def build_probe_connection(
    *,
    channel_operator: str,
    base_url: str,
    adapter: str,
    auth_type: Literal["bearer", "x-api-key"],
    allowed_private_networks: list[str],
    secret_ref: str = "PROBE_CANDIDATE_KEY",
) -> ConnectionConfig:
    return ConnectionConfig(
        channel_operator=channel_operator,
        adapter=adapter,  # type: ignore[arg-type]
        allowed_private_networks=allowed_private_networks,
        base_url=base_url,
        auth=AuthConfig(type=auth_type, secret_ref=secret_ref),
        models_endpoint="/models",
    )


def build_probe_deployment(
    *,
    connection_id: str,
    upstream_model: str,
) -> DeploymentConfig:
    return DeploymentConfig(
        connection=connection_id,
        upstream_model=upstream_model,
        model_author="unknown",
        kind="chat",
        capabilities=Capabilities(streaming=True),
    )


async def probe_chat_capabilities(
    *,
    connection: ConnectionConfig,
    deployment: DeploymentConfig,
    secret: str,
    probes: tuple[ProbeName, ...] = DEFAULT_PROBES,
    timeout_seconds: float = _PROBE_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
    usage_store: UsageStore | None = None,
    usage_server: ServerConfig | None = None,
    pricing_catalog: Mapping[str, PricingConfig] | None = None,
    usage_client_id: str = "modelgw-capability-probe",
    storage_monitor: object | None = None,
) -> dict[str, Any]:
    """Run selected probes and return declared capability map + per-probe notes."""

    provider_secret_header_value(secret)
    ordered = tuple(name for name in DEFAULT_PROBES if name in probes)
    if "chat" not in ordered:
        ordered = ("chat",) + ordered

    details: dict[str, dict[str, Any]] = {}
    capabilities = {
        "streaming": False,
        "tools": False,
        "parallel_tools": False,
        "reasoning": False,
        "multimodal_input": False,
        "json_object": False,
        "json_schema": False,
    }

    target = RouteTarget(
        route_id="capability.probe",
        deployment_id="capability-probe",
        deployment=deployment,
        connection_id=deployment.connection,
        connection=connection,
    )
    async with UpstreamExecutor(transport=transport) as executor:
        for name in ordered:
            result = await _run_one_probe(
                executor=executor,
                target=target,
                secret=secret,
                probe=name,
                timeout_seconds=timeout_seconds,
                usage_store=usage_store,
                usage_server=usage_server,
                pricing_catalog=pricing_catalog or {},
                usage_client_id=usage_client_id,
                storage_monitor=storage_monitor,
            )
            details[name] = result
            if name == "chat" and not result["ok"]:
                # No point probing features if basic chat fails.
                break
            if name == "streaming" and result["ok"]:
                capabilities["streaming"] = True
            if name == "tools" and result["ok"]:
                capabilities["tools"] = True
            if name == "reasoning" and result["ok"]:
                capabilities["reasoning"] = True
            if name == "json_object" and result["ok"]:
                capabilities["json_object"] = True

    # Baseline: if chat works but streaming not probed, keep default streaming=true
    # only when the streaming probe succeeded or was not run.
    if details.get("chat", {}).get("ok"):
        if "streaming" not in details:
            capabilities["streaming"] = True
        # Chat itself does not set tools/reasoning.

    response: dict[str, Any] = {
        "ok": bool(details.get("chat", {}).get("ok")),
        "capabilities": capabilities,
        "details": details,
        "note": (
            "探测仅验证提供商是否接受该请求形态；勾选后路由才会把对应能力派发到此模型。"
            "未勾选视为不支持，客户端若使用该能力会被网关拒绝。"
        ),
    }
    ledger_statuses = {
        str(item.get("usage_ledger_status", ""))
        for item in details.values()
        if item.get("usage_ledger_status")
    }
    if ledger_statuses:
        response["usage_ledger_status"] = (
            "incomplete" if "incomplete" in ledger_statuses else "complete"
        )
    if "incomplete" in ledger_statuses:
        response["warnings"] = [
            "部分真实探测已完成，但 usage ledger 写入失败；探测结果仍保留"
        ]
    return response


async def _run_one_probe(
    *,
    executor: UpstreamExecutor,
    target: RouteTarget,
    secret: str,
    probe: ProbeName,
    timeout_seconds: float,
    usage_store: UsageStore | None,
    usage_server: ServerConfig | None,
    pricing_catalog: Mapping[str, PricingConfig],
    usage_client_id: str,
    storage_monitor: object | None,
) -> dict[str, Any]:
    operation = f"capability.probe.{probe}"
    probe_target = RouteTarget(
        route_id=operation,
        deployment_id=target.deployment_id,
        deployment=target.deployment,
        connection_id=target.connection_id,
        connection=target.connection,
    )
    payload = prepare_payload(
        _probe_payload(probe_target.deployment.upstream_model, probe),
        probe_target,
    )
    if usage_store is not None:
        if usage_server is None:
            raise ValueError("usage_server is required with usage_store")
        result = await executor.post_json_accounted(
            target=probe_target,
            payload=payload,
            secret=secret,
            timeout_seconds=timeout_seconds,
            response_limit_bytes=min(
                target.connection.response_limit_bytes,
                256 * 1024,
            ),
            usage_store=usage_store,
            server=usage_server,
            pricing_catalog=pricing_catalog,
            client_id=usage_client_id,
            route_id=operation,
            metadata=UsageMetadata(operation=operation),
            storage_monitor=storage_monitor,
        )
    else:
        result = await executor.post_json(
            target=probe_target,
            payload=payload,
            secret=secret,
            timeout_seconds=timeout_seconds,
            response_limit_bytes=min(
                target.connection.response_limit_bytes,
                256 * 1024,
            ),
        )

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        if result.usage_ledger_status:
            payload["usage_ledger_status"] = result.usage_ledger_status
        if result.usage_ledger_status == "incomplete":
            payload["warning"] = (
                "真实探测已完成，但 usage ledger 写入失败；探测结果仍保留"
            )
        return payload

    if result.trace is None:
        return finish({
            "ok": False,
            "http_status": None,
            "detail": result.error_detail or "URL/响应未通过安全校验",
        })
    if result.error_type:
        detail = (
            "响应超过安全上限"
            if result.trace.failure_class == "response_too_large"
            else f"网络错误：{result.error_type}"
        )
        return finish({
            "ok": False,
            "http_status": result.status_code,
            "detail": detail,
        })
    assert result.status_code is not None
    status = result.status_code
    content = result.content
    if not result.is_success:
        detail = _error_snippet(content) or f"HTTP {status}"
        return finish({
            "ok": False,
            "http_status": status,
            "detail": detail[:300],
        })

    # Streaming success may be empty SSE with only done; treat 200 as ok.
    if probe == "streaming":
        return finish({
            "ok": True,
            "http_status": status,
            "detail": "流式请求被接受",
        })
    if probe == "tools":
        # Accept any 2xx; some models answer in text without tool_calls.
        return finish({
            "ok": True,
            "http_status": status,
            "detail": "带 tools 的请求被接受",
        })
    if probe == "json_object":
        return finish({
            "ok": True,
            "http_status": status,
            "detail": "json_object 请求被接受",
        })
    if probe == "reasoning":
        return finish({
            "ok": True,
            "http_status": status,
            "detail": "带推理字段的请求被接受",
        })
    return finish({
        "ok": True,
        "http_status": status,
        "detail": "基础聊天请求成功",
    })


def _probe_payload(model: str, probe: ProbeName) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": _PROBE_MAX_TOKENS,
        "stream": False,
    }
    if probe == "chat":
        return base
    if probe == "streaming":
        base["stream"] = True
        base["max_tokens"] = 1
        return base
    if probe == "tools":
        base["messages"] = [
            {
                "role": "user",
                "content": "Call the noop tool once if you can, otherwise say ok.",
            }
        ]
        base["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "noop",
                    "description": "No-op probe tool",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ]
        base["tool_choice"] = "auto"
        base["max_tokens"] = 32
        return base
    if probe == "reasoning":
        # Generic knobs; adapters may rewrite/remove them.
        base["reasoning_effort"] = "low"
        base["messages"] = [{"role": "user", "content": "Reply with one word: ok"}]
        return base
    if probe == "json_object":
        base["response_format"] = {"type": "json_object"}
        base["messages"] = [
            {
                "role": "user",
                "content": 'Return a JSON object: {"ok":true}',
            }
        ]
        base["max_tokens"] = 32
        return base
    return base


def _error_snippet(content: bytes) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        text = content.decode("utf-8", errors="replace").strip()
        return text[:200]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        if payload.get("message"):
            return str(payload["message"])[:200]
    return content.decode("utf-8", errors="replace").strip()[:200]
