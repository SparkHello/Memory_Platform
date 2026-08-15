from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import time
from typing import Any, AsyncIterator, Literal, Mapping

import httpx

from model_gateway.adapters import (
    apply_connection_adapter,
    strip_reasoning_from_assistant_messages,
)
from model_gateway.auth import provider_secret_header_value
from model_gateway.routing import (
    RequestRequirements,
    ResolvedRoute,
    RouteTarget,
    Router,
    retry_after_seconds,
    should_fail_over,
    structured_error_code,
)
from model_gateway.usage import AttemptTrace
from model_gateway.upstream_executor import (
    UpstreamExecutor,
    UpstreamStreamLease,
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


class ProxyNetworkError(RuntimeError):
    pass


class RequestPreparationError(ValueError):
    """Every resolved target failed a local, pre-send configuration check."""


@dataclass(slots=True)
class ProxyHTTPResult:
    content: bytes
    status_code: int
    headers: dict[str, str]
    target: RouteTarget | None
    attempts: int
    attempt_traces: tuple[AttemptTrace, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PreparedRoute:
    route: ResolvedRoute
    payloads: Mapping[str, dict[str, Any]]


class ProxyUpstreamStream:
    def __init__(
        self,
        *,
        lease: UpstreamStreamLease,
        target: RouteTarget,
        attempts: int,
        headers: dict[str, str],
        attempt_traces: tuple[AttemptTrace, ...],
    ) -> None:
        self._lease = lease
        self.client = lease.client
        self.response = lease.response
        self.iterator = lease.iterator
        self.first_chunk = lease.first_chunk
        self.target = target
        self.attempts = attempts
        self.headers = headers
        self.attempt_traces = attempt_traces
        self.active_trace = lease.trace
        self.response_limit_bytes = lease.response_limit_bytes
        self.attempt_started_monotonic = lease.attempt_started_monotonic

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        async for chunk in self._lease.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        await self._lease.aclose()


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
        self.executor = UpstreamExecutor(transport=transport)
        # Compatibility for lifecycle diagnostics; the executor owns the map.
        self._clients = self.executor._clients

    async def complete(
        self,
        *,
        route: ResolvedRoute,
        payload: dict[str, Any],
        secrets: Mapping[str, str],
        request_headers: Mapping[str, str],
        reasoning_origin_deployment: str = "",
        prepared_payloads: Mapping[str, dict[str, Any]] | None = None,
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
            forwarded = (
                prepared_payloads[target.deployment_id]
                if prepared_payloads is not None
                else prepare_payload(
                    payload,
                    target,
                    reasoning_origin_deployment=reasoning_origin_deployment,
                )
            )
            execution = await self.executor.post_json(
                target=target,
                payload=forwarded,
                secret=secret,
                request_headers=request_headers,
                attempt_index=attempts + 1,
            )
            trace = execution.trace
            if trace is None:
                last_network_error = httpx.ConnectError(
                    execution.error_detail or execution.error_type
                )
                continue
            attempts += 1
            attempt_traces.append(trace)
            if execution.error_type:
                last_network_error = httpx.ConnectError(execution.error_type)
                if execution.status_code is not None:
                    self._record_health(
                        target,
                        status_code=execution.status_code,
                        headers=execution.headers,
                        content=b"",
                    )
                if trace.outcome == "connect_failure":
                    continue
                return _network_error_result(
                    execution.error_type,
                    route=route,
                    target=target,
                    attempts=attempts,
                    attempt_traces=tuple(attempt_traces),
                )
            assert execution.status_code is not None
            content = execution.content
            self._record_health(
                target,
                status_code=execution.status_code,
                headers=execution.headers,
                content=content,
            )
            if (
                execution.is_success
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
                status_code=execution.status_code,
                headers=buffered_response_headers(
                    execution.headers,
                    route=route,
                    target=target,
                    attempts=attempts,
                ),
                target=target,
                attempts=attempts,
                attempt_traces=tuple(attempt_traces),
            )
            if execution.is_redirect:
                result = _unsafe_redirect_result(result)
            if execution.is_success:
                return result
            if not should_fail_over(execution.status_code, content):
                return result
            last_result = result

        return _finalize_attempts(
            route=route,
            last_result=last_result,
            last_network_error=last_network_error,
            attempts=attempts,
            attempt_traces=tuple(attempt_traces),
            phase_label="上游网络连接失败",
        )

    async def open_stream(
        self,
        *,
        route: ResolvedRoute,
        payload: dict[str, Any],
        secrets: Mapping[str, str],
        request_headers: Mapping[str, str],
        reasoning_origin_deployment: str = "",
        prepared_payloads: Mapping[str, dict[str, Any]] | None = None,
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
            forwarded = (
                prepared_payloads[target.deployment_id]
                if prepared_payloads is not None
                else prepare_payload(
                    payload,
                    target,
                    reasoning_origin_deployment=reasoning_origin_deployment,
                )
            )
            execution = await self.executor.open_json_stream(
                target=target,
                payload=forwarded,
                secret=secret,
                request_headers=request_headers,
                attempt_index=attempts + 1,
            )
            trace = execution.trace
            if trace is None:
                last_network_error = httpx.ConnectError(
                    execution.error_detail or execution.error_type
                )
                continue

            attempts += 1
            attempt_traces.append(trace)
            if execution.status_code is not None:
                self._record_health(
                    target,
                    status_code=execution.status_code,
                    headers=execution.headers,
                    content=(
                        execution.content if not execution.error_type else b""
                    ),
                )

            if execution.error_type:
                error = execution.error or httpx.ReadError(execution.error_type)
                last_network_error = error
                if trace.outcome == "connect_failure":
                    continue
                return _network_error_result(
                    error,
                    route=route,
                    target=target,
                    attempts=attempts,
                    streaming=True,
                    attempt_traces=tuple(attempt_traces),
                )

            if execution.lease is None:
                assert execution.status_code is not None
                result = ProxyHTTPResult(
                    content=execution.content,
                    status_code=execution.status_code,
                    headers=buffered_response_headers(
                        execution.headers,
                        route=route,
                        target=target,
                        attempts=attempts,
                    ),
                    target=target,
                    attempts=attempts,
                    attempt_traces=tuple(attempt_traces),
                )
                if execution.is_redirect:
                    result = _unsafe_redirect_result(result)
                if not should_fail_over(
                    execution.status_code,
                    execution.content,
                ):
                    return result
                last_result = result
                continue

            return ProxyUpstreamStream(
                lease=execution.lease,
                target=target,
                attempts=attempts,
                attempt_traces=tuple(attempt_traces),
                headers=buffered_response_headers(
                    execution.headers,
                    route=route,
                    target=target,
                    attempts=attempts,
                ),
            )

        return _finalize_attempts(
            route=route,
            last_result=last_result,
            last_network_error=last_network_error,
            attempts=attempts,
            attempt_traces=tuple(attempt_traces),
            phase_label="上游流连接失败",
        )

    async def aclose(self) -> None:
        await self.executor.aclose()

    def _record_health(
        self,
        target: RouteTarget,
        *,
        status_code: int,
        headers: Mapping[str, str],
        content: bytes,
    ) -> None:
        self.router.runtime_health.record_http(
            target,
            status_code=status_code,
            error_code=structured_error_code(content),
            retry_after=retry_after_seconds(
                headers.get("Retry-After", headers.get("retry-after", "")),
                wall_time=self._wall_clock(),
            ),
        )

def _finalize_attempts(
    *,
    route: ResolvedRoute,
    last_result: ProxyHTTPResult | None,
    last_network_error: httpx.HTTPError | None,
    attempts: int,
    attempt_traces: tuple[AttemptTrace, ...],
    phase_label: str,
) -> ProxyHTTPResult:
    """Shared send-loop epilogue for the non-streaming and streaming paths."""

    if last_result is not None:
        if route.required_deployment:
            return affinity_unavailable_result(
                last_result.attempts,
                last_result.target,
                attempt_traces=attempt_traces,
            )
        return last_result
    if route.required_deployment:
        return affinity_unavailable_result(
            attempts,
            route.targets[0] if route.targets else None,
            attempt_traces=attempt_traces,
        )
    detail = _network_failure_detail(
        last_network_error,
        phase_label=phase_label,
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
        attempt_traces=attempt_traces,
    )


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


def _network_error_result(
    exc: httpx.HTTPError | str,
    *,
    route: ResolvedRoute,
    target: RouteTarget,
    attempts: int,
    streaming: bool = False,
    attempt_traces: tuple[AttemptTrace, ...] = (),
) -> ProxyHTTPResult:
    phase = "流响应" if streaming else "响应"
    error_type = exc if isinstance(exc, str) else type(exc).__name__
    return ProxyHTTPResult(
        content=json.dumps(
            {
                "error": {
                    "message": (
                        f"上游{phase}中断：{error_type}；"
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


def prepare_resolved_route(
    *,
    route: ResolvedRoute,
    payload: dict[str, Any],
    secrets: Mapping[str, str],
    kind: Literal["chat", "embedding"],
    reasoning_origin_deployment: str = "",
) -> PreparedRoute:
    """Prepare and revalidate every target before any provider attempt.

    Route selection validates the client payload.  Named adapters and legacy
    deployment transforms run afterwards and can produce a target-specific
    payload, so the final shape must satisfy the same deployment capability
    contract.  Invalid targets are skipped like other ineligible targets; a
    request fails locally only when none remains.
    """

    prepared_targets: list[RouteTarget] = []
    prepared_payloads: dict[str, dict[str, Any]] = {}
    for target in route.targets:
        if target.deployment.request_transform.protected_fields():
            continue
        secret = secrets.get(target.connection.auth.secret_ref, "")
        if secret:
            try:
                provider_secret_header_value(secret)
            except ValueError:
                continue
        forwarded = prepare_payload(
            payload,
            target,
            reasoning_origin_deployment=reasoning_origin_deployment,
        )
        final_requirements = RequestRequirements.from_payload(
            forwarded,
            kind=kind,
        )
        if final_requirements.missing_from(target.deployment):
            continue
        prepared_targets.append(target)
        prepared_payloads[target.deployment_id] = forwarded

    if not prepared_targets:
        raise RequestPreparationError(
            "所有 route target 的最终请求形状或上游密钥配置均无效"
        )
    return PreparedRoute(
        route=replace(route, targets=tuple(prepared_targets)),
        payloads=prepared_payloads,
    )


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


def response_headers(
    response: httpx.Response,
    *,
    route: ResolvedRoute,
    target: RouteTarget,
    attempts: int,
) -> dict[str, str]:
    return buffered_response_headers(
        response.headers,
        route=route,
        target=target,
        attempts=attempts,
    )


def buffered_response_headers(
    response_headers: Mapping[str, str],
    *,
    route: ResolvedRoute,
    target: RouteTarget,
    attempts: int,
) -> dict[str, str]:
    headers = {
        name.lower(): value
        for name, value in response_headers.items()
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
