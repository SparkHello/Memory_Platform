import json

from app.providers.models import ProviderModelConfig, RouteConfig, UsageEvent


def build_success_usage_event(
    *,
    response: dict,
    messages: list[dict],
    route: RouteConfig,
    model: ProviderModelConfig,
    provider: str,
    user_id: str | None,
    conversation_id: str | None,
    cache_hit_tokens: int = 0,
) -> UsageEvent:
    usage = response.get("usage") if isinstance(response, dict) else None
    estimated = not isinstance(usage, dict)
    if estimated:
        prompt_tokens = _estimate_prompt_tokens(messages)
        completion_tokens = _estimate_completion_tokens(response)
        total_tokens = prompt_tokens + completion_tokens
    else:
        prompt_tokens = _coerce_non_negative_int(usage.get("prompt_tokens"))
        completion_tokens = _coerce_non_negative_int(usage.get("completion_tokens"))
        total_tokens = _coerce_non_negative_int(usage.get("total_tokens"))
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens

    input_price, output_price, cache_hit_price = _resolve_pricing(model, prompt_tokens)
    non_cached_prompt = max(0, prompt_tokens - cache_hit_tokens)
    input_cost = (non_cached_prompt / 1_000_000 * input_price
                  + cache_hit_tokens / 1_000_000 * cache_hit_price)
    output_cost = completion_tokens / 1_000_000 * output_price
    total_cost = input_cost + output_cost
    return UsageEvent(
        user_id=user_id,
        conversation_id=conversation_id,
        virtual_model=route.virtual_model,
        provider=provider,
        upstream_model=route.upstream_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=total_cost,
        currency=model.currency,
        estimated=estimated,
        status="success",
    )


def build_error_usage_event(
    *,
    route: RouteConfig,
    model: ProviderModelConfig,
    provider: str,
    user_id: str | None,
    conversation_id: str | None,
    error_type: str,
) -> UsageEvent:
    return UsageEvent(
        user_id=user_id,
        conversation_id=conversation_id,
        virtual_model=route.virtual_model,
        provider=provider,
        upstream_model=route.upstream_model,
        currency=model.currency,
        estimated=False,
        status="error",
        error_type=error_type,
    )


def gateway_debug_payload(event: UsageEvent) -> dict:
    return {
        "virtual_model": event.virtual_model,
        "provider": event.provider,
        "upstream_model": event.upstream_model,
        "estimated": event.estimated,
        "cost": {
            "input": event.input_cost,
            "output": event.output_cost,
            "total": event.total_cost,
            "currency": event.currency,
        },
    }


def _resolve_pricing(model: ProviderModelConfig, prompt_tokens: int) -> tuple[float, float, float]:
    """Resolve flat or tiered pricing. Returns (input_price, output_price, cache_hit_price)."""
    if model.pricing_mode == "tiered" and model.pricing_tiers_json:
        try:
            tiers = json.loads(model.pricing_tiers_json)
            if isinstance(tiers, list):
                for tier in tiers:
                    if not isinstance(tier, dict):
                        continue
                    up_to = tier.get("up_to_tokens")
                    if up_to is None and prompt_tokens > 0:
                        continue
                    if up_to is not None and prompt_tokens > up_to:
                        continue
                    input_price = float(tier.get("input") or 0)
                    output_price = float(tier.get("output") or 0)
                    cache_hit_price = float(tier.get("cache_hit") or 0)
                    return (input_price, output_price, cache_hit_price)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return (
        model.input_price_per_million,
        model.output_price_per_million,
        model.cache_hit_price_per_million,
    )


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
    return max(1, total_chars // 4)


def _estimate_completion_tokens(response: dict) -> int:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    if not isinstance(content, str):
        content = ""
    return max(1, len(content) // 4)


def _coerce_non_negative_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)
