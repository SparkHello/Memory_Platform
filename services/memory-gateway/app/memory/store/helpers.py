"""Pure helpers shared by MemoryStore methods."""
from __future__ import annotations

from datetime import datetime
import json
import math
import sqlite3
from typing import Any, Protocol

from app.memory.models import (
    ConversationBranchNode,
    CoreMemorySection,
    CoreMemorySectionHistory,
    MemoryRecord,
    MemorySensitivity,
    MemorySpace,
    MemoryStability,
    MemoryType,
    RecentContextSummary,
    normalize_iso_text,
    normalize_memory_type,
    normalize_optional_text,
)
from app.memory.redaction import detect_text_sensitivity
from app.memory.store.constants import _SENSITIVITY_RANK
from app.memory.utils import _parse_iso_datetime


class _ConnectableStore(Protocol):
    """store 领域子模块对 MemoryStore 的最小能力要求。

    打破子模块与 MemoryStore 之间的类型层环形依赖：运行时仍传入
    MemoryStore 实例，类型上只要求能打开 SQLite 连接（外加个别公共读方法）。
    """

    def _connect(self) -> sqlite3.Connection: ...

    def get_memory(self, *, memory_id: str, user_id: str) -> MemoryRecord | None: ...


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


def _insert_memory_row(
    *,
    connection: sqlite3.Connection,
    memory: MemoryRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO memories (
            id, user_id, content, type, importance, confidence,
            valence, arousal,
            source_message, source_conversation_id, origin, embedding_json,
            embedding_space_id,
            last_used_at, usage_count, stability, valid_from, valid_until, review_after,
            sensitivity, evidence_memory_ids_json, topics_json, entities_json,
            temporal_subject, temporal_predicate,
            status, digested, decay_lambda, supersedes, superseded_by,
            created_at, updated_at, archived_at, archived, revision
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory.id,
            memory.user_id,
            memory.content,
            memory.type,
            memory.importance,
            memory.confidence,
            memory.valence,
            memory.arousal,
            memory.source_message,
            memory.source_conversation_id,
            memory.origin,
            memory.embedding_json,
            memory.embedding_space_id,
            memory.last_used_at,
            memory.usage_count,
            memory.stability,
            memory.valid_from,
            memory.valid_until,
            memory.review_after,
            memory.sensitivity,
            json.dumps(memory.evidence_memory_ids, ensure_ascii=False),
            json.dumps(memory.topics, ensure_ascii=False),
            json.dumps(memory.entities, ensure_ascii=False),
            memory.temporal_subject,
            memory.temporal_predicate,
            memory.status,
            int(memory.digested),
            memory.decay_lambda,
            memory.supersedes,
            memory.superseded_by,
            memory.created_at,
            memory.updated_at,
            memory.archived_at,
            memory.archived,
            memory.revision,
        ),
    )


def _space_ids_for_memory_ids_on_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    memory_ids: list[str],
) -> dict[str, list[str]]:
    unique_ids = _ordered_unique(memory_ids)
    if not unique_ids:
        return {}
    result = {memory_id: [] for memory_id in unique_ids}
    for offset in range(0, len(unique_ids), 500):
        batch = unique_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT memory_id, space_id
            FROM memory_space_links
            WHERE user_id = ? AND memory_id IN ({placeholders})
            ORDER BY created_at ASC, rowid ASC
            """,
            (user_id, *batch),
        ).fetchall()
        for row in rows:
            result.setdefault(str(row["memory_id"]), []).append(
                str(row["space_id"])
            )
    return result


def _rows_to_memories(
    store: _ConnectableStore, rows: list[sqlite3.Row]
) -> list[MemoryRecord]:
    if not rows:
        return []
    with store._connect() as connection:
        return _rows_to_memories_on_connection(
            connection=connection,
            rows=rows,
        )


def _rows_to_memories_on_connection(
    *,
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> list[MemoryRecord]:
    if not rows:
        return []
    space_ids_by_memory = _space_ids_for_memory_ids_on_connection(
        connection=connection,
        user_id=str(rows[0]["user_id"]),
        memory_ids=[str(row["id"]) for row in rows],
    )
    return [
        _row_to_memory(row, space_ids=space_ids_by_memory.get(str(row["id"]), []))
        for row in rows
    ]


def _row_to_memory(
    row: sqlite3.Row,
    *,
    space_ids: list[str],
) -> MemoryRecord:
    data = dict(row)
    raw_evidence = data.pop("evidence_memory_ids_json", None)
    raw_topics = data.pop("topics_json", None)
    raw_entities = data.pop("entities_json", None)
    data["evidence_memory_ids"] = _json_string_list(raw_evidence)
    data["topics"] = _json_string_list(raw_topics)
    data["entities"] = _json_string_list(raw_entities)
    data["type"] = normalize_memory_type(data.get("type") or "semantic")
    data["origin"] = data.get("origin") or "user_asserted"
    data.setdefault("embedding_space_id", None)
    if not data.get("embedding_json"):
        data["embedding_space_id"] = None
    data["usage_count"] = float(data.get("usage_count") or 0)
    data["digested"] = bool(data.get("digested"))
    data["temporal_subject"] = normalize_optional_text(data.get("temporal_subject"))
    data["temporal_predicate"] = normalize_optional_text(data.get("temporal_predicate"))
    if bool(data["temporal_subject"]) != bool(data["temporal_predicate"]):
        # Pre-validation databases could contain a half-key. Treat it as
        # unkeyed instead of letting one corrupt row break all recall.
        data["temporal_subject"] = None
        data["temporal_predicate"] = None
    data.setdefault("valid_from", None)
    data.setdefault("status", "dynamic")
    data.setdefault("decay_lambda", None)
    for field_name in ("valid_from", "valid_until"):
        try:
            data[field_name] = normalize_iso_text(data.get(field_name))
        except ValueError:
            data[field_name] = None
    starts_at = _parse_iso_datetime(data.get("valid_from"))
    ends_at = _parse_iso_datetime(data.get("valid_until"))
    if starts_at is not None and ends_at is not None and starts_at > ends_at:
        # Preserve the expiry (the conservative current-view boundary) and
        # discard the impossible start on legacy corrupt data.
        data["valid_from"] = None
    try:
        decay_lambda = float(data["decay_lambda"])
    except (TypeError, ValueError):
        decay_lambda = None
    if (
        decay_lambda is None
        or not math.isfinite(decay_lambda)
        or not 0.0 <= decay_lambda <= 10.0
    ):
        decay_lambda = None
    data["decay_lambda"] = decay_lambda
    data.setdefault("supersedes", None)
    data.setdefault("superseded_by", None)
    data["space_ids"] = space_ids
    return MemoryRecord(**data)


def _row_to_memory_space(row: sqlite3.Row) -> MemorySpace:
    payload = dict(row)
    # Tolerate pre-migration rows and NULL metadata.
    if payload.get("color") is not None:
        payload["color"] = str(payload["color"]) or None
    if payload.get("description") is not None:
        text = str(payload["description"]).strip()
        payload["description"] = text or None
    try:
        payload["sort_order"] = int(payload.get("sort_order") or 0)
    except (TypeError, ValueError):
        payload["sort_order"] = 0
    return MemorySpace(**payload)


def _row_to_core_memory_section(row: sqlite3.Row) -> CoreMemorySection:
    data = dict(row)
    raw_evidence = data.pop("evidence_memory_ids_json", None)
    data["evidence_memory_ids"] = _json_string_list(raw_evidence)
    return CoreMemorySection(**data)


def _row_to_core_memory_section_history(row: sqlite3.Row) -> CoreMemorySectionHistory:
    data = dict(row)
    raw_evidence = data.pop("evidence_memory_ids_json", None)
    data["evidence_memory_ids"] = _json_string_list(raw_evidence)
    return CoreMemorySectionHistory(**data)


def _row_to_recent_context_summary(row: sqlite3.Row) -> RecentContextSummary:
    data = dict(row)
    raw_turns = data.pop("recent_turns_json", None)
    try:
        parsed_turns = json.loads(raw_turns) if raw_turns else []
    except json.JSONDecodeError:
        parsed_turns = []
    data["recent_turns"] = parsed_turns if isinstance(parsed_turns, list) else []
    return RecentContextSummary(**data)


def _row_to_conversation_branch_node(
    row: sqlite3.Row,
) -> ConversationBranchNode:
    data = dict(row)
    raw_turns = data.pop("recent_turns_json", None)
    try:
        parsed_turns = json.loads(raw_turns) if raw_turns else []
    except json.JSONDecodeError:
        parsed_turns = []
    data["recent_turns"] = parsed_turns if isinstance(parsed_turns, list) else []
    return ConversationBranchNode(**data)


def _temporal_snapshot(row: sqlite3.Row) -> dict:
    columns = set(row.keys())
    return {
        "id": row["id"],
        "valid_from": row["valid_from"] if "valid_from" in columns else None,
        "valid_until": row["valid_until"] if "valid_until" in columns else None,
        "temporal_subject": row["temporal_subject"] if "temporal_subject" in columns else None,
        "temporal_predicate": row["temporal_predicate"] if "temporal_predicate" in columns else None,
        "status": row["status"] if "status" in columns else None,
        "supersedes": row["supersedes"] if "supersedes" in columns else None,
        "superseded_by": row["superseded_by"] if "superseded_by" in columns else None,
        "updated_at": row["updated_at"],
    }
