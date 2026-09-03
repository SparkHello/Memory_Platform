"""Exact-target provider POST execution and accounting primitives.

Routing, fallback and data-plane circuit breakers deliberately stay outside
this module.  One call targets one already-selected deployment and owns the
wire invariants shared by buffered inference, raw streaming, live health
checks, capability probes and pricing research: URL construction, SSRF
validation, credential headers, timeouts, redirect refusal, bounded raw
response reads or leases, and metadata-only attempt capture.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import socket
import time
from typing import Any, AsyncIterator, Mapping

import httpx

from model_gateway.auth import provider_secret_header_value
from model_gateway.http_safety import require_safe_destination, upstream_url
from model_gateway_contracts import (
    FORBIDDEN_UPSTREAM_FORWARD_HEADERS,
    ConnectionConfig,
    PricingConfig,
    ServerConfig,
)
from model_gateway.routing import RouteTarget, structured_error_code
from model_gateway.storage import (
    ensure_write_capacity,
    estimated_ledger_write_bytes,
)
from model_gateway.usage import AttemptTrace, UsageCapture, UsageMetadata, UsageStore


class ProxyResponseTooLarge(httpx.ReadError):
    pass


class UsageLedgerPreflightError(RuntimeError):
    """The usage ledger could not be proven writable before a paid POST."""


class UsageLedgerRecordError(RuntimeError):
    """A provider attempt completed but its metadata-only record failed."""


@dataclass(slots=True)
class BufferedUpstreamResult:
    target: RouteTarget
    content: bytes = b""
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    trace: AttemptTrace | None = None
    error_type: str = ""
    error_detail: str = ""
    usage_event_id: str = ""
    usage_ledger_status: str = ""

    @property
    def is_success(self) -> bool:
        return (
            self.trace is not None
            and not self.error_type
            and self.status_code is not None
            and 200 <= self.status_code < 300
        )

    @property
    def is_redirect(self) -> bool:
        return (
            self.status_code is not None
            and 300 <= self.status_code < 400
        )


class UpstreamStreamLease:
    """One already-started, bounded raw upstream response stream."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        response: httpx.Response,
        iterator: AsyncIterator[bytes],
        first_chunk: bytes,
        trace: AttemptTrace,
        response_limit_bytes: int,
        attempt_started_monotonic: float,
    ) -> None:
        self.client = client
        self.response = response
        self.iterator = iterator
        self.first_chunk = first_chunk
        self.trace = trace
        self.response_limit_bytes = response_limit_bytes
        self.attempt_started_monotonic = attempt_started_monotonic
        self._closed = False

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        total = 0
        try:
            total += len(self.first_chunk)
            enforce_response_limit(
                total,
                self.response_limit_bytes,
                self.response,
            )
            yield self.first_chunk
            async for chunk in self.iterator:
                if not chunk:
                    continue
                total += len(chunk)
                enforce_response_limit(
                    total,
                    self.response_limit_bytes,
                    self.response,
                )
                yield chunk
            self.trace.response_complete = True
        except httpx.HTTPError as exc:
            self.trace.outcome = "ambiguous_failure"
            self.trace.failure_class = network_failure_class(exc)
            self.trace.billable_unknown = True
            self.trace.response_complete = False
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.response.aclose()


@dataclass(slots=True)
class StreamingUpstreamResult:
    target: RouteTarget
    content: bytes = b""
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    trace: AttemptTrace | None = None
    lease: UpstreamStreamLease | None = None
    error: httpx.HTTPError | None = field(default=None, repr=False)
    error_type: str = ""
    error_detail: str = ""

    @property
    def is_success(self) -> bool:
        return (
            self.lease is not None
            and not self.error_type
            and self.status_code is not None
            and 200 <= self.status_code < 300
        )

    @property
    def is_redirect(self) -> bool:
        return (
            self.status_code is not None
            and 300 <= self.status_code < 400
        )


@dataclass(slots=True)
class _StartedJsonPost:
    client: httpx.AsyncClient | None = None
    response: httpx.Response | None = None
    started_monotonic: float = 0.0
    error: httpx.HTTPError | None = None
    local_error_type: str = ""
    local_error_detail: str = ""


class UpstreamExecutor:
    """Execute one bounded JSON POST against one exact ``RouteTarget``."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.transport = transport
        self._clients: dict[tuple[float, float, float, float], httpx.AsyncClient] = {}

    async def __aenter__(self) -> "UpstreamExecutor":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def post_json(
        self,
        *,
        target: RouteTarget,
        payload: dict[str, Any],
        secret: str,
        request_headers: Mapping[str, str] | None = None,
        attempt_index: int = 1,
        response_limit_bytes: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BufferedUpstreamResult:
        """Return a finite result for one exact target without retrying.

        Local URL, SSRF or credential failures happen before an HTTP send and
        therefore have no ``AttemptTrace``.  Once the transport is invoked,
        every outcome carries exactly one trace whose body-derived state is a
        bounded ``UsageCapture``; request and response bodies never enter it.
        """

        started = await self._start_json_post(
            target=target,
            payload=payload,
            secret=secret,
            request_headers=request_headers or {},
            timeout_seconds=timeout_seconds,
        )
        if started.local_error_type:
            return BufferedUpstreamResult(
                target=target,
                error_type=started.local_error_type,
                error_detail=started.local_error_detail,
            )
        limit = int(
            response_limit_bytes
            if response_limit_bytes is not None
            else target.connection.response_limit_bytes
        )
        if started.error is not None:
            trace = network_attempt_trace(
                target=target,
                attempt_index=attempt_index,
                exc=started.error,
                latency_ms=_elapsed_ms(started.started_monotonic),
            )
            return BufferedUpstreamResult(
                target=target,
                trace=trace,
                error_type=type(started.error).__name__,
            )

        response = started.response
        assert response is not None
        try:
            try:
                content = await read_raw_content(response, limit=limit)
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            trace = network_attempt_trace(
                target=target,
                attempt_index=attempt_index,
                exc=exc,
                latency_ms=_elapsed_ms(started.started_monotonic),
            )
            trace.status_code = response.status_code
            return BufferedUpstreamResult(
                target=target,
                status_code=response.status_code,
                headers=dict(response.headers),
                trace=trace,
                error_type=type(exc).__name__,
            )

        trace = response_attempt_trace(
            target=target,
            attempt_index=attempt_index,
            response=response,
            content=content,
            latency_ms=_elapsed_ms(started.started_monotonic),
            streaming=payload.get("stream") is True,
        )
        return BufferedUpstreamResult(
            target=target,
            content=content,
            status_code=response.status_code,
            headers=dict(response.headers),
            trace=trace,
        )

    async def open_json_stream(
        self,
        *,
        target: RouteTarget,
        payload: dict[str, Any],
        secret: str,
        request_headers: Mapping[str, str] | None = None,
        attempt_index: int = 1,
        response_limit_bytes: int | None = None,
        timeout_seconds: float | None = None,
    ) -> StreamingUpstreamResult:
        """Open one exact raw stream and return only after its first byte.

        HTTP failures are drained into a bounded buffered result. Transport
        failures after a request may have been sent, an empty successful
        stream, and first-byte failures are terminal exact-target outcomes.
        The returned lease never retries or chooses another deployment.
        """

        started = await self._start_json_post(
            target=target,
            payload=payload,
            secret=secret,
            request_headers=request_headers or {},
            timeout_seconds=timeout_seconds,
        )
        if started.local_error_type:
            return StreamingUpstreamResult(
                target=target,
                error_type=started.local_error_type,
                error_detail=started.local_error_detail,
            )
        if started.error is not None:
            trace = network_attempt_trace(
                target=target,
                attempt_index=attempt_index,
                exc=started.error,
                latency_ms=_elapsed_ms(started.started_monotonic),
            )
            return StreamingUpstreamResult(
                target=target,
                trace=trace,
                error=started.error,
                error_type=type(started.error).__name__,
            )

        response = started.response
        client = started.client
        assert response is not None
        assert client is not None
        limit = int(
            response_limit_bytes
            if response_limit_bytes is not None
            else target.connection.response_limit_bytes
        )
        if not response.is_success:
            try:
                content = await read_raw_content(response, limit=limit)
            except httpx.HTTPError as exc:
                trace = network_attempt_trace(
                    target=target,
                    attempt_index=attempt_index,
                    exc=exc,
                    latency_ms=_elapsed_ms(started.started_monotonic),
                )
                trace.request_sent = True
                trace.billable_unknown = True
                trace.outcome = "ambiguous_failure"
                trace.status_code = response.status_code
                return StreamingUpstreamResult(
                    target=target,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    trace=trace,
                    error=exc,
                    error_type=type(exc).__name__,
                )
            finally:
                await response.aclose()
            trace = response_attempt_trace(
                target=target,
                attempt_index=attempt_index,
                response=response,
                content=content,
                latency_ms=_elapsed_ms(started.started_monotonic),
            )
            return StreamingUpstreamResult(
                target=target,
                content=content,
                status_code=response.status_code,
                headers=dict(response.headers),
                trace=trace,
            )

        iterator = response.aiter_raw().__aiter__()
        trace = response_attempt_trace(
            target=target,
            attempt_index=attempt_index,
            response=response,
            content=None,
            latency_ms=_elapsed_ms(started.started_monotonic),
        )
        try:
            first_chunk = await first_non_empty_chunk(iterator)
            if first_chunk is None:
                exc = httpx.ReadError(
                    "upstream stream ended before first byte",
                    request=response.request,
                )
                trace.outcome = "ambiguous_failure"
                trace.failure_class = "empty_stream"
                trace.billable_unknown = True
                await response.aclose()
                return StreamingUpstreamResult(
                    target=target,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    trace=trace,
                    error=exc,
                    error_type=type(exc).__name__,
                )
            enforce_response_limit(len(first_chunk), limit, response)
        except httpx.HTTPError as exc:
            trace.outcome = "ambiguous_failure"
            trace.failure_class = network_failure_class(exc)
            trace.billable_unknown = True
            trace.response_complete = False
            await response.aclose()
            return StreamingUpstreamResult(
                target=target,
                status_code=response.status_code,
                headers=dict(response.headers),
                trace=trace,
                error=exc,
                error_type=type(exc).__name__,
            )

        lease = UpstreamStreamLease(
            client=client,
            response=response,
            iterator=iterator,
            first_chunk=first_chunk,
            trace=trace,
            response_limit_bytes=limit,
            attempt_started_monotonic=started.started_monotonic,
        )
        return StreamingUpstreamResult(
            target=target,
            status_code=response.status_code,
            headers=dict(response.headers),
            trace=trace,
            lease=lease,
        )

    async def post_json_accounted(
        self,
        *,
        target: RouteTarget,
        payload: dict[str, Any],
        secret: str,
        usage_store: UsageStore,
        server: ServerConfig,
        pricing_catalog: Mapping[str, PricingConfig],
        client_id: str,
        route_id: str = "",
        request_headers: Mapping[str, str] | None = None,
        response_limit_bytes: int | None = None,
        timeout_seconds: float | None = None,
        metadata: UsageMetadata | None = None,
        storage_monitor: object | None = None,
    ) -> BufferedUpstreamResult:
        """Preflight, execute and record one potentially billable POST."""

        await preflight_usage_ledger(
            usage_store,
            server=server,
            body_bytes=_json_body_size(payload),
            attempts=1,
            storage_monitor=storage_monitor,
        )
        result = await self.post_json(
            target=target,
            payload=payload,
            secret=secret,
            request_headers=request_headers,
            attempt_index=1,
            response_limit_bytes=response_limit_bytes,
            timeout_seconds=timeout_seconds,
        )
        if result.trace is not None:
            try:
                result.usage_event_id = await record_exact_usage(
                    usage_store,
                    result=result,
                    client_id=client_id,
                    route_id=route_id or target.route_id,
                    pricing_catalog=pricing_catalog,
                    metadata=metadata,
                    storage_monitor=storage_monitor,
                )
                result.usage_ledger_status = "complete"
            except UsageLedgerRecordError:
                # The provider result remains authoritative after a send. The
                # caller can surface this additive warning while the monitor
                # stays latched unavailable; retrying the provider would risk
                # a second billable call.
                result.usage_ledger_status = "incomplete"
        return result

    def client_for(
        self,
        connection: ConnectionConfig,
        *,
        timeout_seconds: float | None = None,
    ) -> httpx.AsyncClient:
        timeouts = connection_timeouts(
            connection,
            timeout_seconds=timeout_seconds,
        )
        existing = self._clients.get(timeouts)
        if existing is not None and not existing.is_closed:
            return existing
        client = upstream_async_client(
            connection,
            transport=self.transport,
            timeout_seconds=timeout_seconds,
        )
        self._clients[timeouts] = client
        return client

    async def _start_json_post(
        self,
        *,
        target: RouteTarget,
        payload: dict[str, Any],
        secret: str,
        request_headers: Mapping[str, str],
        timeout_seconds: float | None,
    ) -> _StartedJsonPost:
        try:
            url = target_url(target)
            headers = upstream_headers(target, secret, request_headers)
            if self.transport is None:
                await require_safe_destination(
                    url,
                    allowed_private_networks=(
                        target.connection.allowed_private_networks
                    ),
                )
        except (OSError, ValueError) as exc:
            # Full reason (hostname, resolved addresses) goes to the server log
            # for diagnostics; the client only gets the bounded category.
            _LOGGER.warning(
                "上游请求在本地被拒绝或失败：%s: %s",
                type(exc).__name__,
                str(exc).strip()[:400],
            )
            return _StartedJsonPost(
                local_error_type=type(exc).__name__,
                local_error_detail=_local_failure_detail(exc),
            )

        started_monotonic = time.monotonic()
        client = self.client_for(
            target.connection,
            timeout_seconds=timeout_seconds,
        )
        try:
            request = client.build_request(
                "POST",
                url,
                headers=headers,
                json=payload,
            )
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            return _StartedJsonPost(
                client=client,
                started_monotonic=started_monotonic,
                error=exc,
            )
        return _StartedJsonPost(
            client=client,
            response=response,
            started_monotonic=started_monotonic,
        )

    async def aclose(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


async def preflight_usage_ledger(
    usage_store: UsageStore,
    *,
    server: ServerConfig,
    body_bytes: int,
    attempts: int,
    storage_monitor: object | None = None,
) -> None:
    """Prove ledger capacity and writability before a provider can be billed."""

    def check() -> None:
        ensure_write_capacity(
            (usage_store.path,),
            server,
            expected_write_bytes=estimated_ledger_write_bytes(
                body_bytes=body_bytes,
                attempts=attempts,
            ),
        )
        usage_store.probe_writable()

    try:
        await asyncio.to_thread(check)
    except Exception as exc:
        _mark_storage_unavailable(storage_monitor)
        raise UsageLedgerPreflightError(
            "usage ledger preflight failed"
        ) from exc


async def record_exact_usage(
    usage_store: UsageStore,
    *,
    result: BufferedUpstreamResult,
    client_id: str,
    route_id: str,
    pricing_catalog: Mapping[str, PricingConfig],
    metadata: UsageMetadata | None = None,
    storage_monitor: object | None = None,
) -> str:
    trace = result.trace
    if trace is None:
        return ""
    target = result.target
    pricing_id = target.deployment.pricing or ""
    pricing = pricing_catalog.get(pricing_id) if pricing_id else None
    complete = (
        trace.outcome == "success"
        and trace.response_complete
        and not trace.capture.malformed
    )
    try:
        return await asyncio.to_thread(
            usage_store.record,
            client_id=client_id,
            kind=target.deployment.kind,
            route_id=route_id,
            target=target,
            status_code=(
                result.status_code if result.status_code is not None else 502
            ),
            latency_ms=trace.latency_ms,
            attempts=1,
            complete=complete,
            capture=trace.capture,
            pricing_id=pricing_id,
            pricing=pricing,
            attempt_traces=(trace,),
            pricing_catalog=pricing_catalog,
            metadata=metadata,
        )
    except Exception as exc:
        _mark_storage_unavailable(storage_monitor)
        raise UsageLedgerRecordError("usage ledger record failed") from exc


_LOGGER = logging.getLogger(__name__)


def connection_timeouts(
    connection: ConnectionConfig,
    *,
    timeout_seconds: float | None = None,
) -> tuple[float, float, float, float]:
    configured = (
        float(connection.connect_timeout_seconds),
        float(connection.read_timeout_seconds),
        float(connection.write_timeout_seconds),
        float(connection.pool_timeout_seconds),
    )
    if timeout_seconds is None:
        return configured
    limit = max(0.1, float(timeout_seconds))
    return tuple(min(limit, value) for value in configured)  # type: ignore[return-value]


def upstream_async_client(
    connection: ConnectionConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float | None = None,
) -> httpx.AsyncClient:
    connect, read, write, pool = connection_timeouts(
        connection,
        timeout_seconds=timeout_seconds,
    )
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(
            connect=connect,
            read=read,
            write=write,
            pool=pool,
        ),
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def upstream_headers(
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


def target_url(target: RouteTarget) -> str:
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


def network_failure_can_fail_over(exc: httpx.HTTPError) -> bool:
    return isinstance(
        exc,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    )


def network_failure_class(exc: httpx.HTTPError) -> str:
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


def classify_network_failure(exc: httpx.HTTPError) -> tuple[bool, str, str]:
    request_sent = not network_failure_can_fail_over(exc)
    return (
        request_sent,
        "ambiguous_failure" if request_sent else "connect_failure",
        network_failure_class(exc),
    )


def network_attempt_trace(
    *,
    target: RouteTarget,
    attempt_index: int,
    exc: httpx.HTTPError,
    latency_ms: int,
) -> AttemptTrace:
    request_sent, outcome, failure_class = classify_network_failure(exc)
    return AttemptTrace(
        attempt_index=attempt_index,
        target=target,
        latency_ms=max(0, latency_ms),
        outcome=outcome,
        failure_class=failure_class,
        request_sent=request_sent,
        billable_unknown=request_sent,
        response_complete=False,
    )


def http_failure_class(status_code: int, content: bytes) -> str:
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


def response_attempt_trace(
    *,
    target: RouteTarget,
    attempt_index: int,
    response: httpx.Response,
    content: bytes | None,
    latency_ms: int,
    streaming: bool = False,
) -> AttemptTrace:
    capture = UsageCapture()
    if content is not None:
        if streaming:
            capture.feed(content)
        else:
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
            else http_failure_class(response.status_code, content or b"")
        ),
        request_sent=True,
        response_complete=content is not None,
        capture=capture,
    )


async def read_raw_content(response: httpx.Response, *, limit: int) -> bytes:
    if response.is_stream_consumed:
        content = response.content
        enforce_response_limit(len(content), limit, response)
        return content
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        enforce_response_limit(total, limit, response)
        chunks.append(chunk)
    return b"".join(chunks)


async def first_non_empty_chunk(
    iterator: AsyncIterator[bytes],
) -> bytes | None:
    while True:
        chunk = await anext(iterator, None)
        if chunk is None:
            return None
        if chunk:
            return chunk


def enforce_response_limit(
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


def _json_body_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _local_failure_detail(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        # DNS failed before any safety decision was made; say so instead of
        # blaming the safety check (common on phones with a flaky VPN/DNS).
        return "上游域名解析失败，请检查网络、VPN 或 DNS 后重试"
    if isinstance(exc, OSError):
        # Bounded on purpose: never echo addresses or OS strings to clients.
        return "连接上游失败（本地网络错误）"
    message = str(exc).strip()
    if "密钥" in message:
        return "上游密钥格式无效"
    # Bounded categories only: the exact hostname/addresses stay in the server log.
    if "本地或私有地址" in message:
        return (
            "上游域名被解析到本地或私有地址，已按安全策略拒绝。这通常是手机上的 VPN/代理"
            "（Clash、Surge 等 fake-ip 模式）造成的：关闭 VPN，或在渠道的"
            " allowed_private_networks 中放行 198.18.0.0/15 与 fc00::/18 后重试"
        )
    if "没有可用地址" in message:
        return "上游域名无法解析到任何地址，请检查网络或 DNS 后重试"
    return "上游 URL 或请求 Header 未通过安全校验"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _mark_storage_unavailable(storage_monitor: object | None) -> None:
    marker = getattr(storage_monitor, "mark_unavailable", None)
    if callable(marker):
        marker()
