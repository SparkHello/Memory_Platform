"""Single implementation of OpenAI-compatible provider quirks.

Before this module the same rules lived twice -- once in ``app/llm/client.py``
for memory/knowledge calls and once in ``app/openai_compat/gateway_client.py``
for the transparent ``/v1`` proxy -- and both decided what to send by sniffing
substrings out of ``base_url`` (``"moonshot" in provider_text``,
``"xiaomimimo"``, ``"bigmodel"``, ...).  That broke for private reverse proxies
whose hostname carries no vendor marker, and it meant a new provider needed a
code change.

Behaviour now comes from :class:`~app.llm.routing.ProviderQuirks`, declared next
to the provider definition.
"""

from __future__ import annotations
from typing import Any, Literal

from app.llm.routing import LLMProvider, ProviderQuirks


_DISABLED_EFFORTS = {"none", "disabled", "off"}

_FAILOVER_STATUS_CODES = {408, 429}


def thinking_payload(
    quirks: ProviderQuirks,
    *,
    thinking: Literal["enabled", "disabled"],
) -> dict[str, Any]:
    """The provider-native switch for reasoning, or ``{}`` if it has none."""
    style = quirks.thinking_style
    if style == "none":
        return {}
    if style == "native_effort":
        return {"reasoning_effort": quirks.reasoning_effort_max} if thinking == "enabled" else {}
    options: dict[str, str] = {"type": thinking}
    if style == "type_object_keep_all" and thinking == "enabled":
        options["keep"] = "all"
    return {"thinking": options}


def resolve_reasoning_effort(quirks: ProviderQuirks, effort: str) -> str:
    normalized = effort.strip().lower()
    if normalized in {"xhigh", "max"}:
        return quirks.reasoning_effort_max
    return "high"


def apply_reasoning_compatibility(
    payload: dict[str, Any],
    *,
    quirks: ProviderQuirks,
    default_thinking: bool = False,
) -> None:
    """Translate a client's generic reasoning fields into provider-native ones.

    An explicit provider-native ``thinking`` object stays authoritative; only
    the generic ``reasoning_effort`` is rewritten or dropped.
    """
    if quirks.strips_reasoning_fields:
        for field in ("reasoning_effort", "thinking", "enable_thinking", "thinking_mode"):
            payload.pop(field, None)
        return

    native_enabled = thinking_payload(quirks, thinking="enabled")

    if (
        default_thinking
        and "thinking" not in payload
        and "reasoning_effort" not in payload
    ):
        # Callers that send no reasoning field at all (an AUTO level over a
        # custom model id) get the provider's own switch resolved after routing.
        payload.update(native_enabled)

    if (
        "thinking" in payload
        and "reasoning_effort" in payload
        and "thinking" in native_enabled
    ):
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        thinking_options = payload.get("thinking")
        thinking_type = (
            str(thinking_options.get("type") or "").lower()
            if isinstance(thinking_options, dict)
            else ""
        )
        if (
            quirks.keeps_reasoning_effort
            and thinking_type != "disabled"
            and effort not in _DISABLED_EFFORTS
        ):
            payload["reasoning_effort"] = resolve_reasoning_effort(quirks, effort)
        else:
            payload.pop("reasoning_effort", None)

    if "thinking" not in payload and "reasoning_effort" in payload:
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        thinking_mode: Literal["enabled", "disabled"] = (
            "disabled" if effort in _DISABLED_EFFORTS else "enabled"
        )
        translated = thinking_payload(quirks, thinking=thinking_mode)
        if "thinking" in translated:
            payload.update(translated)
            if quirks.keeps_reasoning_effort and thinking_mode == "enabled":
                payload["reasoning_effort"] = resolve_reasoning_effort(quirks, effort)
            else:
                payload.pop("reasoning_effort", None)

    if quirks.tool_choice_with_thinking != "any" and payload.get("tools"):
        thinking_options = payload.get("thinking")
        thinking_enabled = (
            isinstance(thinking_options, dict)
            and str(thinking_options.get("type") or "").lower() == "enabled"
        )
        if thinking_enabled and not _tool_choice_survives(payload, quirks):
            # DeepSeek rejects the field outright while reasoning; Kimi rejects
            # only a named function and still accepts "auto".
            payload.pop("tool_choice", None)


def _tool_choice_survives(payload: dict[str, Any], quirks: ProviderQuirks) -> bool:
    if quirks.tool_choice_with_thinking == "none":
        return False
    return payload.get("tool_choice") == "auto"


def auto_tool_choice_allowed(quirks: ProviderQuirks) -> bool:
    """Whether ``tool_choice: "auto"`` may be sent alongside reasoning."""
    return quirks.tool_choice_with_thinking != "none"


def apply_transport_quirks(
    payload: dict[str, Any],
    *,
    provider: LLMProvider,
    stream: bool,
) -> None:
    """Apply the non-reasoning per-provider adjustments in place."""
    if provider.quirks.forces_temperature_one:
        payload["temperature"] = 1.0
    if provider.quirks.rejects_stream_options:
        payload.pop("stream_options", None)


def uses_tool_for_structured_output(provider: LLMProvider) -> bool:
    """Whether structured output must go through a forced tool call.

    Measured 2026-08-03: MiMo ultraspeed does not reject ``response_format`` --
    it silently returns malformed text -- so it must never receive one.
    """
    return provider.quirks.structured_output == "tool_call_only"


def should_fail_over(status_code: int, content: bytes | str) -> bool:
    del content
    return status_code in _FAILOVER_STATUS_CODES or status_code >= 500
