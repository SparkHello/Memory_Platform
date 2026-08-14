from collections.abc import Mapping
from typing import Any

from app.memory.models import MemorySensitivity
from app.sensitivity import (
    SENSITIVITY_RANK as _SENSITIVITY_RANK,
    detected_sensitive_categories,
    detect_text_sensitivity,
)

SENSITIVE_LEVELS = {"private", "sensitive"}

REDACTED_CONTENT_TEXT = "内容已遮罩。请在详情页显式查看完整内容。"
REDACTED_SOURCE_TEXT = "来源原文已遮罩。请在详情页显式查看完整内容。"


def sensitivity_floor(
    declared: MemorySensitivity,
    *texts: str | None,
) -> MemorySensitivity:
    """Raise a declared sensitivity to the deterministic local floor."""
    detected = detect_text_sensitivity("\n".join(text for text in texts if text))
    return max((declared, detected), key=_SENSITIVITY_RANK.__getitem__)


def redact_memory_payload(
    payload: Mapping[str, Any],
    *,
    redact_sensitive: bool,
    sensitivity: str | None = None,
) -> dict[str, Any]:
    data = dict(payload)
    if not redact_sensitive:
        return data

    declared_reason = sensitivity or data.get("sensitivity")
    declared_level: MemorySensitivity = (
        declared_reason if declared_reason in _SENSITIVITY_RANK else "normal"
    )
    local_text = "\n".join(
        str(data.get(field_name) or "")
        for field_name in ("content", "source_message", "source_excerpt")
    )
    reason = sensitivity_floor(declared_level, local_text)
    if reason not in SENSITIVE_LEVELS:
        return data

    redacted_fields: list[str] = []
    if "content" in data:
        data["content"] = REDACTED_CONTENT_TEXT
        redacted_fields.append("content")
    if data.get("source_message"):
        data["source_message"] = REDACTED_SOURCE_TEXT
        redacted_fields.append("source_message")
    if data.get("source_excerpt"):
        data["source_excerpt"] = REDACTED_SOURCE_TEXT
        redacted_fields.append("source_excerpt")
    if data.get("label"):
        data["label"] = "敏感记忆" if reason == "sensitive" else "私密记忆"
        redacted_fields.append("label")
    if data.get("entities"):
        data["entities"] = []
        redacted_fields.append("entities")

    data["redacted"] = True
    data["redaction_reason"] = reason
    data["redacted_fields"] = redacted_fields
    return data
