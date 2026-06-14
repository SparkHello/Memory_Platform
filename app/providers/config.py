from functools import lru_cache
from pathlib import Path
import tomllib

from pydantic import ValidationError

from app.providers.models import (
    ProviderConfig,
    ProviderModelConfig,
    ProvidersConfig,
    RouteConfig,
    RouterConfig,
)


@lru_cache
def load_providers_config(path_text: str) -> ProvidersConfig:
    path = Path(path_text)
    if not path.exists():
        return ProvidersConfig(enabled=False, path=path_text, source="legacy")

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        return ProvidersConfig(
            enabled=False,
            path=path_text,
            source="legacy",
            errors=[f"无法读取 providers 配置：{exc}"],
        )
    except tomllib.TOMLDecodeError as exc:
        return ProvidersConfig(
            enabled=False,
            path=path_text,
            source="legacy",
            errors=[f"providers TOML 解析失败：{exc}"],
        )

    errors: list[str] = []
    try:
        router = RouterConfig.model_validate(raw.get("router") or {})
    except ValidationError as exc:
        router = RouterConfig()
        errors.append(f"router 配置无效：{_first_validation_error(exc)}")

    providers = _load_providers(raw.get("providers"), errors)
    provider_models = _load_provider_models(raw.get("provider_models"), providers, errors)
    routes = _load_routes(raw.get("routes"), providers, provider_models, errors)
    enabled = bool(providers and routes and not errors)
    return ProvidersConfig(
        enabled=enabled,
        path=path_text,
        source="toml" if enabled else "legacy",
        router=router,
        providers=providers,
        provider_models=provider_models,
        routes=routes,
        errors=errors,
    )


def load_effective_providers_config(
    *,
    database_path: str,
    providers_config_path: str,
) -> ProvidersConfig:
    # 配置优先级必须保持明确：SQLite UI 配置 > providers.toml > 旧 UPSTREAM_*。
    from app.providers.store import ProviderStore

    store = ProviderStore(database_path)
    store.init_db()
    sqlite_config = store.load_sqlite_providers_config()
    if sqlite_config.has_routes:
        return sqlite_config

    toml_config = load_providers_config(providers_config_path)
    if toml_config.has_routes:
        return toml_config

    return ProvidersConfig(
        enabled=False,
        path=providers_config_path,
        source="legacy",
        errors=toml_config.errors,
    )


def clear_providers_config_cache() -> None:
    load_providers_config.cache_clear()


def _load_providers(raw: object, errors: list[str]) -> dict[str, ProviderConfig]:
    if raw is None:
        errors.append("缺少 [providers] 配置")
        return {}
    if not isinstance(raw, dict):
        errors.append("[providers] 必须是 TOML table")
        return {}

    providers: dict[str, ProviderConfig] = {}
    for provider_id, payload in raw.items():
        if not isinstance(payload, dict):
            errors.append(f"provider {provider_id} 必须是 table")
            continue
        try:
            provider = ProviderConfig.model_validate({"id": provider_id, **payload})
        except ValidationError as exc:
            errors.append(f"provider {provider_id} 配置无效：{_first_validation_error(exc)}")
            continue
        if not provider.id.strip():
            errors.append("provider id 不能为空")
            continue
        if provider.id in providers:
            errors.append(f"provider {provider.id} 重复")
            continue
        providers[provider.id] = provider
    return providers


def _load_routes(
    raw: object,
    providers: dict[str, ProviderConfig],
    provider_models: dict[str, ProviderModelConfig],
    errors: list[str],
) -> list[RouteConfig]:
    if raw is None:
        errors.append("缺少 [[routes]] 配置")
        return []
    if not isinstance(raw, list):
        errors.append("[[routes]] 必须是 table array")
        return []

    routes: list[RouteConfig] = []
    for index, payload in enumerate(raw, start=1):
        if not isinstance(payload, dict):
            errors.append(f"route #{index} 必须是 table")
            continue
        try:
            route = RouteConfig.model_validate(payload)
        except ValidationError as exc:
            errors.append(f"route #{index} 配置无效：{_first_validation_error(exc)}")
            continue
        if route.provider not in providers:
            errors.append(f"route {route.virtual_model} 引用了不存在的 provider {route.provider}")
            continue
        if route.provider_model_id:
            model = provider_models.get(route.provider_model_id)
            if model is None:
                errors.append(
                    f"route {route.virtual_model} 引用了不存在的 provider_model {route.provider_model_id}"
                )
                continue
            if model.provider != route.provider:
                errors.append(
                    f"route {route.virtual_model} 的 provider_model {route.provider_model_id} 不属于 provider {route.provider}"
                )
                continue
        if not route.virtual_model.strip():
            errors.append(f"route #{index} virtual_model 不能为空")
            continue
        if not route.upstream_model.strip():
            errors.append(f"route #{index} upstream_model 不能为空")
            continue
        routes.append(route)
    return routes


def _load_provider_models(
    raw: object,
    providers: dict[str, ProviderConfig],
    errors: list[str],
) -> dict[str, ProviderModelConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        errors.append("[[provider_models]] 必须是 table array")
        return {}

    provider_models: dict[str, ProviderModelConfig] = {}
    for index, payload in enumerate(raw, start=1):
        if not isinstance(payload, dict):
            errors.append(f"provider_model #{index} 必须是 table")
            continue
        try:
            provider_model = ProviderModelConfig.model_validate(payload)
        except ValidationError as exc:
            errors.append(f"provider_model #{index} 配置无效：{_first_validation_error(exc)}")
            continue
        if provider_model.provider not in providers:
            errors.append(
                f"provider_model {provider_model.id} 引用了不存在的 provider {provider_model.provider}"
            )
            continue
        if not provider_model.upstream_model.strip():
            errors.append(f"provider_model {provider_model.id} upstream_model 不能为空")
            continue
        if provider_model.id in provider_models:
            errors.append(f"provider_model {provider_model.id} 重复")
            continue
        provider_models[provider_model.id] = provider_model
    return provider_models


def _first_validation_error(exc: ValidationError) -> str:
    first_error = exc.errors()[0]
    field = ".".join(str(part) for part in first_error.get("loc", ()))
    message = str(first_error.get("msg", "validation error"))
    return f"{field}: {message}" if field else message
