"""Two-level provider configuration: `providers[]` + `routes{}`.

This replaces the former three-letter `ProviderCode` scheme, whose slots were
exhausted (`upstream` and `embedding` both had to squeeze into ``"D"``).  A
provider is now an ordinary record keyed by a free-form id, and a route target
is the string ``"provider_id/model_id"``.

Secrets never live in ``providers.json``.  Each provider reads its key from the
environment variable ``PROVIDER_<ID>_API_KEY``, which keeps it inside the
memgw-managed ``settings.env`` where ``_is_secret_name`` already masks it.

Embedding configuration deliberately stays on the separate ``EMBEDDING_*``
settings.  ``direct_embedding_space_id()`` derives the stored vector space from
those three values, so routing embeddings through this catalog would silently
invalidate every existing vector.  Embedding entries in the presets are used for
listing and connectivity probes only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from app.llm.routing import LLMProvider, ProviderQuirks


BUILTIN_PRESETS_PATH = Path(__file__).with_name("presets.json")

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

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MODEL_ID_RE = re.compile(r"^[^\s/][^\s]*$")
_PROTOCOLS = {"openai"}


class ProviderConfigError(ValueError):
    pass


# Kept so existing `except CatalogError` sites keep working.
CatalogError = ProviderConfigError


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    kind: str
    quirks: ProviderQuirks


@dataclass(frozen=True, slots=True)
class ProviderDef:
    id: str
    name: str
    protocol: str
    api_host: str
    models: dict[str, ProviderModel]
    urls: dict[str, str] = field(default_factory=dict)
    # Variable names from the retired MKD scheme, still honoured so the
    # migration never requires copying key material between stores.
    legacy_api_key_envs: tuple[str, ...] = ()

    @property
    def api_key_env(self) -> str:
        return f"PROVIDER_{self.id.upper().replace('-', '_')}_API_KEY"

    def resolve_api_key(self, secrets: Mapping[str, str]) -> str:
        for name in (self.api_key_env, *self.legacy_api_key_envs):
            value = str(secrets.get(name, "") or "").strip()
            if value:
                return value
        return ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "api_host": self.api_host,
            "api_key_env": self.api_key_env,
            "legacy_api_key_envs": list(self.legacy_api_key_envs),
            "models": [
                {"id": model.id, "kind": model.kind}
                for model in self.models.values()
            ],
            "urls": dict(self.urls),
        }


def load_providers(path: str | Path = "") -> dict[str, ProviderDef]:
    """Built-in presets, overlaid by the user's ``providers.json`` if present."""
    providers = _parse_providers(_load_json(BUILTIN_PRESETS_PATH))
    overlay_path = str(path).strip()
    if not overlay_path:
        return providers
    resolved = Path(overlay_path).expanduser()
    if not resolved.exists() or resolved.resolve() == BUILTIN_PRESETS_PATH.resolve():
        return providers
    return {**providers, **_parse_providers(_load_json(resolved))}


def load_routes(path: str | Path = "") -> dict[str, list[str]]:
    if not str(path).strip():
        return {}
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return {}
    payload = _load_json(resolved)
    if payload.get("version") != 1:
        raise ProviderConfigError("路由文件 version 必须为 1")
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, dict):
        raise ProviderConfigError("路由文件缺少 routes 对象")
    routes: dict[str, list[str]] = {}
    for route_name, raw_targets in raw_routes.items():
        if route_name not in ROUTE_NAMES:
            raise ProviderConfigError(f"未知功能路由：{route_name}")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ProviderConfigError(f"功能路由 {route_name} 至少需要一个目标")
        targets = [_required_string(value, "路由目标") for value in raw_targets]
        if len(set(targets)) != len(targets):
            raise ProviderConfigError(f"功能路由 {route_name} 不能重复引用同一目标")
        routes[route_name] = targets
    return routes


def split_target(target: str) -> tuple[str, str]:
    provider_id, separator, model_id = target.partition("/")
    if not separator or not provider_id.strip() or not model_id.strip():
        raise ProviderConfigError(
            f"路由目标必须写成 provider/model 形式：{target}"
        )
    return provider_id.strip(), model_id.strip()


def validate_providers_and_routes(
    *,
    providers_path: str | Path = "",
    routes_path: str | Path = "",
) -> tuple[dict[str, ProviderDef], dict[str, list[str]]]:
    providers = load_providers(providers_path)
    routes = load_routes(routes_path)
    for route_name, targets in routes.items():
        for target in targets:
            provider_id, model_id = split_target(target)
            provider = providers.get(provider_id)
            if provider is None:
                raise ProviderConfigError(
                    f"功能路由 {route_name} 引用了不存在的供应商：{provider_id}"
                )
            model = provider.models.get(model_id)
            if model is None:
                raise ProviderConfigError(
                    f"功能路由 {route_name} 引用了 {provider_id} 中不存在的模型：{model_id}"
                )
            if model.kind != "chat":
                raise ProviderConfigError(
                    f"功能路由 {route_name} 只能引用 chat 模型：{target}"
                )
    return providers, routes


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
    secrets: Mapping[str, str] | None = None,
) -> list[LLMProvider]:
    if route_name not in ROUTE_NAMES:
        raise ProviderConfigError(f"未知功能路由：{route_name}")
    definitions, routes = validate_providers_and_routes(
        providers_path=str(getattr(settings, "providers_path", "") or ""),
        routes_path=str(getattr(settings, "routes_path", "") or ""),
    )
    targets = routes.get(route_name)
    if not targets:
        return []
    env = os.environ if secrets is None else secrets
    resolved: list[LLMProvider] = []
    for target in targets:
        provider_id, model_id = split_target(target)
        definition = definitions[provider_id]
        model = definition.models[model_id]
        candidate = LLMProvider(
            code=definition.id,
            base_url=definition.api_host,
            api_key=definition.resolve_api_key(env),
            model=model.id,
            quirks=model.quirks,
        )
        if candidate.configured:
            resolved.append(candidate)
    return resolved


def providers_for_operation(
    settings: Any,
    operation: str,
    *,
    secrets: Mapping[str, str] | None = None,
) -> list[LLMProvider]:
    return providers_for_route(
        settings,
        route_for_operation(operation),
        secrets=secrets,
    )


def configured_provider_ids(
    settings: Any,
    *,
    secrets: Mapping[str, str] | None = None,
) -> list[str]:
    definitions = load_providers(str(getattr(settings, "providers_path", "") or ""))
    env = os.environ if secrets is None else secrets
    return sorted(
        definition.id
        for definition in definitions.values()
        if definition.resolve_api_key(env)
    )


def model_ids_for_providers(providers: Iterable[LLMProvider]) -> list[str]:
    return list(dict.fromkeys(provider.model for provider in providers))


def _parse_providers(payload: dict[str, Any]) -> dict[str, ProviderDef]:
    if payload.get("version") != 1:
        raise ProviderConfigError("供应商文件 version 必须为 1")
    raw_presets = payload.get("presets")
    if not isinstance(raw_presets, dict):
        raise ProviderConfigError("供应商文件缺少 presets 对象")
    providers: dict[str, ProviderDef] = {}
    for provider_id, raw in raw_presets.items():
        normalized_id = _required_string(provider_id, "供应商 ID").lower()
        if not _PROVIDER_ID_RE.fullmatch(normalized_id):
            raise ProviderConfigError(f"供应商 ID 格式无效：{normalized_id}")
        if not isinstance(raw, dict):
            raise ProviderConfigError(f"供应商 {normalized_id} 必须是对象")
        protocol = str(raw.get("protocol") or "openai").strip().lower()
        if protocol not in _PROTOCOLS:
            raise ProviderConfigError(
                f"供应商 {normalized_id} 的 protocol 目前仅支持 openai"
            )
        base_quirks = ProviderQuirks().merged(_quirk_overrides(raw.get("quirks")))
        raw_models = raw.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise ProviderConfigError(f"供应商 {normalized_id} 至少需要一个模型")
        models: dict[str, ProviderModel] = {}
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                raise ProviderConfigError(f"供应商 {normalized_id} 的模型项必须是对象")
            model_id = _required_string(raw_model.get("id"), "模型 ID")
            if not _MODEL_ID_RE.fullmatch(model_id):
                raise ProviderConfigError(f"模型 ID 格式无效：{model_id}")
            if model_id in models:
                raise ProviderConfigError(
                    f"供应商 {normalized_id} 中模型 ID 重复：{model_id}"
                )
            kind = str(raw_model.get("kind") or "chat").strip().lower()
            if kind not in {"chat", "embedding"}:
                raise ProviderConfigError(
                    f"模型 {model_id} 的 kind 仅支持 chat/embedding"
                )
            models[model_id] = ProviderModel(
                id=model_id,
                kind=kind,
                quirks=base_quirks.merged(_quirk_overrides(raw_model.get("quirks"))),
            )
        raw_urls = raw.get("urls") or {}
        if not isinstance(raw_urls, dict):
            raise ProviderConfigError(f"供应商 {normalized_id} 的 urls 必须是对象")
        providers[normalized_id] = ProviderDef(
            id=normalized_id,
            name=str(raw.get("name") or normalized_id).strip(),
            protocol=protocol,
            api_host=_required_string(raw.get("api_host"), "api_host"),
            models=models,
            legacy_api_key_envs=tuple(
                str(name) for name in (raw.get("legacy_api_key_envs") or [])
            ),
            urls={
                str(key): str(value)
                for key, value in raw_urls.items()
                if isinstance(value, str) and value.strip()
            },
        )
    return providers


def _quirk_overrides(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProviderConfigError("quirks 必须是对象")
    return dict(value)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProviderConfigError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(f"配置文件不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderConfigError(f"配置文件顶层必须是对象：{path}")
    return payload


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError(f"{label} 必须是非空字符串")
    return value.strip()
