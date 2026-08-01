from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, AsyncIterator, Literal

from fastapi import HTTPException, status
import httpx

from app.config import Settings
from app.llm.client import (
    _kimi_requires_temperature_one,
    _thinking_payload,
    configured_llm_providers,
)
from app.llm.routing import (
    GLOBAL_PROVIDER_COOLDOWNS,
    LLMProvider,
    ProviderCooldowns,
    ordered_configured_providers,
    retry_after_seconds,
)


logger = logging.getLogger(__name__)

AUTO_MODEL_ID = "memory-auto"
_AUTO_MODEL_ALIASES = {AUTO_MODEL_ID, "memory-gateway", "auto", "default"}
_PASSTHROUGH_RESPONSE_HEADERS = {
    "content-type",
    "cache-control",
    "retry-after",
    "x-request-id",
    "request-id",
    "openai-processing-ms",
    "openai-version",
}


def is_auto_model_id(model: str) -> bool:
    return model.strip().lower() in _AUTO_MODEL_ALIASES


@dataclass(slots=True)
class GatewayHTTPResult:
    content: bytes
    status_code: int
    headers: dict[str, str]
    provider: LLMProvider


class GatewayUpstreamHTTPError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        content: bytes,
        headers: dict[str, str],
    ) -> None:
        super().__init__(f"upstream returned HTTP {status_code}")
        self.status_code = status_code
        self.content = content
        self.headers = headers


class GatewayUpstreamStream:
    """An already-open successful upstream response.

    Opening the connection before FastAPI sends downstream headers lets the
    route return ordinary JSON errors when every configured provider fails.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        response: httpx.Response,
        provider: LLMProvider,
    ) -> None:
        self.client = client
        self.response = response
        self.provider = provider
        self.headers = passthrough_response_headers(response)
        self._closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_bytes():
            yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class OpenAIChatGatewayClient:
    """Loss-minimizing OpenAI Chat Completions proxy client.

    Unlike the internal structured-task client, this client preserves stream,
    multimodal parts, tools, reasoning fields, and arbitrary vendor extras.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldowns: ProviderCooldowns | None = None,
        wall_clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.cooldowns = cooldowns or GLOBAL_PROVIDER_COOLDOWNS
        self._wall_clock = wall_clock

    def list_models(self) -> list[str]:
        ordered = ordered_configured_providers(
            self.settings.llm_provider_priority,
            configured_llm_providers(self.settings),
        )
        if not ordered:
            return []
        model_ids = [AUTO_MODEL_ID, *(provider.model for provider in ordered)]
        return list(dict.fromkeys(model_ids))

    async def complete(
        self,
        payload: dict[str, Any],
        *,
        preferred_provider_code: str | None = None,
    ) -> GatewayHTTPResult:
        providers = self._providers_for_model(
            str(payload.get("model") or ""),
            preferred_provider_code=preferred_provider_code,
        )
        last_http_error: GatewayUpstreamHTTPError | None = None
        last_network_error: httpx.HTTPError | None = None

        for provider in providers:
            request_payload = self._provider_payload(
                payload,
                provider=provider,
                stream=False,
                reasoning_provider_code=preferred_provider_code,
            )
            try:
                async with self._new_client() as client:
                    response = await client.post(
                        self._chat_url(provider),
                        json=request_payload,
                        headers=self._headers(provider),
                    )
                if response.is_success:
                    return GatewayHTTPResult(
                        content=response.content,
                        status_code=response.status_code,
                        headers=passthrough_response_headers(response),
                        provider=provider,
                    )
                error = GatewayUpstreamHTTPError(
                    status_code=response.status_code,
                    content=response.content,
                    headers=passthrough_response_headers(response),
                )
                if not self._should_fail_over(response.status_code, response.content):
                    raise error
                self._record_rate_limit(provider, response)
                last_http_error = error
            except GatewayUpstreamHTTPError:
                raise
            except httpx.HTTPError as exc:
                last_network_error = exc
                logger.warning(
                    "聊天网关上游网络失败，尝试下一 provider。provider=%s error=%s",
                    provider.code,
                    type(exc).__name__,
                )

        if last_http_error is not None:
            raise last_http_error
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "聊天网关调用上游失败"
                + (f"：{last_network_error}" if last_network_error else "")
            ),
        )

    async def open_stream(
        self,
        payload: dict[str, Any],
        *,
        preferred_provider_code: str | None = None,
    ) -> GatewayUpstreamStream:
        providers = self._providers_for_model(
            str(payload.get("model") or ""),
            preferred_provider_code=preferred_provider_code,
        )
        last_http_error: GatewayUpstreamHTTPError | None = None
        last_network_error: httpx.HTTPError | None = None

        for provider in providers:
            client = self._new_client(stream=True)
            request_payload = self._provider_payload(
                payload,
                provider=provider,
                stream=True,
                reasoning_provider_code=preferred_provider_code,
            )
            try:
                request = client.build_request(
                    "POST",
                    self._chat_url(provider),
                    json=request_payload,
                    headers=self._headers(provider),
                )
                response = await client.send(request, stream=True)
            except BaseException as exc:
                await client.aclose()
                if not isinstance(exc, httpx.HTTPError):
                    raise
                last_network_error = exc
                logger.warning(
                    "聊天网关流式上游连接失败，尝试下一 provider。provider=%s error=%s",
                    provider.code,
                    type(exc).__name__,
                )
                continue

            if response.is_success:
                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and not content_type.startswith(
                    ("text/event-stream", "text/plain")
                ):
                    try:
                        await response.aclose()
                    finally:
                        await client.aclose()
                    last_http_error = GatewayUpstreamHTTPError(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        content=openai_error_payload(
                            message=(
                                "上游在 stream=true 时没有返回 SSE；"
                                f"收到 Content-Type {content_type}"
                            ),
                            code="upstream_stream_protocol_error",
                        ),
                        headers={
                            "content-type": "application/json; charset=utf-8"
                        },
                    )
                    continue
                return GatewayUpstreamStream(
                    client=client,
                    response=response,
                    provider=provider,
                )

            try:
                content = await response.aread()
            except httpx.HTTPError as exc:
                last_network_error = exc
                logger.warning(
                    "聊天网关读取上游流式错误体失败，尝试下一 provider。"
                    "provider=%s error=%s",
                    provider.code,
                    type(exc).__name__,
                )
                continue
            finally:
                await response.aclose()
                await client.aclose()
            error = GatewayUpstreamHTTPError(
                status_code=response.status_code,
                content=content,
                headers=passthrough_response_headers(response),
            )
            should_fail_over = self._should_fail_over(response.status_code, content)
            self._record_rate_limit(provider, response)
            if not should_fail_over:
                raise error
            last_http_error = error

        if last_http_error is not None:
            raise last_http_error
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "聊天网关连接流式上游失败"
                + (f"：{last_network_error}" if last_network_error else "")
            ),
        )

    def _providers_for_model(
        self,
        requested_model: str,
        *,
        preferred_provider_code: str | None = None,
    ) -> list[LLMProvider]:
        requested = requested_model.strip()
        if not requested:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="model 不能为空",
            )
        ordered = ordered_configured_providers(
            self.settings.llm_provider_priority,
            configured_llm_providers(self.settings),
        )
        if not ordered:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="聊天网关没有可用的上游模型；请配置 LLM_* 或 UPSTREAM_*",
            )

        if is_auto_model_id(requested):
            selected = ordered
            preferred = next(
                (
                    provider
                    for provider in selected
                    if provider.code == preferred_provider_code
                ),
                None,
            )
            if preferred is not None:
                selected = [
                    preferred,
                    *(provider for provider in selected if provider != preferred),
                ]
        else:
            selected = [provider for provider in ordered if provider.model == requested]
            if not selected:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"聊天网关未配置模型：{requested}",
                )

        eligible = [
            provider for provider in selected if self.cooldowns.remaining(provider) <= 0
        ]
        if not eligible:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="所选聊天模型暂时处于 429 冷却，请稍后重试",
            )
        return eligible

    def _provider_payload(
        self,
        payload: dict[str, Any],
        *,
        provider: LLMProvider,
        stream: bool,
        reasoning_provider_code: str | None = None,
    ) -> dict[str, Any]:
        forwarded = deepcopy(payload)
        # Local-only compatibility extension; never leak it to providers.
        forwarded.pop("conversation_id", None)
        forwarded["model"] = provider.model
        forwarded["stream"] = stream
        if (
            reasoning_provider_code is not None
            and provider.code != reasoning_provider_code
        ):
            _strip_assistant_reasoning_content(forwarded)
        if _provider_rejects_stream_options(provider):
            # FLIT decides whether to send this field from the gateway host.
            # The gateway must instead apply the rule to the selected upstream.
            forwarded.pop("stream_options", None)
        elif stream:
            stream_options = forwarded.get("stream_options")
            if not isinstance(stream_options, dict):
                stream_options = {}
            forwarded["stream_options"] = {
                **stream_options,
                "include_usage": True,
            }
        if _provider_is_mistral(provider):
            # FLIT deliberately sends no reasoning control when connected
            # directly to Mistral. Its gateway-host fallback would otherwise
            # add unsupported OpenAI-style fields.
            for field in (
                "reasoning_effort",
                "thinking",
                "enable_thinking",
                "thinking_mode",
            ):
                forwarded.pop(field, None)
        if _kimi_requires_temperature_one(provider):
            forwarded["temperature"] = 1.0
        _apply_reasoning_compatibility(
            forwarded,
            provider=provider,
            default_thinking=True,
        )
        _ensure_reasoning_content_for_tool_turns(
            forwarded,
            provider=provider,
        )
        return forwarded

    def _new_client(self, *, stream: bool = False) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            self.settings.request_timeout_seconds,
            read=(
                self.settings.chat_gateway_stream_read_timeout_seconds
                if stream
                else self.settings.request_timeout_seconds
            ),
            write=(
                self.settings.chat_gateway_stream_write_timeout_seconds
                if stream
                else self.settings.request_timeout_seconds
            ),
        )
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _chat_url(provider: LLMProvider) -> str:
        return f"{provider.base_url.rstrip('/')}/chat/completions"

    @staticmethod
    def _headers(provider: LLMProvider) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        }

    @staticmethod
    def _should_fail_over(status_code: int, content: bytes) -> bool:
        if status_code in {401, 402, 404, 408, 429} or status_code >= 500:
            return True
        if status_code != 400:
            return False
        detail = content.decode("utf-8", errors="replace").lower()
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

    def _record_rate_limit(
        self,
        provider: LLMProvider,
        response: httpx.Response,
    ) -> None:
        if response.status_code != 429:
            return
        retry_after = retry_after_seconds(
            response.headers.get("Retry-After", ""),
            wall_time=self._wall_clock(),
        )
        seconds = max(self.settings.llm_rate_limit_cooldown_seconds, retry_after)
        self.cooldowns.defer(provider, seconds)


def passthrough_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() in _PASSTHROUGH_RESPONSE_HEADERS
    }


def _apply_reasoning_compatibility(
    payload: dict[str, Any],
    *,
    provider: LLMProvider,
    default_thinking: bool = False,
) -> None:
    """Translate FLIT's generic effort only when a provider needs `thinking`.

    Explicit provider-native `thinking` is authoritative. Kimi K3 uses
    `reasoning_effort` natively, so its client value must not be overwritten.
    """
    provider_text = f"{provider.base_url} {provider.model}".lower()
    is_deepseek = "deepseek" in provider_text
    native_enabled = _provider_thinking_payload(provider, thinking="enabled")
    if (
        default_thinking
        and "thinking" not in payload
        and "reasoning_effort" not in payload
    ):
        # FLIT's AUTO reasoning level sends no generic field for an unknown
        # custom model such as memory-auto. Resolve AUTO after routing so Kimi
        # gets keep=all, K3 keeps its native effort, and other known providers
        # receive their own thinking switch.
        payload.update(native_enabled)

    if (
        "thinking" in payload
        and "reasoning_effort" in payload
        and "thinking" in native_enabled
    ):
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        thinking_options = payload.get("thinking")
        thinking_type = (
            str(thinking_options.get("type") or "").lower()
            if isinstance(thinking_options, dict)
            else ""
        )
        if (
            is_deepseek
            and thinking_type != "disabled"
            and effort not in {"none", "disabled", "off"}
        ):
            payload["reasoning_effort"] = _deepseek_reasoning_effort(effort)
        else:
            payload.pop("reasoning_effort", None)

    if "thinking" not in payload and "reasoning_effort" in payload:
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        thinking_mode: Literal["enabled", "disabled"] = (
            "disabled"
            if effort in {"none", "disabled", "off"}
            else "enabled"
        )
        translated = _provider_thinking_payload(provider, thinking=thinking_mode)
        if "thinking" in translated:
            payload.update(translated)
            if is_deepseek and thinking_mode == "enabled":
                payload["reasoning_effort"] = _deepseek_reasoning_effort(effort)
            else:
                payload.pop("reasoning_effort", None)

    thinking_options = payload.get("thinking")
    thinking_enabled = (
        isinstance(thinking_options, dict)
        and str(thinking_options.get("type") or "").lower() == "enabled"
    )
    if (
        "deepseek" in provider_text
        and thinking_enabled
        and payload.get("tools")
    ):
        # DeepSeek thinking mode rejects tool_choice even when tools themselves
        # are accepted. FLIT normally omits it, but custom bodies may add it.
        payload.pop("tool_choice", None)


def _deepseek_reasoning_effort(effort: str) -> str:
    return "max" if effort in {"xhigh", "max"} else "high"


def _provider_rejects_stream_options(provider: LLMProvider) -> bool:
    base_url = provider.base_url.lower()
    model = _provider_model_leaf(provider)
    return model.startswith("glm-") or _provider_is_mistral(provider) or any(
        marker in base_url
        for marker in (
            "open.bigmodel.cn",
            "bigmodel",
            "z.ai",
            "zhipu",
        )
    )


def _provider_is_mistral(provider: LLMProvider) -> bool:
    model = _provider_model_leaf(provider)
    return (
        "api.mistral.ai" in provider.base_url.lower()
        or model.startswith(
            (
                "mistral-",
                "open-mistral-",
                "ministral-",
                "codestral-",
                "pixtral-",
                "magistral-",
                "devstral-",
            )
        )
    )


def _provider_model_leaf(provider: LLMProvider) -> str:
    return provider.model.lower().rsplit("/", 1)[-1]


def _provider_requires_reasoning_replay(provider: LLMProvider) -> bool:
    provider_text = f"{provider.base_url} {provider.model}".lower()
    return (
        provider.code in {"M", "K"}
        or any(marker in provider_text for marker in ("deepseek", "kimi", "mimo"))
    )


def _ensure_reasoning_content_for_tool_turns(
    payload: dict[str, Any],
    *,
    provider: LLMProvider,
) -> None:
    """Complete FLIT tool history after the real provider is selected.

    FLIT preserves non-empty reasoning deltas, but for an alias such as
    ``memory-auto`` it cannot know that DeepSeek/Kimi/MiMo also require an
    explicit empty ``reasoning_content`` on assistant tool-call history.
    """
    if not _provider_requires_reasoning_replay(provider):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return

    user_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    for position in range(1, len(user_indices)):
        turn_start = user_indices[position - 1] + 1
        turn_end = user_indices[position]
        turn = messages[turn_start:turn_end]
        if any(_assistant_has_tool_calls(message) for message in turn):
            for message in turn:
                _ensure_assistant_reasoning_content(message)

    last_user_index = user_indices[-1] if user_indices else -1
    for message in messages[last_user_index + 1 :]:
        if _assistant_has_tool_calls(message):
            _ensure_assistant_reasoning_content(message)


def _assistant_has_tool_calls(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and bool(message.get("tool_calls") or message.get("function_call"))
    )


def _ensure_assistant_reasoning_content(message: Any) -> None:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return
    if "reasoning_content" in message:
        return
    reasoning = message.get("reasoning")
    message["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""


def _strip_assistant_reasoning_content(payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        message.pop("reasoning_content", None)
        message.pop("reasoning", None)


def _provider_thinking_payload(
    provider: LLMProvider,
    *,
    thinking: Literal["enabled", "disabled"],
) -> dict[str, Any]:
    # Provider codes remain authoritative even when users put Kimi/MiMo behind
    # a private OpenAI-compatible reverse proxy whose hostname has no marker.
    base_url = provider.base_url
    if provider.code == "K":
        base_url = f"{base_url} kimi"
    elif provider.code == "M":
        base_url = f"{base_url} xiaomimimo"
    else:
        model = _provider_model_leaf(provider)
        if model.startswith("deepseek-"):
            base_url = f"{base_url} deepseek"
        elif model.startswith("glm-"):
            base_url = f"{base_url} zhipu"
        elif model.startswith("kimi-"):
            base_url = f"{base_url} kimi"
        elif "mimo" in model:
            base_url = f"{base_url} xiaomimimo"
    return _thinking_payload(
        base_url=base_url,
        model=_provider_model_leaf(provider),
        thinking=thinking,
    )


def openai_error_payload(*, message: str, code: str) -> bytes:
    return json.dumps(
        {
            "error": {
                "message": message,
                "type": "gateway_error",
                "code": code,
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")
