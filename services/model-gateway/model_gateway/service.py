from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from pydantic import ValidationError

from model_gateway.admin import (
    ConnectionCreateRequest,
    DeploymentApplyRequest,
    RouteUpdateRequest,
    SecretUpdateRequest,
    configuration_revision,
    connection_candidate,
    deployment_candidate,
    public_configuration,
    route_candidate,
)
from model_gateway.auth import AuthenticationError, AuthenticatedClient, authenticate_client
from model_gateway.config_store import (
    ConfigError,
    ConfigManager,
    GatewayPaths,
    initialize,
    set_secret,
    write_config,
)
from model_gateway.health import HealthCheckError, check_health
from model_gateway.models import PricingConfig
from model_gateway.proxy import ProxyHTTPResult, ProxyUpstreamStream, RawOpenAIProxy
from model_gateway.routing import RouteAffinityUnavailable, Router, RoutingError
from model_gateway.usage import UsageCapture, UsageStore


_LOGGER = logging.getLogger(__name__)


def create_app(
    *,
    paths: GatewayPaths,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    initialize(paths)
    manager = ConfigManager(paths)
    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=transport)
    usage_store = UsageStore(paths.usage_db)
    usage_store.init_db()
    admin_write_lock = asyncio.Lock()

    app = FastAPI(title="Model Gateway", version="0.1.0")
    app.state.config_manager = manager
    app.state.router = router
    app.state.proxy = proxy
    app.state.usage_store = usage_store

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> dict[str, object]:
        try:
            config, _ = manager.snapshot()
        except Exception as exc:
            return {"status": "error", "detail": type(exc).__name__}
        return {
            "status": "warning" if manager.last_reload_error else "ok",
            "schema_version": config.schema_version,
            "connections": len(config.connections),
            "deployments": len(config.deployments),
            "routes": len(config.routes),
            # Validation errors can echo offending config values.  The liveness
            # endpoint is intentionally unauthenticated, so expose only state.
            "reload_error": (
                "configuration_reload_failed_using_last_known_good"
                if manager.last_reload_error
                else ""
            ),
        }

    @app.get("/readyz")
    async def ready() -> Response:
        try:
            manager.snapshot()
        except Exception as exc:
            return JSONResponse(
                {"status": "not_ready", "detail": type(exc).__name__},
                status_code=503,
            )
        return JSONResponse({"status": "ready"})

    @app.get("/admin/configuration")
    async def admin_configuration(request: Request) -> Response:
        try:
            config, secrets = manager.snapshot()
            client = _authenticate(request, config=config, secrets=secrets)
        except ConfigError:
            return _error(503, "本地网关配置无效；请运行 modelgw doctor")
        except AuthenticationError as exc:
            return _error(401, str(exc))
        return JSONResponse(
            public_configuration(
                config=config,
                secrets=secrets,
                client=client,
                revision=configuration_revision(paths.config),
            )
        )

    @app.post("/admin/routes/validate")
    async def validate_admin_routes(request: Request) -> Response:
        try:
            config, secrets = manager.snapshot()
            client = _authenticate(request, config=config, secrets=secrets)
        except ConfigError:
            return _error(503, "本地网关配置无效；请运行 modelgw doctor")
        except AuthenticationError as exc:
            return _error(401, str(exc))
        forbidden = _require_admin(client)
        if forbidden is not None:
            return forbidden
        payload = await _validated_admin_body(
            request,
            limit=config.server.body_limit_bytes,
            model=RouteUpdateRequest,
            label="路由草稿",
        )
        if isinstance(payload, Response):
            return payload
        current_revision = configuration_revision(paths.config)
        if payload.revision != current_revision:
            return _error(
                409,
                "配置已经被其他操作修改；请刷新页面后重新调整",
                error_type="model_gateway_config_stale",
            )
        try:
            _, changed, warnings = route_candidate(config, payload)
        except (ValueError, ValidationError) as exc:
            return _error(
                400,
                f"路由草稿未通过完整配置校验：{_safe_validation_message(exc)}",
                error_type="model_gateway_config_invalid",
            )
        return JSONResponse(
            {
                "valid": True,
                "revision": current_revision,
                "changed_routes": changed,
                "warnings": warnings,
            }
        )

    @app.put("/admin/routes")
    async def apply_admin_routes(request: Request) -> Response:
        async with admin_write_lock:
            try:
                config, secrets = manager.snapshot()
                client = _authenticate(request, config=config, secrets=secrets)
            except ConfigError:
                return _error(503, "本地网关配置无效；请运行 modelgw doctor")
            except AuthenticationError as exc:
                return _error(401, str(exc))
            forbidden = _require_admin(client)
            if forbidden is not None:
                return forbidden
            payload = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=RouteUpdateRequest,
                label="路由草稿",
            )
            if isinstance(payload, Response):
                return payload
            current_revision = configuration_revision(paths.config)
            if payload.revision != current_revision:
                return _error(
                    409,
                    "配置已经被其他操作修改；请刷新页面后重新调整",
                    error_type="model_gateway_config_stale",
                )
            try:
                candidate, changed, warnings = route_candidate(config, payload)
            except (ValueError, ValidationError) as exc:
                return _error(
                    400,
                    f"路由草稿未通过完整配置校验：{_safe_validation_message(exc)}",
                    error_type="model_gateway_config_invalid",
                )
            write_config(paths.config, candidate)
            manager.force_reload()
            return JSONResponse(
                {
                    "applied": True,
                    "revision": configuration_revision(paths.config),
                    "changed_routes": changed,
                    "warnings": warnings,
                    "restart_required": False,
                }
            )

    @app.post("/admin/connections")
    async def create_admin_connection(request: Request) -> Response:
        async with admin_write_lock:
            try:
                config, secrets = manager.snapshot()
                client = _authenticate(request, config=config, secrets=secrets)
            except ConfigError:
                return _error(503, "本地网关配置无效；请运行 modelgw doctor")
            except AuthenticationError as exc:
                return _error(401, str(exc))
            forbidden = _require_admin(client)
            if forbidden is not None:
                return forbidden
            payload = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=ConnectionCreateRequest,
                label="渠道",
            )
            if isinstance(payload, Response):
                return payload
            current_revision = configuration_revision(paths.config)
            if payload.revision != current_revision:
                return _error(
                    409,
                    "配置已经被其他操作修改；请刷新页面后重新调整",
                    error_type="model_gateway_config_stale",
                )
            try:
                candidate, connection_id = connection_candidate(config, payload)
            except (ValueError, ValidationError) as exc:
                return _error(
                    400,
                    f"渠道草稿未通过完整配置校验：{_safe_validation_message(exc)}",
                    error_type="model_gateway_config_invalid",
                )
            if not payload.dry_run:
                write_config(paths.config, candidate)
                manager.force_reload()
            return JSONResponse(
                {
                    "valid": True,
                    "applied": not payload.dry_run,
                    "connection_id": connection_id,
                    "revision": configuration_revision(paths.config),
                }
            )

    @app.post("/admin/deployments")
    async def apply_admin_deployments(request: Request) -> Response:
        async with admin_write_lock:
            try:
                config, secrets = manager.snapshot()
                client = _authenticate(request, config=config, secrets=secrets)
            except ConfigError:
                return _error(503, "本地网关配置无效；请运行 modelgw doctor")
            except AuthenticationError as exc:
                return _error(401, str(exc))
            forbidden = _require_admin(client)
            if forbidden is not None:
                return forbidden
            payload = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=DeploymentApplyRequest,
                label="部署",
            )
            if isinstance(payload, Response):
                return payload
            current_revision = configuration_revision(paths.config)
            if payload.revision != current_revision:
                return _error(
                    409,
                    "配置已经被其他操作修改；请刷新页面后重新调整",
                    error_type="model_gateway_config_stale",
                )
            try:
                candidate, deployment_ids, changed, warnings = deployment_candidate(
                    config, payload
                )
            except (ValueError, ValidationError) as exc:
                return _error(
                    400,
                    f"部署草稿未通过完整配置校验：{_safe_validation_message(exc)}",
                    error_type="model_gateway_config_invalid",
                )
            if not payload.dry_run:
                write_config(paths.config, candidate)
                manager.force_reload()
            return JSONResponse(
                {
                    "valid": True,
                    "applied": not payload.dry_run,
                    "deployments": [
                        {
                            "id": deployment_id,
                            "upstream_model": draft.upstream_model,
                            "kind": draft.kind,
                        }
                        for deployment_id, draft in zip(
                            deployment_ids, payload.deployments
                        )
                    ],
                    "changed_routes": changed,
                    "warnings": warnings,
                    "revision": configuration_revision(paths.config),
                }
            )

    @app.put("/admin/connections/{connection_id}/secret")
    async def update_admin_connection_secret(
        connection_id: str,
        request: Request,
    ) -> Response:
        async with admin_write_lock:
            try:
                config, secrets = manager.snapshot()
                client = _authenticate(request, config=config, secrets=secrets)
            except ConfigError:
                return _error(503, "本地网关配置无效；请运行 modelgw doctor")
            except AuthenticationError as exc:
                return _error(401, str(exc))
            forbidden = _require_admin(client)
            if forbidden is not None:
                return forbidden
            connection = config.connections.get(connection_id)
            if connection is None:
                return _error(404, "找不到指定 connection")
            payload = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=SecretUpdateRequest,
                label="密钥",
            )
            if isinstance(payload, Response):
                return payload
            set_secret(paths.secrets, connection.auth.secret_ref, payload.value)
            manager.force_reload()
            return JSONResponse(
                {
                    "connection_id": connection_id,
                    "configured": True,
                }
            )

    @app.post("/admin/connections/{connection_id}/check")
    async def check_admin_connection(
        connection_id: str,
        request: Request,
    ) -> Response:
        try:
            config, secrets = manager.snapshot()
            client = _authenticate(request, config=config, secrets=secrets)
        except ConfigError:
            return _error(503, "本地网关配置无效；请运行 modelgw doctor")
        except AuthenticationError as exc:
            return _error(401, str(exc))
        forbidden = _require_admin(client)
        if forbidden is not None:
            return forbidden
        try:
            report = await check_health(
                config=config,
                secrets=secrets,
                connection_id=connection_id,
                live=False,
                client_kind="admin",
                timeout_seconds=10.0,
                transport=transport,
            )
        except HealthCheckError as exc:
            return _error(404, str(exc))
        return JSONResponse(report.as_dict())

    @app.get("/v1/models")
    async def list_models(request: Request) -> Response:
        try:
            config, secrets = manager.snapshot()
            client = _authenticate(request, config=config, secrets=secrets)
        except ConfigError:
            return _error(503, "本地网关配置无效；请运行 modelgw doctor")
        except AuthenticationError as exc:
            return _error(401, str(exc))
        models: list[dict[str, Any]] = []
        for route_id, route in config.routes.items():
            if not route.enabled or not client.config.allows_route(route_id):
                continue
            try:
                router.resolve(
                    requested_model=route_id,
                    kind=route.kind,
                    client=client,
                    config=config,
                )
            except RoutingError:
                continue
            models.append(
                {
                    "id": route_id,
                    "object": "model",
                    "owned_by": "model-gateway",
                    "kind": route.kind,
                    "target_count": len(route.targets),
                }
            )
        if client.config.allow_direct_deployments:
            for deployment_id, deployment in config.deployments.items():
                direct_id = f"deployment:{deployment_id}"
                try:
                    router.resolve(
                        requested_model=direct_id,
                        kind=deployment.kind,
                        client=client,
                        config=config,
                    )
                except RoutingError:
                    continue
                models.append(
                    {
                        "id": direct_id,
                        "object": "model",
                        "owned_by": config.connections[
                            deployment.connection
                        ].channel_operator,
                        "kind": deployment.kind,
                    }
                )
        return JSONResponse({"object": "list", "data": models})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _proxy_request(
            request,
            kind="chat",
            manager=manager,
            router=router,
            proxy=proxy,
            usage_store=usage_store,
        )

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await _proxy_request(
            request,
            kind="embedding",
            manager=manager,
            router=router,
            proxy=proxy,
            usage_store=usage_store,
        )

    return app


async def _proxy_request(
    request: Request,
    *,
    kind: Literal["chat", "embedding"],
    manager: ConfigManager,
    router: Router,
    proxy: RawOpenAIProxy,
    usage_store: UsageStore,
) -> Response:
    try:
        config, secrets = manager.snapshot()
        client = _authenticate(request, config=config, secrets=secrets)
    except ConfigError:
        return _error(503, "本地网关配置无效；请运行 modelgw doctor")
    except AuthenticationError as exc:
        return _error(401, str(exc))

    raw_body = await _read_limited_body(request, config.server.body_limit_bytes)
    if raw_body is None:
        return _error(413, "请求正文超过本地网关限制")
    try:
        payload = json.loads(raw_body, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
        return _error(400, "请求正文必须是 UTF-8 JSON")
    if not isinstance(payload, dict):
        return _error(400, "请求 JSON 顶层必须是对象")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return _error(400, "请求缺少 model")
    if kind == "chat" and "stream" in payload and not isinstance(
        payload["stream"], bool
    ):
        return _error(400, "chat 请求的 stream 必须是布尔值")
    preferred = request.headers.get("x-model-gateway-preferred-deployment", "").strip()
    required = request.headers.get("x-model-gateway-require-deployment", "").strip()
    reasoning_origin = request.headers.get(
        "x-model-gateway-reasoning-origin-deployment", ""
    ).strip()
    try:
        resolved = router.resolve(
            requested_model=model,
            kind=kind,
            client=client,
            config=config,
            preferred_deployment=preferred,
            required_deployment=required,
        )
    except RoutingError as exc:
        return _routing_error(exc)

    if kind == "embedding" and "dimensions" in payload:
        requested_dimensions = payload["dimensions"]
        expected_dimensions = resolved.targets[0].deployment.dimensions
        if (
            isinstance(requested_dimensions, bool)
            or not isinstance(requested_dimensions, int)
            or requested_dimensions != expected_dimensions
        ):
            return _error(
                400,
                "embedding 请求 dimensions 必须与 route 的向量空间声明一致",
                error_type="model_gateway_embedding_dimensions_mismatch",
            )

    started = time.monotonic()
    is_stream = kind == "chat" and payload.get("stream") is True
    if is_stream:
        result = await proxy.open_stream(
            route=resolved,
            payload=payload,
            secrets=secrets,
            request_headers=request.headers,
            reasoning_origin_deployment=reasoning_origin,
        )
        if isinstance(result, ProxyHTTPResult):
            await _record_non_stream(
                usage_store,
                client=client,
                kind=kind,
                route_id=resolved.route_id,
                result=result,
                started=started,
                pricing=_pricing_for_result(config, result),
            )
            return Response(
                content=result.content,
                status_code=result.status_code,
                headers=result.headers,
            )
        return _streaming_response(
            result,
            usage_store=usage_store,
            client=client,
            kind=kind,
            route_id=resolved.route_id,
            started=started,
            pricing_id=result.target.deployment.pricing or "",
            pricing=(
                config.pricing.get(result.target.deployment.pricing)
                if result.target.deployment.pricing
                else None
            ),
        )

    result = await proxy.complete(
        route=resolved,
        payload=payload,
        secrets=secrets,
        request_headers=request.headers,
        reasoning_origin_deployment=reasoning_origin,
    )
    await _record_non_stream(
        usage_store,
        client=client,
        kind=kind,
        route_id=resolved.route_id,
        result=result,
        started=started,
        pricing=_pricing_for_result(config, result),
    )
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=result.headers,
    )


def _streaming_response(
    stream: ProxyUpstreamStream,
    *,
    usage_store: UsageStore,
    client: AuthenticatedClient,
    kind: str,
    route_id: str,
    started: float,
    pricing_id: str,
    pricing: PricingConfig | None,
) -> StreamingResponse:
    capture = UsageCapture()

    async def body() -> Any:
        complete = False
        try:
            async for chunk in stream.aiter_raw():
                capture.feed(chunk)
                yield chunk
            complete = capture.saw_done and not capture.malformed
        finally:
            await stream.aclose()
            try:
                await asyncio.to_thread(
                    usage_store.record,
                    client_id=client.id,
                    kind=kind,
                    route_id=route_id,
                    target=stream.target,
                    status_code=stream.response.status_code,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    attempts=stream.attempts,
                    complete=complete,
                    capture=capture,
                    pricing_id=pricing_id,
                    pricing=pricing,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "usage recording failed after stream (%s)",
                    type(exc).__name__,
                )

    return StreamingResponse(
        body(),
        status_code=stream.response.status_code,
        headers=stream.headers,
    )


async def _record_non_stream(
    usage_store: UsageStore,
    *,
    client: AuthenticatedClient,
    kind: str,
    route_id: str,
    result: ProxyHTTPResult,
    started: float,
    pricing: tuple[str, PricingConfig | None],
) -> None:
    try:
        capture = UsageCapture()
        capture.from_non_stream(result.content)
        await asyncio.to_thread(
            usage_store.record,
            client_id=client.id,
            kind=kind,
            route_id=route_id,
            target=result.target,
            status_code=result.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=result.attempts,
            complete=200 <= result.status_code < 300 and not capture.malformed,
            capture=capture,
            pricing_id=pricing[0],
            pricing=pricing[1],
        )
    except Exception as exc:
        _LOGGER.warning(
            "usage recording failed after non-stream response (%s)",
            type(exc).__name__,
        )


def _pricing_for_result(
    config: Any, result: ProxyHTTPResult
) -> tuple[str, PricingConfig | None]:
    if result.target is None or not result.target.deployment.pricing:
        return "", None
    pricing_id = result.target.deployment.pricing
    return pricing_id, config.pricing.get(pricing_id)


def _authenticate(request: Request, *, config: Any, secrets: dict[str, str]) -> AuthenticatedClient:
    return authenticate_client(
        request.headers.get("authorization", ""),
        config=config,
        secrets=secrets,
    )


def _require_admin(client: AuthenticatedClient) -> JSONResponse | None:
    if client.config.kind == "admin":
        return None
    return _error(
        403,
        "该操作需要 Model Gateway admin 客户端密钥",
        error_type="model_gateway_admin_required",
    )


async def _validated_admin_body(
    request: Request,
    *,
    limit: int,
    model: Any,
    label: str,
) -> Any:
    raw = await _read_limited_body(request, limit)
    if raw is None:
        return _error(413, f"{label}请求超过本地网关限制")
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
        return _error(400, f"{label}请求必须是 UTF-8 JSON")
    try:
        return model.model_validate(payload)
    except ValidationError:
        # ValidationError may include the submitted input, which is unacceptable
        # for one-way secret writes. Keep all admin body errors value-free.
        return _error(400, f"{label}请求格式无效")


def _safe_validation_message(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    return message[:500] or type(exc).__name__


async def _read_limited_body(request: Request, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _error(
    status_code: int,
    message: str,
    *,
    error_type: str = "model_gateway_error",
) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type}},
        status_code=status_code,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _routing_error(exc: RoutingError) -> JSONResponse:
    if isinstance(exc, RouteAffinityUnavailable):
        code = "model_gateway_affinity_unavailable"
        return JSONResponse(
            {"error": {"message": str(exc), "type": code, "code": code}},
            status_code=exc.status_code,
        )
    return _error(exc.status_code, str(exc))
