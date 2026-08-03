from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
import threading
import time
from typing import Any, Literal, Mapping


ProviderCode = Literal["M", "K", "D"]

ThinkingStyle = Literal[
    "none",
    "type_object",
    "type_object_keep_all",
    "native_effort",
]
StructuredOutput = Literal["json_schema", "json_object", "tool_call_only"]
# How much of `tool_choice` a provider tolerates while reasoning is enabled.
#   any       -- no restriction
#   auto_only -- rejects a named function, accepts "auto" (Kimi)
#   none      -- rejects the field entirely (DeepSeek)
ToolChoicePolicy = Literal["any", "auto_only", "none"]


@dataclass(frozen=True, slots=True)
class ProviderQuirks:
    """Declarative provider behaviour, replacing base_url substring sniffing.

    Every field here used to be a hard-coded `"moonshot" in base_url` style
    test spread across the chat and gateway clients.  Declaring them keeps the
    knowledge with the provider definition, where it can be corrected without a
    code change.
    """

    thinking_style: ThinkingStyle = "none"
    reasoning_effort_max: str = "high"
    # DeepSeek accepts `reasoning_effort` alongside `thinking`; most providers
    # reject the pair and need the generic field dropped after translation.
    keeps_reasoning_effort: bool = False
    # Providers with no reasoning support at all (e.g. Mistral) must not even
    # receive the generic fields; leaving them in place is an upstream 400.
    strips_reasoning_fields: bool = False
    tool_choice_with_thinking: ToolChoicePolicy = "any"
    structured_output: StructuredOutput = "json_object"
    forces_temperature_one: bool = False
    rejects_stream_options: bool = False
    requires_reasoning_replay: bool = False

    def merged(self, overrides: dict[str, Any]) -> ProviderQuirks:
        if not overrides:
            return self
        known = {f for f in ProviderQuirks.__slots__}
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"未知的 quirks 字段：{', '.join(sorted(unknown))}")
        return ProviderQuirks(
            **{**{name: getattr(self, name) for name in known}, **overrides}
        )


@dataclass(frozen=True, slots=True)
class LLMProvider:
    """A resolved `provider_id/model_id` route target ready to be called."""

    code: str
    base_url: str
    api_key: str
    model: str
    quirks: ProviderQuirks = field(default_factory=ProviderQuirks)

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())

    @property
    def cooldown_key(self) -> str:
        key_fingerprint = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:16]
        return f"{self.code}:{self.base_url.rstrip('/')}:{key_fingerprint}"


class ProviderCooldowns:
    """Process-local 429 cooldown registry shared by all routed LLM clients."""

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._deadlines: dict[str, float] = {}
        self._lock = threading.Lock()

    def remaining(self, provider: LLMProvider) -> float:
        now = self._clock()
        with self._lock:
            deadline = self._deadlines.get(provider.cooldown_key, 0.0)
            if deadline <= now:
                self._deadlines.pop(provider.cooldown_key, None)
                return 0.0
            return deadline - now

    def defer(self, provider: LLMProvider, seconds: float) -> None:
        deadline = self._clock() + max(0.0, seconds)
        with self._lock:
            self._deadlines[provider.cooldown_key] = max(
                deadline,
                self._deadlines.get(provider.cooldown_key, 0.0),
            )


class ProviderCoolingDown(RuntimeError):
    pass


GLOBAL_PROVIDER_COOLDOWNS = ProviderCooldowns()


def normalize_provider_priority(value: str) -> str:
    normalized = "".join(value.upper().split())
    if not normalized:
        return "D"
    invalid = set(normalized) - {"M", "K", "D"}
    if invalid:
        raise ValueError("仅允许使用 M、K、D")
    if len(set(normalized)) != len(normalized):
        raise ValueError("不能重复包含同一模型代号")
    return normalized


def effective_provider_priority(value: str) -> list[ProviderCode]:
    priority = list(normalize_provider_priority(value))
    if priority == ["M"]:
        priority.append("D")
    return priority  # type: ignore[return-value]


def ordered_configured_providers(
    priority: str,
    providers: Mapping[ProviderCode, LLMProvider],
) -> list[LLMProvider]:
    return [
        providers[code]
        for code in effective_provider_priority(priority)
        if providers[code].configured
    ]


def retry_after_seconds(value: str, *, wall_time: float) -> float:
    value = value.strip()
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        return max(0.0, retry_at.timestamp() - wall_time)
    except (TypeError, ValueError, OverflowError):
        return 0.0
