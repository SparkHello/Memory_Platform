from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import time
from typing import Any, AsyncIterator, Mapping

import httpx

from model_gateway.adapters import (
    apply_connection_adapter,
    strip_reasoning_from_assistant_messages,
)
from model_gateway.auth import provider_secret_header_value
from model_gateway.http_safety import require_safe_destination, upstream_url
from model_gateway.models import FORBIDDEN_UPSTREAM_FORWARD_HEADERS
from model_gateway.routing import (
    ResolvedRoute,
    RouteTarget,
    Router,
    retry_after_seconds,
    should_fail_over,
    structured_error_code,
)
from model_gateway.usage import AttemptTrace, UsageCapture


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


class ProxyNetworkError(RuntimeError):
    pass


class ProxyResponseTooLarge(httpx.ReadError):
    pass


@dataclass(slots=True)
class ProxyHTTPResult:
    content: bytes
    status_code: int
    headers: dict[str, str]
    target: RouteTarget | None
    attempts: int
    attempt_traces: tuple[AttemptTrace, ...] = field(default_factory=tuple)


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
        attempt_traces: tuple[AttemptTrace, ...],
        active_trace: AttemptTrace,
        response_limit_bytes: int,
        attempt_started_monotonic: float,
    ) -> None:
        self.client = client
        self.response = response
        self.iterator = iterator
        self.first_chunk = first_chunk
        self.target = target
        self.attempts = attempts
        self.headers = headers
        self.attempt_traces = attempt_traces
        self.active_trace = active_trace
        self.response_limit_bytes = response_limit_bytes
        self.attempt_started_monotonic = attempt_started_monotonic
        self._closed = False

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        total = 0
        try:
            if self.first_chunk:
                total += len(self.first_chunk)
                _enforce_response_limit(
                    total,
                    self.response_limit_bytes,
                    self.response,
                )
                yield self.first_chunk
            async for chunk in self.iterator:
                if chunk:
                    total += len(chunk)
                    _enforce_response_limit(total, self.response_limit_bytes, self.response)
                    yield chunk
            self.active_trace.response_complete = True
        except httpx.HTTPError as exc:
            self.active_trace.outcome = "ambiguous_failure"
            self.active_trace.failure_class = _network_failure_class(exc)
            self.active_trace.billable_unknown = True
            self.active_trace.response_complete = False
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.response.aclose()


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
        self._clients: dict[tuple[float, float, float, float], httpx.AsyncClient] = {}

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
        attempt_traces: list[AttemptTrace] = []
        for target in route.targets:
            secret = secrets.get(target.connection.auth.secret_ref, "")
            if not secret:
                continue
            if route.route is not None and attempts >= route.route.max_attempts:
                break
            if not self.router.runtime_health.available(target):
                continue
            url = self._url(target)
            if self.transport is None:
                try:
                    await require_safe_destination(
                        url,
                        allowed_private_networks=(
                            target.connection.allowed_private_networks
                        ),
                    )
                except (OSError, ValueError) as exc:
                    last_network_error = httpx.ConnectError(
                        _destination_validation_message(exc)
                    )
                    continue
            attempts += 1
            attempt_started = time.monotonic()
            response: httpx.Response | None = None
            try:
                client = self._client(target)
                request = client.build_request(
                    "POST",
                    url,
                    headers=self._headers(target, secret, request_headers),
                    json=prepare_payload(
                        payload,
                        target,
                        reasoning_origin_deployment=reasoning_origin_deployment,
                    ),
                )
                response = await client.send(request, stream=True)
                try:
                    content = await _read_raw_content(
                        response,
                        limit=target.connection.response_limit_bytes,
                    )
                finally:
                    await response.aclose()
            except httpx.HTTPError as exc:
                last_network_error = exc
                trace = _network_attempt_trace(
                    target=target,
                    attempt_index=attempts,
                    exc=exc,
                    latency_ms=int((time.monotonic() - attempt_started) * 1000),
                )
                attempt_traces.append(trace)
                if response is not None:
                    trace.status_code = response.status_code
                    self._record_health(target, response, b"")
                if _network_error_can_fail_over(exc):
                    continue
                return _network_error_result(
                    exc,
                    route=route,
                    target=target,
                    attempts=attempts,
                    attempt_traces=tuple(attempt_traces),
                )
            trace = _response_attempt_trace(
                target=target,
                attempt_index=attempts,
                response=response,
                content=content,
                latency_ms=int((time.monotonic() - attempt_started) * 1000),
            )
            attempt_traces.append(trace)
            self._record_health(target, response, content)
            if (
                response.is_success
                and target.deployment.kind == "embedding"
                and not _valid_embedding_response(
                    content,
                    dimensions=int(target.deployment.dimensions or 0),
                )
            ):
                trace.outcome = "ambiguous_failure"
                trace.failure_class = "invalid_embedding_response"
                trace.billable_unknown = True
                return _invalid_embedding_result(
                    target=target,
                    attempts=attempts,
                    attempt_traces=tuple(attempt_traces),
                )
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
                attempt_traces=tuple(attempt_traces),
            )
            if response.is_redirect:
                result = _unsafe_redirect_result(result)
            if response.is_success:
                return result
            if not should_fail_over(response.status_code, content):
                return result
            last_result = result

        if last_result is not None:
            if route.required_deployment:
                return affinity_unavailable_result(
                    last_result.attempts,
                    last_result.target,
                    attempt_traces=tuple(attempt_traces),
                )
            return last_result
        if route.required_deployment:
            return affinity_unavailable_result(
                attempts,
                route.targets[0] if route.targets else None,
                attempt_traces=tuple(attempt_traces),
            )
        detail = _network_failure_detail(
            last_network_error,
            phase_label="上游网络连接失败",
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
            attempt_traces=tuple(attempt_traces),
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
        attempt_traces: list[AttemptTrace] = []
        for target in route.targets:
            secret = secrets.get(target.connection.auth.secret_ref, "")
            if not secret:
                continue
            if route.route is not None and attempts >= route.route.max_attempts:
                break
            if not self.router.runtime_health.available(target):
                continue
            url = self._url(target)
            if self.transport is None:
                try:
                    await require_safe_destination(
                        url,
                        allowed_private_networks=(
                            target.connection.allowed_private_networks
                        ),
                    )
                except (OSError, ValueError) as exc:
                    last_network_error = httpx.ConnectError(
                        _destination_validation_message(exc)
                    )
                    continue
            attempts += 1
            attempt_started = time.monotonic()
            client = self._client(target)
            response: httpx.Response | None = None
            try:
                request = client.build_request(
                    "POST",
                    url,
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
                attempt_traces.append(
                    _network_attempt_trace(
                        target=target,
                        attempt_index=attempts,
                        exc=exc,
                        latency_ms=int(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    )
                )
                if _network_error_can_fail_over(exc):
                    continue
                return _network_error_result(
                    exc,
                    route=route,
                    target=target,
                    attempts=attempts,
                    streaming=True,
                    attempt_traces=tuple(attempt_traces),
                )
            if not response.is_success:
                try:
                    content = await _read_raw_content(
                        response,
                        limit=target.connection.response_limit_bytes,
                    )
                except httpx.HTTPError as exc:
                    last_network_error = exc
                    trace = _network_attempt_trace(
                        target=target,
                        attempt_index=attempts,
                        exc=exc,
                        latency_ms=int(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    )
                    trace.request_sent = True
                    trace.billable_unknown = True
                    trace.outcome = "ambiguous_failure"
                    trace.status_code = response.status_code
                    attempt_traces.append(trace)
                    self._record_health(target, response, b"")
                    await response.aclose()
                    return _network_error_result(
                        exc,
                        route=route,
                        target=target,
                        attempts=attempts,
                        streaming=True,
                        attempt_traces=tuple(attempt_traces),
                    )
                trace = _response_attempt_trace(
                    target=target,
                    attempt_index=attempts,
                    response=response,
                    content=content,
                    latency_ms=int((time.monotonic() - attempt_started) * 1000),
                )
                attempt_traces.append(trace)
                self._record_health(target, response, content)
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
                    attempt_traces=tuple(attempt_traces),
                )
                if response.is_redirect:
                    result = _unsafe_redirect_result(result)
                await response.aclose()
                if not should_fail_over(response.status_code, content):
                    return result
                last_result = result
                continue
            iterator = response.aiter_raw().__aiter__()
            trace = _response_attempt_trace(
                target=target,
                attempt_index=attempts,
                response=response,
                content=None,
                latency_ms=int((time.monotonic() - attempt_started) * 1000),
            )
            attempt_traces.append(trace)
            self._record_health(target, response, b"")
            try:
                first_chunk = await _first_non_empty_chunk(iterator)
            except httpx.HTTPError as exc:
                last_network_error = exc
                trace.outcome = "ambiguous_failure"
                trace.failure_class = _network_failure_class(exc)
                trace.billable_unknown = True
                await response.aclose()
                return _network_error_result(
                    exc,
                    route=route,
                    target=target,
                    attempts=attempts,
                    streaming=True,
                    attempt_traces=tuple(attempt_traces),
                )
            if first_chunk is None:
                last_network_error = httpx.ReadError(
                    "upstream stream ended before first byte"
                )
                trace.outcome = "ambiguous_failure"
                trace.failure_class = "empty_stream"
                trace.billable_unknown = True
                await response.aclose()
                return _network_error_result(
                    last_network_error,
                    route=route,
                    target=target,
                    attempts=attempts,
                    streaming=True,
                    attempt_traces=tuple(attempt_traces),
                )
            try:
                _enforce_response_limit(
                    len(first_chunk),
                    target.connection.response_limit_bytes,
                    response,
                )
            except ProxyResponseTooLarge as exc:
                trace.outcome = "ambiguous_failure"
                trace.failure_class = "response_too_large"
                trace.billable_unknown = True
                await response.aclose()
                return _network_error_result(
                    exc,
                    route=route,
                    target=target,
                    attempts=attempts,
                    streaming=True,
                    attempt_traces=tuple(attempt_traces),
                )
            return ProxyUpstreamStream(
                client=client,
                response=response,
                iterator=iterator,
                first_chunk=first_chunk,
                target=target,
                attempts=attempts,
                attempt_traces=tuple(attempt_traces),
                active_trace=trace,
                response_limit_bytes=target.connection.response_limit_bytes,
                attempt_started_monotonic=attempt_started,
                headers=response_headers(
                    response,
                    route=route,
                    target=target,
                    attempts=attempts,
                ),
            )

        if last_result is not None:
            if route.required_deployment:
                return affinity_unavailable_result(
                    last_result.attempts,
                    last_result.target,
                    attempt_traces=tuple(attempt_traces),
                )
            return last_result
        if route.required_deployment:
            return affinity_unavailable_result(
                attempts,
                route.targets[0] if route.targets else None,
                attempt_traces=tuple(attempt_traces),
            )
        detail = _network_failure_detail(
            last_network_error,
            phase_label="上游流连接失败",
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
            attempt_traces=tuple(attempt_traces),
        )

    def _client(self, target: RouteTarget) -> httpx.AsyncClient:
        connection = target.connection
        key = (
            float(connection.connect_timeout_seconds),
            float(connection.read_timeout_seconds),
            float(connection.write_timeout_seconds),
            float(connection.pool_timeout_seconds),
        )
        existing = self._clients.get(key)
        if existing is not None and not existing.is_closed:
            return existing
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(
                connect=connection.connect_timeout_seconds,
                read=connection.read_timeout_seconds,
                write=connection.write_timeout_seconds,
                pool=connection.pool_timeout_seconds,
            ),
            # Never forward an upstream credential across a redirect boundary.
            "follow_redirects": False,
            # Provider credentials must never be redirected through ambient
            # HTTP(S)_PROXY settings inherited from a shell or container.
            "trust_env": False,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        client = httpx.AsyncClient(**kwargs)
        self._clients[key] = client
        return client

    async def aclose(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()

    @staticmethod
    def _url(target: RouteTarget) -> str:
        endpoint = (
            target.connection.chat_endpoint
            if target.deployment.kind == "chat"
            else target.connection.embeddings_endpoint
        )
        return upstream_url(
            target.connection.base_url,
            endpoint,
            allowed_private_networks=target.connection.allowed_private_networks,
        )

    @staticmethod
    def _headers(
        target: RouteTarget,
        secret: str,
        request_headers: Mapping[str, str],
    ) -> dict[str, str]:
        provider_secret_header_value(secret)
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
                and normalized not in FORBIDDEN_UPSTREAM_FORWARD_HEADERS
                and not normalized.startswith("x-model-gateway-")
            ):
                headers[name] = value
        return headers

    def _record_health(
        self,
        target: RouteTarget,
        response: httpx.Response,
        content: bytes,
    ) -> None:
        self.router.runtime_health.record_http(
            target,
            status_code=response.status_code,
            error_code=structured_error_code(content),
            retry_after=retry_after_seconds(
                response.headers.get("Retry-After", ""),
                wall_time=self._wall_clock(),
            ),
        )


def _destination_validation_message(exc: BaseException) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return f"上游地址安全校验失败：{detail}"


def _network_failure_detail(
    exc: httpx.HTTPError | None,
    *,
    phase_label: str,
) -> str:
    if exc is None:
        return "route 没有配置可用的上游密钥"
    message = str(exc).strip()
    if message and message != type(exc).__name__:
        # Prefer the concrete ConnectError message (e.g. destination validation).
        if "上游地址安全校验失败" in message:
            return message
        return f"{phase_label}：{message}"
    return f"{phase_label}：{type(exc).__name__}"


def _network_error_can_fail_over(exc: httpx.HTTPError) -> bool:
    """Only retry failures that happen before an HTTP request can be sent."""

    return isinstance(
        exc,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    )


def _network_failure_class(exc: httpx.HTTPError) -> str:
    if isinstance(exc, ProxyResponseTooLarge):
        return "response_too_large"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.ReadError):
        return "read_error"
    if isinstance(exc, httpx.WriteError):
        return "write_error"
    if isinstance(exc, httpx.ProtocolError):
        return "protocol_error"
    return "other_network"


def _network_attempt_trace(
    *,
    target: RouteTarget,
    attempt_index: int,
    exc: httpx.HTTPError,
    latency_ms: int,
) -> AttemptTrace:
    request_sent = not _network_error_can_fail_over(exc)
    return AttemptTrace(
        attempt_index=attempt_index,
        target=target,
        latency_ms=max(0, latency_ms),
        outcome="ambiguous_failure" if request_sent else "connect_failure",
        failure_class=_network_failure_class(exc),
        request_sent=request_sent,
        billable_unknown=request_sent,
        response_complete=False,
    )


def _http_failure_class(status_code: int, content: bytes) -> str:
    if status_code == 401:
        return "http_auth"
    if status_code == 402:
        return "http_billing"
    if structured_error_code(content) in {
        "deployment_not_found",
        "invalid_model",
        "invalid_model_name",
        "model_not_found",
        "model_not_found_error",
    }:
        return "http_model_not_found"
    if status_code == 429:
        return "http_rate_limit"
    if status_code >= 500:
        return "http_server"
    if 300 <= status_code < 400:
        return "http_redirect"
    return "http_other"


def _response_attempt_trace(
    *,
    target: RouteTarget,
    attempt_index: int,
    response: httpx.Response,
    content: bytes | None,
    latency_ms: int,
) -> AttemptTrace:
    capture = UsageCapture()
    if content is not None:
        capture.from_non_stream(content)
    return AttemptTrace(
        attempt_index=attempt_index,
        target=target,
        status_code=response.status_code,
        latency_ms=max(0, latency_ms),
        outcome="success" if response.is_success else "http_error",
        failure_class=(
            "none"
            if response.is_success
            else _http_failure_class(response.status_code, content or b"")
        ),
        request_sent=True,
        response_complete=content is not None,
        capture=capture,
    )


def _network_error_result(
    exc: httpx.HTTPError,
    *,
    route: ResolvedRoute,
    target: RouteTarget,
    attempts: int,
    streaming: bool = False,
    attempt_traces: tuple[AttemptTrace, ...] = (),
) -> ProxyHTTPResult:
    phase = "流响应" if streaming else "响应"
    return ProxyHTTPResult(
        content=json.dumps(
            {
                "error": {
                    "message": (
                        f"上游{phase}中断：{type(exc).__name__}；"
                        "为避免请求可能已计费，不自动切换 deployment"
                    ),
                    "type": "model_gateway_ambiguous_upstream_error",
                    "code": "model_gateway_ambiguous_upstream_error",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        status_code=502,
        headers={
            "content-type": "application/json; charset=utf-8",
            **target_attribution_headers(
                route=route,
                target=target,
                attempts=attempts,
            ),
        },
        target=target,
        attempts=attempts,
        attempt_traces=attempt_traces,
    )


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
    if target.deployment.kind == "embedding":
        # The deployment's declared vector identity is authoritative. This is
        # intentionally last so an adapter/transform cannot remove or alter it.
        forwarded["dimensions"] = target.deployment.dimensions
    return forwarded


def _valid_embedding_response(content: bytes, *, dimensions: int) -> bool:
    if dimensions < 1:
        return False
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("embedding"), list)
        and len(item["embedding"]) == dimensions
        for item in data
    )


def _invalid_embedding_result(
    *,
    target: RouteTarget,
    attempts: int,
    attempt_traces: tuple[AttemptTrace, ...],
) -> ProxyHTTPResult:
    return ProxyHTTPResult(
        content=json.dumps(
            {
                "error": {
                    "message": "上游 embedding 响应与配置的向量维度不一致",
                    "type": "model_gateway_invalid_embedding_response",
                    "code": "model_gateway_invalid_embedding_response",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        status_code=502,
        # Attribution headers are withheld because the vector identity has not
        # been validated. The internal target remains available to usage ledgers.
        headers={"content-type": "application/json; charset=utf-8"},
        target=target,
        attempts=attempts,
        attempt_traces=attempt_traces,
    )


async def _read_raw_content(response: httpx.Response, *, limit: int) -> bytes:
    """Collect an HTTP response without applying content decoding.

    ``httpx.Response.content`` is decoded according to ``Content-Encoding``.
    Passing those decoded bytes downstream together with the original encoding
    header corrupts the response, so the transparent path must consume the raw
    stream.  ``MockTransport`` may construct an already-buffered response; that
    case is only a compatibility fallback for in-process transports.
    """

    if response.is_stream_consumed:
        content = response.content
        _enforce_response_limit(len(content), limit, response)
        return content
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        _enforce_response_limit(total, limit, response)
        chunks.append(chunk)
    return b"".join(chunks)


def _enforce_response_limit(
    size: int,
    limit: int,
    response: httpx.Response,
) -> None:
    if size <= limit:
        return
    raise ProxyResponseTooLarge(
        "upstream response exceeded configured byte limit",
        request=response.request,
    )


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
        target_attribution_headers(
            route=route,
            target=target,
            attempts=attempts,
        )
    )
    return headers


def target_attribution_headers(
    *,
    route: ResolvedRoute,
    target: RouteTarget,
    attempts: int,
) -> dict[str, str]:
    headers = {
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
    if target.deployment.kind == "embedding":
        headers["x-model-gateway-embedding-space"] = target.deployment.embedding_space
        headers["x-model-gateway-embedding-dimensions"] = str(
            target.deployment.dimensions
        )
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
        attempt_traces=result.attempt_traces,
    )


def affinity_unavailable_result(
    attempts: int,
    target: RouteTarget | None,
    *,
    attempt_traces: tuple[AttemptTrace, ...] = (),
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
        attempt_traces=attempt_traces,
    )
