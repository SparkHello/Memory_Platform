from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable

from app.llm.routing import LLMProvider, ordered_configured_providers


BUILTIN_CATALOG_PATH = Path(__file__).with_name("catalog") / "models.json"
BUILTIN_ROUTES_PATH = Path(__file__).with_name("catalog") / "routes.json"

ROUTE_NAMES = (
    "chat",
    "memory.extract",
    "memory.compact",
    "memory.core",
    "memory.review",
    "knowledge.fast",
    "knowledge.pro",
    "pricing.research",
)

_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[^\s/][^\s]*$")
_PROVIDER_CODES = {
    "mimo": "M",
    "kimi": "K",
    "deepseek": "D",
    "upstream": "D",
    "embedding": "D",
}


class CatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    provider: str
    model: str
    kind: str
    capabilities: tuple[str, ...] = ()
    official_url: str = ""

    @property
    def provider_code(self) -> str:
        return _PROVIDER_CODES[self.provider]

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "official_url": self.official_url,
        }


def load_model_catalog(path: str | Path = "") -> dict[str, ModelSpec]:
    builtins = _parse_model_catalog(_load_json(BUILTIN_CATALOG_PATH))
    if not str(path).strip():
        return builtins
    overlay_path = Path(path).expanduser()
    if overlay_path.resolve() == BUILTIN_CATALOG_PATH.resolve():
        return builtins
    overlay = _parse_model_catalog(_load_json(overlay_path))
    return {**builtins, **overlay}


def load_routes(path: str | Path = "") -> dict[str, list[str]]:
    if not str(path).strip():
        return {}
    payload = _load_json(Path(path).expanduser())
    if payload.get("version") != 1:
        raise CatalogError("模型路由文件 version 必须为 1")
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, dict):
        raise CatalogError("模型路由文件缺少 routes 对象")
    routes: dict[str, list[str]] = {}
    for route_name, raw_model_ids in raw_routes.items():
        if route_name not in ROUTE_NAMES:
            raise CatalogError(f"未知功能路由：{route_name}")
        if not isinstance(raw_model_ids, list) or not raw_model_ids:
            raise CatalogError(f"功能路由 {route_name} 至少需要一个模型")
        model_ids = [_required_string(value, "模型 ID") for value in raw_model_ids]
        if len(set(model_ids)) != len(model_ids):
            raise CatalogError(f"功能路由 {route_name} 不能重复引用同一模型")
        routes[route_name] = model_ids
    return routes


def validate_catalog_and_routes(
    *,
    catalog_path: str | Path = "",
    routes_path: str | Path = "",
) -> tuple[dict[str, ModelSpec], dict[str, list[str]]]:
    catalog = load_model_catalog(catalog_path)
    routes = load_routes(routes_path)
    for route_name, model_ids in routes.items():
        for model_id in model_ids:
            if model_id not in catalog:
                raise CatalogError(
                    f"功能路由 {route_name} 引用了不存在的模型：{model_id}"
                )
            if catalog[model_id].kind != "chat":
                raise CatalogError(
                    f"功能路由 {route_name} 只能引用 chat 模型：{model_id}"
                )
            if route_name == "knowledge.pro" and catalog[model_id].provider not in {
                "deepseek",
                "upstream",
            }:
                raise CatalogError(
                    "功能路由 knowledge.pro 当前只支持 deepseek/upstream 模型"
                )
    return catalog, routes


def route_for_operation(operation: str) -> str:
    normalized = operation.strip().lower()
    if normalized in {"memory-extractor", "memory-ingester"}:
        return "memory.extract"
    if normalized == "memory-context-compactor":
        return "memory.compact"
    if normalized == "core-memory-consolidator":
        return "memory.core"
    if normalized == "memory-review-editor":
        return "memory.review"
    if normalized == "pricing-research":
        return "pricing.research"
    return "memory.extract"


def providers_for_route(
    settings: Any,
    route_name: str,
    *,
    legacy_priority: str | None = None,
) -> list[LLMProvider]:
    if route_name not in ROUTE_NAMES:
        raise CatalogError(f"未知功能路由：{route_name}")
    routes_path = str(getattr(settings, "model_routes_path", "") or "").strip()
    if not routes_path:
        return ordered_configured_providers(
            legacy_priority or settings.llm_provider_priority,
            legacy_provider_map(settings),
        )

    catalog, routes = validate_catalog_and_routes(
        catalog_path=str(getattr(settings, "model_catalog_path", "") or ""),
        routes_path=routes_path,
    )
    model_ids = routes.get(route_name)
    if not model_ids:
        return ordered_configured_providers(
            legacy_priority or settings.llm_provider_priority,
            legacy_provider_map(settings),
        )
    providers = [provider_for_spec(settings, catalog[model_id]) for model_id in model_ids]
    return [provider for provider in providers if provider.configured]


def providers_for_operation(settings: Any, operation: str) -> list[LLMProvider]:
    return providers_for_route(settings, route_for_operation(operation))


def legacy_provider_map(settings: Any) -> dict[str, LLMProvider]:
    deepseek = LLMProvider(
        code="D",
        base_url=settings.llm_deepseek_base_url,
        api_key=settings.llm_deepseek_api_key,
        model=settings.llm_deepseek_flash_model,
    )
    if not deepseek.configured:
        deepseek = LLMProvider(
            code="D",
            base_url=settings.upstream_base_url,
            api_key=settings.upstream_api_key,
            model=settings.upstream_model,
        )
    return {
        "M": LLMProvider(
            code="M",
            base_url=settings.llm_mimo_base_url,
            api_key=settings.llm_mimo_api_key,
            model=settings.llm_mimo_model,
        ),
        "K": LLMProvider(
            code="K",
            base_url=settings.llm_kimi_base_url,
            api_key=settings.llm_kimi_api_key,
            model=settings.llm_kimi_model,
        ),
        "D": deepseek,
    }


def provider_for_spec(settings: Any, spec: ModelSpec) -> LLMProvider:
    if spec.provider == "mimo":
        base_url, api_key = settings.llm_mimo_base_url, settings.llm_mimo_api_key
    elif spec.provider == "kimi":
        base_url, api_key = settings.llm_kimi_base_url, settings.llm_kimi_api_key
    elif spec.provider == "deepseek":
        base_url, api_key = (
            settings.llm_deepseek_base_url,
            settings.llm_deepseek_api_key,
        )
    elif spec.provider == "upstream":
        base_url, api_key = settings.upstream_base_url, settings.upstream_api_key
    else:
        base_url, api_key = settings.embedding_base_url, settings.embedding_api_key
    return LLMProvider(
        code=spec.provider_code,
        base_url=base_url,
        api_key=api_key,
        model=spec.model,
    )


def model_ids_for_providers(providers: Iterable[LLMProvider]) -> list[str]:
    return list(dict.fromkeys(provider.model for provider in providers))


def _parse_model_catalog(payload: dict[str, Any]) -> dict[str, ModelSpec]:
    if payload.get("version") != 1:
        raise CatalogError("模型目录 version 必须为 1")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise CatalogError("模型目录缺少 models 数组")
    models: dict[str, ModelSpec] = {}
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise CatalogError("models 中的每一项都必须是对象")
        model_id = _required_string(raw.get("id"), "模型 ID").lower()
        if not _MODEL_ID_RE.fullmatch(model_id):
            raise CatalogError(f"模型 ID 格式无效：{model_id}")
        if model_id in models:
            raise CatalogError(f"模型 ID 重复：{model_id}")
        provider = _required_string(raw.get("provider"), "provider").lower()
        if provider not in _PROVIDER_CODES:
            raise CatalogError(
                f"模型 {model_id} 的 provider 仅支持 mimo/kimi/deepseek/upstream/embedding"
            )
        kind = _required_string(raw.get("kind", "chat"), "kind").lower()
        if kind not in {"chat", "embedding"}:
            raise CatalogError(f"模型 {model_id} 的 kind 仅支持 chat/embedding")
        if (provider == "embedding") != (kind == "embedding"):
            raise CatalogError(
                f"模型 {model_id} 的 embedding provider 与 kind 必须同时使用"
            )
        capabilities = raw.get("capabilities", [])
        if not isinstance(capabilities, list) or any(
            not isinstance(value, str) or not value.strip() for value in capabilities
        ):
            raise CatalogError(f"模型 {model_id} 的 capabilities 必须是字符串数组")
        models[model_id] = ModelSpec(
            id=model_id,
            provider=provider,
            model=_required_string(raw.get("model"), "model"),
            kind=kind,
            capabilities=tuple(dict.fromkeys(value.strip() for value in capabilities)),
            official_url=str(raw.get("official_url") or "").strip(),
        )
    return models


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"配置文件不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError(f"配置文件顶层必须是对象：{path}")
    return payload


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label} 必须是非空字符串")
    return value.strip()
