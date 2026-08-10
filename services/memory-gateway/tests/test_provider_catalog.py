import json

import pytest

from app.api.deps import direct_embedding_space_id
from app.config import Settings
from app.llm.protocol import (
    apply_reasoning_compatibility,
    auto_tool_choice_allowed,
    should_fail_over,
    thinking_payload,
    uses_tool_for_structured_output,
)
from app.llm.routing import LLMProvider, ProviderQuirks
from app.providers.catalog import (
    ProviderConfigError,
    load_providers,
    providers_for_route,
    split_target,
    validate_providers_and_routes,
)


# 现有 867 条知识向量与 46 条记忆向量都存放在这个空间里。它由
# EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIMENSIONS 推导，任何一项变动
# 都会让全部存量向量失效并需要重新嵌入，因此固定为断言。
STORED_EMBEDDING_SPACE = (
    "direct-openai-compatible-v1:"
    "c74ae26d2588a0a34071ab28445577c4007529561907a3e976b902fe8aedf5e9"
)


def test_stored_embedding_space_is_not_invalidated() -> None:
    settings = Settings(
        _env_file=None,
        EMBEDDING_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1",
        EMBEDDING_MODEL="qwen3.7-text-embedding",
        EMBEDDING_DIMENSIONS=1024,
    )

    assert direct_embedding_space_id(settings) == STORED_EMBEDDING_SPACE


def test_presets_load_with_stable_key_env_names() -> None:
    providers = load_providers()

    assert set(providers) == {"kimi", "deepseek", "mimo", "dashscope", "zhipu"}
    assert providers["kimi"].api_key_env == "PROVIDER_KIMI_API_KEY"
    assert providers["deepseek"].api_host == "https://api.deepseek.com"


def test_model_quirks_layer_over_provider_defaults() -> None:
    providers = load_providers()

    code = providers["kimi"].models["kimi-k2.7-code"].quirks
    # 供应商级默认值被继承……
    assert code.structured_output == "json_schema"
    assert code.requires_reasoning_replay is True
    # ……模型级覆盖生效。
    assert code.thinking_style == "type_object_keep_all"
    assert code.forces_temperature_one is True


def test_measured_structured_output_support_is_declared() -> None:
    providers = load_providers()

    # 2026-08-03 实测：MiMo 给 response_format 会静默输出乱码，只能走工具调用。
    mimo = providers["mimo"].models["mimo-v2.5-pro-ultraspeed"]
    assert mimo.quirks.structured_output == "tool_call_only"
    # DeepSeek 明确拒绝 json_schema，只支持 json_object。
    flash = providers["deepseek"].models["deepseek-v4-flash"]
    assert flash.quirks.structured_output == "json_object"


def test_split_target_rejects_malformed_route_targets() -> None:
    assert split_target("kimi/kimi-k2.7-code") == ("kimi", "kimi-k2.7-code")
    with pytest.raises(ProviderConfigError):
        split_target("kimi")


def test_routes_reject_unknown_provider_and_embedding_targets(tmp_path) -> None:
    routes = tmp_path / "routes.json"
    routes.write_text(
        json.dumps({"version": 1, "routes": {"chat": ["nope/whatever"]}}),
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError, match="不存在的供应商"):
        validate_providers_and_routes(routes_path=routes)

    routes.write_text(
        json.dumps({"version": 1, "routes": {"chat": ["zhipu/embedding-3"]}}),
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError, match="只能引用 chat 模型"):
        validate_providers_and_routes(routes_path=routes)


def test_route_resolution_skips_providers_without_a_key(tmp_path) -> None:
    routes = tmp_path / "routes.json"
    routes.write_text(
        json.dumps(
            {
                "version": 1,
                "routes": {"chat": ["kimi/kimi-k2.7-code", "deepseek/deepseek-v4-flash"]},
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, ROUTES_PATH=str(routes))

    resolved = providers_for_route(
        settings,
        "chat",
        secrets={"PROVIDER_DEEPSEEK_API_KEY": "secret"},
    )

    assert [provider.code for provider in resolved] == ["deepseek"]
    assert resolved[0].model == "deepseek-v4-flash"
    assert resolved[0].quirks.keeps_reasoning_effort is True


def test_thinking_payload_covers_every_declared_style() -> None:
    keep_all = ProviderQuirks(thinking_style="type_object_keep_all")
    assert thinking_payload(keep_all, thinking="enabled") == {
        "thinking": {"type": "enabled", "keep": "all"}
    }
    assert thinking_payload(keep_all, thinking="disabled") == {
        "thinking": {"type": "disabled"}
    }

    native = ProviderQuirks(thinking_style="native_effort", reasoning_effort_max="max")
    assert thinking_payload(native, thinking="enabled") == {"reasoning_effort": "max"}
    assert thinking_payload(native, thinking="disabled") == {}

    assert thinking_payload(ProviderQuirks(), thinking="enabled") == {}


def test_generic_effort_is_translated_and_tool_choice_dropped() -> None:
    quirks = ProviderQuirks(
        thinking_style="type_object",
        reasoning_effort_max="max",
        keeps_reasoning_effort=True,
        tool_choice_with_thinking="none",
    )
    payload = {
        "reasoning_effort": "xhigh",
        "tools": [{"type": "function"}],
        "tool_choice": {"type": "function"},
    }

    apply_reasoning_compatibility(payload, quirks=quirks)

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert "tool_choice" not in payload


def test_auto_only_provider_keeps_auto_but_drops_a_named_function() -> None:
    # Kimi rejects `tool_choice: {"type": "function", ...}` while reasoning is on
    # but accepts "auto"; collapsing both into one boolean would silently strip
    # the "auto" it relies on today.
    quirks = ProviderQuirks(
        thinking_style="type_object_keep_all",
        tool_choice_with_thinking="auto_only",
    )

    kept = {
        "thinking": {"type": "enabled"},
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
    }
    apply_reasoning_compatibility(kept, quirks=quirks)
    assert kept["tool_choice"] == "auto"

    dropped = {
        "thinking": {"type": "enabled"},
        "tools": [{"type": "function"}],
        "tool_choice": {"type": "function", "function": {"name": "x"}},
    }
    apply_reasoning_compatibility(dropped, quirks=quirks)
    assert "tool_choice" not in dropped

    assert auto_tool_choice_allowed(quirks) is True
    assert auto_tool_choice_allowed(ProviderQuirks(tool_choice_with_thinking="none")) is False


def test_provider_without_native_effort_drops_the_generic_field() -> None:
    quirks = ProviderQuirks(thinking_style="type_object")
    payload = {"reasoning_effort": "high"}

    apply_reasoning_compatibility(payload, quirks=quirks)

    assert payload["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in payload


def test_structured_output_dispatch_and_failover_rules() -> None:
    tool_only = LLMProvider(
        code="mimo",
        base_url="https://example.invalid/v1",
        api_key="k",
        model="mimo-v2.5-pro-ultraspeed",
        quirks=ProviderQuirks(structured_output="tool_call_only"),
    )
    assert uses_tool_for_structured_output(tool_only) is True

    assert should_fail_over(429, b"") is True
    assert should_fail_over(503, b"") is True
    assert not should_fail_over(
        400,
        b'{"error":{"code":"model_not_found","message":"invalid model"}}',
    )
    assert should_fail_over(400, b'{"error":"invalid model"}') is False
    assert should_fail_over(
        400,
        b'{"error":{"message":"the prompt says model not found"}}',
    ) is False
    assert should_fail_over(400, b'{"error":"context too long"}') is False
    assert should_fail_over(403, b"") is False
