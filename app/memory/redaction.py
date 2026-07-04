from collections.abc import Mapping
from typing import Any


SENSITIVE_LEVELS = {"private", "sensitive"}

REDACTED_CONTENT_TEXT = "内容已遮罩。请在详情页显式查看完整内容。"
REDACTED_SOURCE_TEXT = "来源原文已遮罩。请在详情页显式查看完整内容。"


def redact_memory_payload(
    payload: Mapping[str, Any],
    *,
    redact_sensitive: bool,
    sensitivity: str | None = None,
) -> dict[str, Any]:
    data = dict(payload)
    if not redact_sensitive:
        return data

    reason = sensitivity or data.get("sensitivity")
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

    data["redacted"] = True
    data["redaction_reason"] = reason
    data["redacted_fields"] = redacted_fields
    return data
