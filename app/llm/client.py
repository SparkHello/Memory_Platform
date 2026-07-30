import asyncio
import json
import logging
import time
from typing import Any, Literal

from fastapi import HTTPException, status
import httpx

from app.config import Settings
from app.llm.routing import (
    GLOBAL_PROVIDER_COOLDOWNS,
    LLMProvider,
    ProviderCooldowns,
    ProviderCoolingDown,
    ordered_configured_providers,
    retry_after_seconds,
)
from app.openai_compat.schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldowns: ProviderCooldowns | None = None,
        wall_clock: Any = time.time,
    ):
        self.settings = settings
        self.transport = transport
        self.cooldowns = cooldowns or GLOBAL_PROVIDER_COOLDOWNS
        self._wall_clock = wall_clock

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        messages: list[dict[str, str]],
        *,
        thinking: Literal["enabled", "disabled"] = "enabled",
        structured_tool: dict[str, Any] | None = None,
    ) -> dict:
        providers = ordered_configured_providers(
            self.settings.llm_provider_priority,
            self._providers(),
        )
        if not providers:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="没有可用的上游模型；请配置所选 provider 的 API key",
            )

        started_at = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                response, provider = await self._request_with_failover(
                    providers=providers,
                    request=request,
                    messages=messages,
                    thinking=thinking,
                    structured_tool=structured_tool,
                )
        except (TimeoutError, httpx.TimeoutException) as exc:
            elapsed = time.monotonic() - started_at
            logger.warning(
                "上游模型调用达到总超时。elapsed_seconds=%.2f",
                elapsed,
            )
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    "调用上游模型 API 超时"
                    f"（{self.settings.request_timeout_seconds:g} 秒），请稍后重试"
                ),
            ) from exc
        except ProviderCoolingDown as exc:
            logger.warning("所有已配置上游模型都处于 429 冷却。")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="所有已配置上游模型暂时处于 429 冷却，请稍后重试",
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = _safe_error_detail(exc.response)
            logger.warning(
                "上游模型返回错误。status_code=%s elapsed_seconds=%.2f",
                exc.response.status_code,
                time.monotonic() - started_at,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"上游模型 API 返回错误：{detail}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "上游模型网络调用失败。error_type=%s elapsed_seconds=%.2f",
                type(exc).__name__,
                time.monotonic() - started_at,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"调用上游模型 API 失败：{exc}",
            ) from exc

        logger.info(
            "上游模型调用完成。provider=%s model=%s elapsed_seconds=%.2f",
            provider.code,
            provider.model,
            time.monotonic() - started_at,
        )
        try:
            return _json_from_utf8_bytes(response)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="上游模型 API 返回了无法解析的 JSON",
            ) from exc

    async def _request_with_failover(
        self,
        *,
        providers: list[LLMProvider],
        request: ChatCompletionRequest,
        messages: list[dict[str, str]],
        thinking: Literal["enabled", "disabled"],
        structured_tool: dict[str, Any] | None,
    ) -> tuple[httpx.Response, LLMProvider]:
        eligible = [
            provider
            for provider in providers
            if self.cooldowns.remaining(provider) <= 0
        ]
        if not eligible:
            raise ProviderCoolingDown("all configured providers are cooling down")

        last_error: httpx.HTTPError | None = None
        for provider in eligible:
            payload = _provider_payload(
                provider=provider,
                request=request,
                messages=messages,
                thinking=thinking,
                structured_tool=structured_tool,
            )
            try:
                return await self._post(provider=provider, payload=payload), provider
            except httpx.HTTPStatusError as exc:
                if not _should_fail_over(exc.response):
                    raise
                last_error = exc
                if exc.response.status_code == 429:
                    self._defer_after_429(provider, exc.response)
                else:
                    logger.warning(
                        "上游 provider 不可用，尝试下一项。provider=%s model=%s status_code=%s",
                        provider.code,
                        provider.model,
                        exc.response.status_code,
                    )
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "上游 provider 网络调用失败，尝试下一项。provider=%s model=%s error_type=%s",
                    provider.code,
                    provider.model,
                    type(exc).__name__,
                )

        if last_error is not None:
            raise last_error
        raise ProviderCoolingDown("all configured providers are cooling down")

    async def _post(
        self,
        *,
        provider: LLMProvider,
        payload: dict[str, Any],
    ) -> httpx.Response:
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        client_kwargs: dict[str, Any] = {
            "timeout": self.settings.request_timeout_seconds,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response

    def _providers(self) -> dict[Literal["M", "K", "D"], LLMProvider]:
        deepseek = LLMProvider(
            code="D",
            base_url=self.settings.llm_deepseek_base_url,
            api_key=self.settings.llm_deepseek_api_key,
            model=self.settings.llm_deepseek_flash_model,
        )
        if not deepseek.configured:
            deepseek = LLMProvider(
                code="D",
                base_url=self.settings.upstream_base_url,
                api_key=self.settings.upstream_api_key,
                model=self.settings.upstream_model,
            )
        return {
            "M": LLMProvider(
                code="M",
                base_url=self.settings.llm_mimo_base_url,
                api_key=self.settings.llm_mimo_api_key,
                model=self.settings.llm_mimo_model,
            ),
            "K": LLMProvider(
                code="K",
                base_url=self.settings.llm_kimi_base_url,
                api_key=self.settings.llm_kimi_api_key,
                model=self.settings.llm_kimi_model,
            ),
            "D": deepseek,
        }

    def _defer_after_429(
        self,
        provider: LLMProvider,
        response: httpx.Response,
    ) -> None:
        retry_after = retry_after_seconds(
            response.headers.get("Retry-After", ""),
            wall_time=self._wall_clock(),
        )
        seconds = max(self.settings.llm_rate_limit_cooldown_seconds, retry_after)
        self.cooldowns.defer(provider, seconds)
        logger.warning(
            "上游模型触发 429，临时跳过 provider=%s model=%s cooldown_seconds=%.1f",
            provider.code,
            provider.model,
            seconds,
        )


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        data = _json_from_utf8_bytes(response)
    except (UnicodeError, json.JSONDecodeError):
        return response.content.decode("utf-8", errors="replace")[:500]
    return str(data)[:500]


def _provider_payload(
    *,
    provider: LLMProvider,
    request: ChatCompletionRequest,
    messages: list[dict[str, str]],
    thinking: Literal["enabled", "disabled"],
    structured_tool: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = request.model_dump(
        exclude_none=True,
        exclude={"conversation_id"},
    )
    payload["model"] = provider.model
    payload["messages"] = messages
    payload["stream"] = False
    if _kimi_requires_temperature_one(provider):
        payload["temperature"] = 1.0
    payload.update(
        _thinking_payload(
            base_url=provider.base_url,
            model=provider.model,
            thinking=thinking,
        )
    )
    if structured_tool is not None and _uses_tool_for_structured_output(provider):
        payload.pop("response_format", None)
        payload["tools"] = [{"type": "function", "function": structured_tool}]
        payload["tool_choice"] = {
            "type": "function",
            "function": {"name": structured_tool["name"]},
        }
    return payload


def _kimi_requires_temperature_one(provider: LLMProvider) -> bool:
    if provider.code != "K":
        return False
    model = provider.model.lower()
    return model.startswith("kimi-k2.7") or model.startswith("kimi-for-coding")


def _uses_tool_for_structured_output(provider: LLMProvider) -> bool:
    return provider.code == "M" and "ultraspeed" in provider.model.lower()


def _should_fail_over(response: httpx.Response) -> bool:
    if response.status_code in {401, 402, 404, 408, 429} or response.status_code >= 500:
        return True
    if response.status_code != 400:
        return False
    detail = _safe_error_detail(response).lower()
    return any(
        marker in detail
        for marker in (
            "invalid model",
            "invalid temperature",
            "model not found",
            "not supported model",
            "unsupported model",
        )
    )


def _json_from_utf8_bytes(response: httpx.Response) -> dict:
    try:
        raw_text = response.content.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("上游响应不是合法 UTF-8，已使用替换字符解码。")
        raw_text = response.content.decode("utf-8", errors="replace")
    return json.loads(raw_text)


def _thinking_payload(
    *,
    base_url: str = "",
    model: str = "",
    thinking: Literal["enabled", "disabled"] = "enabled",
) -> dict:
    provider_text = base_url.lower()
    model_text = model.lower()
    if "moonshot" in provider_text or "kimi" in provider_text:
        if model_text.startswith("kimi-k3"):
            return {"reasoning_effort": "max"} if thinking == "enabled" else {}
        options: dict[str, str] = {"type": thinking}
        if thinking == "enabled" and model_text.startswith("kimi-k2.7"):
            options["keep"] = "all"
        return {"thinking": options}
    if any(marker in provider_text for marker in ("deepseek", "xiaomimimo", "mimo.mi.com")):
        return {"thinking": {"type": thinking}}

    is_zhipu = any(marker in provider_text for marker in ("bigmodel", "z.ai", "zhipu"))
    thinking_model = model_text.startswith(("glm-5", "glm-4.7", "glm-4.6", "glm-4.5"))
    if is_zhipu and thinking_model:
        return {"thinking": {"type": thinking}}
    return {}
