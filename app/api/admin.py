import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
import httpx
from pydantic import BaseModel, Field

from app.api.deps import get_provider_store, get_providers_config, require_api_key
from app.config import Settings, get_settings
from app.providers.config import clear_providers_config_cache, load_providers_config
from app.providers.models import (
    ProviderConfig,
    ProviderModelConfig,
    ProvidersConfig,
    RouteConfig,
)
from app.providers.router import route_public_summary
from app.providers.store import ProviderStore

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_api_key)],
)


class BalanceAdjustmentRequest(BaseModel):
    amount_delta: float
    currency: str = Field(default="CNY", min_length=1)
    reason: str = ""


class ProviderCreateRequest(BaseModel):
    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str | None = None
    enabled: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0)


class ProviderPatchRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)


class ProviderModelCreateRequest(BaseModel):
    provider: str = Field(min_length=1)
    upstream_model: str = Field(min_length=1)
    display_name: str = ""
    api_format: Literal["openai_compatible", "claude_sdk"] = "openai_compatible"
    pricing_mode: Literal["flat", "tiered"] = "flat"
    pricing_tiers_json: str = ""
    input_price_per_million: float = Field(default=0.0, ge=0.0)
    output_price_per_million: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="CNY", min_length=1)
    enabled: bool = True


class ProviderModelPatchRequest(BaseModel):
    provider: str | None = None
    upstream_model: str | None = None
    display_name: str | None = None
    api_format: Literal["openai_compatible", "claude_sdk"] | None = None
    pricing_mode: Literal["flat", "tiered"] | None = None
    pricing_tiers_json: str | None = None
    input_price_per_million: float | None = Field(default=None, ge=0.0)
    output_price_per_million: float | None = Field(default=None, ge=0.0)
    currency: str | None = None
    enabled: bool | None = None


class RouteCreateRequest(BaseModel):
    virtual_model: str = Field(min_length=1)
    provider_model_id: str | None = None
    provider: str | None = None
    upstream_model: str | None = None
    priority: int = 100
    input_price_per_million: float = Field(default=0.0, ge=0.0)
    output_price_per_million: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="CNY", min_length=1)
    min_balance: float = Field(default=0.0, ge=0.0)
    enabled: bool = True


class RoutePatchRequest(BaseModel):
    virtual_model: str | None = None
    provider_model_id: str | None = None
    provider: str | None = None
    upstream_model: str | None = None
    priority: int | None = None
    input_price_per_million: float | None = Field(default=None, ge=0.0)
    output_price_per_million: float | None = Field(default=None, ge=0.0)
    currency: str | None = None
    min_balance: float | None = Field(default=None, ge=0.0)
    enabled: bool | None = None


class ProviderTestRequest(BaseModel):
    upstream_model: str | None = None


@router.get("/providers")
def get_providers(
    config: Annotated[ProvidersConfig, Depends(get_providers_config)],
) -> dict:
    return {
        "enabled": config.enabled,
        "path": config.path,
        "source": config.source,
        "errors": config.errors,
        "router": config.router.model_dump(),
        "providers": [_provider_summary(provider) for provider in config.providers.values()],
        "provider_models": [
            _provider_model_config_summary(model)
            for model in config.provider_models.values()
        ],
        "routes": [route_public_summary(route) for route in config.routes],
    }


@router.get("/provider-config")
def get_provider_config(
    config: Annotated[ProvidersConfig, Depends(get_providers_config)],
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    sqlite_config = store.load_sqlite_providers_config()
    editable_config = sqlite_config if (sqlite_config.providers or sqlite_config.routes) else config
    return {
        "source": config.source,
        "providers": [
            _provider_config_summary(provider)
            for provider in editable_config.providers.values()
        ],
        "provider_models": [
            _provider_model_config_summary(model)
            for model in editable_config.provider_models.values()
        ],
        "routes": [
            _route_config_summary(route, index)
            for index, route in enumerate(editable_config.routes)
        ],
    }


@router.post("/provider-config/providers")
def create_provider_config(
    request: ProviderCreateRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    provider = store.upsert_provider_config(
        provider=request.provider,
        name=request.name,
        base_url=request.base_url,
        api_key=request.api_key,
        enabled=request.enabled,
        timeout_seconds=request.timeout_seconds,
    )
    return {"provider": _provider_config_summary(provider)}


@router.patch("/provider-config/providers/{provider}")
def patch_provider_config(
    provider: str,
    request: ProviderPatchRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    updated = store.patch_provider_config(
        provider=provider,
        name=request.name,
        base_url=request.base_url,
        api_key_update=request.api_key,
        update_api_key="api_key" in request.model_fields_set,
        enabled=request.enabled,
        timeout_seconds=request.timeout_seconds,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider 不存在")
    return {"provider": _provider_config_summary(updated)}


@router.delete("/provider-config/providers/{provider}")
def delete_provider_config(
    provider: str,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    updated = store.disable_provider_config(provider)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider 不存在")
    return {"provider": _provider_config_summary(updated)}


@router.post("/provider-config/models")
def create_provider_model_config(
    request: ProviderModelCreateRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    _ensure_provider_exists(store, request.provider)
    provider_model = store.create_provider_model_config(
        provider=request.provider,
        upstream_model=request.upstream_model,
        display_name=request.display_name,
        api_format=request.api_format,
        pricing_mode=request.pricing_mode,
        pricing_tiers_json=request.pricing_tiers_json,
        input_price_per_million=request.input_price_per_million,
        output_price_per_million=request.output_price_per_million,
        currency=request.currency,
        enabled=request.enabled,
    )
    return {"model": _provider_model_config_summary(provider_model)}


@router.patch("/provider-config/models/{model_id}")
def patch_provider_model_config(
    model_id: str,
    request: ProviderModelPatchRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    if request.provider is not None:
        _ensure_provider_exists(store, request.provider)
    provider_model = store.patch_provider_model_config(
        model_id=model_id,
        provider=request.provider,
        upstream_model=request.upstream_model,
        display_name=request.display_name,
        api_format=request.api_format,
        pricing_mode=request.pricing_mode,
        pricing_tiers_json=request.pricing_tiers_json,
        input_price_per_million=request.input_price_per_million,
        output_price_per_million=request.output_price_per_million,
        currency=request.currency,
        enabled=request.enabled,
    )
    if provider_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider model 不存在")
    return {"model": _provider_model_config_summary(provider_model)}


@router.delete("/provider-config/models/{model_id}")
def delete_provider_model_config(
    model_id: str,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    provider_model = store.disable_provider_model_config(model_id)
    if provider_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider model 不存在")
    return {"model": _provider_model_config_summary(provider_model)}


@router.post("/provider-config/routes")
def create_route_config(
    request: RouteCreateRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    route_provider, route_upstream_model, route_input_price, route_output_price, route_currency = (
        _resolve_route_provider_model(request, store)
    )
    route = store.create_route_config(
        virtual_model=request.virtual_model,
        provider=route_provider,
        upstream_model=route_upstream_model,
        provider_model_id=request.provider_model_id,
        priority=request.priority,
        input_price_per_million=route_input_price,
        output_price_per_million=route_output_price,
        currency=route_currency,
        min_balance=request.min_balance,
        enabled=request.enabled,
    )
    return {"route": _route_config_summary(route, 0)}


@router.patch("/provider-config/routes/{route_id}")
def patch_route_config(
    route_id: str,
    request: RoutePatchRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    route_updates = _resolve_route_patch(request, store)
    route = store.patch_route_config(
        route_id=route_id,
        virtual_model=request.virtual_model,
        provider=route_updates.get("provider", request.provider),
        upstream_model=route_updates.get("upstream_model", request.upstream_model),
        provider_model_id=route_updates.get("provider_model_id", request.provider_model_id),
        priority=request.priority,
        input_price_per_million=route_updates.get(
            "input_price_per_million",
            request.input_price_per_million,
        ),
        output_price_per_million=route_updates.get(
            "output_price_per_million",
            request.output_price_per_million,
        ),
        currency=route_updates.get("currency", request.currency),
        min_balance=request.min_balance,
        enabled=request.enabled,
    )
    if route is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="route 不存在")
    return {"route": _route_config_summary(route, 0)}


@router.delete("/provider-config/routes/{route_id}")
def delete_route_config(
    route_id: str,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    deleted = store.delete_route_config(route_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="route 不存在")
    return {"deleted": True}


@router.post("/provider-config/import-toml")
def import_toml_provider_config(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    clear_providers_config_cache()
    config = load_providers_config(settings.providers_config_path)
    if not config.providers and not config.routes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="providers.toml 不存在或没有可导入配置",
        )

    imported_providers = 0
    imported_models = 0
    imported_routes = 0
    for provider in config.providers.values():
        store.upsert_provider_config(
            provider=provider.id,
            name=provider.name,
            base_url=provider.base_url,
            api_key=None,
            enabled=provider.enabled,
            timeout_seconds=provider.timeout_seconds,
        )
        imported_providers += 1
    for provider_model in config.provider_models.values():
        store.upsert_provider_model_by_identity(provider_model)
        imported_models += 1
    for route in config.routes:
        store.upsert_route_by_identity(route)
        imported_routes += 1

    return {
        "providers": imported_providers,
        "provider_models": imported_models,
        "routes": imported_routes,
    }


@router.get("/provider-config/export-toml", response_class=PlainTextResponse)
def export_toml_provider_config(
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> PlainTextResponse:
    config = store.load_sqlite_providers_config()
    return PlainTextResponse(_export_toml(config), media_type="text/plain; charset=utf-8")


@router.post("/provider-config/providers/{provider}/test")
async def test_provider_config(
    provider: str,
    request: ProviderTestRequest,
    config: Annotated[ProvidersConfig, Depends(get_providers_config)],
) -> dict:
    provider_config = config.providers.get(provider)
    if provider_config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider 不存在")
    api_key = provider_config.api_key or os.getenv(provider_config.api_key_env, "")
    if not api_key:
        return {
            "success": False,
            "status": None,
            "error_type": "missing_key",
            "message": "provider API key 未配置",
        }

    upstream_model = request.upstream_model or _first_provider_route_model(provider, config.routes)
    if not upstream_model:
        return {
            "success": False,
            "status": None,
            "error_type": "missing_route",
            "message": "没有可用于测试的 enabled route",
        }

    payload = {
        "model": upstream_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = f"{provider_config.base_url.rstrip('/')}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=provider_config.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return {
            "success": False,
            "status": None,
            "error_type": "timeout",
            "message": "provider 连接测试超时",
        }
    except httpx.HTTPError as exc:
        return {
            "success": False,
            "status": None,
            "error_type": "network_error",
            "message": _redact(str(exc), api_key)[:300],
        }

    if response.status_code >= 400:
        return {
            "success": False,
            "status": response.status_code,
            "error_type": _classify_provider_test_error(response),
            "message": _redact(response.text, api_key)[:300],
        }
    return {
        "success": True,
        "status": response.status_code,
        "error_type": None,
        "message": "provider 连接测试成功",
    }


@router.get("/balances")
def get_balances(
    config: Annotated[ProvidersConfig, Depends(get_providers_config)],
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    provider_ids = list(config.providers) if config.providers else None
    balances = store.list_balances(provider_ids=provider_ids)
    return {"data": [record.model_dump() for record in balances]}


@router.post("/balances/{provider}/adjust")
def adjust_balance(
    provider: str,
    request: BalanceAdjustmentRequest,
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    balance, adjustment = store.adjust_balance(
        provider=provider,
        amount_delta=request.amount_delta,
        currency=request.currency,
        reason=request.reason,
    )
    return {
        "balance": balance.model_dump(),
        "adjustment": adjustment.model_dump(),
    }


@router.get("/usage")
def get_usage(
    store: Annotated[ProviderStore, Depends(get_provider_store)],
    limit: int = Query(default=100, ge=1, le=1000),
    provider: str | None = None,
    virtual_model: str | None = None,
    status: str | None = None,
) -> dict:
    events = store.list_usage_events(
        limit=limit,
        provider=provider,
        virtual_model=virtual_model,
        status=status,
    )
    return {"data": [event.model_dump() for event in events]}


@router.get("/usage/summary")
def get_usage_summary(
    store: Annotated[ProviderStore, Depends(get_provider_store)],
) -> dict:
    return {"data": store.usage_summary()}


def _provider_summary(provider: ProviderConfig) -> dict:
    return {
        "id": provider.id,
        "provider": provider.id,
        "name": provider.name,
        "enabled": provider.enabled,
        "base_url": provider.base_url,
        "api_key_env": provider.api_key_env,
        "api_key_configured": bool(provider.api_key or os.getenv(provider.api_key_env, "")),
        "timeout_seconds": provider.timeout_seconds,
    }


def _provider_config_summary(provider: ProviderConfig) -> dict:
    return {
        "provider": provider.id,
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "enabled": provider.enabled,
        "timeout_seconds": provider.timeout_seconds,
        "api_key_configured": bool(provider.api_key or os.getenv(provider.api_key_env, "")),
        "created_at": provider.created_at,
        "updated_at": provider.updated_at,
    }


def _provider_model_config_summary(model: ProviderModelConfig) -> dict:
    return {
        "id": model.id,
        "provider": model.provider,
        "upstream_model": model.upstream_model,
        "display_name": model.display_name,
        "api_format": model.api_format,
        "pricing_mode": model.pricing_mode,
        "pricing_tiers_json": model.pricing_tiers_json,
        "input_price_per_million": model.input_price_per_million,
        "output_price_per_million": model.output_price_per_million,
        "currency": model.currency,
        "enabled": model.enabled,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def _route_config_summary(route: RouteConfig, index: int) -> dict:
    return {
        "id": route.id or f"toml:{index}",
        "virtual_model": route.virtual_model,
        "provider": route.provider,
        "upstream_model": route.upstream_model,
        "provider_model_id": route.provider_model_id,
        "priority": route.priority,
        "input_price_per_million": route.input_price_per_million,
        "output_price_per_million": route.output_price_per_million,
        "currency": route.currency,
        "min_balance": route.min_balance,
        "enabled": route.enabled,
        "created_at": route.created_at,
        "updated_at": route.updated_at,
    }


def _ensure_provider_exists(store: ProviderStore, provider: str) -> None:
    if store.get_provider_config(provider) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider 不存在")


def _ensure_provider_model_exists(
    store: ProviderStore,
    model_id: str,
) -> ProviderModelConfig:
    provider_model = store.get_provider_model_config(model_id)
    if provider_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider model 不存在")
    return provider_model


def _resolve_route_provider_model(
    request: RouteCreateRequest,
    store: ProviderStore,
) -> tuple[str, str, float, float, str]:
    if request.provider_model_id:
        provider_model = _ensure_provider_model_exists(store, request.provider_model_id)
        _ensure_provider_exists(store, provider_model.provider)
        return (
            provider_model.provider,
            provider_model.upstream_model,
            provider_model.input_price_per_million,
            provider_model.output_price_per_million,
            provider_model.currency,
        )
    if not request.provider or not request.upstream_model:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="请提供 provider_model_id，或同时提供 provider 和 upstream_model",
        )
    _ensure_provider_exists(store, request.provider)
    return (
        request.provider,
        request.upstream_model,
        request.input_price_per_million,
        request.output_price_per_million,
        request.currency,
    )


def _resolve_route_patch(
    request: RoutePatchRequest,
    store: ProviderStore,
) -> dict:
    if request.provider_model_id:
        provider_model = _ensure_provider_model_exists(store, request.provider_model_id)
        _ensure_provider_exists(store, provider_model.provider)
        return {
            "provider_model_id": provider_model.id,
            "provider": provider_model.provider,
            "upstream_model": provider_model.upstream_model,
            "input_price_per_million": provider_model.input_price_per_million,
            "output_price_per_million": provider_model.output_price_per_million,
            "currency": provider_model.currency,
        }
    if request.provider is not None:
        _ensure_provider_exists(store, request.provider)
    return {}


def _first_provider_route_model(provider: str, routes: list[RouteConfig]) -> str | None:
    provider_routes = [
        route
        for route in routes
        if route.provider == provider and route.enabled
    ]
    provider_routes.sort(key=lambda route: route.priority, reverse=True)
    return provider_routes[0].upstream_model if provider_routes else None


def _classify_provider_test_error(response: httpx.Response) -> str:
    text = response.text.lower()
    if any(marker in text for marker in ("insufficient_quota", "quota", "balance")):
        return "quota"
    if response.status_code in {401, 403}:
        return "auth"
    if response.status_code == 402:
        return "quota"
    if response.status_code == 429:
        return "rate_limit"
    if response.status_code >= 500:
        return "server_error"
    return "http_error"


def _redact(text: str, secret: str) -> str:
    if secret:
        return text.replace(secret, "[redacted]")
    return text


def _export_toml(config: ProvidersConfig) -> str:
    default_model = config.router.default_model or _default_model_from_routes(config.routes)
    lines = [
        "[router]",
        f'default_model = "{_toml_escape(default_model or "")}"',
        f"fallback_enabled = {str(config.router.fallback_enabled).lower()}",
        "",
    ]
    for provider in config.providers.values():
        key_env = provider.api_key_env or f"{_env_name(provider.id)}_API_KEY"
        lines.extend(
            [
                f"[providers.{provider.id}]",
                f'name = "{_toml_escape(provider.name)}"',
                f'base_url = "{_toml_escape(provider.base_url)}"',
                f'api_key_env = "{_toml_escape(key_env)}"',
                f"enabled = {str(provider.enabled).lower()}",
                f"timeout_seconds = {provider.timeout_seconds:g}",
                "# API key is not exported. Set it in the environment or re-enter it in the UI.",
                "",
            ]
        )
    for model in config.provider_models.values():
        lines.extend(
            [
                "[[provider_models]]",
                f'id = "{_toml_escape(model.id)}"',
                f'provider = "{_toml_escape(model.provider)}"',
                f'upstream_model = "{_toml_escape(model.upstream_model)}"',
                f'display_name = "{_toml_escape(model.display_name)}"',
                f'api_format = "{_toml_escape(model.api_format)}"',
                f'pricing_mode = "{_toml_escape(model.pricing_mode)}"',
                f'pricing_tiers_json = "{_toml_escape(model.pricing_tiers_json)}"',
                f"input_price_per_million = {model.input_price_per_million:g}",
                f"output_price_per_million = {model.output_price_per_million:g}",
                f'currency = "{_toml_escape(model.currency)}"',
                f"enabled = {str(model.enabled).lower()}",
                "",
            ]
        )
    for route in config.routes:
        lines.extend(
            [
                "[[routes]]",
                f'virtual_model = "{_toml_escape(route.virtual_model)}"',
                f'provider = "{_toml_escape(route.provider)}"',
                f'upstream_model = "{_toml_escape(route.upstream_model)}"',
                (
                    f'provider_model_id = "{_toml_escape(route.provider_model_id)}"'
                    if route.provider_model_id
                    else "# provider_model_id is optional for legacy route compatibility."
                ),
                f"priority = {route.priority}",
                f"input_price_per_million = {route.input_price_per_million:g}",
                f"output_price_per_million = {route.output_price_per_million:g}",
                f'currency = "{_toml_escape(route.currency)}"',
                f"min_balance = {route.min_balance:g}",
                f"enabled = {str(route.enabled).lower()}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _default_model_from_routes(routes: list[RouteConfig]) -> str | None:
    enabled_routes = [route for route in routes if route.enabled]
    enabled_routes.sort(key=lambda route: route.priority, reverse=True)
    return enabled_routes[0].virtual_model if enabled_routes else None


def _env_name(provider: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in provider).upper()


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
