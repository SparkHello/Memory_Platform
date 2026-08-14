from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from pydantic import ValidationError

from model_gateway.admin import (
    BundleApplyRequest,
    CandidateDiscoverRequest,
    CapabilityProbeRequest,
    ConnectionCreateRequest,
    DeploymentApplyRequest,
    EnabledUpdateRequest,
    RevisionRequest,
    RouteUpdateRequest,
    SecretUpdateRequest,
    bundle_candidate,
    configuration_revision,
    connection_candidate,
    deployment_candidate,
    public_configuration,
    route_candidate,
)
from model_gateway.capability_probe import (
    build_probe_connection,
    build_probe_deployment,
    probe_chat_capabilities,
)
from model_gateway.auth import (
    AuthenticationError,
    AuthenticatedClient,
    authenticate_client,
    client_token_bytes,
    provider_secret_header_value,
    validate_secret_domains,
)
from model_gateway.config_store import (
    ConfigConflict,
    ConfigError,
    ConfigManager,
    GatewayPaths,
    commit_control_plane,
    initialize,
)
from model_gateway.health import HealthCheckError, check_health
from model_gateway.models import AuthConfig, ConnectionConfig, GatewayConfig, PricingConfig
from model_gateway.proxy import ProxyHTTPResult, ProxyUpstreamStream, RawOpenAIProxy
from model_gateway.routing import (
    RequestRequirements,
    RouteAffinityUnavailable,
    RouteCapabilityUnavailable,
    Router,
    RoutingError,
)
from model_gateway.usage import (
    DAILY_RETENTION_DAYS,
    RAW_RETENTION_DAYS,
    UsageCapture,
    UsageMetadata,
    UsageStore,
)
from model_gateway.storage import (
    StorageCapacityError,
    StorageErrorMiddleware,
    StorageFaultMonitor,
    ensure_ledger_write_capacity,
    estimated_ledger_write_bytes,
    is_storage_exhausted,
    storage_readiness_reason,
)


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
    storage_monitor = StorageFaultMonitor()
    try:
        usage_store.init_db()
    except (OSError, sqlite3.Error) as exc:
        # Keep liveness available so /readyz can report only a safe storage
        # reason. Data-plane preflight remains fail-closed until the ledger is
        # writable again.
        storage_monitor.mark_unavailable()
        _LOGGER.error(
            "usage ledger initialization failed (%s)",
            type(exc).__name__,
        )
    admin_write_lock = asyncio.Lock()

    async def _admin_context(request: Request):
        """Snapshot + authenticate + require admin; returns context or error."""
        try:
            config, secrets = await manager.snapshot_async()
            client = _authenticate(request, config=config, secrets=secrets)
        except ConfigError:
            return _error(503, "本地网关配置无效；请运行 modelgw doctor")
        except AuthenticationError as exc:
            return _error(401, str(exc))
        forbidden = _require_admin(client)
        if forbidden is not None:
            return forbidden
        return config, secrets, client

    def _commit_admin_change(expected_revision, *, config=None, secret_updates=None):
        """Commit + reload, mapping ConfigConflict to the stale error."""
        try:
            commit = commit_control_plane(
                paths,
                expected_revision=expected_revision,
                config=config,
                secret_updates=secret_updates,
            )
        except ConfigConflict:
            return _config_stale_error()
        manager.force_reload()
        return commit

    # OpenAPI is off by default; Model port is internal but still avoid free recon.
    openapi_enabled = os.environ.get(
        "MODEL_GATEWAY_ENABLE_OPENAPI", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    app = FastAPI(
        title="Model Gateway",
        version="0.2.0",
        docs_url="/docs" if openapi_enabled else None,
        redoc_url="/redoc" if openapi_enabled else None,
        openapi_url="/openapi.json" if openapi_enabled else None,
    )
    app.add_middleware(StorageErrorMiddleware, monitor=storage_monitor)
    app.state.config_manager = manager
    app.state.router = router
    app.state.proxy = proxy
    app.state.usage_store = usage_store
    app.state.storage_monitor = storage_monitor
    app.router.add_event_handler("shutdown", proxy.aclose)

    # init_db prunes once at startup; long-lived deployments that never
    # restart also need the retention policy applied periodically.
    usage_prune_task: dict[str, asyncio.Task | None] = {"task": None}

    async def _daily_usage_prune() -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                await asyncio.to_thread(usage_store.prune, vacuum=False)
            except (OSError, sqlite3.Error) as exc:
                _LOGGER.warning(
                    "periodic usage prune failed (%s)", type(exc).__name__
                )

    def _start_usage_prune() -> None:
        usage_prune_task["task"] = asyncio.create_task(_daily_usage_prune())

    async def _stop_usage_prune() -> None:
        task = usage_prune_task["task"]
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.router.add_event_handler("startup", _start_usage_prune)
    app.router.add_event_handler("shutdown", _stop_usage_prune)

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> dict[str, object]:
        try:
            config, _ = await manager.snapshot_async()
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
            config, secrets = await manager.snapshot_async()
        except Exception as exc:
            return JSONResponse(
                {"status": "not_ready", "detail": type(exc).__name__},
                status_code=503,
            )
        if manager.last_reload_error:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "reason": "configuration_reload_failed",
                },
                status_code=503,
            )
        disk_reason = await asyncio.to_thread(
            storage_readiness_reason,
            paths,
            config.server,
            usage_probe=usage_store,
        )
        if disk_reason:
            return JSONResponse(
                {"status": "not_ready", "reason": disk_reason},
                status_code=503,
            )
        latched_reason = storage_monitor.consume_after_successful_probe()
        if latched_reason:
            return JSONResponse(
                {"status": "not_ready", "reason": latched_reason},
                status_code=503,
            )
        if not _has_ready_route(config, secrets):
            return JSONResponse(
                {
                    "status": "not_ready",
                    "reason": "no_enabled_route_with_configured_provider",
                },
                status_code=503,
            )
        return JSONResponse({"status": "ready"})

    @app.get("/admin/configuration")
    async def admin_configuration(request: Request) -> Response:
        try:
            config, secrets = await manager.snapshot_async()
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

    @app.get("/admin/portable-config")
    async def admin_portable_config(request: Request) -> Response:
        """Return full GatewayConfig JSON without secrets for stack backup.

        Provider/admin secret values stay in secrets.env and are never included.
        """
        context = await _admin_context(request)
        if isinstance(context, JSONResponse):
            return context
        config, secrets, client = context
        del secrets
        payload = config.model_dump(mode="json", exclude_none=False)
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return Response(
            content=body.encode("utf-8"),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="model-gateway-config.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/admin/routes/validate")
    async def validate_admin_routes(request: Request) -> Response:
        context = await _admin_context(request)
        if isinstance(context, JSONResponse):
            return context
        config, secrets, client = context
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
                "配置已经被其他操作修改；请重新读取当前 revision 后重试",
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
            context = await _admin_context(request)
            if isinstance(context, JSONResponse):
                return context
            config, secrets, client = context
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
                    "配置已经被其他操作修改；请重新读取当前 revision 后重试",
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
            commit = _commit_admin_change(payload.revision, config=candidate)
            if isinstance(commit, JSONResponse):
                return commit
            return JSONResponse(
                {
                    "applied": True,
                    "revision": commit.revision,
                    "changed_routes": changed,
                    "warnings": warnings,
                    "restart_required": False,
                }
            )

    @app.post("/admin/connections")
    async def create_admin_connection(request: Request) -> Response:
        async with admin_write_lock:
            context = await _admin_context(request)
            if isinstance(context, JSONResponse):
                return context
            config, secrets, client = context
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
                    "配置已经被其他操作修改；请重新读取当前 revision 后重试",
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
                commit = _commit_admin_change(payload.revision, config=candidate)
                if isinstance(commit, JSONResponse):
                    return commit
            return JSONResponse(
                {
                    "valid": True,
                    "applied": not payload.dry_run,
                    "connection_id": connection_id,
                    "revision": (
                        commit.revision
                        if not payload.dry_run
                        else configuration_revision(paths.config)
                    ),
                }
            )

    @app.post("/admin/deployments")
    async def apply_admin_deployments(request: Request) -> Response:
        async with admin_write_lock:
            context = await _admin_context(request)
            if isinstance(context, JSONResponse):
                return context
            config, secrets, client = context
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
                    "配置已经被其他操作修改；请重新读取当前 revision 后重试",
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
                commit = _commit_admin_change(payload.revision, config=candidate)
                if isinstance(commit, JSONResponse):
                    return commit
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
                    "revision": (
                        commit.revision
                        if not payload.dry_run
                        else configuration_revision(paths.config)
                    ),
                }
            )

    @app.put("/admin/connections/{connection_id}/secret")
    async def update_admin_connection_secret(
        connection_id: str,
        request: Request,
    ) -> Response:
        async with admin_write_lock:
            context = await _admin_context(request)
            if isinstance(context, JSONResponse):
                return context
            config, secrets, client = context
            connection = config.connections.get(connection_id)
            if connection is None:
                return _error(404, "找不到指定 connection")
            payload = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=SecretUpdateRequest,
                label="密钥",
                detail_fields=False,
            )
            if isinstance(payload, Response):
                return payload
            candidate_secrets = dict(secrets)
            candidate_secrets[connection.auth.secret_ref] = payload.value
            try:
                validate_secret_domains(config=config, secrets=candidate_secrets)
            except ValueError:
                return _error(
                    400,
                    "渠道密钥不能与任何本地 client 密钥相同",
                    error_type="model_gateway_secret_domain_conflict",
                )
            revision = configuration_revision(paths.config)
            if payload.revision and payload.revision != revision:
                return _config_stale_error()
            revision = payload.revision or revision
            report = await check_health(
                config=config,
                secrets=candidate_secrets,
                connection_id=connection_id,
                live=False,
                client_kind="interactive",
                timeout_seconds=10.0,
                transport=transport,
            )
            if not _candidate_discovery_ok(report):
                return _error(
                    400,
                    "候选渠道密钥未通过只读 models discovery；原密钥保持不变",
                    error_type="model_gateway_candidate_key_rejected",
                )
            result = _commit_admin_change(
                revision,
                secret_updates={connection.auth.secret_ref: payload.value},
            )
            if isinstance(result, JSONResponse):
                return result
            router.runtime_health.clear_connection(
                connection_id,
                tuple(
                    deployment_id
                    for deployment_id, deployment in config.deployments.items()
                    if deployment.connection == connection_id
                ),
            )
            return JSONResponse(
                {
                    "connection_id": connection_id,
                    "configured": True,
                }
            )

    @app.post("/admin/channels/discover")
    async def discover_admin_candidate(request: Request) -> Response:
        context = await _admin_context(request)
        if isinstance(context, JSONResponse):
            return context
        config, secrets, client = context
        payload = await _validated_admin_body(
            request,
            limit=config.server.body_limit_bytes,
            model=CandidateDiscoverRequest,
            label="渠道发现",
            detail_fields=False,
        )
        if isinstance(payload, Response):
            return payload
        if payload.revision != configuration_revision(paths.config):
            return _config_stale_error()
        candidate_config = config
        connection_id = payload.connection
        connection = config.connections.get(connection_id) if connection_id else None
        if connection_id and connection is None:
            return _error(404, "找不到指定 connection")
        if connection is None:
            connection_id = "candidate-discovery"
            suffix = 2
            while connection_id in config.connections:
                connection_id = f"candidate-discovery-{suffix}"
                suffix += 1
            secret_ref = "DISCOVERY_CANDIDATE_KEY"
            while any(
                item.auth.secret_ref == secret_ref
                for item in config.connections.values()
            ):
                secret_ref += "_X"
            try:
                connection = ConnectionConfig(
                    channel_operator=payload.channel_operator,
                    adapter=payload.adapter_value,
                    allowed_private_networks=payload.allowed_private_networks,
                    base_url=payload.base_url,
                    auth=AuthConfig(type=payload.auth_type, secret_ref=secret_ref),
                    models_endpoint=payload.models_endpoint,
                )
                graph = config.model_dump(mode="python", exclude_none=False)
                graph["connections"] = {
                    **graph["connections"],
                    connection_id: connection.model_dump(
                        mode="python", exclude_none=False
                    ),
                }
                candidate_config = GatewayConfig.model_validate(graph)
            except (ValueError, ValidationError) as exc:
                return _error(
                    400,
                    "渠道 discovery 草稿未通过安全校验："
                    + _safe_validation_message(exc),
                    error_type="model_gateway_config_invalid",
                )
        candidate_secrets = dict(secrets)
        candidate_secrets[connection.auth.secret_ref] = payload.secret_value
        try:
            validate_secret_domains(config=candidate_config, secrets=candidate_secrets)
        except ValueError:
            return _error(
                400,
                "渠道密钥不能与任何本地 client 密钥相同",
                error_type="model_gateway_secret_domain_conflict",
            )
        report = await check_health(
            config=candidate_config,
            secrets=candidate_secrets,
            connection_id=connection_id,
            live=False,
            client_kind="interactive",
            timeout_seconds=10.0,
            transport=transport,
        )
        discovered = (
            list(report.connections[0].discovered_models)
            if report.connections
            else []
        )
        return JSONResponse(
            {
                "valid": _candidate_discovery_ok(report),
                "persisted": False,
                "revision": payload.revision,
                "candidate": {
                    "connection_id": payload.connection,
                    "channel_operator": connection.channel_operator,
                    "base_url": connection.base_url,
                    "adapter": connection.adapter,
                    "auth_type": connection.auth.type,
                    "allowed_private_networks": list(
                        connection.allowed_private_networks
                    ),
                    "models_endpoint": connection.models_endpoint,
                },
                "models": [
                    {"id": model_id, "model_author": "unknown", "aliases": []}
                    for model_id in discovered
                ],
                "report": report.as_dict(),
            },
            status_code=200 if _candidate_discovery_ok(report) else 400,
        )

    @app.post("/admin/channels/probe-capabilities")
    async def probe_admin_candidate_capabilities(request: Request) -> Response:
        """Run cheap live probes; never persists connection or secret."""
        context = await _admin_context(request)
        if isinstance(context, JSONResponse):
            return context
        config, secrets, client = context
        payload = await _validated_admin_body(
            request,
            limit=config.server.body_limit_bytes,
            model=CapabilityProbeRequest,
            label="能力探测",
            detail_fields=True,
        )
        if isinstance(payload, Response):
            return payload
        if payload.revision != configuration_revision(paths.config):
            return _config_stale_error()
        try:
            connection = build_probe_connection(
                channel_operator=payload.channel_operator,
                base_url=payload.base_url,
                adapter=payload.adapter,
                auth_type=payload.auth_type,
                allowed_private_networks=list(payload.allowed_private_networks),
            )
            deployment = build_probe_deployment(
                connection_id="capability-probe",
                upstream_model=payload.upstream_model,
            )
            graph = config.model_dump(mode="python", exclude_none=False)
            graph["connections"] = {
                **graph["connections"],
                "capability-probe": connection.model_dump(
                    mode="python", exclude_none=False
                ),
            }
            probe_config = GatewayConfig.model_validate(graph)
            probe_secrets = dict(secrets)
            probe_secrets[connection.auth.secret_ref] = payload.candidate_key
            validate_secret_domains(config=probe_config, secrets=probe_secrets)
        except (ValueError, ValidationError) as exc:
            return _error(
                400,
                "能力探测草稿未通过安全校验：" + _safe_validation_message(exc),
                error_type="model_gateway_config_invalid",
            )
        result = await probe_chat_capabilities(
            connection=connection,
            deployment=deployment,
            secret=payload.candidate_key,
            probes=tuple(payload.probes),  # type: ignore[arg-type]
            timeout_seconds=25.0,
            transport=transport,
        )
        return JSONResponse(
            {
                "persisted": False,
                "revision": payload.revision,
                "upstream_model": payload.upstream_model,
                **result,
            }
        )

    @app.post("/admin/channel-bundles/validate")
    async def validate_admin_bundle(request: Request) -> Response:
        return await _handle_admin_bundle(request, apply=False)

    @app.post("/admin/channel-bundles/apply")
    async def apply_admin_bundle(request: Request) -> Response:
        async with admin_write_lock:
            return await _handle_admin_bundle(request, apply=True)

    async def _handle_admin_bundle(request: Request, *, apply: bool) -> Response:
        context = await _admin_context(request)
        if isinstance(context, JSONResponse):
            return context
        config, secrets, client = context
        payload = await _validated_admin_body(
            request,
            limit=config.server.body_limit_bytes,
            model=BundleApplyRequest,
            label="原子渠道 bundle",
            detail_fields=False,
        )
        if isinstance(payload, Response):
            return payload
        if payload.revision != configuration_revision(paths.config):
            return _config_stale_error()
        try:
            (
                candidate,
                connection_id,
                secret_ref,
                deployment_ids,
                changed_routes,
                embedding_connection_id,
            ) = bundle_candidate(config, payload)
            candidate_secrets = dict(secrets)
            candidate_secrets[secret_ref] = payload.connection.secret
            validate_secret_domains(config=candidate, secrets=candidate_secrets)
        except (ValueError, ValidationError) as exc:
            return _error(
                400,
                f"bundle 未通过完整配置校验：{_safe_validation_message(exc)}",
                error_type="model_gateway_config_invalid",
            )
        report = await check_health(
            config=candidate,
            secrets=candidate_secrets,
            connection_id=connection_id,
            live=False,
            client_kind="interactive",
            timeout_seconds=10.0,
            transport=transport,
        )
        if not _candidate_discovery_ok(report):
            return _error(
                400,
                "候选渠道密钥未通过只读 models discovery；现有配置和密钥未改变",
                error_type="model_gateway_candidate_key_rejected",
            )
        revision = payload.revision
        if apply:
            commit = _commit_admin_change(
                revision,
                config=candidate,
                secret_updates={secret_ref: payload.connection.secret},
            )
            if isinstance(commit, JSONResponse):
                return commit
            revision = commit.revision
            router.runtime_health.clear_connection(connection_id, deployment_ids)
            if embedding_connection_id != connection_id:
                router.runtime_health.clear_connection(embedding_connection_id)
        return JSONResponse(
            {
                "valid": True,
                "applied": apply,
                "connection_id": connection_id,
                "embedding_connection_id": embedding_connection_id,
                "deployment_ids": deployment_ids,
                "changed_routes": changed_routes,
                "revision": revision,
                "discovery": report.as_dict(),
            }
        )

    @app.patch("/admin/connections/{connection_id}")
    async def update_admin_connection(
        connection_id: str, request: Request
    ) -> Response:
        return await _set_admin_object_enabled(
            request, collection="connections", item_id=connection_id
        )

    @app.patch("/admin/deployments/{deployment_id}")
    async def update_admin_deployment(
        deployment_id: str, request: Request
    ) -> Response:
        return await _set_admin_object_enabled(
            request, collection="deployments", item_id=deployment_id
        )

    async def _set_admin_object_enabled(
        request: Request, *, collection: str, item_id: str
    ) -> Response:
        async with admin_write_lock:
            context = await _admin_context(request)
            if isinstance(context, JSONResponse):
                return context
            config, secrets, client = context
            body = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=EnabledUpdateRequest,
                label="对象状态",
            )
            if isinstance(body, Response):
                return body
            records = getattr(config, collection)
            if item_id not in records:
                return _error(404, "找不到指定对象")
            payload = config.model_dump(mode="python", exclude_none=False)
            record = dict(payload[collection][item_id])
            record["enabled"] = body.enabled
            payload[collection][item_id] = record
            try:
                candidate = type(config).model_validate(payload)
            except (ValueError, ValidationError) as exc:
                return _error(
                    400,
                    f"对象修改未通过完整配置校验：{_safe_validation_message(exc)}",
                    error_type="model_gateway_config_invalid",
                )
            commit = _commit_admin_change(body.revision, config=candidate)
            if isinstance(commit, JSONResponse):
                return commit
            return JSONResponse(
                {
                    "updated": True,
                    "id": item_id,
                    "enabled": body.enabled,
                    "revision": commit.revision,
                }
            )

    @app.delete("/admin/{collection}/{item_id}")
    async def delete_admin_object(
        collection: Literal["connections", "deployments", "pricing"],
        item_id: str,
        request: Request,
    ) -> Response:
        async with admin_write_lock:
            context = await _admin_context(request)
            if isinstance(context, JSONResponse):
                return context
            config, secrets, client = context
            body = await _validated_admin_body(
                request,
                limit=config.server.body_limit_bytes,
                model=RevisionRequest,
                label="删除对象",
            )
            if isinstance(body, Response):
                return body
            records = getattr(config, collection)
            if item_id not in records:
                return _error(404, "找不到指定对象")
            blockers = _object_references(config, collection, item_id)
            if blockers:
                return _error(
                    409,
                    "对象仍被引用：" + ", ".join(blockers),
                    error_type="model_gateway_object_referenced",
                )
            payload = config.model_dump(mode="python", exclude_none=False)
            del payload[collection][item_id]
            candidate = type(config).model_validate(payload)
            secret_updates: dict[str, None] = {}
            if collection == "connections":
                secret_updates[config.connections[item_id].auth.secret_ref] = None
            commit = _commit_admin_change(
                body.revision,
                config=candidate,
                secret_updates=secret_updates,
            )
            if isinstance(commit, JSONResponse):
                return commit
            return JSONResponse(
                {
                    "deleted": True,
                    "collection": collection,
                    "id": item_id,
                    "revision": commit.revision,
                }
            )

    @app.post("/admin/connections/{connection_id}/check")
    async def check_admin_connection(
        connection_id: str,
        request: Request,
    ) -> Response:
        context = await _admin_context(request)
        if isinstance(context, JSONResponse):
            return context
        config, secrets, client = context
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
            config, secrets = await manager.snapshot_async()
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
            storage_monitor=storage_monitor,
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
            storage_monitor=storage_monitor,
        )

    @app.get("/v1/usage/events")
    async def usage_events(request: Request) -> Response:
        authenticated = await _usage_query_client(request, manager)
        if isinstance(authenticated, Response):
            return authenticated
        client, _ = authenticated
        try:
            filters = _usage_query_filters(request, client=client, raw=True)
            rows = await asyncio.to_thread(usage_store.events, **filters)
        except ValueError:
            return _error(
                400,
                "usage 查询参数无效",
                error_type="model_gateway_usage_query_invalid",
            )
        return JSONResponse(
            {
                "object": "list",
                "data": rows,
                "retention": {"raw_days": RAW_RETENTION_DAYS},
            }
        )

    @app.get("/v1/usage/summary")
    async def usage_summary(request: Request) -> Response:
        authenticated = await _usage_query_client(request, manager)
        if isinstance(authenticated, Response):
            return authenticated
        client, _ = authenticated
        try:
            filters = _usage_query_filters(request, client=client, raw=False)
            summary = await asyncio.to_thread(usage_store.summary, **filters)
        except ValueError:
            return _error(
                400,
                "usage 查询参数无效",
                error_type="model_gateway_usage_query_invalid",
            )
        return JSONResponse(summary)

    return app


async def _proxy_request(
    request: Request,
    *,
    kind: Literal["chat", "embedding"],
    manager: ConfigManager,
    router: Router,
    proxy: RawOpenAIProxy,
    usage_store: UsageStore,
    storage_monitor: StorageFaultMonitor,
) -> Response:
    try:
        config, secrets = await manager.snapshot_async()
        client = _authenticate(request, config=config, secrets=secrets)
    except ConfigError:
        return _error(503, "本地网关配置无效；请运行 modelgw doctor")
    except AuthenticationError as exc:
        return _error(401, str(exc))

    try:
        usage_metadata = _usage_metadata_from_request(request, client)
    except PermissionError:
        return _error(
            403,
            "usage 归因 Header 只允许 backend client 使用",
            error_type="model_gateway_usage_metadata_forbidden",
        )
    except ValueError:
        return _error(
            400,
            "usage 归因 Header 必须是 opaque ASCII ID",
            error_type="model_gateway_usage_metadata_invalid",
        )

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
    requirements = RequestRequirements.from_payload(payload, kind=kind)
    try:
        resolved = router.resolve(
            requested_model=model,
            kind=kind,
            client=client,
            config=config,
            preferred_deployment=preferred,
            required_deployment=required,
            requirements=requirements,
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

    # Authentication, JSON validation and route/capability resolution have all
    # succeeded, but no provider HTTP request has been built or sent yet.  Keep
    # enough durable room for this logical request and every allowed attempt.
    route_attempts = min(
        len(resolved.targets),
        resolved.route.max_attempts if resolved.route is not None else 1,
    )
    try:
        await asyncio.to_thread(
            ensure_ledger_write_capacity,
            usage_store.path,
            config.server,
            expected_write_bytes=estimated_ledger_write_bytes(
                body_bytes=len(raw_body),
                attempts=route_attempts,
            ),
            usage_probe=usage_store,
        )
    except StorageCapacityError:
        storage_monitor.mark_unavailable()
        return _insufficient_storage_error()

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
            event_id, ledger_complete = await _record_non_stream(
                usage_store,
                client=client,
                kind=kind,
                route_id=resolved.route_id,
                result=result,
                started=started,
                pricing=_pricing_for_result(config, result),
                pricing_catalog=config.pricing,
                metadata=usage_metadata,
                storage_monitor=storage_monitor,
            )
            headers = _usage_response_headers(
                result.headers,
                event_id=event_id,
                metadata=usage_metadata,
                ledger_status="complete" if ledger_complete else "incomplete",
            )
            return Response(
                content=result.content,
                status_code=result.status_code,
                headers=headers,
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
            pricing_catalog=config.pricing,
            metadata=usage_metadata,
            storage_monitor=storage_monitor,
        )

    result = await proxy.complete(
        route=resolved,
        payload=payload,
        secrets=secrets,
        request_headers=request.headers,
        reasoning_origin_deployment=reasoning_origin,
    )
    event_id, ledger_complete = await _record_non_stream(
        usage_store,
        client=client,
        kind=kind,
        route_id=resolved.route_id,
        result=result,
        started=started,
        pricing=_pricing_for_result(config, result),
        pricing_catalog=config.pricing,
        metadata=usage_metadata,
        storage_monitor=storage_monitor,
    )
    headers = _usage_response_headers(
        result.headers,
        event_id=event_id,
        metadata=usage_metadata,
        ledger_status="complete" if ledger_complete else "incomplete",
    )
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=headers,
    )


# Strong references keep shielded accounting tasks alive after the client
# disconnects and the owning generator is torn down.
_STREAM_FINALIZE_TASKS: set[asyncio.Task] = set()


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
    pricing_catalog: dict[str, PricingConfig],
    metadata: UsageMetadata,
    storage_monitor: StorageFaultMonitor,
) -> StreamingResponse:
    capture = UsageCapture()

    async def finalize(complete: bool) -> None:
        try:
            await stream.aclose()
        except httpx.HTTPError as exc:
            stream.active_trace.outcome = "ambiguous_failure"
            stream.active_trace.failure_class = "other_network"
            stream.active_trace.billable_unknown = True
            stream.active_trace.response_complete = False
            _LOGGER.warning(
                "upstream stream close failed (%s)",
                type(exc).__name__,
            )
        stream.active_trace.capture = capture
        stream.active_trace.latency_ms = int(
            (time.monotonic() - stream.attempt_started_monotonic) * 1000
        )
        if (
            not stream.active_trace.response_complete
            and stream.active_trace.outcome == "success"
        ):
            stream.active_trace.outcome = "ambiguous_failure"
            stream.active_trace.failure_class = "read_error"
            stream.active_trace.billable_unknown = True
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
                attempt_traces=stream.attempt_traces,
                pricing_catalog=pricing_catalog,
                metadata=metadata,
            )
        except Exception as exc:
            if is_storage_exhausted(exc) or isinstance(exc, StorageCapacityError):
                storage_monitor.mark_unavailable()
            _LOGGER.warning(
                "usage recording failed after stream (%s)",
                type(exc).__name__,
            )

    async def body() -> Any:
        complete = False
        try:
            async for chunk in stream.aiter_raw():
                capture.feed(chunk)
                yield chunk
            complete = capture.saw_done and not capture.malformed
        finally:
            # A client disconnect cancels this generator mid-yield. Shield the
            # upstream close + usage accounting so the ledger records the
            # (incomplete) event exactly once instead of losing it.
            finalize_task = asyncio.ensure_future(finalize(complete))
            _STREAM_FINALIZE_TASKS.add(finalize_task)
            finalize_task.add_done_callback(_STREAM_FINALIZE_TASKS.discard)
            await asyncio.shield(finalize_task)

    headers = _usage_response_headers(
        stream.headers,
        event_id="",
        metadata=metadata,
        ledger_status="deferred",
    )
    return StreamingResponse(
        body(),
        status_code=stream.response.status_code,
        headers=headers,
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
    pricing_catalog: dict[str, PricingConfig],
    metadata: UsageMetadata,
    storage_monitor: StorageFaultMonitor,
) -> tuple[str, bool]:
    try:
        capture = UsageCapture()
        capture.from_non_stream(result.content)
        event_id = await asyncio.to_thread(
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
            attempt_traces=result.attempt_traces,
            pricing_catalog=pricing_catalog,
            metadata=metadata,
        )
        return event_id, True
    except Exception as exc:
        if is_storage_exhausted(exc) or isinstance(exc, StorageCapacityError):
            storage_monitor.mark_unavailable()
        _LOGGER.warning(
            "usage recording failed after non-stream response (%s)",
            type(exc).__name__,
        )
        return "", False


def _insufficient_storage_error() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "Model Gateway 可用存储空间不足，请释放空间后重试",
                "type": "gateway_error",
                "code": "model_gateway_insufficient_storage",
                "attempts": 0,
            }
        },
        status_code=507,
        headers={
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "x-model-gateway-attempts": "0",
        },
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


def _usage_metadata_from_request(
    request: Request, client: AuthenticatedClient
) -> UsageMetadata:
    values = {
        "correlation_id": request.headers.get(
            "x-model-gateway-correlation-id", ""
        ).strip(),
        "operation": request.headers.get("x-model-gateway-operation", "").strip(),
        "user_tag": request.headers.get("x-model-gateway-user-tag", "").strip(),
    }
    if any(values.values()) and client.config.kind != "backend":
        raise PermissionError("usage metadata requires backend client")
    return UsageMetadata(**values)


async def _usage_query_client(
    request: Request, manager: ConfigManager
) -> tuple[AuthenticatedClient, dict[str, str]] | Response:
    try:
        config, secrets = await manager.snapshot_async()
        client = _authenticate(request, config=config, secrets=secrets)
    except ConfigError:
        return _error(503, "本地网关配置无效；请运行 modelgw doctor")
    except AuthenticationError as exc:
        return _error(401, str(exc))
    if client.config.kind not in {"backend", "admin"}:
        return _error(
            403,
            "usage 查询只允许 backend 或 admin client",
            error_type="model_gateway_usage_query_forbidden",
        )
    return client, secrets


def _usage_query_filters(
    request: Request,
    *,
    client: AuthenticatedClient,
    raw: bool,
) -> dict[str, Any]:
    requested_client = request.query_params.get("client_id", "").strip()
    if client.config.kind == "backend":
        if requested_client and requested_client != client.id:
            raise ValueError("backend cannot query another client")
        client_id = client.id
    else:
        client_id = requested_client
    default_days = RAW_RETENTION_DAYS if raw else 30
    days = int(request.query_params.get("days", str(default_days)))
    result: dict[str, Any] = {
        "client_id": client_id,
        "operation": request.query_params.get("operation", "").strip(),
        "user_tag": request.query_params.get("user_tag", "").strip(),
        "days": days,
    }
    if raw:
        result["event_id"] = request.query_params.get("event_id", "").strip()
        result["correlation_id"] = request.query_params.get(
            "correlation_id", ""
        ).strip()
        result["limit"] = int(request.query_params.get("limit", "100"))
    return result


def _usage_response_headers(
    source: dict[str, str] | Any,
    *,
    event_id: str,
    metadata: UsageMetadata,
    ledger_status: str = "",
) -> dict[str, str]:
    headers = dict(source)
    if event_id:
        headers["x-model-gateway-usage-event-id"] = event_id
    if metadata.correlation_id:
        headers["x-model-gateway-correlation-id"] = metadata.correlation_id
    if ledger_status:
        headers["x-model-gateway-usage-ledger-status"] = ledger_status
    return headers


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
    detail_fields: bool = True,
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
    except ValidationError as exc:
        # Never echo submitted values. Non-secret endpoints get value-free
        # field paths so the caller can act; one-way secret writes keep the
        # fully generic message.
        if detail_fields:
            return _error(400, f"{label}请求格式无效：{_safe_validation_message(exc)}")
        return _error(400, f"{label}请求格式无效")


def _safe_validation_message(exc: Exception) -> str:
    if not isinstance(exc, ValidationError):
        return type(exc).__name__
    locations: list[str] = []
    categories: set[str] = set()
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        if location and location not in locations:
            locations.append(location[:160])
        message = str(error.get("msg", "")).lower()
        if "https" in message:
            categories.add("必须使用 HTTPS")
        if "embedding" in message or "向量" in message:
            categories.add("embedding 配置不兼容")
        safe_capabilities = tuple(
            capability
            for capability in (
                "streaming",
                "tools",
                "parallel_tools",
                "reasoning",
                "multimodal_input",
                "json_object",
                "json_schema",
            )
            if capability in message
        )
        if safe_capabilities:
            categories.add(
                "deployment capability 不满足 route："
                + ", ".join(safe_capabilities)
            )
        if "引用" in message or "不存在" in message:
            categories.add("对象引用无效")
    details = []
    if locations:
        details.append("字段 " + ", ".join(locations[:8]))
    details.extend(sorted(categories))
    return "；".join(details) or "配置字段未通过安全校验"


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


def _config_stale_error() -> JSONResponse:
    return _error(
        409,
        "配置已经被其他操作修改；请重新读取当前 revision 后重试",
        error_type="model_gateway_config_stale",
    )


def _candidate_discovery_ok(report: Any) -> bool:
    return bool(report.connections) and all(
        connection.status in {"connected", "connected_unverified"}
        and connection.level in {"ok", "warning"}
        for connection in report.connections
    )


def _has_ready_route(config: Any, secrets: dict[str, str]) -> bool:
    try:
        validate_secret_domains(config=config, secrets=secrets)
    except ValueError:
        return False
    operational_clients = []
    for client in config.clients.values():
        if not client.enabled or client.kind not in {"backend", "interactive"}:
            continue
        try:
            client_token_bytes(
                secrets.get(client.secret_ref, ""),
                allow_legacy_weak=client.allow_legacy_weak_secret,
            )
        except ValueError:
            continue
        operational_clients.append(client)
    if not operational_clients:
        return False
    for route_id, route in config.routes.items():
        if not route.enabled:
            continue
        allowed_clients = [
            client
            for client in operational_clients
            if client.allows_route(route_id)
        ]
        if not allowed_clients:
            continue
        target_ids = list(route.targets)
        if route.fallback_scope == "none":
            target_ids = target_ids[:1]
        elif route.fallback_scope == "same_channel" and target_ids:
            primary = config.deployments.get(target_ids[0])
            if primary is None:
                continue
            target_ids = [
                target_id
                for target_id in target_ids
                if config.deployments.get(target_id) is not None
                and config.deployments[target_id].connection == primary.connection
            ]
        for target_id in target_ids:
            deployment = config.deployments.get(target_id)
            if deployment is None or not deployment.enabled:
                continue
            connection = config.connections.get(deployment.connection)
            if (
                connection is None
                or not connection.enabled
                or connection.usage_scope == "disabled"
            ):
                continue
            if connection.usage_scope == "interactive_only" and not any(
                client.kind == "interactive" for client in allowed_clients
            ):
                continue
            try:
                provider_secret_header_value(
                    secrets.get(connection.auth.secret_ref, "")
                )
            except ValueError:
                continue
            return True
    return False


def _object_references(config: Any, collection: str, item_id: str) -> list[str]:
    if collection == "connections":
        return sorted(
            f"deployment:{deployment_id}"
            for deployment_id, deployment in config.deployments.items()
            if deployment.connection == item_id
        )
    if collection == "deployments":
        return sorted(
            f"route:{route_id}"
            for route_id, route in config.routes.items()
            if item_id in route.targets
        )
    return sorted(
        f"deployment:{deployment_id}"
        for deployment_id, deployment in config.deployments.items()
        if deployment.pricing == item_id
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _routing_error(exc: RoutingError) -> JSONResponse:
    if isinstance(exc, RouteCapabilityUnavailable):
        code = "model_gateway_capability_unavailable"
        return JSONResponse(
            {
                "error": {
                    "message": str(exc),
                    "type": code,
                    "code": code,
                    "required_capabilities": list(exc.capabilities),
                }
            },
            status_code=exc.status_code,
        )
    if isinstance(exc, RouteAffinityUnavailable):
        code = "model_gateway_affinity_unavailable"
        return JSONResponse(
            {"error": {"message": str(exc), "type": code, "code": code}},
            status_code=exc.status_code,
        )
    return _error(exc.status_code, str(exc))
