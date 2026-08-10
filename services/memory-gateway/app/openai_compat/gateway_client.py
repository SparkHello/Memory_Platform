from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, AsyncIterator, Literal

from fastapi import HTTPException, status
import httpx

from app.config import Settings
from app.llm.model_gateway import (
    MODEL_GATEWAY_PREFERRED_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_REASONING_ORIGIN_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_REQUIRE_DEPLOYMENT_HEADER,
    ModelGatewayProtocolError,
    parse_model_gateway_metadata,
    validate_model_gateway_metadata,
)
from app.llm.protocol import apply_reasoning_compatibility, should_fail_over
from app.llm.runtime import resolve_model_runtime
from app.llm.routing import (
    GLOBAL_PROVIDER_COOLDOWNS,
    LLMProvider,
    ProviderCooldowns,
    retry_after_seconds,
)
from app.providers.catalog import providers_for_route
from app.usage.attribution import model_gateway_usage_headers


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
_MODEL_GATEWAY_RESPONSE_HEADERS = {
    "x-model-gateway-route",
    "x-model-gateway-deployment",
    "x-model-gateway-connection",
    "x-model-gateway-channel-operator",
    "x-model-gateway-model-author",
    "x-model-gateway-vendor",
    "x-model-gateway-upstream-model",
    "x-model-gateway-attempts",
    "x-model-gateway-pricing",
    "x-model-gateway-embedding-space",
    "x-model-gateway-embedding-dimensions",
    "x-model-gateway-usage-event-id",
    "x-model-gateway-correlation-id",
    "x-model-gateway-usage-ledger-status",
}


def is_auto_model_id(model: str) -> bool:
    return model.strip().lower() in _AUTO_MODEL_ALIASES


@dataclass(slots=True)
class GatewayHTTPResult:
    content: bytes
    status_code: int
    headers: dict[str, str]
    provider: Any


@dataclass(frozen=True, slots=True)
class CentralGatewayProvider:
    """Validated central attribution exposed to chat finalization and usage."""

    code: str
    base_url: str
    model: str
    deployment_id: str
    connection_id: str
    vendor: str
    model_author: str
    route: str


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
        provider: Any,
        include_model_gateway_headers: bool = False,
    ) -> None:
        self.client = client
        self.response = response
        self.provider = provider
        self.headers = passthrough_response_headers(
            response,
            include_model_gateway=include_model_gateway_headers,
        )
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
        self.runtime = resolve_model_runtime(settings)
        self.transport = transport
        self.cooldowns = cooldowns or GLOBAL_PROVIDER_COOLDOWNS
        self._wall_clock = wall_clock

    def list_models(self) -> list[str]:
        runtime = self.runtime
        if runtime.is_central:
            route = runtime.route_for("chat")
            return [AUTO_MODEL_ID, route]
        ordered = providers_for_route(self.settings, "chat")
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
        runtime = self.runtime
        if runtime.is_central:
            return await self._complete_via_model_gateway(
                payload,
                preferred_deployment=preferred_provider_code,
            )
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
                if not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise HTTPException(
                        status_code=(
                            status.HTTP_504_GATEWAY_TIMEOUT
                            if isinstance(exc, httpx.TimeoutException)
                            else status.HTTP_502_BAD_GATEWAY
                        ),
                        detail="聊天网关上游请求失败；为避免重复计费，未自动重发",
                    ) from exc
                last_network_error = exc
                logger.warning(
                    "聊天网关上游建连失败，尝试下一 provider。provider=%s error=%s",
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
        runtime = self.runtime
        if runtime.is_central:
            return await self._open_model_gateway_stream(
                payload,
                preferred_deployment=preferred_provider_code,
            )
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
                if not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise HTTPException(
                        status_code=(
                            status.HTTP_504_GATEWAY_TIMEOUT
                            if isinstance(exc, httpx.TimeoutException)
                            else status.HTTP_502_BAD_GATEWAY
                        ),
                        detail="聊天网关流式上游请求失败；为避免重复计费，未自动重发",
                    ) from exc
                last_network_error = exc
                logger.warning(
                    "聊天网关流式上游建连失败，尝试下一 provider。provider=%s error=%s",
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
                    raise GatewayUpstreamHTTPError(
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
                return GatewayUpstreamStream(
                    client=client,
                    response=response,
                    provider=provider,
                )

            try:
                content = await response.aread()
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=(
                        status.HTTP_504_GATEWAY_TIMEOUT
                        if isinstance(exc, httpx.TimeoutException)
                        else status.HTTP_502_BAD_GATEWAY
                    ),
                    detail="聊天网关读取上游错误失败；为避免重复计费，未自动重发",
                ) from exc
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

    async def _complete_via_model_gateway(
        self,
        payload: dict[str, Any],
        *,
        preferred_deployment: str | None,
    ) -> GatewayHTTPResult:
        route = self._central_chat_route(payload)
        forwarded = self._central_payload(payload, route=route, stream=False)
        affinity = (preferred_deployment or "").strip()
        response = await self._post_central(forwarded, affinity=affinity)
        if not response.is_success:
            raise GatewayUpstreamHTTPError(
                status_code=response.status_code,
                content=response.content,
                headers=passthrough_response_headers(
                    response,
                    include_model_gateway=True,
                ),
            )
        provider = self._central_provider(
            response,
            expected_route=route,
            expected_deployment=affinity,
        )
        return GatewayHTTPResult(
            content=response.content,
            status_code=response.status_code,
            headers=passthrough_response_headers(
                response,
                include_model_gateway=True,
            ),
            provider=provider,
        )

    async def _open_model_gateway_stream(
        self,
        payload: dict[str, Any],
        *,
        preferred_deployment: str | None,
    ) -> GatewayUpstreamStream:
        route = self._central_chat_route(payload)
        forwarded = self._central_payload(payload, route=route, stream=True)
        affinity = (preferred_deployment or "").strip()

        client = self._new_client(stream=True)
        try:
            request = client.build_request(
                "POST",
                self._central_chat_url(),
                json=forwarded,
                headers=self._central_headers(affinity=affinity),
            )
            response = await client.send(request, stream=True)
        except BaseException as exc:
            await client.aclose()
            if not isinstance(exc, httpx.HTTPError):
                raise
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="调用中央模型网关失败",
            ) from exc

        if response.is_success:
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith(
                ("text/event-stream", "text/plain")
            ):
                await response.aclose()
                await client.aclose()
                raise GatewayUpstreamHTTPError(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    content=openai_error_payload(
                        message=(
                            "中央模型网关在 stream=true 时没有返回 SSE；"
                            f"收到 Content-Type {content_type}"
                        ),
                        code="upstream_stream_protocol_error",
                    ),
                    headers={"content-type": "application/json; charset=utf-8"},
                )
            try:
                provider = self._central_provider(
                    response,
                    expected_route=route,
                    expected_deployment=affinity,
                )
            except GatewayUpstreamHTTPError:
                await response.aclose()
                await client.aclose()
                raise
            return GatewayUpstreamStream(
                client=client,
                response=response,
                provider=provider,
                include_model_gateway_headers=True,
            )

        try:
            content = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        raise GatewayUpstreamHTTPError(
            status_code=response.status_code,
            content=content,
            headers=passthrough_response_headers(
                response,
                include_model_gateway=True,
            ),
        )

    async def _post_central(
        self,
        payload: dict[str, Any],
        *,
        affinity: str,
    ) -> httpx.Response:
        try:
            async with self._new_client() as client:
                return await client.post(
                    self._central_chat_url(),
                    json=payload,
                    headers=self._central_headers(affinity=affinity),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="调用中央模型网关失败",
            ) from exc

    def _central_chat_route(self, payload: dict[str, Any]) -> str:
        runtime = self.runtime
        route = runtime.route_for("chat")
        requested = str(payload.get("model") or "").strip()
        if not requested:
            raise HTTPException(status_code=422, detail="model 不能为空")
        if requested not in {AUTO_MODEL_ID, route}:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"聊天网关未配置模型：{requested}",
            )
        return route

    @staticmethod
    def _central_payload(
        payload: dict[str, Any],
        *,
        route: str,
        stream: bool,
    ) -> dict[str, Any]:
        forwarded = deepcopy(payload)
        forwarded.pop("conversation_id", None)
        forwarded["model"] = route
        forwarded["stream"] = stream
        return forwarded

    def _central_chat_url(self) -> str:
        runtime = self.runtime
        return f"{runtime.base_url}/chat/completions"

    def _central_headers(self, *, affinity: str) -> dict[str, str]:
        runtime = self.runtime
        headers = {
            "Authorization": f"Bearer {runtime.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
            **model_gateway_usage_headers(
                signing_secret=self.settings.gateway_signing_secret,
                operation="chat_completion",
            ),
        }
        if affinity:
            headers[MODEL_GATEWAY_PREFERRED_DEPLOYMENT_HEADER] = affinity
            headers[MODEL_GATEWAY_REQUIRE_DEPLOYMENT_HEADER] = affinity
            headers[MODEL_GATEWAY_REASONING_ORIGIN_DEPLOYMENT_HEADER] = affinity
        return headers

    def _central_provider(
        self,
        response: httpx.Response,
        *,
        expected_route: str,
        expected_deployment: str,
    ) -> CentralGatewayProvider:
        metadata = parse_model_gateway_metadata(response.headers)
        try:
            validate_model_gateway_metadata(
                metadata,
                expected_route=expected_route,
                expected_deployment=expected_deployment,
            )
        except ModelGatewayProtocolError as exc:
            raise GatewayUpstreamHTTPError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content=openai_error_payload(
                    message=str(exc),
                    code="model_gateway_protocol_error",
                ),
                headers={"content-type": "application/json; charset=utf-8"},
            ) from exc
        runtime = self.runtime
        return CentralGatewayProvider(
            code="",
            base_url=runtime.base_url,
            model=metadata.upstream_model,
            deployment_id=metadata.deployment_id,
            connection_id=metadata.connection_id,
            vendor=metadata.channel_operator,
            model_author=metadata.model_author,
            route=metadata.route,
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
                status_code=422,
                detail="model 不能为空",
            )
        ordered = providers_for_route(self.settings, "chat")
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
        if provider.quirks.rejects_stream_options:
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
        if provider.quirks.forces_temperature_one:
            forwarded["temperature"] = 1.0
        apply_reasoning_compatibility(
            forwarded,
            quirks=provider.quirks,
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
            # Never replay a bearer credential to a redirect target. Provider
            # and central-gateway base URLs must already be canonical.
            "follow_redirects": False,
            # Bearer credentials must never be handed to an ambient
            # HTTP(S)_PROXY inherited by the service process.
            "trust_env": False,
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
        return should_fail_over(status_code, content)

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


def passthrough_response_headers(
    response: httpx.Response,
    *,
    include_model_gateway: bool = False,
) -> dict[str, str]:
    allowed = set(_PASSTHROUGH_RESPONSE_HEADERS)
    if include_model_gateway:
        allowed.update(_MODEL_GATEWAY_RESPONSE_HEADERS)
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() in allowed
    }


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
    if not provider.quirks.requires_reasoning_replay:
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
