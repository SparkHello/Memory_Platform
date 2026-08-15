from __future__ import annotations

from copy import deepcopy

import pytest

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import GatewayConfig
from model_gateway.routing import (
    RequestRequirements,
    RouteCapabilityUnavailable,
    RouteForbidden,
    RouteUnavailable,
    Router,
    RuntimeHealthRegistry,
    retry_after_seconds,
    should_fail_over,
)

from conftest import config_payload


def test_preferred_deployment_is_first_even_beyond_normal_attempt_window(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    gateway_config.routes["memory.chat"].max_attempts = 1
    resolved = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
        preferred_deployment="chat-reseller",
    )
    assert [target.deployment_id for target in resolved.targets] == [
        "chat-reseller",
        "chat-official",
    ]


def test_required_deployment_disables_fallback(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    resolved = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
        required_deployment="chat-reseller",
    )
    assert [target.deployment_id for target in resolved.targets] == ["chat-reseller"]
    assert resolved.required_deployment == "chat-reseller"


def test_v2_default_fallback_scope_truncates_to_primary_target(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    # The shared fixture is explicit schema v2, so memory.chat keeps the v2
    # default fallback_scope="none": only the primary target is eligible and a
    # cooling primary does not escape to another channel.
    assert gateway_config.routes["memory.chat"].fallback_scope == "none"
    resolved = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    assert [target.deployment_id for target in resolved.targets] == ["chat-official"]

    clock = [10.0]
    health = RuntimeHealthRegistry(clock=lambda: clock[0])
    health.defer("official", 60)
    with pytest.raises(RouteUnavailable, match="限流冷却"):
        Router(runtime_health=health).resolve(
            requested_model="memory.chat",
            kind="chat",
            client=backend_client,
            config=gateway_config,
        )


def test_backend_cannot_use_interactive_only_connection() -> None:
    payload = config_payload()
    payload["connections"]["official"]["usage_scope"] = "interactive_only"
    payload["routes"]["memory.chat"]["targets"] = ["chat-official"]
    config = GatewayConfig.model_validate(payload)
    client = AuthenticatedClient(id="memory-gateway", config=config.clients["memory-gateway"])
    with pytest.raises(RouteForbidden, match="使用条款"):
        Router().resolve(
            requested_model="memory.chat", kind="chat", client=client, config=config
        )


def test_cooling_connection_is_skipped(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    clock = [10.0]
    health = RuntimeHealthRegistry(clock=lambda: clock[0])
    health.defer("official", 60)
    resolved = Router(runtime_health=health).resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    assert [target.connection_id for target in resolved.targets] == ["reseller"]


def test_max_attempts_counts_eligible_targets_not_cooling_targets(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    gateway_config.routes["memory.chat"].max_attempts = 1
    clock = [10.0]
    health = RuntimeHealthRegistry(clock=lambda: clock[0])
    health.defer("official", 60)
    resolved = Router(runtime_health=health).resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    assert [target.deployment_id for target in resolved.targets] == ["chat-reseller"]


def test_request_requirements_are_derived_from_openai_chat_shape() -> None:
    requirements = RequestRequirements.from_payload(
        {
            "stream": True,
            "tools": [{"type": "function"}],
            "parallel_tool_calls": True,
            "reasoning_effort": "high",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": "https://example"}},
                    ],
                }
            ],
            "response_format": {"type": "json_schema", "json_schema": {}},
        },
        kind="chat",
    )

    assert requirements.required_capabilities == (
        "streaming",
        "tools",
        "parallel_tools",
        "reasoning",
        "multimodal_input",
        "json_schema",
    )


def test_explicitly_disabled_reasoning_does_not_require_reasoning() -> None:
    requirements = RequestRequirements.from_payload(
        {
            "enable_thinking": False,
            "reasoning_effort": "max",
            "response_format": {"type": "json_object"},
        },
        kind="chat",
    )

    assert requirements.reasoning is False
    assert requirements.reasoning_state == "disabled"
    assert requirements.json_object is True


@pytest.mark.parametrize(
    ("policy", "tool_choice", "allowed"),
    [
        ("any", "required", True),
        ("any", {"type": "function", "function": {"name": "lookup"}}, True),
        ("auto_only", "auto", True),
        ("auto_only", "required", False),
        (
            "auto_only",
            {"type": "function", "function": {"name": "lookup"}},
            False,
        ),
        ("none", "auto", False),
    ],
)
def test_reasoning_tool_choice_policy_filters_before_routing(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
    policy: str,
    tool_choice: object,
    allowed: bool,
) -> None:
    for deployment in gateway_config.deployments.values():
        if deployment.kind == "chat":
            deployment.tool_choice_with_reasoning = policy
    requirements = RequestRequirements.from_payload(
        {
            "thinking": {"type": "enabled"},
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "tool_choice": tool_choice,
        },
        kind="chat",
    )

    if allowed:
        resolved = Router().resolve(
            requested_model="memory.chat",
            kind="chat",
            client=backend_client,
            config=gateway_config,
            requirements=requirements,
        )
        assert resolved.targets
    else:
        with pytest.raises(RouteCapabilityUnavailable) as raised:
            Router().resolve(
                requested_model="memory.chat",
                kind="chat",
                client=backend_client,
                config=gateway_config,
                requirements=requirements,
            )
        assert raised.value.capabilities == ("tool_choice_with_reasoning",)


def test_reasoning_disabled_allows_specific_tool_choice_under_safe_default(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    requirements = RequestRequirements.from_payload(
        {
            "enable_thinking": False,
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "tool_choice": {
                "type": "function",
                "function": {"name": "lookup"},
            },
        },
        kind="chat",
    )

    resolved = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
        requirements=requirements,
    )

    assert requirements.reasoning_state == "disabled"
    assert requirements.tool_choice == "specific"
    assert resolved.targets


def test_runtime_capabilities_filter_route_targets(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    gateway_config.deployments["chat-official"].capabilities.json_schema = False
    requirements = RequestRequirements.from_payload(
        {"response_format": {"type": "json_schema"}},
        kind="chat",
    )

    resolved = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
        requirements=requirements,
    )

    assert [target.deployment_id for target in resolved.targets] == ["chat-reseller"]


def test_runtime_capability_failure_is_distinct_from_transient_unavailability(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    for deployment in gateway_config.deployments.values():
        if deployment.kind == "chat":
            deployment.capabilities.json_schema = False

    with pytest.raises(RouteCapabilityUnavailable) as raised:
        Router().resolve(
            requested_model="memory.chat",
            kind="chat",
            client=backend_client,
            config=gateway_config,
            requirements=RequestRequirements(json_schema=True),
        )

    assert raised.value.status_code == 422
    assert raised.value.capabilities == ("json_schema",)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_explicit_transient_provider_failures_can_fallback(status: int) -> None:
    assert should_fail_over(status, b"error") is True


def test_content_or_policy_rejection_does_not_fallback() -> None:
    assert should_fail_over(301, b"") is False
    assert should_fail_over(401, b"") is False
    assert should_fail_over(402, b"") is False
    assert should_fail_over(404, b"") is False
    assert should_fail_over(403, b"content policy") is False
    assert should_fail_over(400, b"invalid messages") is False
    assert should_fail_over(400, b"model not found") is False
    assert (
        should_fail_over(400, b'{"error":{"code":"model_not_found"}}')
        is False
    )


def test_consecutive_server_failures_open_only_deployment_breaker(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    now = [100.0]
    health = RuntimeHealthRegistry(
        clock=lambda: now[0],
        server_failure_threshold=3,
        server_failure_cooldown_seconds=20,
    )
    router = Router(runtime_health=health)
    route = router.resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    first, second = route.targets

    health.record_http(first, status_code=500)
    health.record_http(first, status_code=502)
    assert health.remaining_target(first.connection_id, first.deployment_id) == 0
    health.record_http(first, status_code=503)
    assert health.remaining_target(first.connection_id, first.deployment_id) == 20
    assert health.remaining_target(second.connection_id, second.deployment_id) == 0

    now[0] += 21
    assert health.remaining_target(first.connection_id, first.deployment_id) == 0


def test_retry_after_must_be_finite_and_is_capped() -> None:
    assert retry_after_seconds("inf") == 0
    assert retry_after_seconds("nan") == 0
    assert retry_after_seconds("-10") == 0
    assert retry_after_seconds("999999999") == 86_400
    assert retry_after_seconds("120", cap_seconds=60) == 60
