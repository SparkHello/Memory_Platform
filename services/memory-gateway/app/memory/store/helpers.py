"""Pure helpers shared by MemoryStore methods."""
from __future__ import annotations

from datetime import datetime
import json
import math
import sqlite3
from typing import Any

from app.memory.models import MemorySensitivity, MemoryStability, MemoryType
from app.memory.redaction import detect_text_sensitivity
from app.memory.store.constants import _SENSITIVITY_RANK
from app.memory.utils import _parse_iso_datetime


def _json_string_list(raw_value: str | None) -> list[str]:
    try:
        values = json.loads(raw_value) if raw_value else []
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if value]


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_like_safe(value: str) -> bool:
    # decision log 的 candidate_json 用 ensure_ascii=False 存储，片段中只有
    # 引号、反斜杠和控制字符会被 JSON 转义，导致 LIKE 无法匹配原文。
    return '"' not in value and "\\" not in value and all(
        ord(char) >= 0x20 for char in value
    )


def _time_ripple_anchor(row: sqlite3.Row):
    return _parse_iso_datetime(row["valid_from"] or row["created_at"])


def _time_ripple_profiles(rows: list[sqlite3.Row]) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for row in rows:
        anchor = _time_ripple_anchor(row)
        if anchor is None:
            continue
        profiles[str(row["id"])] = {
            "anchor": anchor,
            "topics": _casefold_set(_json_string_list(row["topics_json"])),
            "spaces": set(),
        }
    return profiles


def _casefold_set(values: list[str]) -> set[str]:
    return {value.casefold() for value in values if value}


def _coerce_string_list(raw_value: object) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    return [str(value) for value in raw_value if value]


def _coerce_int(raw_value: object, *, default: int) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _coerce_float(raw_value: object, *, default: float) -> float:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return default


def _coerce_float_or_none(raw_value: object) -> float | None:
    if raw_value in (None, ""):
        return None
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _bounded_float(raw_value: object, *, default: float) -> float:
    value = _coerce_float(raw_value, default=default)
    return max(0.0, min(1.0, value))


def _average_float(values: list[float], *, default: float) -> float:
    if not values:
        return default
    return round(sum(values) / len(values), 3)


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _core_section_audit_summaries(sections: list[dict]) -> list[dict]:
    summaries: list[dict] = []
    for section in sections:
        section_name = section.get("section")
        if not section_name:
            continue
        summary = {"section": str(section_name)}
        section_id = section.get("id")
        if section_id:
            summary["id"] = str(section_id)
        version = section.get("version")
        if version is not None:
            summary["version"] = version
        summaries.append(summary)
    return summaries


def _merge_core_section_audit_summaries(*section_groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for sections in section_groups:
        for summary in _core_section_audit_summaries(sections):
            identity = (
                str(summary.get("id") or ""),
                str(summary.get("section") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(summary)
    return merged


def _join_memory_contents(memories: list[MemoryRecord]) -> str:
    parts = []
    for memory in memories:
        normalized = memory.content.strip().rstrip("。.!?！？")
        if normalized:
            parts.append(normalized)
    if not parts:
        return ""
    return "；".join(_ordered_unique(parts)) + "。"


def _merged_type(memories: list[MemoryRecord]) -> MemoryType:
    types = {memory.type for memory in memories}
    return memories[0].type if len(types) == 1 else "semantic"


def _merged_stability(memories: list[MemoryRecord]) -> MemoryStability:
    values = {memory.stability for memory in memories}
    for stability in ("stable", "medium", "temporary"):
        if stability in values:
            return stability
    return "stable"


def _merged_sensitivity(memories: list[MemoryRecord]) -> MemorySensitivity:
    values = {memory.sensitivity for memory in memories}
    for sensitivity in ("sensitive", "private", "normal"):
        if sensitivity in values:
            return sensitivity
    return "normal"


def _shared_value(values: list[str | None]) -> str | None:
    non_empty = {value for value in values if value}
    return next(iter(non_empty)) if len(non_empty) == 1 else None


def _earliest_datetime_text(values: list[str]) -> str | None:
    if not values:
        return None
    return min(values)

def _sensitivity_with_floor(
    *,
    declared: MemorySensitivity,
    content: str,
    source_message: str | None = None,
    entities: list[str] | None = None,
) -> MemorySensitivity:
    detected = detect_text_sensitivity(
        "\n".join(
            part
            for part in (content, source_message or "", *(entities or []))
            if part
        )
    )
    return max((declared, detected), key=_SENSITIVITY_RANK.__getitem__)

