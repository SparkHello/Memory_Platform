from __future__ import annotations

from copy import deepcopy

import pytest

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import GatewayConfig
from model_gateway.routing import (
    CooldownRegistry,
    RouteForbidden,
    Router,
    should_fail_over,
)

from conftest import config_payload


def test_preferred_deployment_is_first_even_beyond_normal_attempt_window(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
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
    clock = [10.0]
    cooldowns = CooldownRegistry(clock=lambda: clock[0])
    cooldowns.defer("official", 60)
    resolved = Router(cooldowns).resolve(
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
    gateway_config.routes["memory.chat"].max_attempts = 1
    clock = [10.0]
    cooldowns = CooldownRegistry(clock=lambda: clock[0])
    cooldowns.defer("official", 60)
    resolved = Router(cooldowns).resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    assert [target.deployment_id for target in resolved.targets] == ["chat-reseller"]


@pytest.mark.parametrize("status", [401, 402, 404, 408, 429, 500, 503])
def test_provider_level_failures_can_fallback(status: int) -> None:
    assert should_fail_over(status, b"error") is True


def test_content_or_policy_rejection_does_not_fallback() -> None:
    assert should_fail_over(403, b"content policy") is False
    assert should_fail_over(400, b"invalid messages") is False
    assert should_fail_over(400, b"model not found") is True
