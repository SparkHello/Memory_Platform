"""Shared router, models, and helpers for /memories routes."""
from datetime import UTC, datetime, timedelta
from functools import partial
import hashlib
import json
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.config import Settings, get_settings
from app.api.deps import (
    get_embedding_client,
    get_llm_client,
    get_memory_search_service,
    get_memory_store,
    get_signing_secret,
    get_user_id,
    require_api_key,
)
from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import (
    render_core_memory_context,
    render_memory_context,
    render_recent_context_summary_context,
)
from app.memory.classification import classify_memory
from app.memory.core import CoreMemoryConsolidator, safe_core_memory_sections
from app.memory.evaluation import (
    EvaluationError,
    MAX_RECALL_EVAL_K,
    build_recall_workbench,
    delete_user_eval_workspace,
    init_eval,
    run_diagnosis,
    run_recall_eval,
    save_labels,
)
from app.memory.extractor import validate_candidate_for_save
from app.memory.graph_traverse import traverse_memory_network
from app.memory.health import MemoryHealthChecker
from app.memory.ingest import MemoryIngestService
from app.memory.models import (
    CandidateMemory,
    CoreMemorySection,
    CoreMemorySectionName,
    MemoryRecord,
    MemoryRelation,
    MemoryReviewRiskTag,
    MemoryReviewRevisionOperation,
    MemoryReviewSeverity,
    MemorySensitivity,
    MemoryStatus,
    MemorySurfaceMode,
    MemoryStability,
    MemoryType,
    RecentContextSummary,
    normalize_iso_text,
    normalize_optional_text,
)
from app.memory.network import build_memory_network
from app.memory.resolver import MemoryResolver
from app.memory.purge_preview import (
    PurgePreviewTokenError,
    sign_purge_preview,
    verify_purge_preview,
)
from app.memory.review import MemoryReviewer
from app.memory.review_revision import (
    ReviewRevisionError,
    apply_review_revision,
    find_related_review_revision_memories,
    preview_review_revision,
)
from app.memory.report import (
    MemorySelectionConflict,
    build_obsidian_markdown_zip,
    build_memory_export,
    build_memory_selection_export,
    build_memory_report,
    format_memory_export,
    restore_memory_export,
)
from app.memory.redaction import (
    detect_text_sensitivity,
    redact_memory_payload,
    sensitivity_floor,
)
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    embedding_space_id_for,
    search_cache_stats,
)
from app.memory.store import (
    MemoryStore,
    PurgePreviewConflictError,
    RevisionConflictError,
)
from app.memory.utils import parse_embedding_vector
from app.usage.context import model_usage_scope

router = APIRouter(
    prefix="/memories",
    tags=["memories"],
    dependencies=[Depends(require_api_key)],
)

QUERY_MAX_CHARS = 4096
MEMORY_TEXT_MAX_CHARS = 65_536
NOTE_MAX_CHARS = 20_000
PUBLIC_ID_MAX_CHARS = 200
PublicId = Annotated[str, Field(min_length=1, max_length=PUBLIC_ID_MAX_CHARS)]

class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=QUERY_MAX_CHARS)
    limit: int = Field(default=8, ge=1, le=20)
    include_sensitive: bool = False
    redact_sensitive: bool = False

class MemorySurfaceRequest(BaseModel):
    limit: int = Field(default=8, ge=1, le=20)
    mode: MemorySurfaceMode = "balanced"
    include_archived: bool = False
    include_sensitive: bool = False
    redact_sensitive: bool = False

class MemoryNetworkRequest(BaseModel):
    limit: int = Field(default=80, ge=1, le=150)
    similarity_threshold: float = Field(default=0.42, ge=0.0, le=1.0)
    max_similarity_edges: int = Field(default=80, ge=0, le=200)
    space_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)
    type: MemoryType | None = None
    sensitivity: MemorySensitivity | None = None
    valence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    valence_max: float | None = Field(default=None, ge=0.0, le=1.0)
    arousal_min: float | None = Field(default=None, ge=0.0, le=1.0)
    arousal_max: float | None = Field(default=None, ge=0.0, le=1.0)
    redact_sensitive: bool = False

class MemoryNetworkTraverseRequest(BaseModel):
    seed_id: PublicId
    depth: int = Field(default=2, ge=1, le=3)
    limit: int = Field(default=10, ge=1, le=50)
    similarity_threshold: float = Field(default=0.42, ge=0.0, le=1.0)
    max_candidates: int = Field(default=500, ge=2, le=1000)
    max_edges: int = Field(default=1500, ge=0, le=5000)
    redact_sensitive: bool = False

class MemoryMergeRequest(BaseModel):
    memory_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=2,
        max_length=100,
    )
    content: str | None = Field(default=None, max_length=20_000)

class MemoryReviewRevisionPreviewRequest(BaseModel):
    memory_ids: list[PublicId] = Field(min_length=1, max_length=100)
    user_note: str = Field(min_length=1, max_length=NOTE_MAX_CHARS)
    recommendation_reason: str | None = Field(default=None, max_length=NOTE_MAX_CHARS)
    relation: MemoryRelation | None = None
    suggested_content: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_CHARS)
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity | None = None

class MemoryReviewRevisionRelatedRequest(BaseModel):
    memory_ids: list[PublicId] = Field(min_length=1, max_length=100)
    user_note: str = Field(min_length=1, max_length=NOTE_MAX_CHARS)
    recommendation_reason: str | None = Field(default=None, max_length=NOTE_MAX_CHARS)
    suggested_content: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_CHARS)
    limit: int = Field(default=8, ge=1, le=8)

class MemoryReviewRevisionApplyRequest(BaseModel):
    memory_ids: list[PublicId] = Field(min_length=1, max_length=100)
    operations: list[MemoryReviewRevisionOperation] = Field(min_length=1, max_length=100)
    preview_token: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX_CHARS)
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity | None = None

    @model_validator(mode="after")
    def validate_operation_resource_bounds(self):
        for operation in self.operations:
            if len(operation.reason) > NOTE_MAX_CHARS:
                raise ValueError("operation.reason 不能超过 20000 个字符")
            if operation.content is not None and len(operation.content) > MEMORY_TEXT_MAX_CHARS:
                raise ValueError("operation.content 不能超过 65536 个字符")
            if len(operation.memory_ids) > 100 or any(
                not memory_id or len(memory_id) > PUBLIC_ID_MAX_CHARS
                for memory_id in operation.memory_ids
            ):
                raise ValueError("operation.memory_ids 超出数量或 ID 长度限制")
            if operation.target_memory_id is not None and (
                not operation.target_memory_id
                or len(operation.target_memory_id) > PUBLIC_ID_MAX_CHARS
            ):
                raise ValueError("operation.target_memory_id 不能超过 200 个字符")
        return self

MemoryReviewGovernanceAction = Literal[
    "confirm_valid",
    "snooze",
    "lower_importance",
    "move_to_trash",
    "merge",
]

class MemoryReviewActionRequest(BaseModel):
    action: MemoryReviewGovernanceAction
    memory_ids: list[PublicId] = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=NOTE_MAX_CHARS)
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity | None = None
    review_after: str | None = None
    content: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_CHARS)

class MemoryUpdateRequest(BaseModel):
    content: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_CHARS)
    type: MemoryType | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    valence: float | None = Field(default=None, ge=0.0, le=1.0)
    arousal: float | None = Field(default=None, ge=0.0, le=1.0)
    stability: MemoryStability | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    review_after: str | None = None
    sensitivity: MemorySensitivity | None = None
    source_message: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_CHARS)
    source_conversation_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)
    topics: list[str] | None = None
    entities: list[str] | None = None
    temporal_subject: str | None = None
    temporal_predicate: str | None = None
    status: MemoryStatus | None = None
    preserve_metadata: bool = False
    expected_revision: int | None = Field(default=None, ge=1)

class MemorySpacesUpdateRequest(BaseModel):
    space_ids: list[str] = Field(default_factory=list)
    create_space_names: list[str] = Field(default_factory=list)
    expected_revision: int | None = Field(default=None, ge=1)


class MemorySpaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=7)
    description: str | None = Field(default=None, max_length=500)
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class MemorySpaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=7)
    description: str | None = Field(default=None, max_length=500)
    sort_order: int | None = Field(default=None, ge=0, le=9999)

class CoreMemoryUpdateRequest(BaseModel):
    content: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_CHARS)
    evidence_memory_ids: list[PublicId] | None = Field(default=None, max_length=1000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_revision: int | None = Field(default=None, ge=1)

class MemoryRestoreExportRequest(BaseModel):
    data: dict
    overwrite: bool = False
    include_deleted: bool = False
    dry_run: bool = False

class MemorySelectionExportRequest(BaseModel):
    memory_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1,
        max_length=1000,
    )

    @field_validator("memory_ids")
    @classmethod
    def normalize_unique_memory_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("memory_ids 不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("memory_ids 不能重复")
        return normalized

class MemoryPurgeRequest(BaseModel):
    confirm_memory_id: PublicId

class MemoryBatchPurgePreviewRequest(BaseModel):
    memory_ids: list[PublicId] = Field(min_length=1, max_length=1000)

    @field_validator("memory_ids")
    @classmethod
    def validate_unique_memory_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("memory_ids 不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("memory_ids 不能重复")
        return normalized

class MemoryBatchPurgeCommitRequest(MemoryBatchPurgePreviewRequest):
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    preview_token: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX_CHARS)

class MemoryIngestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX_CHARS)
    conversation_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)

class RecentContextUpsertRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)
    summary: str = Field(min_length=1, max_length=12000)

class MemorySaveRequest(BaseModel):
    """直接保存一条结构化记忆，对齐 MCP save_memory。"""
    content: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX_CHARS)
    type: MemoryType = "semantic"
    importance: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    valence: float = Field(default=0.5, ge=0.0, le=1.0)
    arousal: float = Field(default=0.3, ge=0.0, le=1.0)
    stability: MemoryStability = "stable"
    sensitivity: MemorySensitivity = "normal"
    source_quote: str = Field(default="", max_length=MEMORY_TEXT_MAX_CHARS)
    valid_from: str | None = None
    valid_until: str | None = None
    review_after: str | None = None
    temporal_subject: str | None = None
    temporal_predicate: str | None = None
    topics: list[str] | None = None
    entities: list[str] | None = None

class MemoryForgetRequest(BaseModel):
    """按自然语言搜索并批量软删除，对齐 MCP forget_memories。"""
    query: str = Field(default="", max_length=QUERY_MAX_CHARS)
    limit: int = Field(default=5, ge=1, le=10)

class MemoryContextRequest(BaseModel):
    """一站式上下文检索。"""
    query: str = Field(default="", max_length=QUERY_MAX_CHARS)
    include_core_memory: bool = True
    include_recent_context: bool = True
    search_limit: int = Field(default=5, ge=1, le=20)
    conversation_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)
    format: Literal["json", "markdown"] = "json"

class MemoryContextExplainRequest(BaseModel):
    """解释一次上下文召回，不记录使用次数。"""
    query: str = Field(default="", max_length=QUERY_MAX_CHARS)
    include_core_memory: bool = True
    include_recent_context: bool = True
    limit: int = Field(default=5, ge=1, le=20)
    conversation_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)
    include_sensitive: bool = False
    redact_sensitive: bool = False

MemorySearchFeedbackValue = Literal["useful", "not_useful", "wrong", "missing"]

class MemorySearchFeedbackRequest(BaseModel):
    query: str = Field(default="", max_length=QUERY_MAX_CHARS)
    memory_id: str | None = Field(default=None, max_length=PUBLIC_ID_MAX_CHARS)
    feedback: MemorySearchFeedbackValue
    note: str | None = Field(default=None, max_length=NOTE_MAX_CHARS)

class MemoryReEmbedRequest(BaseModel):
    """重新生成记忆 embedding。指定 memory_ids 或 scan 扫描缺失/无效/维度不匹配的 embedding。"""
    memory_ids: list[PublicId] | None = Field(default=None, max_length=1000)
    scan: bool = False
    include_sensitive: bool = False

class RecallEvalLabelRequest(BaseModel):
    id: PublicId
    # 不在此处限制 query 非空：交给 save_labels 的领域校验，给出带 label id 的友好提示
    # （否则刚新增、query 仍为空的标注会被 Pydantic 拦下并返回原始 422）。
    query: str = Field(default="", max_length=QUERY_MAX_CHARS)
    judgment: Literal["unlabeled", "relevant", "no_answer"] | None = None
    relevant_ids: list[PublicId] = Field(default_factory=list, max_length=1000)
    note: str | None = Field(default=None, max_length=NOTE_MAX_CHARS)

class RecallEvalLabelsRequest(BaseModel):
    labels: list[RecallEvalLabelRequest] = Field(default_factory=list)

class RecallEvalRunRequest(BaseModel):
    mode: Literal["keyword", "embedding"] = "keyword"
    k: int = Field(default=8, ge=1, le=MAX_RECALL_EVAL_K)

def _safe_download_filename_part(value: str) -> str:
    cleaned = "".join(
        character
        if character.isascii() and (character.isalnum() or character in {"-", "_"})
        else "-"
        for character in value
    ).strip("-_")
    return cleaned[:80] or "default"

def _purge_preview_http_conflict(exc: PurgePreviewConflictError) -> HTTPException:
    detail: dict[str, object] = {
        "code": exc.code,
        "message": exc.message,
    }
    if exc.missing_memory_ids:
        detail["missing_memory_ids"] = exc.missing_memory_ids
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

def _cleanup_eval_after_purge(
    *,
    settings: Settings,
    user_id: str,
) -> tuple[dict, list[str]]:
    try:
        return delete_user_eval_workspace(settings.eval_dir, user_id=user_id), []
    except Exception:
        # SQLite has already committed. Report the irreversible database result
        # truthfully and surface the independent filesystem cleanup failure.
        return (
            {
                "workspace_removed": False,
                "legacy_artifacts_removed": 0,
                "cleanup_failed": True,
            },
            [
                "记忆已永久删除，但本地评测工作区清理失败；"
                "请检查 EVAL_DIR 权限并手动清理残留评测文件。"
            ],
        )

def _memory_to_response(memory: MemoryRecord, *, redact_sensitive: bool = False) -> dict:
    payload = memory.model_dump(exclude={"embedding_json"})
    return redact_memory_payload(payload, redact_sensitive=redact_sensitive)

def _revision_etag(resource: str, resource_id: str, revision: int) -> str:
    identity = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()[:24]
    # Memory responses can be redacted without changing the persisted row.
    # A weak validator truthfully represents the shared underlying revision.
    return f'W/"{resource}:{identity}:r{revision}"'

def _core_memory_collection_etag(sections: list[CoreMemorySection]) -> str:
    fingerprint = hashlib.sha256(
        "\n".join(
            f"{section.id}:{section.revision}"
            for section in sorted(sections, key=lambda item: item.id)
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f'"core-memory:{fingerprint}"'

def _raise_revision_conflict(exc: RevisionConflictError) -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "revision_conflict",
            "resource": exc.resource,
            "resource_id": exc.resource_id,
            "expected_revision": exc.expected_revision,
            "current_revision": exc.current_revision,
            "message": "记录已被其他操作更新，请刷新后重试。",
        },
    ) from exc

def _surface_hit_to_dict(hit, *, redact_sensitive: bool = False) -> dict:
    payload = _memory_to_response(hit.memory, redact_sensitive=redact_sensitive)
    payload.update(
        {
            "final_score": hit.final_score,
            "activation_count": hit.activation_count,
            "last_active_at": hit.last_active_at,
            "freshness_bonus": hit.freshness_bonus,
            "surface_reason": hit.surface_reason,
            "surface_score": hit.surface_score,
            "surface_mode": hit.surface_mode,
            "surface_reason_text": hit.surface_reason_text,
            "life_score": hit.life_score,
            "days_since_last_active": hit.days_since_last_active,
            "review_signals": hit.review_signals,
        }
    )
    return payload

def _search_hit_to_dict(hit, *, redact_sensitive: bool = False) -> dict:
    payload = _memory_to_response(hit.memory, redact_sensitive=redact_sensitive)
    payload.update(
        {
            "relevance": hit.relevance,
            "channels": hit.channels,
            "topic_score": hit.topic_score,
            "total_score": hit.total_score,
            "final_score": hit.final_score,
            "activation_count": hit.activation_count,
            "last_active_at": hit.last_active_at,
            "freshness_bonus": hit.freshness_bonus,
            "score_breakdown": hit.score_breakdown,
        }
    )
    return payload

def _traversal_edge_to_dict(edge) -> dict:
    return {
        "source": edge.source,
        "target": edge.target,
        "kind": edge.kind,
        "weight": edge.weight,
        "label": edge.label,
    }

def _classification_payload(memory: MemoryRecord) -> dict:
    return {
        "topics": list(memory.topics),
        "entities": list(memory.entities),
        "space_ids": list(memory.space_ids),
    }

def _write_classification_log(
    *,
    store: MemoryStore,
    user_id: str,
    memory_id: str,
    before: dict,
    after: dict,
) -> None:
    store.create_decision_log(
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "classification_update",
                "memory_id": memory_id,
                "before": before,
                "after": after,
            },
            ensure_ascii=False,
        ),
        decision="update",
        reason="更新记忆分类标签",
    )

def _derive_context_search_query(
    *,
    query: str,
    user_id: str,
    store: MemoryStore,
    conversation_id: str | None,
) -> str:
    search_query = query.strip()
    if search_query:
        return search_query
    recent = store.get_recent_context_summary(
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if not recent or not recent.summary:
        return ""
    return _search_query_from_recent_summary(recent.summary)

def _search_query_from_recent_summary(summary: str) -> str:
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if not lines:
        return ""
    user_prefixes = ("\u7528\u6237\uff1a", "\u7528\u6237:", "User:")
    for line in reversed(lines):
        for prefix in user_prefixes:
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                if value:
                    return value
    return lines[-1]

def _recent_context_payload(
    *,
    store: MemoryStore,
    user_id: str,
    conversation_id: str | None,
    include_recent_context: bool,
) -> dict:
    if not include_recent_context:
        return {"found": False, "summary": ""}
    recent = store.get_recent_context_summary(
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if recent:
        return {"found": True, "summary": recent.summary}
    return {"found": False, "summary": ""}

def _safe_core_sections(*, store: MemoryStore, user_id: str) -> list:
    return safe_core_memory_sections(store=store, user_id=user_id)

def _load_review_action_memories(
    *,
    store: MemoryStore,
    user_id: str,
    memory_ids: list[str],
) -> list[MemoryRecord]:
    memories: list[MemoryRecord] = []
    for memory_id in _ordered_unique(memory_ids):
        memory = store.get_memory(memory_id=memory_id, user_id=user_id)
        if memory is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"记忆不存在或已删除：{memory_id}",
            )
        memories.append(memory)
    return memories

def _review_action_after_payload(
    *,
    store: MemoryStore,
    user_id: str,
    memory_ids: list[str],
) -> list[dict]:
    after: list[dict] = []
    for memory_id in memory_ids:
        memory = store.get_memory(memory_id=memory_id, user_id=user_id)
        if memory is None:
            after.append({"id": memory_id, "archived": True})
            continue
        after.append(_memory_audit_payload(memory))
    return after

def _memory_audit_payload(memory: MemoryRecord) -> dict:
    payload = memory.model_dump(exclude={"embedding_json", "source_message"})
    if memory.source_message:
        payload["source_message_length"] = len(memory.source_message)
        payload["source_message_sha256"] = hashlib.sha256(
            memory.source_message.encode("utf-8")
        ).hexdigest()

    text = "\n".join(
        part
        for part in (memory.content, memory.source_message, *memory.entities)
        if part
    )
    if memory.sensitivity == "normal" and detect_text_sensitivity(text) == "normal":
        return payload

    payload.pop("content", None)
    payload.pop("entities", None)
    payload["content_length"] = len(memory.content)
    payload["content_sha256"] = hashlib.sha256(
        memory.content.encode("utf-8")
    ).hexdigest()
    payload["redacted"] = True
    return payload

def _affected_core_sections_for_memory_ids(
    *,
    store: MemoryStore,
    user_id: str,
    memory_ids: list[str],
) -> list:
    touched = set(memory_ids)
    if not touched:
        return []
    return [
        section
        for section in store.list_core_memory_sections(user_id=user_id)
        if touched & set(section.evidence_memory_ids)
    ]

def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

def _find_memories_needing_embedding(
    *,
    store: MemoryStore,
    user_id: str,
    expected_dimensions: int,
    expected_space_id: str,
) -> list[str]:
    """扫描缺失、损坏、维度错误或不属于当前空间的记忆向量。"""
    memory_ids: list[str] = []
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT id, embedding_json, embedding_space_id
            FROM memories
            WHERE user_id = ? AND archived = 0
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    for row in rows:
        raw = row["embedding_json"]
        if not raw or row["embedding_space_id"] != expected_space_id:
            memory_ids.append(row["id"])
            continue
        vector = parse_embedding_vector(raw)
        if vector is None:
            memory_ids.append(row["id"])
            continue
        if len(vector) != expected_dimensions:
            memory_ids.append(row["id"])
            continue
    return memory_ids

# Domain route modules use ``from .common import *``. Include private helpers
# (``_memory_to_response`` etc.) so star-import does not hide them.
__all__ = [name for name in globals() if not name.startswith("__")]
