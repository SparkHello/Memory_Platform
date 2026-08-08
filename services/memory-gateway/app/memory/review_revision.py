from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import render_memory_review_revision_messages
from app.memory.models import (
    MemoryRecord,
    MemoryRelation,
    MemoryReviewRiskTag,
    MemoryReviewSeverity,
    MemoryReviewRevisionOperation,
    MemoryReviewRevisionPreview,
)
from app.memory.redaction import detect_text_sensitivity
from app.memory.review_policy import build_review_policy
from app.memory.search import MemorySearchService
from app.memory.store import MemoryStore
from app.memory.utils import (
    _char_overlap,
    _has_negation,
    _normalize,
    _parse_json_object,
    _term_jaccard,
)
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.context import model_usage_scope


class ReviewRevisionError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_REVIEW_REVISION_TOOL_NAME = "submit_memory_review_revision"


async def find_related_review_revision_memories(
    *,
    user_id: str,
    store: MemoryStore,
    search_service: MemorySearchService,
    memory_ids: list[str],
    user_note: str,
    recommendation_reason: str | None = None,
    suggested_content: str | None = None,
    limit: int = 8,
) -> dict:
    note = user_note.strip()
    if not note:
        raise ReviewRevisionError(422, "user_note 不能为空")

    selected_ids = _ordered_unique(memory_ids)
    if not selected_ids:
        raise ReviewRevisionError(422, "memory_ids 不能为空")
    selected_memories = _load_memories(store=store, user_id=user_id, memory_ids=selected_ids)
    selected_set = set(selected_ids)
    capped_limit = max(1, min(limit, 8))
    core_map = _core_evidence_map(store=store, user_id=user_id)
    candidates: dict[str, dict] = {}

    query = _related_query(
        selected_memories=selected_memories,
        user_note=note,
        recommendation_reason=recommendation_reason,
        suggested_content=suggested_content,
    )
    search_hits = await search_service.search_hits(
        query=query,
        user_id=user_id,
        limit=max(capped_limit * 2, 8),
        record_usage=False,
    )
    for hit in search_hits:
        if hit.memory.id in selected_set:
            continue
        _upsert_related_candidate(
            candidates,
            memory=hit.memory,
            relation="none",
            reason="搜索召回了与本次修改说明相近的记忆",
            channels=[f"search:{channel}" for channel in hit.channels],
            score=max(0.0, min(1.0, hit.relevance / 100.0)),
            core_map=core_map,
        )

    active_memories = store.list_memories(user_id=user_id, limit=1000)
    for selected in selected_memories:
        for memory in active_memories:
            if memory.id in selected_set:
                continue
            relation, score, reason = _rule_relation(selected, memory)
            if relation == "none":
                continue
            _upsert_related_candidate(
                candidates,
                memory=memory,
                relation=relation,
                reason=reason,
                channels=["rule"],
                score=score,
                core_map=core_map,
            )

    ordered = sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate["score"],
            int(candidate["is_core_memory_evidence"]),
            candidate["memory"]["importance"],
            candidate["memory"]["updated_at"],
        ),
        reverse=True,
    )
    return {"data": ordered[:capped_limit]}


async def preview_review_revision(
    *,
    user_id: str,
    store: MemoryStore,
    llm_client: OpenAICompatibleClient,
    secret: str,
    memory_ids: list[str],
    user_note: str,
    recommendation_reason: str | None = None,
    relation: MemoryRelation | str | None = None,
    suggested_content: str | None = None,
    risk_tags: list[MemoryReviewRiskTag] | None = None,
    severity: MemoryReviewSeverity | None = None,
) -> MemoryReviewRevisionPreview:
    note = user_note.strip()
    if not note:
        raise ReviewRevisionError(422, "user_note 不能为空")

    allowed_ids = _ordered_unique(memory_ids)
    if not allowed_ids:
        raise ReviewRevisionError(422, "memory_ids 不能为空")
    memories = _load_memories(store=store, user_id=user_id, memory_ids=allowed_ids)
    memory_map = {memory.id: memory for memory in memories}

    messages = render_memory_review_revision_messages(
        memories=memories,
        user_note=note,
        recommendation_reason=recommendation_reason,
        relation=relation,
        suggested_content=suggested_content,
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=messages,
        temperature=0.0,
        max_tokens=2048,
        response_format={"type": "json_object"},
        stream=False,
    )
    try:
        with model_usage_scope(user_id=user_id):
            response = await llm_client.create_chat_completion(
                request=request,
                messages=messages,
                thinking="enabled",
                structured_tool=_review_revision_structured_tool(),
            )
        raw_output = _review_revision_raw_output(response["choices"][0]["message"])
    except HTTPException as exc:
        raise ReviewRevisionError(
            exc.status_code,
            f"调用修改模型失败：{exc.detail}",
        ) from exc
    except Exception as exc:
        raise ReviewRevisionError(502, f"调用修改模型失败：{exc}") from exc

    data = _parse_review_revision_output(raw_output if isinstance(raw_output, str) else "")
    if data is None:
        raise ReviewRevisionError(502, "AI 修改预览输出不是合法 JSON")
    raw_operations = _raw_review_revision_operations(data)
    if raw_operations is None:
        raise ReviewRevisionError(502, "AI 修改预览缺少 operations")

    operations = _normalize_operations(
        raw_operations,
        allowed_ids=allowed_ids,
        memory_map=memory_map,
    )
    if not operations:
        operations = [
            MemoryReviewRevisionOperation(
                operation="no_change",
                memory_ids=allowed_ids,
                reason=str(data.get("reason") or "AI 未给出可执行修改"),
            )
        ]
    _assert_operation_coverage(operations, allowed_ids=allowed_ids)

    reason = str(data.get("reason") or "")
    preview = MemoryReviewRevisionPreview(
        operations=operations,
        preview_token=_sign_preview(
            secret=secret,
            payload=_token_payload(
                user_id=user_id,
                allowed_ids=allowed_ids,
                operations=operations,
                user_note=note,
                recommendation_reason=recommendation_reason,
                relation=relation,
                suggested_content=suggested_content,
                risk_tags=risk_tags or [],
                severity=severity,
                memories=memories,
            ),
        ),
        reason=reason,
    )
    return preview


def _review_revision_structured_tool() -> dict[str, Any]:
    operation_properties: dict[str, Any] = {
        "operation": {
            "type": "string",
            "enum": ["update", "merge", "archive", "no_change"],
        },
        "reason": {"type": "string"},
        "memory_ids": {"type": "array", "items": {"type": "string"}},
        "target_memory_id": {"type": ["string", "null"]},
        "content": {"type": ["string", "null"]},
        "type": {
            "type": ["string", "null"],
            "enum": [
                "episodic",
                "semantic",
                "procedural",
                "emotional",
                "reflective",
                None,
            ],
        },
        "importance": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "valence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "arousal": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "stability": {
            "type": ["string", "null"],
            "enum": ["temporary", "medium", "stable", None],
        },
        "valid_until": {"type": ["string", "null"]},
        "sensitivity": {
            "type": ["string", "null"],
            "enum": ["normal", "private", "sensitive", None],
        },
    }
    return {
        "name": _REVIEW_REVISION_TOOL_NAME,
        "description": "返回记忆体检修改预览；只使用传入的 memory id。",
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": operation_properties,
                        "required": ["operation", "memory_ids", "reason"],
                    },
                },
                "reason": {"type": "string"},
            },
            "required": ["operations"],
        },
    }


def _review_revision_raw_output(message: dict[str, Any]) -> str:
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            continue
        if function.get("name") != _REVIEW_REVISION_TOOL_NAME:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            return arguments
        if isinstance(arguments, dict):
            return json.dumps(arguments, ensure_ascii=False)
    content = message.get("content")
    return content if isinstance(content, str) else ""


def apply_review_revision(
    *,
    user_id: str,
    store: MemoryStore,
    secret: str,
    memory_ids: list[str],
    operations: list[MemoryReviewRevisionOperation],
    preview_token: str,
    risk_tags: list[MemoryReviewRiskTag] | None = None,
    severity: MemoryReviewSeverity | None = None,
) -> dict:
    allowed_ids = _ordered_unique(memory_ids)
    payload = _verify_preview(secret=secret, token=preview_token)
    operation_payloads = [_operation_payload(operation) for operation in operations]
    if (
        payload.get("user_id") != user_id
        or payload.get("allowed_memory_ids") != allowed_ids
        or payload.get("operations") != operation_payloads
    ):
        raise ReviewRevisionError(409, "修改预览已失效或与提交内容不一致")

    memories = _load_memories(store=store, user_id=user_id, memory_ids=allowed_ids)
    expected_versions = payload.get("memory_versions")
    current_versions = {memory.id: memory.updated_at for memory in memories}
    if expected_versions != current_versions:
        raise ReviewRevisionError(409, "修改预览已过期：记忆在预览后发生了变化")
    memory_map = {memory.id: memory for memory in memories}
    _assert_operations_are_applicable(operations, allowed_ids=allowed_ids, memory_map=memory_map)
    affected_core_sections = _affected_core_sections(
        store=store,
        user_id=user_id,
        memory_ids=_affected_operation_memory_ids(operations),
    )

    results: list[dict] = []
    for operation in operations:
        if operation.operation == "no_change":
            store.create_decision_log(
                user_id=user_id,
                conversation_id=None,
                candidate_json=_decision_log_json(
                    payload=payload,
                    operation=operation,
                    risk_tags=risk_tags,
                    severity=severity,
                    memories=[
                        memory_map[memory_id]
                        for memory_id in operation.memory_ids
                        if memory_id in memory_map
                    ],
                ),
                decision="ignore",
                reason=operation.reason or "体检修改预览未产生变更",
            )
            results.append(
                {
                    "operation": "no_change",
                    "memory_ids": operation.memory_ids,
                    "reason": operation.reason,
                }
            )
            continue

        if operation.operation == "update":
            updated = _apply_update(store, user_id, memory_map, operation)
            store.create_decision_log(
                user_id=user_id,
                conversation_id=None,
                candidate_json=_decision_log_json(
                    payload=payload,
                    operation=operation,
                    risk_tags=risk_tags,
                    severity=severity,
                    memories=[
                        memory_map[memory_id]
                        for memory_id in operation.memory_ids
                        if memory_id in memory_map
                    ],
                ),
                decision="update",
                reason=operation.reason or "体检 AI 修改已更新记忆",
            )
            results.append(
                {
                    "operation": "update",
                    "memory_id": updated.id,
                    "memory": updated.model_dump(exclude={"embedding_json"}),
                }
            )
            memory_map[updated.id] = updated
            continue

        if operation.operation == "merge":
            merged = _apply_merge(store, user_id, memory_map, operation)
            store.create_decision_log(
                user_id=user_id,
                conversation_id=None,
                candidate_json=_decision_log_json(
                    payload=payload,
                    operation=operation,
                    risk_tags=risk_tags,
                    severity=severity,
                    memories=[
                        memory_map[memory_id]
                        for memory_id in operation.memory_ids
                        if memory_id in memory_map
                    ],
                ),
                decision="update",
                reason=operation.reason or "体检 AI 修改已合并记忆",
            )
            results.append(
                {
                    "operation": "merge",
                    "memory_id": merged.id,
                    "memory": merged.model_dump(exclude={"embedding_json"}),
                    "archived_memory_ids": [
                        memory_id for memory_id in operation.memory_ids if memory_id != merged.id
                    ],
                }
            )
            for memory_id in operation.memory_ids:
                if memory_id != merged.id:
                    memory_map.pop(memory_id, None)
            memory_map[merged.id] = merged
            continue

        if operation.operation == "archive":
            archived_ids: list[str] = []
            for memory_id in operation.memory_ids:
                if store.archive_memory(memory_id=memory_id, user_id=user_id):
                    archived_ids.append(memory_id)
                    memory_map.pop(memory_id, None)
            store.create_decision_log(
                user_id=user_id,
                conversation_id=None,
                candidate_json=_decision_log_json(
                    payload=payload,
                    operation=operation,
                    risk_tags=risk_tags,
                    severity=severity,
                    memories=[
                        memory_map[memory_id]
                        for memory_id in operation.memory_ids
                        if memory_id in memory_map
                    ],
                ),
                decision="update",
                reason=operation.reason or "体检 AI 修改已归档记忆",
            )
            results.append({"operation": "archive", "archived_memory_ids": archived_ids})

    return {
        "applied": True,
        "results": results,
        "affected_core_sections": [
            section.model_dump() for section in affected_core_sections
        ],
    }


def _normalize_operations(
    raw_operations: list,
    *,
    allowed_ids: list[str],
    memory_map: dict[str, MemoryRecord],
) -> list[MemoryReviewRevisionOperation]:
    operations: list[MemoryReviewRevisionOperation] = []
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict):
            raise ReviewRevisionError(502, "AI 修改预览包含非法 operation")
        raw_operation = _coerce_raw_operation(raw_operation)
        try:
            operation = MemoryReviewRevisionOperation.model_validate(raw_operation)
        except ValidationError as exc:
            raise ReviewRevisionError(502, f"AI 修改预览 operation 不符合 schema：{exc}") from exc
        operations.append(_normalize_operation(operation, allowed_ids, memory_map))
    return operations


def _parse_review_revision_output(text: str) -> dict | None:
    data = _parse_json_object(text)
    if data is not None:
        return data
    for candidate_text in (_strip_json_fence(text), _first_json_array_block(text)):
        if not candidate_text:
            continue
        try:
            parsed = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return {"operations": parsed}
        if isinstance(parsed, dict):
            return parsed
    return None


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _first_json_array_block(text: str) -> str | None:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _raw_review_revision_operations(data: dict) -> list | None:
    raw_operations = data.get("operations")
    if isinstance(raw_operations, dict):
        return [raw_operations]
    if isinstance(raw_operations, list):
        return raw_operations
    if isinstance(data.get("operation"), str):
        return [data]
    return None


def _coerce_raw_operation(raw_operation: dict) -> dict:
    coerced = dict(raw_operation)
    operation = coerced.get("operation")
    if isinstance(operation, str):
        coerced["operation"] = _coerce_operation_name(operation)

    memory_ids = coerced.get("memory_ids")
    if isinstance(memory_ids, str):
        coerced["memory_ids"] = [memory_ids]
    elif memory_ids is None:
        coerced["memory_ids"] = []
    elif isinstance(memory_ids, list):
        coerced["memory_ids"] = [str(memory_id) for memory_id in memory_ids if memory_id]

    target_memory_id = coerced.get("target_memory_id")
    if isinstance(target_memory_id, list):
        coerced["target_memory_id"] = str(target_memory_id[0]) if target_memory_id else None
    elif target_memory_id is not None and not isinstance(target_memory_id, str):
        coerced["target_memory_id"] = str(target_memory_id)

    reason = coerced.get("reason")
    if reason is not None and not isinstance(reason, str):
        coerced["reason"] = str(reason)
    content = coerced.get("content")
    if content is not None and not isinstance(content, str):
        coerced["content"] = str(content)
    valid_until = coerced.get("valid_until")
    if valid_until is not None and not isinstance(valid_until, str):
        coerced["valid_until"] = None

    for field in ("importance",):
        coerced[field] = _coerce_int_or_none(coerced.get(field))
    for field in ("confidence", "valence", "arousal"):
        coerced[field] = _coerce_float_or_none(coerced.get(field))
    coerced["type"] = _coerce_literal_or_none(
        coerced.get("type"),
        {"episodic", "semantic", "procedural", "emotional", "reflective"},
    )
    coerced["stability"] = _coerce_literal_or_none(
        coerced.get("stability"),
        {"temporary", "medium", "stable"},
    )
    coerced["sensitivity"] = _coerce_literal_or_none(
        coerced.get("sensitivity"),
        {"normal", "private", "sensitive"},
    )
    coerced["review_policy"] = None
    return coerced


def _coerce_operation_name(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "edit": "update",
        "modify": "update",
        "revise": "update",
        "更新": "update",
        "修改": "update",
        "combine": "merge",
        "dedupe": "merge",
        "合并": "merge",
        "delete": "archive",
        "remove": "archive",
        "trash": "archive",
        "archive_memory": "archive",
        "move_to_archive": "archive",
        "move_to_trash": "archive",
        "soft_delete": "archive",
        "归档": "archive",
        "移入回收站": "archive",
        "keep": "no_change",
        "none": "no_change",
        "ignore": "no_change",
        "skip": "no_change",
        "no_action": "no_change",
        "not_change": "no_change",
        "unchanged": "no_change",
        "leave_unchanged": "no_change",
        "no_change_needed": "no_change",
        "不修改": "no_change",
        "无需修改": "no_change",
    }
    return aliases.get(normalized, normalized)


def _coerce_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 10 else None


def _coerce_float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _coerce_literal_or_none(value: Any, allowed: set[str]) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized if normalized in allowed else None


def _normalize_operation(
    operation: MemoryReviewRevisionOperation,
    allowed_ids: list[str],
    memory_map: dict[str, MemoryRecord],
) -> MemoryReviewRevisionOperation:
    if operation.operation == "no_change":
        memory_ids = _operation_memory_ids(operation)
        _assert_subset(memory_ids, allowed_ids)
        return MemoryReviewRevisionOperation(
            operation="no_change",
            memory_ids=memory_ids,
            reason=operation.reason or "信息不足，暂不修改",
        )

    memory_ids = _operation_memory_ids(operation)
    _assert_subset(memory_ids, allowed_ids)

    if operation.operation == "archive":
        return operation.model_copy(
            update={
                "memory_ids": memory_ids,
                "target_memory_id": None,
                "content": None,
                "review_policy": None,
            }
        )

    if operation.operation == "update":
        target_id = operation.target_memory_id or memory_ids[0]
        if target_id not in allowed_ids:
            raise ReviewRevisionError(422, "target_memory_id 不在本次体检记忆范围内")
        content = _required_content(operation)
        target = memory_map[target_id]
        normalized = operation.model_copy(
            update={
                "memory_ids": memory_ids if memory_ids else [target_id],
                "target_memory_id": target_id,
                "content": content,
            }
        )
        return normalized.model_copy(update={"review_policy": _policy_for_operation(target, normalized)})

    if operation.operation == "merge":
        if len(memory_ids) < 2:
            raise ReviewRevisionError(422, "merge 至少需要两个 memory_id")
        content = _required_content(operation)
        target_id = operation.target_memory_id if operation.target_memory_id in memory_ids else memory_ids[0]
        ordered_ids = [target_id, *[memory_id for memory_id in memory_ids if memory_id != target_id]]
        target = memory_map[target_id]
        normalized = operation.model_copy(
            update={
                "memory_ids": ordered_ids,
                "target_memory_id": target_id,
                "content": content,
            }
        )
        return normalized.model_copy(update={"review_policy": _policy_for_operation(target, normalized)})

    raise ReviewRevisionError(422, f"未知修改操作：{operation.operation}")


def _assert_operations_are_applicable(
    operations: list[MemoryReviewRevisionOperation],
    *,
    allowed_ids: list[str],
    memory_map: dict[str, MemoryRecord],
) -> None:
    _assert_operation_coverage(operations, allowed_ids=allowed_ids)
    for operation in operations:
        memory_ids = _operation_memory_ids(operation)
        _assert_subset(memory_ids, allowed_ids)
        for memory_id in memory_ids:
            if memory_id not in memory_map:
                raise ReviewRevisionError(404, f"记忆不存在或已删除：{memory_id}")
        if operation.operation == "no_change":
            continue
        if operation.operation in {"update", "merge"}:
            if operation.review_policy is None:
                raise ReviewRevisionError(422, "修改操作缺少 review_policy")
            _required_content(operation)


def _apply_update(
    store: MemoryStore,
    user_id: str,
    memory_map: dict[str, MemoryRecord],
    operation: MemoryReviewRevisionOperation,
) -> MemoryRecord:
    target_id = operation.target_memory_id or operation.memory_ids[0]
    existing = memory_map[target_id]
    updated = store.update_memory(
        memory_id=existing.id,
        user_id=user_id,
        content=operation.content or existing.content,
        type=operation.type or existing.type,
        importance=operation.importance if operation.importance is not None else existing.importance,
        confidence=operation.confidence if operation.confidence is not None else existing.confidence,
        valence=operation.valence if operation.valence is not None else existing.valence,
        arousal=operation.arousal if operation.arousal is not None else existing.arousal,
        source_message=existing.source_message,
        source_conversation_id=existing.source_conversation_id,
        embedding_json=None if operation.content and operation.content != existing.content else existing.embedding_json,
        stability=operation.stability or existing.stability,
        valid_from=existing.valid_from,
        valid_until=operation.valid_until if operation.valid_until is not None else existing.valid_until,
        review_after=operation.review_policy.review_after if operation.review_policy else existing.review_after,
        sensitivity=operation.sensitivity or existing.sensitivity,
        evidence_memory_ids=existing.evidence_memory_ids,
        temporal_subject=existing.temporal_subject,
        temporal_predicate=existing.temporal_predicate,
    )
    if updated is None:
        raise ReviewRevisionError(404, "目标记忆不存在或已删除")
    return updated


def _apply_merge(
    store: MemoryStore,
    user_id: str,
    memory_map: dict[str, MemoryRecord],
    operation: MemoryReviewRevisionOperation,
) -> MemoryRecord:
    result = store.merge_memories(
        user_id=user_id,
        memory_ids=operation.memory_ids,
        content=operation.content,
    )
    if result.memory is None:
        raise ReviewRevisionError(422, result.reason)
    merged = result.memory
    updated = store.update_memory(
        memory_id=merged.id,
        user_id=user_id,
        content=operation.content or merged.content,
        type=operation.type or merged.type,
        importance=operation.importance if operation.importance is not None else merged.importance,
        confidence=operation.confidence if operation.confidence is not None else merged.confidence,
        valence=operation.valence if operation.valence is not None else merged.valence,
        arousal=operation.arousal if operation.arousal is not None else merged.arousal,
        source_message=merged.source_message,
        source_conversation_id=merged.source_conversation_id,
        embedding_json=None,
        stability=operation.stability or merged.stability,
        valid_from=merged.valid_from,
        valid_until=operation.valid_until if operation.valid_until is not None else merged.valid_until,
        review_after=operation.review_policy.review_after if operation.review_policy else merged.review_after,
        sensitivity=operation.sensitivity or merged.sensitivity,
        evidence_memory_ids=merged.evidence_memory_ids,
        temporal_subject=merged.temporal_subject,
        temporal_predicate=merged.temporal_predicate,
    )
    if updated is None:
        raise ReviewRevisionError(404, "合并后的目标记忆不存在或已删除")
    return updated


def _policy_for_operation(
    target: MemoryRecord,
    operation: MemoryReviewRevisionOperation,
) -> Any:
    return build_review_policy(
        content=operation.content or target.content,
        type=operation.type or target.type,
        stability=operation.stability or target.stability,
        sensitivity=operation.sensitivity or target.sensitivity,
    )


def _related_query(
    *,
    selected_memories: list[MemoryRecord],
    user_note: str,
    recommendation_reason: str | None,
    suggested_content: str | None,
) -> str:
    parts = [
        user_note,
        recommendation_reason or "",
        suggested_content or "",
        *[memory.content for memory in selected_memories],
    ]
    return " ".join(part for part in parts if part).strip()[:800]


def _rule_relation(left: MemoryRecord, right: MemoryRecord) -> tuple[MemoryRelation, float, str]:
    if left.type != right.type:
        return "none", 0.0, ""
    left_normalized = _normalize(left.content)
    right_normalized = _normalize(right.content)
    if not left_normalized or not right_normalized:
        return "none", 0.0, ""
    if left_normalized == right_normalized:
        return "same", 1.0, "同类型记忆内容重复"
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return "supplement", 0.92, "同类型记忆存在包含或补充关系"

    score = max(_term_jaccard(left.content, right.content), _char_overlap(left.content, right.content))
    if score < 0.45:
        return "none", 0.0, ""
    if _has_negation(left.content) != _has_negation(right.content):
        return "conflict", score, "同类型记忆相似但否定关系不同，可能冲突"
    return "supersede", score, "同类型记忆高度相似，可能存在替代关系"


def _upsert_related_candidate(
    candidates: dict[str, dict],
    *,
    memory: MemoryRecord,
    relation: MemoryRelation,
    reason: str,
    channels: list[str],
    score: float,
    core_map: dict[str, list[dict]],
) -> None:
    existing = candidates.get(memory.id)
    core_sections = core_map.get(memory.id, [])
    payload = {
        "memory": memory.model_dump(exclude={"embedding_json"}),
        "relation": relation,
        "reason": reason,
        "channels": _ordered_unique(channels),
        "score": round(max(0.0, min(1.0, score)), 4),
        "is_core_memory_evidence": bool(core_sections),
        "core_memory_sections": core_sections,
    }
    if existing is None:
        candidates[memory.id] = payload
        return

    existing["score"] = round(max(existing["score"], payload["score"]), 4)
    existing["channels"] = _ordered_unique([*existing["channels"], *payload["channels"]])
    if existing["relation"] == "none" and payload["relation"] != "none":
        existing["relation"] = payload["relation"]
        existing["reason"] = payload["reason"]
    elif payload["relation"] != "none" and "rule" in payload["channels"]:
        existing["reason"] = f"{existing['reason']}；{payload['reason']}"
    existing["is_core_memory_evidence"] = existing["is_core_memory_evidence"] or payload["is_core_memory_evidence"]
    existing["core_memory_sections"] = payload["core_memory_sections"] or existing["core_memory_sections"]


def _core_evidence_map(
    *,
    store: MemoryStore,
    user_id: str,
) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for section in store.list_core_memory_sections(user_id=user_id):
        section_payload = section.model_dump()
        for memory_id in section.evidence_memory_ids:
            result.setdefault(memory_id, []).append(section_payload)
    return result


def _affected_operation_memory_ids(operations: list[MemoryReviewRevisionOperation]) -> list[str]:
    affected: list[str] = []
    for operation in operations:
        if operation.operation == "no_change":
            continue
        affected.extend(_operation_memory_ids(operation))
    return _ordered_unique(affected)


def _affected_core_sections(
    *,
    store: MemoryStore,
    user_id: str,
    memory_ids: list[str],
) -> list:
    if not memory_ids:
        return []
    touched = set(memory_ids)
    sections = [
        section
        for section in store.list_core_memory_sections(user_id=user_id)
        if touched & set(section.evidence_memory_ids)
    ]
    return sections


def _load_memories(
    *,
    store: MemoryStore,
    user_id: str,
    memory_ids: list[str],
) -> list[MemoryRecord]:
    memories: list[MemoryRecord] = []
    for memory_id in memory_ids:
        memory = store.get_memory(memory_id=memory_id, user_id=user_id)
        if memory is None:
            raise ReviewRevisionError(404, f"记忆不存在或已删除：{memory_id}")
        memories.append(memory)
    return memories


def _operation_memory_ids(operation: MemoryReviewRevisionOperation) -> list[str]:
    memory_ids = list(operation.memory_ids)
    if operation.target_memory_id and operation.target_memory_id not in memory_ids:
        memory_ids.insert(0, operation.target_memory_id)
    return _ordered_unique(memory_ids)


def _operation_handled_memory_ids(operation: MemoryReviewRevisionOperation) -> list[str]:
    if operation.operation == "update":
        target_id = operation.target_memory_id or (operation.memory_ids[0] if operation.memory_ids else None)
        return [target_id] if target_id else []
    return _operation_memory_ids(operation)


def _required_content(operation: MemoryReviewRevisionOperation) -> str:
    content = (operation.content or "").strip()
    if not content:
        raise ReviewRevisionError(422, f"{operation.operation} 操作必须提供 content")
    return content


def _assert_subset(memory_ids: list[str], allowed_ids: list[str]) -> None:
    if not memory_ids:
        raise ReviewRevisionError(422, "operation.memory_ids 不能为空")
    invalid = [memory_id for memory_id in memory_ids if memory_id not in allowed_ids]
    if invalid:
        raise ReviewRevisionError(422, f"operation 引用了本次体检范围外的记忆：{invalid}")


def _assert_operation_coverage(
    operations: list[MemoryReviewRevisionOperation],
    *,
    allowed_ids: list[str],
) -> None:
    referenced_ids: list[str] = []
    handled_ids: list[str] = []
    for operation in operations:
        referenced_ids.extend(_operation_memory_ids(operation))
        handled_ids.extend(_operation_handled_memory_ids(operation))
    handled = set(handled_ids)
    invalid = [memory_id for memory_id in _ordered_unique(referenced_ids) if memory_id not in allowed_ids]
    if invalid:
        raise ReviewRevisionError(422, f"operation 引用了本次体检范围外的记忆：{invalid}")
    missing = [memory_id for memory_id in allowed_ids if memory_id not in handled]
    if missing:
        raise ReviewRevisionError(422, f"AI 修改预览没有覆盖本次已选记忆：{missing}")


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _token_payload(
    *,
    user_id: str,
    allowed_ids: list[str],
    operations: list[MemoryReviewRevisionOperation],
    user_note: str,
    recommendation_reason: str | None,
    relation: str | None,
    suggested_content: str | None,
    risk_tags: list[MemoryReviewRiskTag],
    severity: MemoryReviewSeverity | None,
    memories: list[MemoryRecord],
) -> dict:
    now = datetime.now(UTC)
    return {
        "version": 2,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
        "user_id": user_id,
        "allowed_memory_ids": allowed_ids,
        "memory_versions": {memory.id: memory.updated_at for memory in memories},
        "operations": [_operation_payload(operation) for operation in operations],
        "user_note": user_note,
        "recommendation_reason": recommendation_reason,
        "relation": relation,
        "suggested_content": suggested_content,
        "risk_tags": risk_tags,
        "severity": severity,
    }


def _operation_payload(operation: MemoryReviewRevisionOperation) -> dict:
    return operation.model_dump(mode="json", exclude_none=False)


def _sign_preview(*, secret: str, payload: dict) -> str:
    payload_json = _canonical_json(payload).encode("utf-8")
    signature = hmac.new(_secret_bytes(secret), payload_json, hashlib.sha256).digest()
    return f"{_b64(payload_json)}.{_b64(signature)}"


def _verify_preview(*, secret: str, token: str) -> dict:
    try:
        payload_part, signature_part = token.split(".", 1)
        payload_json = _unb64(payload_part)
        expected = hmac.new(_secret_bytes(secret), payload_json, hashlib.sha256).digest()
        actual = _unb64(signature_part)
    except Exception as exc:
        raise ReviewRevisionError(409, "修改预览 token 无效") from exc
    if not hmac.compare_digest(expected, actual):
        raise ReviewRevisionError(409, "修改预览 token 无效")
    try:
        payload = json.loads(payload_json.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewRevisionError(409, "修改预览 token 无效") from exc
    if not isinstance(payload, dict) or payload.get("version") != 2:
        raise ReviewRevisionError(409, "修改预览 token 无效")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewRevisionError(409, "修改预览 token 无效") from exc
    if expires_at <= datetime.now(UTC):
        raise ReviewRevisionError(409, "修改预览 token 已过期")
    return payload


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def _secret_bytes(secret: str) -> bytes:
    return (secret or "memory-gateway-review-revision").encode("utf-8")


def _decision_log_json(
    *,
    payload: dict,
    operation: MemoryReviewRevisionOperation,
    risk_tags: list[MemoryReviewRiskTag] | None,
    severity: MemoryReviewSeverity | None,
    memories: list[MemoryRecord],
) -> str:
    return json.dumps(
        {
            "source": "review_modify",
            "user_note": payload.get("user_note"),
            "recommendation_reason": payload.get("recommendation_reason"),
            "relation": payload.get("relation"),
            "risk_tags": payload.get("risk_tags") or risk_tags or [],
            "severity": payload.get("severity") or severity,
            "operation": _redacted_operation_payload(operation, memories),
        },
        ensure_ascii=False,
    )


def _redacted_operation_payload(
    operation: MemoryReviewRevisionOperation,
    memories: list[MemoryRecord],
) -> dict:
    data = _operation_payload(operation)
    if operation.content is None:
        return data
    sensitive = (operation.sensitivity or "normal") != "normal" or any(
        memory.sensitivity != "normal" for memory in memories
    )
    if not sensitive:
        sensitive = detect_text_sensitivity(operation.content) != "normal"
    if not sensitive:
        return data
    data.pop("content", None)
    data["content_length"] = len(operation.content)
    data["content_sha256"] = hashlib.sha256(
        operation.content.encode("utf-8")
    ).hexdigest()
    data["redacted"] = True
    return data
