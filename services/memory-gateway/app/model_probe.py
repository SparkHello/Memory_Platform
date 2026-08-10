from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable

import httpx

from app.llm.client import _provider_payload
from app.model_catalog import ModelSpec, provider_for_spec
from app.openai_compat.schemas import ChatCompletionRequest


PROBE_PROVIDERS = ("mimo", "kimi", "deepseek", "upstream", "embedding")


@dataclass(frozen=True, slots=True)
class ModelProbeResult:
    model_id: str
    provider: str
    model: str
    status: str
    detail: str
    configured: bool
    failed: bool


def check_model_catalog(
    settings: Any,
    models: Iterable[ModelSpec],
    *,
    provider_filter: str = "",
    live: bool = False,
    timeout_seconds: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> list[ModelProbeResult]:
    selected = [
        model
        for model in models
        if not provider_filter or model.provider == provider_filter
    ]
    grouped: dict[tuple[str, str, str], list[ModelSpec]] = {}
    for spec in selected:
        provider = provider_for_spec(settings, spec)
        grouped.setdefault(
            (spec.provider, provider.base_url.rstrip("/"), provider.api_key),
            [],
        ).append(spec)

    results: list[ModelProbeResult] = []
    client_kwargs: dict[str, Any] = {
        "timeout": timeout_seconds,
        # Provider credentials must never be inherited by a workstation proxy
        # or replayed to a redirect target during discovery/live checks.
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    with httpx.Client(**client_kwargs) as client:
        for (_, base_url, api_key), specs in grouped.items():
            if not base_url or not api_key:
                results.extend(
                    _result(
                        spec,
                        status="not_configured",
                        detail="API Key 或 Base URL 未配置",
                        configured=False,
                        failed=False,
                    )
                    for spec in specs
                )
                continue
            if live:
                results.extend(
                    _live_probe(
                        client,
                        settings=settings,
                        spec=spec,
                        base_url=base_url,
                        api_key=api_key,
                    )
                    for spec in specs
                )
                continue
            results.extend(
                _catalog_probe(
                    client,
                    specs=specs,
                    base_url=base_url,
                    api_key=api_key,
                )
            )
    return results


def _catalog_probe(
    client: httpx.Client,
    *,
    specs: list[ModelSpec],
    base_url: str,
    api_key: str,
) -> list[ModelProbeResult]:
    try:
        response = client.get(
            f"{base_url}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        return [
            _result(
                spec,
                status="network_error",
                detail=f"连接失败：{type(exc).__name__}",
                configured=True,
                failed=True,
            )
            for spec in specs
        ]
    if response.status_code in {401, 403}:
        detail = f"鉴权失败（HTTP {response.status_code}）"
        return [_result(spec, "auth_failed", detail, True, True) for spec in specs]
    if response.status_code == 429:
        return [
            _result(spec, "rate_limited", "连接成功，但 provider 返回 429", True, True)
            for spec in specs
        ]
    if response.status_code in {404, 405}:
        return [
            _result(
                spec,
                "check_unsupported",
                f"provider 不支持 GET /models（HTTP {response.status_code}）",
                True,
                False,
            )
            for spec in specs
        ]
    if response.status_code >= 400:
        return [
            _result(
                spec,
                "provider_error",
                f"provider 返回 HTTP {response.status_code}",
                True,
                True,
            )
            for spec in specs
        ]
    model_ids = _model_ids_from_response(response)
    if not model_ids:
        return [
            _result(
                spec,
                "connected",
                "连接与鉴权正常；provider 未返回可识别的模型列表",
                True,
                False,
            )
            for spec in specs
        ]
    normalized_ids = {value.lower().rsplit("/", 1)[-1] for value in model_ids}
    return [
        (
            _result(spec, "available", "连接、鉴权和模型列表均正常", True, False)
            if spec.model.lower().rsplit("/", 1)[-1] in normalized_ids
            else _result(
                spec,
                "connected_unlisted",
                "连接与鉴权正常，但模型未出现在 /models 列表中",
                True,
                False,
            )
        )
        for spec in specs
    ]


def _live_probe(
    client: httpx.Client,
    *,
    settings: Any,
    spec: ModelSpec,
    base_url: str,
    api_key: str,
) -> ModelProbeResult:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        if spec.kind == "embedding":
            response = client.post(
                f"{base_url}/embeddings",
                headers=headers,
                json={"model": spec.model, "input": ["ping"]},
            )
        else:
            provider = provider_for_spec(settings, spec)
            request = ChatCompletionRequest(
                model="model-health-check",
                messages=[{"role": "user", "content": "Reply OK."}],
                max_tokens=1,
                temperature=0.0,
            )
            payload = _provider_payload(
                provider=provider,
                request=request,
                messages=[{"role": "user", "content": "Reply OK."}],
                thinking="disabled",
                structured_tool=None,
            )
            response = client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError as exc:
        return _result(
            spec,
            "network_error",
            f"连接失败：{type(exc).__name__}",
            True,
            True,
        )
    if response.is_success:
        return _result(
            spec,
            "live_ok",
            "最小真实请求成功（可能产生少量费用）",
            True,
            False,
        )
    if response.status_code in {401, 403}:
        status = "auth_failed"
    elif response.status_code == 429:
        status = "rate_limited"
    elif response.status_code == 404:
        status = "model_not_found"
    else:
        status = "provider_error"
    return _result(
        spec,
        status,
        f"最小真实请求返回 HTTP {response.status_code}：{_safe_error(response)}",
        True,
        True,
    )


def _model_ids_from_response(response: httpx.Response) -> set[str]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return set()
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("data") or payload.get("models") or []
    else:
        return set()
    if not isinstance(raw_items, list):
        return set()
    model_ids: set[str] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name")
            if isinstance(value, str) and value.strip():
                model_ids.add(value.strip())
    return model_ids


def _safe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:160].replace("\n", " ")
    return str(payload)[:160].replace("\n", " ")


def _result(
    spec: ModelSpec,
    status: str,
    detail: str,
    configured: bool,
    failed: bool,
) -> ModelProbeResult:
    return ModelProbeResult(
        model_id=spec.id,
        provider=spec.provider,
        model=spec.model,
        status=status,
        detail=detail,
        configured=configured,
        failed=failed,
    )
