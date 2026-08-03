from __future__ import annotations

from typing import Any

from model_gateway.models import ConnectionConfig, DeploymentConfig


_DISABLED_EFFORTS = {"none", "disabled", "off"}


def apply_connection_adapter(
    payload: dict[str, Any],
    *,
    connection: ConnectionConfig,
    deployment: DeploymentConfig,
) -> None:
    """Apply the selected provider's documented OpenAI-compat differences.

    The generic adapter never guesses. Named adapters only translate reasoning
    controls and the small compatibility rules declared by their provider.
    A deployment's declarative request_transform still runs afterwards and is
    therefore authoritative for a particular account or model version.
    """

    if deployment.kind != "chat" or connection.adapter == "generic":
        return
    adapter = connection.adapter
    model = deployment.upstream_model.lower().rsplit("/", 1)[-1]
    explicit_thinking = payload.get("thinking")
    effort_value = payload.get("reasoning_effort")
    effort = str(effort_value or "").strip().lower()

    desired: str | None = None
    if isinstance(explicit_thinking, dict):
        thinking_type = str(explicit_thinking.get("type") or "").strip().lower()
        if thinking_type in {"enabled", "disabled"}:
            desired = thinking_type
    elif effort_value is not None:
        desired = "disabled" if effort in _DISABLED_EFFORTS else "enabled"
    elif deployment.reasoning_default != "inherit":
        desired = deployment.reasoning_default

    if adapter == "kimi":
        _apply_kimi(payload, model=model, desired=desired)
    elif adapter == "deepseek":
        _apply_deepseek(payload, effort=effort, desired=desired)
    elif adapter == "mimo":
        _apply_mimo(payload, desired=desired)

    _ensure_tool_reasoning_fields(payload)


def strip_reasoning_from_assistant_messages(payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        message.pop("reasoning_content", None)
        message.pop("reasoning", None)


def _apply_kimi(payload: dict[str, Any], *, model: str, desired: str | None) -> None:
    if model.startswith("kimi-k2.7") or model.startswith("kimi-for-coding"):
        payload["temperature"] = 1.0
    if desired is None:
        return
    if model.startswith("kimi-k3"):
        payload.pop("thinking", None)
        if desired == "enabled":
            payload.setdefault("reasoning_effort", "max")
        elif payload.get("reasoning_effort") is not None:
            payload.pop("reasoning_effort", None)
        return
    options: dict[str, str] = {"type": desired}
    if desired == "enabled" and model.startswith("kimi-k2.7"):
        options["keep"] = "all"
    payload["thinking"] = options
    payload.pop("reasoning_effort", None)


def _apply_deepseek(
    payload: dict[str, Any], *, effort: str, desired: str | None
) -> None:
    if desired is None:
        return
    payload["thinking"] = {"type": desired}
    if desired == "enabled" and effort:
        payload["reasoning_effort"] = "max" if effort in {"xhigh", "max"} else "high"
    elif desired == "disabled":
        payload.pop("reasoning_effort", None)
    if desired == "enabled" and payload.get("tools"):
        payload.pop("tool_choice", None)


def _apply_mimo(payload: dict[str, Any], *, desired: str | None) -> None:
    if desired is None:
        return
    payload["thinking"] = {"type": desired}
    payload.pop("reasoning_effort", None)


def _ensure_tool_reasoning_fields(payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and (message.get("tool_calls") or message.get("function_call"))
            and "reasoning_content" not in message
        ):
            reasoning = message.get("reasoning")
            message["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
