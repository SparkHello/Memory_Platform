from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import time
from typing import Any, AsyncIterator, Mapping

import httpx

from model_gateway.adapters import (
    apply_connection_adapter,
    strip_reasoning_from_assistant_messages,
)
from model_gateway.routing import (
    ResolvedRoute,
    RouteTarget,
    Router,
    retry_after_seconds,
    should_fail_over,
)


PASSTHROUGH_RESPONSE_HEADERS = {
    "content-type",
    "cache-control",
    "retry-after",
    "x-request-id",
    "request-id",
    "openai-processing-ms",
    "openai-version",
    "content-encoding",
}
FORBIDDEN_FORWARD_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
}


class ProxyNetworkError(RuntimeError):
    pass


@dataclass(slots=True)
class ProxyHTTPResult:
    content: bytes
    status_code: int
    headers: dict[str, str]
    target: RouteTarget | None
    attempts: int


class ProxyUpstreamStream:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        response: httpx.Response,
        iterator: AsyncIterator[bytes],
        first_chunk: bytes | None,
        target: RouteTarget,
        attempts: int,
        headers: dict[str, str],
    ) -> None:
        self.client = client
        self.response = response
        self.iterator = iterator
        self.first_chunk = first_chunk
        self.target = target
        self.attempts = attempts
        self.headers = headers
        self._closed = False

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        if self.first_chunk:
            yield self.first_chunk
        async for chunk in self.iterator:
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class RawOpenAIProxy:
    def __init__(
        self,
        *,
        router: Router,
        transport: httpx.AsyncBaseTransport | None = None,
        wall_clock: Any = time.time,
    ) -> None:
        self.router = router
        self.transport = transport
        self._wall_clock = wall_clock

    async def complete(
        self,
        *,
        route: ResolvedRoute,
        payload: dict[str, Any],
        secrets: Mapping[str, str],
        request_headers: Mapping[str, str],
        reasoning_origin_deployment: str = "",
    ) -> ProxyHTTPResult:
        last_result: ProxyHTTPResult | None = None
        last_network_error: httpx.HTTPError | None = None
        attempts = 0
        for target in route.targets:
            secret = secrets.get(target.connection.auth.secret_ref, "")
            if not secret:
                continue
            if route.route is not None and attempts >= route.route.max_attempts:
                break
            attempts += 1
            try:
                async with self._client(target) as client:
                    request = client.build_request(
                        "POST",
                        self._url(target, stream=False),
                        headers=self._headers(target, secret, request_headers),
                        json=prepare_payload(
                            payload,
                            target,
                            reasoning_origin_deployment=reasoning_origin_deployment,
                        ),
                    )
                    response = await client.send(request, stream=True)
                    try:
                        content = await _read_raw_content(response)
                    finally:
                        await response.aclose()
            except httpx.HTTPError as exc:
                last_network_error = exc
                continue
            result = ProxyHTTPResult(
                content=content,
                status_code=response.status_code,
                headers=response_headers(
                    response,
                    route=route,
                    target=target,
                    attempts=attempts,
                ),
                target=target,
                attempts=attempts,
            )
            if response.is_redirect:
                result = _unsafe_redirect_result(result)
            if response.is_success:
                return result
            if not should_fail_over(response.status_code, content):
                return result
            self._record_cooldown(target, response)
            last_result = result

        if last_result is not None:
            if route.required_deployment:
                return affinity_unavailable_result(last_result.attempts, last_result.target)
            return last_result
        if route.required_deployment:
            return affinity_unavailable_result(attempts, route.targets[0] if route.targets else None)
        detail = (
            f"上游网络连接失败：{type(last_network_error).__name__}"
            if last_network_error is not None
            else "route 没有配置可用的上游密钥"
        )
        return ProxyHTTPResult(
            content=json.dumps(
                {"error": {"message": detail, "type": "gateway_error"}},
                ensure_ascii=False,
            ).encode("utf-8"),
            status_code=502,
            headers={"content-type": "application/json; charset=utf-8"},
            target=None,
            attempts=attempts,
        )

    async def open_stream(
        self,
        *,
        route: ResolvedRoute,
        payload: dict[str, Any],
        secrets: Mapping[str, str],
        request_headers: Mapping[str, str],
        reasoning_origin_deployment: str = "",
    ) -> ProxyUpstreamStream | ProxyHTTPResult:
        last_result: ProxyHTTPResult | None = None
        last_network_error: httpx.HTTPError | None = None
        attempts = 0
        for target in route.targets:
            secret = secrets.get(target.connection.auth.secret_ref, "")
            if not secret:
                continue
            if route.route is not None and attempts >= route.route.max_attempts:
                break
            attempts += 1
            client = self._client(target, stream=True)
            try:
                request = client.build_request(
                    "POST",
                    self._url(target, stream=True),
                    headers=self._headers(target, secret, request_headers),
                    json=prepare_payload(
                        payload,
                        target,
                        reasoning_origin_deployment=reasoning_origin_deployment,
                    ),
                )
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                last_network_error = exc
                await client.aclose()
                continue
            if not response.is_success:
                content = await response.aread()
                result = ProxyHTTPResult(
                    content=content,
                    status_code=response.status_code,
                    headers=response_headers(
                        response,
                        route=route,
                        target=target,
                        attempts=attempts,
                    ),
                    target=target,
                    attempts=attempts,
                )
                if response.is_redirect:
                    result = _unsafe_redirect_result(result)
                await response.aclose()
                await client.aclose()
                if not should_fail_over(response.status_code, content):
                    return result
                self._record_cooldown(target, response)
                last_result = result
                continue
            iterator = response.aiter_raw().__aiter__()
            try:
                first_chunk = await _first_non_empty_chunk(iterator)
            except httpx.HTTPError as exc:
                last_network_error = exc
                await response.aclose()
                await client.aclose()
                continue
            if first_chunk is None:
                last_network_error = httpx.ReadError("upstream stream ended before first byte")
                await response.aclose()
                await client.aclose()
                continue
            return ProxyUpstreamStream(
                client=client,
                response=response,
                iterator=iterator,
                first_chunk=first_chunk,
                target=target,
                attempts=attempts,
                headers=response_headers(
                    response,
                    route=route,
                    target=target,
                    attempts=attempts,
                ),
            )

        if last_result is not None:
            if route.required_deployment:
                return affinity_unavailable_result(last_result.attempts, last_result.target)
            return last_result
        if route.required_deployment:
            return affinity_unavailable_result(attempts, route.targets[0] if route.targets else None)
        detail = (
            f"上游流连接失败：{type(last_network_error).__name__}"
            if last_network_error is not None
            else "route 没有配置可用的上游密钥"
        )
        return ProxyHTTPResult(
            content=json.dumps(
                {"error": {"message": detail, "type": "gateway_error"}},
                ensure_ascii=False,
            ).encode("utf-8"),
            status_code=502,
            headers={"content-type": "application/json; charset=utf-8"},
            target=None,
            attempts=attempts,
        )

    def _client(self, target: RouteTarget, *, stream: bool = False) -> httpx.AsyncClient:
        timeout = target.connection.timeout_seconds
        read_timeout = timeout
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(
                connect=min(timeout, 30.0),
                read=read_timeout,
                write=timeout,
                pool=timeout,
            ),
            # Never forward an upstream credential across a redirect boundary.
            "follow_redirects": False,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _url(target: RouteTarget, *, stream: bool) -> str:
        del stream
        endpoint = (
            target.connection.chat_endpoint
            if target.deployment.kind == "chat"
            else target.connection.embeddings_endpoint
        )
        return f"{target.connection.base_url}{endpoint}"

    @staticmethod
    def _headers(
        target: RouteTarget,
        secret: str,
        request_headers: Mapping[str, str],
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": request_headers.get("accept", "application/json"),
            "Accept-Encoding": "identity",
        }
        if target.connection.auth.type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        else:
            headers["X-Api-Key"] = secret
        allowed = {name.lower() for name in target.connection.forward_headers}
        for name, value in request_headers.items():
            normalized = name.lower()
            if (
                normalized in allowed
                and normalized not in FORBIDDEN_FORWARD_HEADERS
                and not normalized.startswith("x-model-gateway-")
            ):
                headers[name] = value
        return headers

    def _record_cooldown(self, target: RouteTarget, response: httpx.Response) -> None:
        if response.status_code != 429:
            return
        seconds = max(
            target.connection.rate_limit_cooldown_seconds,
            retry_after_seconds(
                response.headers.get("Retry-After", ""),
                wall_time=self._wall_clock(),
            ),
        )
        self.router.cooldowns.defer(target.connection_id, seconds)


def prepare_payload(
    payload: dict[str, Any],
    target: RouteTarget,
    *,
    reasoning_origin_deployment: str = "",
) -> dict[str, Any]:
    forwarded = deepcopy(payload)
    forwarded["model"] = target.deployment.upstream_model
    if (
        reasoning_origin_deployment
        and reasoning_origin_deployment != target.deployment_id
    ):
        strip_reasoning_from_assistant_messages(forwarded)
    apply_connection_adapter(
        forwarded,
        connection=target.connection,
        deployment=target.deployment,
    )
    transform = target.deployment.request_transform
    for name in transform.remove:
        forwarded.pop(name, None)
    for name, value in transform.set_if_missing.items():
        forwarded.setdefault(name, deepcopy(value))
    for name, value in transform.force.items():
        forwarded[name] = deepcopy(value)
    return forwarded


async def _read_raw_content(response: httpx.Response) -> bytes:
    """Collect an HTTP response without applying content decoding.

    ``httpx.Response.content`` is decoded according to ``Content-Encoding``.
    Passing those decoded bytes downstream together with the original encoding
    header corrupts the response, so the transparent path must consume the raw
    stream.  ``MockTransport`` may construct an already-buffered response; that
    case is only a compatibility fallback for in-process transports.
    """

    if response.is_stream_consumed:
        return response.content
    return b"".join([chunk async for chunk in response.aiter_raw()])


async def _first_non_empty_chunk(iterator: AsyncIterator[bytes]) -> bytes | None:
    while True:
        chunk = await anext(iterator, None)
        if chunk is None or chunk:
            return chunk


def response_headers(
    response: httpx.Response,
    *,
    route: ResolvedRoute,
    target: RouteTarget,
    attempts: int,
) -> dict[str, str]:
    headers = {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() in PASSTHROUGH_RESPONSE_HEADERS
    }
    headers.update(
        {
            "x-model-gateway-route": route.route_id,
            "x-model-gateway-deployment": target.deployment_id,
            "x-model-gateway-connection": target.connection_id,
            "x-model-gateway-channel-operator": target.connection.channel_operator,
            "x-model-gateway-model-author": target.deployment.model_author,
            # Compatibility alias from the first protocol revision. Its value
            # remains the channel operator, never the model author.
            "x-model-gateway-vendor": target.connection.channel_operator,
            "x-model-gateway-upstream-model": target.deployment.upstream_model,
            "x-model-gateway-attempts": str(attempts),
        }
    )
    if target.deployment.kind == "embedding":
        headers["x-model-gateway-embedding-space"] = target.deployment.embedding_space
        headers["x-model-gateway-embedding-dimensions"] = str(target.deployment.dimensions)
    if target.deployment.pricing:
        headers["x-model-gateway-pricing"] = target.deployment.pricing
    return headers


def _unsafe_redirect_result(result: ProxyHTTPResult) -> ProxyHTTPResult:
    """Do not expose a remote Location to a caller holding a local bearer key."""

    return ProxyHTTPResult(
        content=json.dumps(
            {
                "error": {
                    "message": "上游端点返回重定向；请把 connection.base_url 配置为最终 HTTPS 地址",
                    "type": "gateway_upstream_redirect",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        status_code=502,
        headers={
            **result.headers,
            "content-type": "application/json; charset=utf-8",
        },
        target=result.target,
        attempts=result.attempts,
    )


def affinity_unavailable_result(
    attempts: int, target: RouteTarget | None
) -> ProxyHTTPResult:
    return ProxyHTTPResult(
        content=json.dumps(
            {
                "error": {
                    "message": "要求的原 deployment 当前不可用；调用方必须移除该来源的私有推理后再重试",
                    "type": "model_gateway_affinity_unavailable",
                    "code": "model_gateway_affinity_unavailable",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        status_code=409,
        headers={"content-type": "application/json; charset=utf-8"},
        target=target,
        attempts=attempts,
    )
