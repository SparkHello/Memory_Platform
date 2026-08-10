import asyncio
import json
import logging
import time
from typing import Any, Literal

from fastapi import HTTPException, status
import httpx

from app.config import Settings
from app.llm.model_gateway import (
    ModelGatewayProtocolError,
    parse_model_gateway_metadata,
    validate_model_gateway_metadata,
)
from app.llm.runtime import ModelRuntime, resolve_model_runtime
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.attribution import model_gateway_usage_headers

logger = logging.getLogger(__name__)


class OpenAICompatibleClient:
    """Internal structured-task client that always talks to Model Gateway."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        wall_clock: Any = time.time,
        usage_recorder: Any = None,
    ):
        self.settings = settings
        self.transport = transport
        self._wall_clock = wall_clock
        # Central gateway records usage itself; keep the argument for call-site
        # compatibility during the dual-path removal.
        self.usage_recorder = usage_recorder

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        messages: list[dict[str, str]],
        *,
        thinking: Literal["enabled", "disabled"] = "enabled",
        structured_tool: dict[str, Any] | None = None,
    ) -> dict:
        runtime = resolve_model_runtime(self.settings)
        return await self._create_via_model_gateway(
            request=request,
            messages=messages,
            thinking=thinking,
            structured_tool=structured_tool,
            runtime=runtime,
        )

    async def _create_via_model_gateway(
        self,
        *,
        request: ChatCompletionRequest,
        messages: list[dict[str, str]],
        thinking: Literal["enabled", "disabled"],
        structured_tool: dict[str, Any] | None,
        runtime: ModelRuntime,
    ) -> dict:
        try:
            model = runtime.route_for(request.model)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc

        # The central gateway owns provider selection and vendor-specific
        # thinking/structured-output adaptations. Keep this request generic.
        payload = request.model_dump(
            exclude_none=True,
            exclude={"conversation_id"},
        )
        payload["model"] = model
        payload["messages"] = messages
        payload["stream"] = False
        payload.setdefault(
            "reasoning_effort",
            "high" if thinking == "enabled" else "none",
        )
        if structured_tool is not None:
            payload.pop("response_format", None)
            payload["tools"] = [{"type": "function", "function": structured_tool}]
            # DashScope Qwen/DeepSeek and current Kimi coding models reject a
            # *specific* forced function while reasoning is enabled.  Keep
            # reasoning for review-quality work, but intentionally request
            # automatic selection; the caller accepts either tool arguments or
            # JSON content.  With reasoning disabled we can retain the strict
            # single-function contract.
            payload["tool_choice"] = (
                "auto"
                if thinking == "enabled"
                else {
                    "type": "function",
                    "function": {"name": structured_tool["name"]},
                }
            )

        started_at = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                response = await self._post_model_gateway(
                    payload=payload,
                    runtime=runtime,
                )
        except (TimeoutError, httpx.TimeoutException) as exc:
            logger.warning(
                "中央模型网关调用达到总超时。elapsed_seconds=%.2f",
                time.monotonic() - started_at,
            )
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    "调用中央模型网关超时"
                    f"（{self.settings.request_timeout_seconds:g} 秒），请稍后重试"
                ),
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = _safe_error_detail(exc.response)
            logger.warning(
                "中央模型网关返回错误。status_code=%s elapsed_seconds=%.2f",
                exc.response.status_code,
                time.monotonic() - started_at,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"中央模型网关返回错误：{detail}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "中央模型网关网络调用失败。error_type=%s elapsed_seconds=%.2f",
                type(exc).__name__,
                time.monotonic() - started_at,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"调用中央模型网关失败：{exc}",
            ) from exc

        metadata = parse_model_gateway_metadata(response.headers)
        try:
            validate_model_gateway_metadata(metadata, expected_route=model)
        except ModelGatewayProtocolError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        logger.info(
            "中央模型网关调用完成。route=%s deployment=%s connection=%s "
            "vendor=%s model=%s elapsed_seconds=%.2f",
            metadata.route or request.model,
            metadata.deployment_id or "unknown",
            metadata.connection_id or "unknown",
            metadata.channel_operator or "unknown",
            metadata.upstream_model or model,
            time.monotonic() - started_at,
        )
        try:
            return _json_from_utf8_bytes(response)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="中央模型网关返回了无法解析的 JSON",
            ) from exc

    async def _post_model_gateway(
        self,
        *,
        payload: dict[str, Any],
        runtime: ModelRuntime,
    ) -> httpx.Response:
        url = f"{runtime.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {runtime.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            **model_gateway_usage_headers(
                signing_secret=self.settings.gateway_signing_secret,
                operation=str(payload.get("model") or "memory.task"),
            ),
        }
        client_kwargs: dict[str, Any] = {
            "timeout": self.settings.request_timeout_seconds,
            "follow_redirects": False,
            "trust_env": False,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        data = _json_from_utf8_bytes(response)
    except (UnicodeError, json.JSONDecodeError):
        return response.content.decode("utf-8", errors="replace")[:500]
    return str(data)[:500]


def _json_from_utf8_bytes(response: httpx.Response) -> dict:
    try:
        raw_text = response.content.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("上游响应不是合法 UTF-8，已使用替换字符解码。")
        raw_text = response.content.decode("utf-8", errors="replace")
    return json.loads(raw_text)
