from datetime import UTC, datetime, timedelta
from functools import partial
import hashlib
import json
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_settings
from app.api.deps import (
    get_embedding_client,
    get_llm_client,
    get_memory_search_service,
    get_memory_store,
    get_user_id,
    require_api_key,
)
from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import (
    render_core_memory_context,
    render_memory_context,
    render_recent_context_summary_context,
)
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
from app.memory.extractor import (
    detect_text_sensitivity,
    sensitivity_floor,
    validate_candidate_for_save,
)
from app.memory.graph_traverse import traverse_memory_network
from app.memory.health import MemoryHealthChecker, _embedding_vector
from app.memory.ingest import MemoryIngestService
from app.memory.models import (
    CandidateMemory,
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
from app.memory.review import MemoryReviewer
from app.memory.review_revision import (
    ReviewRevisionError,
    apply_review_revision,
    find_related_review_revision_memories,
    preview_review_revision,
)
from app.memory.report import (
    build_obsidian_markdown_zip,
    build_memory_export,
    build_memory_report,
    format_memory_export,
    restore_memory_export,
)
from app.memory.redaction import redact_memory_payload
from app.memory.search import EmbeddingClient, MemorySearchService, NullEmbeddingClient
from app.memory.store import MemoryStore

router = APIRouter(
    prefix="/memories",
    tags=["memories"],
    dependencies=[Depends(require_api_key)],
)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)
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
    space_id: str | None = None
    type: MemoryType | None = None
    sensitivity: MemorySensitivity | None = None
    valence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    valence_max: float | None = Field(default=None, ge=0.0, le=1.0)
    arousal_min: float | None = Field(default=None, ge=0.0, le=1.0)
    arousal_max: float | None = Field(default=None, ge=0.0, le=1.0)
    redact_sensitive: bool = False


class MemoryNetworkTraverseRequest(BaseModel):
    seed_id: str = Field(min_length=1)
    depth: int = Field(default=2, ge=1, le=3)
    limit: int = Field(default=10, ge=1, le=50)
    similarity_threshold: float = Field(default=0.42, ge=0.0, le=1.0)
    max_candidates: int = Field(default=500, ge=2, le=1000)
    max_edges: int = Field(default=1500, ge=0, le=5000)
    redact_sensitive: bool = False


class MemoryMergeRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=2)
    content: str | None = None


class MemoryReviewRevisionPreviewRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=1)
    user_note: str = Field(min_length=1)
    recommendation_reason: str | None = None
    relation: MemoryRelation | None = None
    suggested_content: str | None = None
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity | None = None


class MemoryReviewRevisionRelatedRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=1)
    user_note: str = Field(min_length=1)
    recommendation_reason: str | None = None
    suggested_content: str | None = None
    limit: int = Field(default=8, ge=1, le=8)


class MemoryReviewRevisionApplyRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=1)
    operations: list[MemoryReviewRevisionOperation] = Field(min_length=1)
    preview_token: str = Field(min_length=1)
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity | None = None


MemoryReviewGovernanceAction = Literal[
    "confirm_valid",
    "snooze",
    "lower_importance",
    "move_to_trash",
    "merge",
]


class MemoryReviewActionRequest(BaseModel):
    action: MemoryReviewGovernanceAction
    memory_ids: list[str] = Field(min_length=1)
    reason: str | None = None
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity | None = None
    review_after: str | None = None
    content: str | None = None


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
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
    source_message: str | None = None
    source_conversation_id: str | None = None
    topics: list[str] | None = None
    entities: list[str] | None = None
    temporal_subject: str | None = None
    temporal_predicate: str | None = None
    status: MemoryStatus | None = None


class MemorySpacesUpdateRequest(BaseModel):
    space_ids: list[str] = Field(default_factory=list)
    create_space_names: list[str] = Field(default_factory=list)


class MemoryRestoreExportRequest(BaseModel):
    data: dict
    overwrite: bool = False
    include_deleted: bool = False


class MemoryPurgeRequest(BaseModel):
    confirm_memory_id: str = Field(min_length=1)


class MemoryIngestRequest(BaseModel):
    text: str = Field(min_length=1)
    conversation_id: str | None = None


class RecentContextUpsertRequest(BaseModel):
    conversation_id: str | None = None
    summary: str = Field(min_length=1, max_length=12000)


class MemorySaveRequest(BaseModel):
    """直接保存一条结构化记忆，对齐 MCP save_memory。"""
    content: str = Field(min_length=1)
    type: MemoryType = "semantic"
    importance: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    valence: float = Field(default=0.5, ge=0.0, le=1.0)
    arousal: float = Field(default=0.3, ge=0.0, le=1.0)
    stability: MemoryStability = "stable"
    sensitivity: MemorySensitivity = "normal"
    source_quote: str = ""
    valid_from: str | None = None
    valid_until: str | None = None
    review_after: str | None = None
    temporal_subject: str | None = None
    temporal_predicate: str | None = None
    topics: list[str] | None = None
    entities: list[str] | None = None


class MemoryForgetRequest(BaseModel):
    """按自然语言搜索并批量软删除，对齐 MCP forget_memories。"""
    query: str = ""
    limit: int = Field(default=5, ge=1, le=10)


class MemoryContextRequest(BaseModel):
    """一站式上下文检索。"""
    query: str = ""
    include_core_memory: bool = True
    include_recent_context: bool = True
    search_limit: int = Field(default=5, ge=1, le=20)
    conversation_id: str | None = None
    format: Literal["json", "markdown"] = "json"


class MemoryContextExplainRequest(BaseModel):
    """解释一次上下文召回，不记录使用次数。"""
    query: str = ""
    include_core_memory: bool = True
    include_recent_context: bool = True
    limit: int = Field(default=5, ge=1, le=20)
    conversation_id: str | None = None
    include_sensitive: bool = False
    redact_sensitive: bool = False


MemorySearchFeedbackValue = Literal["useful", "not_useful", "wrong", "missing"]


class MemorySearchFeedbackRequest(BaseModel):
    query: str = ""
    memory_id: str | None = None
    feedback: MemorySearchFeedbackValue
    note: str | None = None


class MemoryReEmbedRequest(BaseModel):
    """重新生成记忆 embedding。指定 memory_ids 或 scan 扫描缺失/无效/维度不匹配的 embedding。"""
    memory_ids: list[str] | None = None
    scan: bool = False
    include_sensitive: bool = False


class RecallEvalLabelRequest(BaseModel):
    id: str = Field(min_length=1)
    # 不在此处限制 query 非空：交给 save_labels 的领域校验，给出带 label id 的友好提示
    # （否则刚新增、query 仍为空的标注会被 Pydantic 拦下并返回原始 422）。
    query: str = ""
    judgment: Literal["unlabeled", "relevant", "no_answer"] | None = None
    relevant_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class RecallEvalLabelsRequest(BaseModel):
    labels: list[RecallEvalLabelRequest] = Field(default_factory=list)


class RecallEvalRunRequest(BaseModel):
    mode: Literal["keyword", "embedding"] = "keyword"
    k: int = Field(default=8, ge=1, le=MAX_RECALL_EVAL_K)


@router.get("")
def list_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    redact_sensitive: bool = False,
    status_filter: Annotated[MemoryStatus | Literal["all"] | None, Query(alias="status")] = None,
) -> dict[str, list[dict]]:
    memories = store.list_memories(user_id=user_id, status=status_filter)
    return {
        "data": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ]
    }


@router.get("/deleted")
def list_deleted_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=200, ge=1, le=1000),
    redact_sensitive: bool = False,
) -> dict[str, list[dict]]:
    memories = store.list_archived_memories(user_id=user_id, limit=limit)
    return {
        "data": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ]
    }


@router.get("/spaces")
def list_memory_spaces(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    return {"data": store.list_memory_space_summaries(user_id=user_id)}


@router.get("/spaces/{space_id}")
def get_memory_space(
    space_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=200, ge=1, le=1000),
    redact_sensitive: bool = False,
) -> dict:
    space = store.get_memory_space(user_id=user_id, space_id=space_id)
    if space is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="空间不存在或已归档",
        )
    memories = store.list_memories_for_space(
        user_id=user_id,
        space_id=space_id,
        limit=limit,
    )
    return {
        "space": space.model_dump(),
        "memories": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ],
    }


@router.get("/report", response_model=None)
def get_memory_report(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    response_format: Literal["json", "markdown"] = Query(default="json", alias="format"),
) -> dict | PlainTextResponse:
    report = build_memory_report(store=store, user_id=user_id)
    if response_format == "markdown":
        return PlainTextResponse(report["markdown"], media_type="text/markdown")
    return report


@router.get("/export", response_model=None)
def export_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    include_deleted: bool = True,
    response_format: Literal["json", "markdown", "obsidian_markdown"] = Query(
        default="json",
        alias="format",
    ),
) -> dict | PlainTextResponse | Response:
    export_data = build_memory_export(
        store=store,
        user_id=user_id,
        include_deleted=include_deleted,
    )
    if response_format == "markdown":
        return PlainTextResponse(
            format_memory_export(export_data),
            media_type="text/markdown",
        )
    if response_format == "obsidian_markdown":
        filename = f"memory-obsidian-export-{_safe_download_filename_part(user_id)}.zip"
        return Response(
            build_obsidian_markdown_zip(export_data),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return export_data


@router.post("/restore")
def restore_memories_from_export(
    body: MemoryRestoreExportRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    return restore_memory_export(
        store=store,
        user_id=user_id,
        export_data=body.data,
        overwrite=body.overwrite,
        include_deleted=body.include_deleted,
    )


def _safe_download_filename_part(value: str) -> str:
    cleaned = "".join(
        character
        if character.isascii() and (character.isalnum() or character in {"-", "_"})
        else "-"
        for character in value
    ).strip("-_")
    return cleaned[:80] or "default"


@router.get("/decision-logs")
def list_decision_logs(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    conversation_id: str | None = None,
    memory_id: str | None = None,
    limit: int = 100,
) -> dict[str, list[dict]]:
    logs = store.list_decision_logs(
        user_id=user_id,
        conversation_id=conversation_id,
        memory_id=memory_id,
        limit=limit,
    )
    return {"data": [log.model_dump() for log in logs]}


@router.get("/timeline")
def get_memory_timeline(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    subject: Annotated[str, Query(min_length=1)],
    predicate: str | None = None,
    include_archived: bool = False,
    redact_sensitive: bool = False,
) -> dict[str, object]:
    memories = store.list_memory_timeline(
        user_id=user_id,
        subject=subject,
        predicate=predicate,
        include_archived=include_archived,
    )
    return {
        "subject": normalize_optional_text(subject),
        "predicate": normalize_optional_text(predicate),
        "data": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ],
    }


@router.get("/recent-context")
def list_recent_context_summaries(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, list[dict]]:
    summaries = store.list_recent_context_summaries(user_id=user_id, limit=limit)
    return {"data": [summary.model_dump() for summary in summaries]}


@router.post("/recent-context")
def upsert_recent_context_summary(
    body: RecentContextUpsertRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, dict]:
    summary_text = body.summary.strip()
    if not summary_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="summary must not be empty",
        )
    conversation_id = body.conversation_id.strip() if body.conversation_id else None
    summary = store.upsert_recent_context_summary(
        user_id=user_id,
        conversation_id=conversation_id or None,
        summary=summary_text,
    )
    return {"data": summary.model_dump()}


@router.get("/core")
def list_core_memory(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    sections = store.list_core_memory_sections(user_id=user_id)
    return {"data": [section.model_dump() for section in sections]}


@router.get("/core/history")
def list_core_memory_history(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    section: CoreMemorySectionName | None = None,
    limit: int = 50,
) -> dict[str, list[dict]]:
    history = store.list_core_memory_section_history(
        user_id=user_id,
        section=section,
        limit=limit,
    )
    return {"data": [item.model_dump() for item in history]}


@router.post("/core/consolidate")
async def consolidate_core_memory(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
) -> dict:
    consolidator = CoreMemoryConsolidator(store=store, llm_client=llm_client)
    result = await consolidator.consolidate(user_id=user_id)
    return result.model_dump()


@router.post("/search")
async def search_memories(
    body: MemorySearchRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict[str, list[dict]]:
    hits = await search_service.search_hits(
        query=body.query,
        user_id=user_id,
        limit=body.limit,
        include_sensitive=body.include_sensitive,
    )
    return {
        "data": [
            _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
            for hit in hits
        ]
    }


@router.post("/surface")
def surface_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
    body: MemorySurfaceRequest | None = None,
) -> dict[str, list[dict]]:
    hits = search_service.surface_memories(
        user_id=user_id,
        limit=body.limit if body else 8,
        mode=body.mode if body else "balanced",
        include_archived=body.include_archived if body else False,
        include_sensitive=body.include_sensitive if body else False,
    )
    return {
        "data": [
            _surface_hit_to_dict(
                hit,
                redact_sensitive=body.redact_sensitive if body else False,
            )
            for hit in hits
        ]
    }


@router.post("/network")
def memory_network(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    body: MemoryNetworkRequest | None = None,
) -> dict:
    request = body or MemoryNetworkRequest()
    return build_memory_network(
        store=store,
        user_id=user_id,
        limit=request.limit,
        similarity_threshold=request.similarity_threshold,
        max_similarity_edges=request.max_similarity_edges,
        space_id=request.space_id,
        memory_type=request.type,
        sensitivity=request.sensitivity,
        valence_min=request.valence_min,
        valence_max=request.valence_max,
        arousal_min=request.arousal_min,
        arousal_max=request.arousal_max,
        redact_sensitive=request.redact_sensitive,
    )


@router.post("/network/traverse")
def memory_network_traverse(
    body: MemoryNetworkTraverseRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    result = traverse_memory_network(
        store=store,
        user_id=user_id,
        seed_id=body.seed_id.strip(),
        depth=body.depth,
        limit=body.limit,
        similarity_threshold=body.similarity_threshold,
        max_candidates=body.max_candidates,
        max_edges=body.max_edges,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="seed memory not found or archived",
        )
    return {
        "seed": _memory_to_response(
            result.seed,
            redact_sensitive=body.redact_sensitive,
        ),
        "results": [
            {
                "memory": _memory_to_response(
                    item.memory,
                    redact_sensitive=body.redact_sensitive,
                ),
                "score": round(item.score, 6),
                "depth": item.depth,
                "path": [_traversal_edge_to_dict(edge) for edge in item.path],
            }
            for item in result.results
        ],
        "meta": {
            "depth": result.meta.depth,
            "limit": result.meta.limit,
            "similarity_threshold": result.meta.similarity_threshold,
            "candidate_count": result.meta.candidate_count,
            "edge_count": result.meta.edge_count,
            "reachable_count": result.meta.reachable_count,
            "iterations": result.meta.iterations,
            "converged": result.meta.converged,
        },
    }


@router.post("/ingest")
async def ingest_memory_text(
    body: MemoryIngestRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    ingester = MemoryIngestService(
        store=store,
        embedding_client=embedding_client,
        llm_client=llm_client,
        allow_sensitive_egress=settings.allow_sensitive_egress,
    )
    result = await ingester.ingest(
        user_id=user_id,
        text=body.text,
        conversation_id=body.conversation_id,
        source="rest_ingest",
    )
    return result.model_dump()


@router.post("/merge")
def merge_memories(
    body: MemoryMergeRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    result = store.merge_memories(
        user_id=user_id,
        memory_ids=body.memory_ids,
        content=body.content,
    )
    payload = result.model_dump()
    if result.memory:
        payload["memory"] = result.memory.model_dump(exclude={"embedding_json"})
    return payload


@router.post("/review")
def review_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = 200,
) -> dict:
    reviewer = MemoryReviewer(store=store)
    return reviewer.review(user_id=user_id, limit=limit).model_dump()


@router.get("/health")
def memory_database_health(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    checker = MemoryHealthChecker(
        store=store,
        expected_embedding_dimensions=settings.embedding_dimensions,
        embedding_enabled=bool(settings.embedding_api_key),
    )
    return checker.check(user_id=user_id).model_dump()


@router.get("/evaluation/diagnosis")
def memory_evaluation_diagnosis(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    result = run_diagnosis(settings.database_path, user_id=user_id)
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result["error"]),
        )
    return result


@router.post("/evaluation/recall/init")
def memory_evaluation_recall_init(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    result = init_eval(
        source_db=settings.database_path,
        eval_dir=settings.eval_dir,
        user_id=user_id,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(result["error"]),
        )
    return result


@router.get("/evaluation/recall/workbench")
def memory_evaluation_recall_workbench(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    redact_sensitive: bool = Query(default=True),
) -> dict:
    try:
        return build_recall_workbench(
            eval_dir=settings.eval_dir,
            user_id=user_id,
            redact_sensitive=redact_sensitive,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.put("/evaluation/recall/labels")
def memory_evaluation_recall_labels(
    body: RecallEvalLabelsRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return save_labels(
            eval_dir=settings.eval_dir,
            user_id=user_id,
            labels=[label.model_dump() for label in body.labels],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/evaluation/recall/run")
def memory_evaluation_recall_run(
    body: RecallEvalRunRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
) -> dict:
    if body.mode == "embedding" and isinstance(embedding_client, NullEmbeddingClient):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未配置 embedding 服务，无法运行语义召回评测",
        )
    try:
        return run_recall_eval(
            eval_dir=settings.eval_dir,
            user_id=user_id,
            mode=body.mode,
            k=body.k,
            embedding_client=embedding_client,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/re-embed")
async def re_embed_memories(
    body: MemoryReEmbedRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """重新生成记忆的 embedding 向量。

    支持两种模式：
    - 指定 memory_ids：对列表中每条活跃记忆重新生成 embedding
    - scan=true：扫描缺失/无效/维度不匹配的活跃记忆并重新生成
    """
    if isinstance(embedding_client, NullEmbeddingClient):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未配置 embedding 服务，无法重新生成 embedding",
        )

    memory_ids: list[str] = []
    if body.memory_ids:
        memory_ids = list(dict.fromkeys(body.memory_ids))  # 去重保序
        if not memory_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="memory_ids 不能为空列表",
            )
    elif body.scan:
        memory_ids = _find_memories_needing_embedding(
            store=store,
            user_id=user_id,
            expected_dimensions=settings.embedding_dimensions,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="请指定 memory_ids 或设置 scan=true",
        )

    if not memory_ids:
        return {"re_embedded": 0, "memory_ids": [], "failed_ids": []}

    succeeded: list[str] = []
    failed: list[str] = []
    for memory_id in memory_ids:
        memory = store.get_memory(memory_id=memory_id, user_id=user_id)
        if memory is None:
            failed.append(memory_id)
            continue
        if memory.sensitivity != "normal" and not body.include_sensitive:
            failed.append(memory_id)
            continue
        embedding = await embedding_client.embed(memory.content)
        if embedding is None:
            failed.append(memory_id)
            continue
        embedding_json = json.dumps(embedding, ensure_ascii=False)
        if store.update_memory_embedding(
            memory_id=memory_id,
            user_id=user_id,
            embedding_json=embedding_json,
        ):
            succeeded.append(memory_id)
        else:
            failed.append(memory_id)

    return {
        "re_embedded": len(succeeded),
        "memory_ids": succeeded,
        "failed_ids": failed,
    }


@router.post("/archive-expired")
def archive_expired_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    """归档所有 valid_until 已过期的活跃记忆。

    valid_until 会按 ISO 8601 解析并统一比较实际时刻，支持不同时区偏移。
    """
    count = store.archive_expired_memories(user_id=user_id)
    return {"archived": count}


@router.post("/review/actions")
def apply_memory_review_action(
    body: MemoryReviewActionRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memories = _load_review_action_memories(
        store=store,
        user_id=user_id,
        memory_ids=body.memory_ids,
    )
    affected_core_sections = _affected_core_sections_for_memory_ids(
        store=store,
        user_id=user_id,
        memory_ids=[memory.id for memory in memories],
    )
    before = [_memory_audit_payload(memory) for memory in memories]
    default_review_after = (datetime.now(UTC) + timedelta(days=15)).isoformat()
    review_after = body.review_after or default_review_after
    results: list[dict] = []

    if body.action in {"confirm_valid", "snooze"}:
        for memory in memories:
            updated = store.update_memory(
                memory_id=memory.id,
                user_id=user_id,
                content=memory.content,
                type=memory.type,
                importance=memory.importance,
                confidence=memory.confidence,
                valence=memory.valence,
                arousal=memory.arousal,
                source_message=memory.source_message,
                source_conversation_id=memory.source_conversation_id,
                embedding_json=memory.embedding_json,
                stability=memory.stability,
                valid_from=memory.valid_from,
                valid_until=memory.valid_until,
                review_after=review_after,
                sensitivity=memory.sensitivity,
                evidence_memory_ids=memory.evidence_memory_ids,
                temporal_subject=memory.temporal_subject,
                temporal_predicate=memory.temporal_predicate,
            )
            if updated is not None:
                results.append(
                    {
                        "operation": body.action,
                        "memory_id": updated.id,
                        "memory": updated.model_dump(exclude={"embedding_json"}),
                    }
                )
        decision = "ignore" if body.action == "snooze" else "update"
        audit_reason = body.reason or (
            "体检建议已稍后提醒" if body.action == "snooze" else "体检建议已确认仍有效"
        )
    elif body.action == "lower_importance":
        for memory in memories:
            updated = store.update_memory(
                memory_id=memory.id,
                user_id=user_id,
                content=memory.content,
                type=memory.type,
                importance=max(1, memory.importance - 1),
                confidence=memory.confidence,
                valence=memory.valence,
                arousal=memory.arousal,
                source_message=memory.source_message,
                source_conversation_id=memory.source_conversation_id,
                embedding_json=memory.embedding_json,
                stability=memory.stability,
                valid_from=memory.valid_from,
                valid_until=memory.valid_until,
                review_after=memory.review_after,
                sensitivity=memory.sensitivity,
                evidence_memory_ids=memory.evidence_memory_ids,
                temporal_subject=memory.temporal_subject,
                temporal_predicate=memory.temporal_predicate,
            )
            if updated is not None:
                results.append(
                    {
                        "operation": "lower_importance",
                        "memory_id": updated.id,
                        "memory": updated.model_dump(exclude={"embedding_json"}),
                    }
                )
        decision = "update"
        audit_reason = body.reason or "体检建议已降低记忆重要度"
    elif body.action == "move_to_trash":
        archived_ids: list[str] = []
        for memory in memories:
            if store.archive_memory(memory_id=memory.id, user_id=user_id):
                archived_ids.append(memory.id)
        results.append({"operation": "move_to_trash", "archived_memory_ids": archived_ids})
        decision = "update"
        audit_reason = body.reason or "体检建议已移入回收站"
    elif body.action == "merge":
        result = store.merge_memories(
            user_id=user_id,
            memory_ids=[memory.id for memory in memories],
            content=body.content,
        )
        if result.memory is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=result.reason,
            )
        results.append(
            {
                "operation": "merge",
                "memory_id": result.memory.id,
                "memory": result.memory.model_dump(exclude={"embedding_json"}),
                "archived_memory_ids": result.archived_memory_ids,
            }
        )
        decision = "update"
        audit_reason = body.reason or "体检建议已合并记忆"

    after = _review_action_after_payload(
        store=store,
        user_id=user_id,
        memory_ids=[memory.id for memory in memories],
    )
    store.create_decision_log(
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "review_action",
                "action": body.action,
                "memory_ids": [memory.id for memory in memories],
                "risk_tags": body.risk_tags,
                "severity": body.severity,
                "before": before,
                "after": after,
            },
            ensure_ascii=False,
        ),
        decision=decision,
        reason=audit_reason,
    )
    return {
        "applied": True,
        "action": body.action,
        "results": results,
        "affected_core_sections": [
            section.model_dump() for section in affected_core_sections
        ],
    }


@router.post("/review/revise/preview")
async def preview_memory_review_revision(
    body: MemoryReviewRevisionPreviewRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    if not settings.allow_sensitive_egress:
        selected = [
            store.get_memory(memory_id=memory_id, user_id=user_id)
            for memory_id in body.memory_ids
        ]
        review_text = "\n".join(
            part
            for part in (
                body.user_note,
                body.recommendation_reason or "",
                body.suggested_content or "",
                *(
                    text
                    for memory in selected
                    if memory is not None
                    for text in (
                        memory.content,
                        memory.source_message or "",
                        *memory.entities,
                    )
                ),
            )
            if part
        )
        if any(memory and memory.sensitivity != "normal" for memory in selected) or (
            detect_text_sensitivity(review_text) != "normal"
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "敏感内容未发送给远程体检模型；如确需处理，请显式启用 "
                    "ALLOW_SENSITIVE_EGRESS"
                ),
            )
    try:
        preview = await preview_review_revision(
            user_id=user_id,
            store=store,
            llm_client=llm_client,
            secret=settings.gateway_api_key,
            memory_ids=body.memory_ids,
            user_note=body.user_note,
            recommendation_reason=body.recommendation_reason,
            relation=body.relation,
            suggested_content=body.suggested_content,
            risk_tags=body.risk_tags,
            severity=body.severity,
        )
    except ReviewRevisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return preview.model_dump()


@router.post("/review/revise/related")
async def related_memory_review_revision(
    body: MemoryReviewRevisionRelatedRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict:
    try:
        return await find_related_review_revision_memories(
            user_id=user_id,
            store=store,
            search_service=search_service,
            memory_ids=body.memory_ids,
            user_note=body.user_note,
            recommendation_reason=body.recommendation_reason,
            suggested_content=body.suggested_content,
            limit=body.limit,
        )
    except ReviewRevisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/review/revise/apply")
def apply_memory_review_revision(
    body: MemoryReviewRevisionApplyRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return apply_review_revision(
            user_id=user_id,
            store=store,
            secret=settings.gateway_api_key,
            memory_ids=body.memory_ids,
            operations=body.operations,
            preview_token=body.preview_token,
            risk_tags=body.risk_tags,
            severity=body.severity,
        )
    except ReviewRevisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("")
async def save_memory(
    body: MemorySaveRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
) -> dict:
    """直接保存一条结构化记忆，跳过 LLM 提取。对齐 MCP save_memory。"""
    try:
        candidate = CandidateMemory(
            action="create",
            memory=body.content.strip(),
            type=body.type,
            importance=body.importance,
            confidence=body.confidence,
            valence=body.valence,
            arousal=body.arousal,
            stability=body.stability,
            sensitivity=sensitivity_floor(
                body.sensitivity,
                body.content,
                body.source_quote,
                *(body.entities or []),
            ),
            source_quote=body.source_quote.strip(),
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            review_after=body.review_after,
            temporal_subject=body.temporal_subject,
            temporal_predicate=body.temporal_predicate,
            topics=body.topics or [],
            entities=body.entities or [],
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        field = ".".join(str(p) for p in first_error.get("loc", ()))
        return {"action": "ignore", "reason": f"参数不合法（字段 {field}）"}

    rejection = validate_candidate_for_save(candidate)
    if rejection:
        return {"action": "ignore", "reason": rejection}

    resolver = MemoryResolver(store=store, embedding_client=embedding_client)
    fields_set = getattr(body, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(body, "__fields_set__", set())
    explicit_classification = bool({"topics", "entities"} & set(fields_set))
    result = await resolver.resolve(
        user_id=user_id,
        candidate=candidate,
        source_message=candidate.source_quote,
        conversation_id=None,
        auto_classify=not explicit_classification,
    )
    return {
        "action": result.action,
        "relation": result.relation,
        "reason": result.reason,
        "memory_id": result.memory.id if result.memory else None,
    }


@router.post("/forget")
async def forget_memories(
    body: MemoryForgetRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict:
    """按自然语言搜索并批量软删除。对齐 MCP forget_memories。"""
    normalized_query = body.query.strip()
    if not normalized_query:
        return {"deleted_count": 0, "deleted": [], "query": body.query}

    matches = await search_service.search(
        query=normalized_query,
        user_id=user_id,
        limit=body.limit,
        record_usage=False,
    )

    def _archive_matches() -> list[dict]:
        archived: list[dict] = []
        for memory in matches:
            if store.archive_memory(memory_id=memory.id, user_id=user_id):
                archived.append(memory.model_dump(exclude={"embedding_json"}))
        return archived

    deleted = await anyio.to_thread.run_sync(_archive_matches)
    return {
        "deleted_count": len(deleted),
        "deleted": deleted,
        "query": normalized_query,
    }


@router.post("/context", response_model=None)
async def get_memory_context(
    body: MemoryContextRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict | PlainTextResponse:
    """一站式上下文检索：核心记忆 + RAG 检索 + 近期上下文。"""
    core_sections: list = []
    search_results: list = []
    search_results_raw: list = []
    recent_context: dict = {"found": False, "summary": ""}

    search_query = await anyio.to_thread.run_sync(
        partial(
            _derive_context_search_query,
            query=body.query,
            user_id=user_id,
            store=store,
            conversation_id=body.conversation_id,
        )
    )

    if search_query and body.include_core_memory is not False:
        core_sections = await anyio.to_thread.run_sync(
            partial(_safe_core_sections, store=store, user_id=user_id)
        )

    if search_query:
        search_results_raw = await search_service.search(
            query=search_query,
            user_id=user_id,
            limit=body.search_limit,
            record_usage=False,
        )
        search_results = [
            m.model_dump(exclude={"embedding_json"}) for m in search_results_raw
        ]
    elif body.include_core_memory:
        core_sections = await anyio.to_thread.run_sync(
            partial(_safe_core_sections, store=store, user_id=user_id)
        )

    if body.include_recent_context:
        recent = await anyio.to_thread.run_sync(
            partial(
                store.get_recent_context_summary,
                user_id=user_id,
                conversation_id=body.conversation_id,
            )
        )
        if recent:
            recent_context = {"found": True, "summary": recent.summary}

    if body.format == "markdown":
        core_md = render_core_memory_context(core_sections) if core_sections else ""
        recent_md = ""
        if recent_context["found"]:
            recent_obj = RecentContextSummary(
                id="", user_id=user_id, conversation_id=body.conversation_id,
                summary=recent_context["summary"],
                created_at="", updated_at="", archived=0,
            )
            recent_md = render_recent_context_summary_context(recent_obj)
        search_md = ""
        if search_results_raw:
            search_md = render_memory_context(search_results_raw)
        blocks = [b for b in (core_md, recent_md, search_md) if b]
        return PlainTextResponse("\n\n".join(blocks), media_type="text/markdown")

    return {
        "core_memory": [s.model_dump() for s in core_sections] if core_sections else [],
        "search_results": search_results,
        "recent_context": recent_context,
    }


@router.post("/context/explain")
async def explain_memory_context(
    body: MemoryContextExplainRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict:
    """解释上下文构建过程；该调试接口不会增加 usage_count。"""
    search_query = await anyio.to_thread.run_sync(
        partial(
            _derive_context_search_query,
            query=body.query,
            user_id=user_id,
            store=store,
            conversation_id=body.conversation_id,
        )
    )
    core_sections = (
        await anyio.to_thread.run_sync(
            partial(_safe_core_sections, store=store, user_id=user_id)
        )
        if body.include_core_memory
        else []
    )
    core_payload = [section.model_dump() for section in core_sections]
    recent_context = await anyio.to_thread.run_sync(
        partial(
            _recent_context_payload,
            store=store,
            user_id=user_id,
            conversation_id=body.conversation_id,
            include_recent_context=body.include_recent_context,
        )
    )

    search_hits = []
    if search_query:
        explain_limit = min(20, max(body.limit + 5, body.limit * 2))
        search_hits = await search_service.search_hits(
            query=search_query,
            user_id=user_id,
            limit=explain_limit,
            record_usage=False,
            include_sensitive=body.include_sensitive,
        )

    selected_hits = search_hits[: body.limit]
    search_results = [
        _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
        for hit in selected_hits
    ]
    candidate_pool = [
        _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
        for hit in search_hits
    ]
    excluded_candidates: list[dict] = []
    for hit in search_hits[body.limit:]:
        payload = _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
        payload["excluded_reason"] = "rank_below_limit"
        excluded_candidates.append(payload)

    context_package = {
        "query": search_query,
        "core_memory": core_payload,
        "search_results": search_results,
        "recent_context": recent_context,
    }
    return {
        "context_package": context_package,
        "core_memory": core_payload,
        "search_results": search_results,
        "recent_context": recent_context,
        "candidate_pool": candidate_pool,
        "excluded_candidates": excluded_candidates,
    }


@router.post("/search-feedback")
def create_search_feedback(
    body: MemorySearchFeedbackRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memory_id = body.memory_id.strip() if body.memory_id else None
    if body.feedback != "missing" and not memory_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="memory_id 是必填项",
        )
    if memory_id and store.get_memory(memory_id=memory_id, user_id=user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )

    log = store.create_decision_log(
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "search_feedback",
                "query": body.query.strip(),
                "memory_id": memory_id,
                "feedback": body.feedback,
                "note": (body.note or "").strip(),
            },
            ensure_ascii=False,
        ),
        decision="ignore",
        reason=f"召回反馈：{body.feedback}",
    )
    return {"recorded": True, "log": log.model_dump()}


@router.patch("/{memory_id}/spaces")
def update_memory_spaces(
    memory_id: str,
    body: MemorySpacesUpdateRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    existing = store.get_memory(memory_id=memory_id, user_id=user_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    before = _classification_payload(existing)
    try:
        memory = store.replace_memory_spaces(
            memory_id=memory_id,
            user_id=user_id,
            space_ids=body.space_ids,
            create_space_names=body.create_space_names,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    after = _classification_payload(memory)
    if before != after:
        _write_classification_log(
            store=store,
            user_id=user_id,
            memory_id=memory_id,
            before=before,
            after=after,
        )
    return {"updated": True, "memory": memory.model_dump(exclude={"embedding_json"})}


@router.patch("/{memory_id}")
def update_memory(
    memory_id: str,
    body: MemoryUpdateRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    existing = store.get_memory(memory_id=memory_id, user_id=user_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )

    updates = body.model_dump(exclude_unset=True)
    if "content" in body.model_fields_set:
        if body.content is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="content 不能为 null",
            )
        content = body.content.strip()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="content 不能为空",
            )
        updates["content"] = content
    if "topics" in body.model_fields_set and body.topics is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="topics 不能为 null",
        )
    if "entities" in body.model_fields_set and body.entities is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="entities 不能为 null",
        )

    if "valid_from" in body.model_fields_set:
        try:
            updates["valid_from"] = normalize_iso_text(body.valid_from)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="valid_from must be an ISO date or datetime",
            ) from exc
    if "temporal_subject" in body.model_fields_set:
        updates["temporal_subject"] = normalize_optional_text(body.temporal_subject)
    if "temporal_predicate" in body.model_fields_set:
        updates["temporal_predicate"] = normalize_optional_text(body.temporal_predicate)

    content = updates.get("content", existing.content)
    embedding_json = None if content != existing.content else existing.embedding_json
    before = _classification_payload(existing)

    # 校验 status 值
    if "status" in body.model_fields_set and body.status not in {"dynamic", "resolved", "archived", "pinned"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"无效的 status 值: {body.status}，仅支持 dynamic/resolved/archived/pinned",
        )

    try:
        memory = store.update_memory(
            memory_id=memory_id,
            user_id=user_id,
            content=content,
            type=updates.get("type", existing.type),
            importance=updates.get("importance", existing.importance),
            confidence=updates.get("confidence", existing.confidence),
            valence=updates.get("valence", existing.valence),
            arousal=updates.get("arousal", existing.arousal),
            source_message=updates.get("source_message", existing.source_message),
            source_conversation_id=updates.get(
                "source_conversation_id",
                existing.source_conversation_id,
            ),
            embedding_json=embedding_json,
            stability=updates.get("stability", existing.stability),
            valid_from=updates.get("valid_from", existing.valid_from),
            valid_until=updates.get("valid_until", existing.valid_until),
            review_after=updates.get("review_after", existing.review_after),
            sensitivity=updates.get("sensitivity", existing.sensitivity),
            evidence_memory_ids=existing.evidence_memory_ids,
            topics=updates.get("topics", existing.topics),
            entities=updates.get("entities", existing.entities),
            temporal_subject=updates.get("temporal_subject", existing.temporal_subject),
            temporal_predicate=updates.get("temporal_predicate", existing.temporal_predicate),
            status=updates.get("status", None),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    after = _classification_payload(memory)
    if before != after:
        _write_classification_log(
            store=store,
            user_id=user_id,
            memory_id=memory_id,
            before=before,
            after=after,
        )
    return {"updated": True, "memory": memory.model_dump(exclude={"embedding_json"})}


@router.post("/{memory_id}/temporal/restore")
def restore_temporal_memory(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memory = store.restore_temporal_memory(memory_id=memory_id, user_id=user_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found or deleted",
        )
    return {"restored": True, "memory": memory.model_dump(exclude={"embedding_json"})}


@router.get("/{memory_id}")
def get_memory(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    redact_sensitive: bool = False,
) -> dict:
    memory = store.get_memory(memory_id=memory_id, user_id=user_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    return {"memory": _memory_to_response(memory, redact_sensitive=redact_sensitive)}


@router.post("/{memory_id}/restore")
def restore_memory(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memory = store.restore_memory(memory_id=memory_id, user_id=user_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory does not exist or is not deleted.",
        )
    return {"restored": True, "memory": memory.model_dump(exclude={"embedding_json"})}


@router.delete("/deleted/{memory_id}/purge")
def purge_deleted_memory(
    memory_id: str,
    body: MemoryPurgeRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    if body.confirm_memory_id != memory_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confirm_memory_id 必须与路径中的 memory_id 完全一致",
        )

    archived = store.list_archived_memories(user_id=user_id, limit=10000)
    if not any(memory.id == memory_id for memory in archived):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory does not exist or is not deleted.",
        )

    eval_cleanup = delete_user_eval_workspace(settings.eval_dir, user_id=user_id)
    affected_core_sections = _affected_core_sections_for_memory_ids(
        store=store,
        user_id=user_id,
        memory_ids=[memory_id],
    )
    affected_payload = [section.model_dump() for section in affected_core_sections]
    result = store.purge_archived_memory(
        memory_id=memory_id,
        user_id=user_id,
        affected_core_sections=affected_payload,
        call_source="rest_api",
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory does not exist or is not deleted.",
        )
    _, log = result
    return {
        "purged": True,
        "id": memory_id,
        "audit_log_id": log.id,
        "affected_core_memory_sections": affected_payload,
        "evaluation_cleanup": eval_cleanup,
    }


@router.get("/{memory_id}/why")
def why_remember(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    redact_sensitive: bool = False,
) -> dict:
    explanation = store.explain_memory_source(memory_id=memory_id, user_id=user_id)
    if explanation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    memory = store.get_memory(memory_id=memory_id, user_id=user_id)
    sensitivity = memory.sensitivity if memory else None
    return redact_memory_payload(
        explanation.model_dump(),
        redact_sensitive=redact_sensitive,
        sensitivity=sensitivity,
    )


@router.delete("/{memory_id}")
def delete_memory(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, str | bool]:
    archived = store.archive_memory(memory_id=memory_id, user_id=user_id)
    if not archived:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    return {"id": memory_id, "archived": True}


def _memory_to_response(memory: MemoryRecord, *, redact_sensitive: bool = False) -> dict:
    payload = memory.model_dump(exclude={"embedding_json"})
    return redact_memory_payload(payload, redact_sensitive=redact_sensitive)


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
) -> list[str]:
    """扫描活跃记忆中缺失、无效或维度不匹配 embedding 的记忆 ID。"""
    memory_ids: list[str] = []
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT id, embedding_json
            FROM memories
            WHERE user_id = ? AND archived = 0
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    for row in rows:
        raw = row["embedding_json"]
        if not raw:
            memory_ids.append(row["id"])
            continue
        vector = _embedding_vector(raw)
        if vector is None:
            memory_ids.append(row["id"])
            continue
        if len(vector) != expected_dimensions:
            memory_ids.append(row["id"])
            continue
    return memory_ids
