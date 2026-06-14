from app.providers.models import RouteConfig, UsageEvent


def build_success_usage_event(
    *,
    response: dict,
    messages: list[dict],
    route: RouteConfig,
    provider: str,
    user_id: str | None,
    conversation_id: str | None,
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

    input_cost = prompt_tokens / 1_000_000 * route.input_price_per_million
    output_cost = completion_tokens / 1_000_000 * route.output_price_per_million
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
        currency=route.currency,
        estimated=estimated,
        status="success",
    )


def build_error_usage_event(
    *,
    route: RouteConfig,
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
        currency=route.currency,
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
