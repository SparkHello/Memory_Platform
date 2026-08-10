import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
import httpx

from app.api.deps import get_user_id, require_api_key
from app.config import Settings, get_settings
from app.llm.runtime import resolve_model_runtime
from app.usage.attribution import (
    UsageAttributionNotConfigured,
    model_gateway_user_tag,
)
from app.usage.store import UsageStore


router = APIRouter(
    prefix="/usage",
    tags=["model usage"],
    dependencies=[Depends(require_api_key)],
)


_MAX_MODEL_USAGE_SUMMARY_BYTES = 1024 * 1024
_MODEL_USAGE_SUMMARY_FIELDS = frozenset(
    {
        "days",
        "filters",
        "calls",
        "complete_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_costs",
        "incomplete_cost_calls",
        "attempts",
        "deployments",
        "retention",
    }
)


@router.get("/summary")
async def model_usage_summary(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    range: Annotated[
        Literal["7", "30", "90", "all"],
        Query(description="统计最近 7/30/90 天或全部历史"),
    ] = "30",
):
    days = None if range == "all" else int(range)
    runtime = resolve_model_runtime(settings)
    if not runtime.is_central:
        return await asyncio.to_thread(
            UsageStore(settings.database_path).summary,
            user_id=user_id,
            days=days,
        )
    try:
        user_tag = model_gateway_user_tag(
            signing_secret=settings.gateway_signing_secret,
            user_id=user_id,
        )
    except UsageAttributionNotConfigured:
        return _usage_proxy_error("model_gateway_usage_attribution_unavailable")
    try:
        async with httpx.AsyncClient(
            timeout=min(float(settings.request_timeout_seconds), 10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{runtime.base_url.rstrip('/')}/usage/summary",
                headers={
                    "Authorization": f"Bearer {runtime.api_key}",
                    "Accept": "application/json",
                },
                params={
                    "days": 365 if days is None else days,
                    "user_tag": user_tag,
                },
            )
    except httpx.HTTPError:
        return _usage_proxy_error("model_gateway_usage_unavailable")
    if response.status_code in {401, 403}:
        return _usage_proxy_error("model_gateway_usage_auth_failed")
    if response.status_code != 200:
        return _usage_proxy_error("model_gateway_usage_unavailable")
    if len(response.content) > _MAX_MODEL_USAGE_SUMMARY_BYTES:
        return _usage_proxy_error(
            "model_gateway_usage_invalid_response",
            status_code=502,
        )
    try:
        payload = response.json()
    except ValueError:
        return _usage_proxy_error(
            "model_gateway_usage_invalid_response",
            status_code=502,
        )
    if not _valid_model_usage_summary(payload):
        return _usage_proxy_error(
            "model_gateway_usage_invalid_response",
            status_code=502,
        )
    return {name: payload[name] for name in _MODEL_USAGE_SUMMARY_FIELDS if name in payload}


def _usage_proxy_error(code: str, *, status_code: int = 503) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": "中央模型用量暂时不可用",
            }
        },
    )


def _valid_model_usage_summary(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    integer_fields = (
        "days",
        "calls",
        "complete_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "incomplete_cost_calls",
    )
    if any(
        isinstance(payload.get(name), bool)
        or not isinstance(payload.get(name), int)
        or payload[name] < 0
        for name in integer_fields
    ):
        return False
    return (
        isinstance(payload.get("estimated_costs"), dict)
        and isinstance(payload.get("attempts"), dict)
        and isinstance(payload.get("deployments"), list)
        and isinstance(payload.get("retention"), dict)
    )
