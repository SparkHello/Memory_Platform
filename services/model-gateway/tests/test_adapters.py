from __future__ import annotations

import pytest

from model_gateway.adapters import apply_connection_adapter
from model_gateway.models import ConnectionConfig, DeploymentConfig
from model_gateway.proxy import prepare_payload
from model_gateway.routing import Router


def connection(adapter: str) -> ConnectionConfig:
    return ConnectionConfig.model_validate(
        {
            "channel_operator": "vendor",
            "adapter": adapter,
            "base_url": "https://vendor.example/v1",
            "auth": {"secret_ref": "UPSTREAM"},
        }
    )


def deployment(
    model: str,
    *,
    reasoning_default: str = "enabled",
    adapter_profile: str = "inherit",
) -> DeploymentConfig:
    return DeploymentConfig.model_validate(
        {
            "connection": "vendor",
            "upstream_model": model,
            "model_author": "author",
            "adapter_profile": adapter_profile,
            "reasoning_default": reasoning_default,
        }
    )


def test_kimi_k27_adapter_sets_keep_and_temperature() -> None:
    payload = {"messages": [], "reasoning_effort": "high"}
    apply_connection_adapter(
        payload,
        connection=connection("kimi"),
        deployment=deployment("kimi-k2.7-code"),
    )
    assert payload["temperature"] == 1.0
    assert payload["thinking"] == {"type": "enabled", "keep": "all"}
    assert "reasoning_effort" not in payload


def test_kimi_k3_keeps_native_reasoning_effort() -> None:
    payload = {"messages": []}
    apply_connection_adapter(
        payload,
        connection=connection("kimi"),
        deployment=deployment("kimi-k3-thinking"),
    )
    assert payload["reasoning_effort"] == "max"
    assert "thinking" not in payload


@pytest.mark.parametrize("model", ["k3", "k3-256k"])
def test_kimi_code_k3_ids_use_native_effort_with_code_default(model: str) -> None:
    payload = {"messages": [], "thinking": {"type": "enabled"}}
    apply_connection_adapter(
        payload,
        connection=connection("kimi"),
        deployment=deployment(model),
    )

    assert payload["reasoning_effort"] == "high"
    assert "thinking" not in payload


def test_kimi_for_coding_uses_k27_thinking_shape() -> None:
    payload = {
        "messages": [],
        "reasoning_effort": "high",
        "tool_choice": "auto",
    }
    apply_connection_adapter(
        payload,
        connection=connection("kimi"),
        deployment=deployment("kimi-for-coding"),
    )

    assert payload["temperature"] == 1.0
    assert payload["thinking"] == {"type": "enabled", "keep": "all"}
    assert "reasoning_effort" not in payload
    assert payload["tool_choice"] == "auto"


def test_deepseek_adapter_maps_effort_without_rewriting_tool_choice() -> None:
    payload = {
        "messages": [],
        "reasoning_effort": "max",
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
    }
    apply_connection_adapter(
        payload,
        connection=connection("deepseek"),
        deployment=deployment("deepseek-reasoner"),
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert payload["tool_choice"] == "auto"


@pytest.mark.parametrize(
    "model",
    ["deepseek-v4-pro", "deepseek-v4-flash-0731"],
)
def test_dashscope_deepseek_v4_profile_uses_enable_thinking_and_keeps_tools(
    model: str,
) -> None:
    payload = {
        "messages": [],
        "thinking": {"type": "enabled"},
        "reasoning_effort": "xhigh",
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
    }
    apply_connection_adapter(
        payload,
        connection=connection("generic"),
        deployment=deployment(model, adapter_profile="dashscope_deepseek_v4"),
    )

    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == "xhigh"
    assert payload["tool_choice"] == "auto"
    assert "thinking" not in payload


def test_dashscope_deepseek_v4_profile_can_disable_thinking() -> None:
    payload = {
        "messages": [],
        "thinking": {"type": "disabled"},
        "reasoning_effort": "max",
    }
    apply_connection_adapter(
        payload,
        connection=connection("deepseek"),
        deployment=deployment(
            "deepseek-v4-flash",
            adapter_profile="dashscope_deepseek_v4",
        ),
    )

    assert payload["enable_thinking"] is False
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("high", True), ("none", False)],
)
def test_dashscope_openai_adapter_maps_qwen_reasoning_to_enable_thinking(
    effort: str,
    expected: bool,
) -> None:
    payload = {
        "messages": [],
        "reasoning_effort": effort,
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
    }

    apply_connection_adapter(
        payload,
        connection=connection("dashscope_openai"),
        deployment=deployment("qwen3.6-flash-2026-04-16"),
    )

    assert payload["enable_thinking"] is expected
    assert payload["tool_choice"] == "auto"
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload


def test_mimo_adapter_adds_empty_reasoning_field_to_tool_history() -> None:
    payload = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ]
    }
    apply_connection_adapter(
        payload,
        connection=connection("mimo"),
        deployment=deployment("mimo-v2.5-pro-ultraspeed"),
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["messages"][0]["reasoning_content"] == ""


def test_fallback_strips_reasoning_from_a_different_origin(
    gateway_config, backend_client
) -> None:
    router = Router()
    target = router.resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
        preferred_deployment="chat-reseller",
    ).targets[0]
    payload = {
        "model": "memory.chat",
        "messages": [
            {
                "role": "assistant",
                "reasoning_content": "official-private-state",
                "content": "visible",
            }
        ],
    }
    forwarded = prepare_payload(
        payload,
        target,
        reasoning_origin_deployment="chat-official",
    )
    assert forwarded["messages"][0] == {"role": "assistant", "content": "visible"}
    assert payload["messages"][0]["reasoning_content"] == "official-private-state"
