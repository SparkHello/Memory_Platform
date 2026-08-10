from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, AsyncIterator

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
from app.llm.runtime import resolve_model_runtime
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
    """Loss-minimizing OpenAI Chat Completions proxy via Model Gateway.

    Unlike the internal structured-task client, this client preserves stream,
    multimodal parts, tools, reasoning fields, and arbitrary vendor extras.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        wall_clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self.runtime = resolve_model_runtime(settings)
        self.transport = transport
        self._wall_clock = wall_clock

    def list_models(self) -> list[str]:
        route = self.runtime.route_for("chat")
        return [AUTO_MODEL_ID, route]

    async def complete(
        self,
        payload: dict[str, Any],
        *,
        preferred_provider_code: str | None = None,
    ) -> GatewayHTTPResult:
        return await self._complete_via_model_gateway(
            payload,
            preferred_deployment=preferred_provider_code,
        )

    async def open_stream(
        self,
        payload: dict[str, Any],
        *,
        preferred_provider_code: str | None = None,
    ) -> GatewayUpstreamStream:
        return await self._open_model_gateway_stream(
            payload,
            preferred_deployment=preferred_provider_code,
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
        route = self.runtime.route_for("chat")
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
        return f"{self.runtime.base_url}/chat/completions"

    def _central_headers(self, *, affinity: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.runtime.api_key}",
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
        return CentralGatewayProvider(
            code="",
            base_url=self.runtime.base_url,
            model=metadata.upstream_model,
            deployment_id=metadata.deployment_id,
            connection_id=metadata.connection_id,
            vendor=metadata.channel_operator,
            model_author=metadata.model_author,
            route=metadata.route,
        )

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
