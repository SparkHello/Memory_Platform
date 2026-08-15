"""Cheap live probes that infer which chat capabilities a model accepts.

Results are **advisory**: a 200 means the provider accepted the request shape;
it does not prove production-quality support. Unchecked capabilities in the
console mean the router will not send that feature to this deployment.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx

from model_gateway.adapters import apply_connection_adapter
from model_gateway.auth import provider_secret_header_value
from model_gateway.discovery import (
    probe_client,
    read_bounded_response,
    upstream_auth_headers,
)
from model_gateway.http_safety import require_safe_destination, upstream_url
from model_gateway.models import (
    AuthConfig,
    Capabilities,
    ConnectionConfig,
    DeploymentConfig,
)


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

    async with probe_client(connection, timeout_seconds, transport) as client:
        for name in ordered:
            result = await _run_one_probe(
                client=client,
                connection=connection,
                deployment=deployment,
                secret=secret,
                probe=name,
                validate_destination=transport is None,
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

    return {
        "ok": bool(details.get("chat", {}).get("ok")),
        "capabilities": capabilities,
        "details": details,
        "note": (
            "探测仅验证提供商是否接受该请求形态；勾选后路由才会把对应能力派发到此模型。"
            "未勾选视为不支持，客户端若使用该能力会被网关拒绝。"
        ),
    }


async def _run_one_probe(
    *,
    client: httpx.AsyncClient,
    connection: ConnectionConfig,
    deployment: DeploymentConfig,
    secret: str,
    probe: ProbeName,
    validate_destination: bool,
) -> dict[str, Any]:
    payload = _probe_payload(deployment.upstream_model, probe)
    apply_connection_adapter(
        payload,
        connection=connection,
        deployment=deployment,
    )
    try:
        url = upstream_url(
            connection.base_url,
            connection.chat_endpoint,
            allowed_private_networks=connection.allowed_private_networks,
        )
        if validate_destination:
            await require_safe_destination(
                url,
                allowed_private_networks=connection.allowed_private_networks,
            )
        async with client.stream(
            "POST",
            url,
            headers=upstream_auth_headers(connection, secret),
            json=payload,
        ) as response:
            if response.is_success:
                content = await read_bounded_response(
                    response,
                    min(connection.response_limit_bytes, 256 * 1024),
                )
            else:
                content = b""
                # Drain a small error body for classification.
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > 2048:
                        break
                status = response.status_code
                detail = _error_snippet(content) or f"HTTP {status}"
                return {
                    "ok": False,
                    "http_status": status,
                    "detail": detail[:300],
                }
            status = response.status_code
    except (httpx.HTTPError, OSError) as exc:
        return {
            "ok": False,
            "http_status": None,
            "detail": f"网络错误：{type(exc).__name__}",
        }
    except ValueError as exc:
        return {
            "ok": False,
            "http_status": None,
            "detail": str(exc)[:300] or "URL/响应未通过安全校验",
        }

    # Streaming success may be empty SSE with only done; treat 200 as ok.
    if probe == "streaming":
        return {
            "ok": True,
            "http_status": status,
            "detail": "流式请求被接受",
        }
    if probe == "tools":
        # Accept any 2xx; some models answer in text without tool_calls.
        return {
            "ok": True,
            "http_status": status,
            "detail": "带 tools 的请求被接受",
        }
    if probe == "json_object":
        return {
            "ok": True,
            "http_status": status,
            "detail": "json_object 请求被接受",
        }
    if probe == "reasoning":
        return {
            "ok": True,
            "http_status": status,
            "detail": "带推理字段的请求被接受",
        }
    return {
        "ok": True,
        "http_status": status,
        "detail": "基础聊天请求成功",
    }


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
