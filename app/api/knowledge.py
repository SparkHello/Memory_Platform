from functools import partial
import json
from typing import Annotated, Any, Literal, Sequence

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.deps import (
    get_knowledge_search_agent,
    get_knowledge_embedding_indexer,
    get_knowledge_store,
    get_user_id,
    require_api_key,
)
from app.config import Settings, get_settings
from app.knowledge.agent import KnowledgeSearchAgent
from app.knowledge.backup import build_knowledge_export, restore_knowledge_export
from app.knowledge.models import KnowledgeDocument, KnowledgeSearchHit, KnowledgeVersion
from app.knowledge.parsing import parse_knowledge_file
from app.knowledge.retrieval import KnowledgeEmbeddingIndexer
from app.knowledge.store import (
    KnowledgeConflictError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeSensitivityConfirmationRequired,
    KnowledgeStore,
    KnowledgeValidationError,
)


router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[Depends(require_api_key)],
)


KnowledgeSensitivity = Literal["normal", "private", "sensitive"]
KnowledgeQuality = Literal["fast", "balanced", "deep"]


class KnowledgeUploadBeginRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content_type: Literal["text/plain", "text/markdown"] = "text/markdown"
    source_name: str = Field(default="", max_length=500)
    replace_document_ref: str = Field(default="", max_length=300)
    sensitivity: KnowledgeSensitivity = "normal"
    tags: list[str] | None = Field(default=None, max_length=32)
    metadata: dict[str, str | int | float | bool] | None = None


class KnowledgeUploadPartRequest(BaseModel):
    # REST/Web may use transport-efficient parts.  The stricter 20,000
    # character ceiling is enforced by the public MCP append tool.
    text: str = Field(min_length=1, max_length=1_048_576)


class KnowledgeUploadCommitRequest(BaseModel):
    expected_parts: int = Field(ge=1, le=100_000)
    expected_sha256: str = Field(default="", max_length=64)
    confirm_sensitivity_override: bool = False


class KnowledgeDocumentUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    source_name: str | None = Field(default=None, max_length=500)
    sensitivity: KnowledgeSensitivity | None = None
    tags: list[str] | None = Field(default=None, max_length=32)
    metadata: dict[str, str | int | float | bool] | None = None


class KnowledgePurgeRequest(BaseModel):
    confirm_document_id: str = Field(min_length=1, max_length=200)


class KnowledgeSearchRequest(BaseModel):
    request: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=5, ge=1, le=10)
    document_refs: list[str] = Field(default_factory=list, max_length=50)
    quality: KnowledgeQuality = "balanced"
    include_sensitive: bool = False
    tags: list[str] = Field(default_factory=list, max_length=32)
    metadata_filter: dict[str, str | int | float | bool] = Field(default_factory=dict)


class KnowledgeReadRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=300)
    cursor: str = Field(default="", max_length=4000)
    max_chars: int = Field(default=12_000, ge=1, le=20_000)
    include_sensitive: bool = False


class KnowledgeRestoreRequest(BaseModel):
    data: dict


@router.get("/status")
def knowledge_status(
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    provider_priority = list(settings.llm_provider_priority)
    if provider_priority == ["M"]:
        provider_priority.append("D")
    provider_configured = {
        "M": bool(
            settings.llm_mimo_base_url
            and settings.llm_mimo_api_key
            and settings.llm_mimo_model
        ),
        "K": bool(
            settings.llm_kimi_base_url
            and settings.llm_kimi_api_key
            and settings.llm_kimi_model
        ),
        "D": bool(
            settings.llm_deepseek_base_url
            and settings.llm_deepseek_api_key
            and settings.llm_deepseek_flash_model
        ),
    }
    configured_providers = [
        code for code in provider_priority if provider_configured[code]
    ]
    llm_provider_configured = {
        **provider_configured,
        "D": bool(
            provider_configured["D"]
            or (
                settings.upstream_base_url
                and settings.upstream_api_key
                and settings.upstream_model
            )
        ),
    }
    llm_configured_providers = [
        code for code in provider_priority if llm_provider_configured[code]
    ]
    init_error = str(getattr(request.app.state, "knowledge_init_error", "") or "")
    if init_error:
        return {
            "available": False,
            "status": "unavailable",
            "error": init_error,
            "agent_enabled": False,
            "agent_egress_policy": settings.knowledge_agent_egress_policy,
            "agent_timeout_seconds": settings.knowledge_agent_timeout_seconds,
            "agent_provider_priority": settings.llm_provider_priority,
            "agent_configured_providers": configured_providers,
            "agent_rate_limit_cooldown_seconds": (
                settings.llm_rate_limit_cooldown_seconds
            ),
            "llm_provider_priority": settings.llm_provider_priority,
            "llm_configured_providers": llm_configured_providers,
            "llm_rate_limit_cooldown_seconds": (
                settings.llm_rate_limit_cooldown_seconds
            ),
        }
    payload = _store_call(store.status, user_id=user_id)
    return {
        "available": True,
        "status": "ok",
        **payload,
        "agent_enabled": bool(
            configured_providers
            and settings.knowledge_agent_egress_policy != "none"
        ),
        "agent_egress_policy": settings.knowledge_agent_egress_policy,
        "agent_timeout_seconds": settings.knowledge_agent_timeout_seconds,
        "agent_provider_priority": settings.llm_provider_priority,
        "llm_provider_priority": settings.llm_provider_priority,
        "agent_configured_providers": configured_providers,
        "llm_configured_providers": llm_configured_providers,
        "agent_rate_limit_cooldown_seconds": (
            settings.llm_rate_limit_cooldown_seconds
        ),
        "llm_rate_limit_cooldown_seconds": settings.llm_rate_limit_cooldown_seconds,
        "agent_mimo_model": settings.llm_mimo_model,
        "agent_kimi_model": settings.llm_kimi_model,
        "agent_flash_model": settings.llm_deepseek_flash_model,
        "agent_pro_model": settings.llm_deepseek_pro_model,
        "sensitive_egress_enabled": settings.allow_sensitive_egress,
        "embedding_enabled": bool(settings.embedding_api_key and settings.embedding_model),
        "embedding_model": settings.embedding_model if settings.embedding_api_key else "",
        "max_document_bytes": settings.knowledge_max_document_bytes,
        "embedding_batch_size": settings.knowledge_embedding_batch_size,
        "hybrid_vector_weight": settings.knowledge_hybrid_vector_weight,
        "embedding_min_cosine": settings.knowledge_embedding_min_cosine,
    }


@router.get("/documents")
def list_knowledge_documents(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    document_status: Literal["active", "deleted", "all"] = Query(
        default="active", alias="status"
    ),
    query: str = "",
    limit: int = Query(default=100, ge=1, le=1000),
    include_sensitive: bool = True,
) -> dict:
    documents = _store_call(
        store.list_documents,
        user_id=user_id,
        status=document_status,
        query=query,
        limit=limit,
        include_sensitive=include_sensitive,
    )
    return {"data": [_document_payload(item) for item in documents]}


@router.get("/documents/{document_id}")
def get_knowledge_document(
    document_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> dict:
    detail = _store_call(store.get_document_detail, user_id=user_id, document_ref=document_id)
    if isinstance(detail, tuple):
        document, versions = detail
    else:
        document = detail["document"] if isinstance(detail, dict) else detail.document
        versions = detail["versions"] if isinstance(detail, dict) else detail.versions
    return {
        "document": _document_payload(document),
        "versions": [_version_payload(item) for item in versions],
    }


@router.post("/uploads")
def begin_knowledge_upload(
    body: KnowledgeUploadBeginRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> dict:
    session = _store_call(
        store.begin_upload,
        user_id=user_id,
        title=body.title,
        content_type=body.content_type,
        source_name=body.source_name,
        replace_document_ref=body.replace_document_ref,
        sensitivity=body.sensitivity,
        tags=body.tags,
        metadata=body.metadata,
    )
    payload = _model_payload(session)
    payload["upload_id"] = payload.get("id", "")
    return payload


@router.put("/uploads/{upload_id}/parts/{sequence}")
def append_knowledge_upload(
    upload_id: str,
    sequence: int,
    body: KnowledgeUploadPartRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> dict:
    part = _store_call(
        store.append_upload,
        user_id=user_id,
        upload_id=upload_id,
        sequence=sequence,
        text=body.text,
    )
    return _model_payload(part)


@router.post("/uploads/{upload_id}/commit")
async def commit_knowledge_upload(
    upload_id: str,
    body: KnowledgeUploadCommitRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    indexer: Annotated[
        KnowledgeEmbeddingIndexer, Depends(get_knowledge_embedding_indexer)
    ],
) -> dict:
    result = await anyio.to_thread.run_sync(
        partial(
            _store_call,
            store.commit_upload,
            user_id=user_id,
            upload_id=upload_id,
            expected_parts=body.expected_parts,
            expected_sha256=body.expected_sha256,
            confirm_sensitivity_override=body.confirm_sensitivity_override,
        )
    )
    return await _commit_and_embed(
        store=store,
        indexer=indexer,
        user_id=user_id,
        result=result,
    )


@router.post("/import")
async def import_knowledge_file(
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    indexer: Annotated[
        KnowledgeEmbeddingIndexer, Depends(get_knowledge_embedding_indexer)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
    filename: str = Query(min_length=1, max_length=500),
    title: str = Query(default="", max_length=300),
    source_name: str = Query(default="", max_length=500),
    replace_document_ref: str = Query(default="", max_length=300),
    sensitivity: KnowledgeSensitivity = "normal",
    confirm_sensitivity_override: bool = False,
    tags: str = Query(default="", max_length=2000),
    metadata_json: str = Query(default="", max_length=4000),
) -> dict:
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > settings.knowledge_max_document_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    "source file exceeds "
                    f"{settings.knowledge_max_document_bytes} bytes"
                ),
            )
    try:
        parsed = await anyio.to_thread.run_sync(
            partial(
                parse_knowledge_file,
                bytes(raw),
                filename=filename,
                content_type=request.headers.get("content-type", ""),
            )
        )
    except Exception as exc:
        _raise_store_error(exc)
    metadata = _metadata_query(metadata_json)
    metadata["source_format"] = parsed.source_format
    if parsed.page_count is not None:
        metadata["page_count"] = parsed.page_count
    parsed_tags = [value.strip() for value in tags.split(",") if value.strip()]
    result = await anyio.to_thread.run_sync(
        partial(
            _store_call,
            _commit_text,
            store=store,
            user_id=user_id,
            title=title.strip() or parsed.suggested_title,
            source_name=source_name.strip() or parsed.source_name,
            content_type=parsed.content_type,
            sensitivity=sensitivity,
            confirm_sensitivity_override=confirm_sensitivity_override,
            replace_document_ref=replace_document_ref,
            tags=parsed_tags,
            metadata=metadata,
            text=parsed.text,
        )
    )
    payload = await _commit_and_embed(
        store=store,
        indexer=indexer,
        user_id=user_id,
        result=result,
    )
    payload["import"] = {
        "source_format": parsed.source_format,
        "page_count": parsed.page_count,
        "warnings": list(parsed.warnings),
    }
    return payload


@router.delete("/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_knowledge_upload(
    upload_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> None:
    _store_call(store.cancel_upload, user_id=user_id, upload_id=upload_id)


@router.patch("/documents/{document_id}")
def update_knowledge_document(
    document_id: str,
    body: KnowledgeDocumentUpdateRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> dict:
    document = _store_call(
        store.update_document,
        user_id=user_id,
        document_ref=document_id,
        title=body.title,
        source_name=body.source_name,
        sensitivity=body.sensitivity,
        tags=body.tags,
        metadata=body.metadata,
    )
    return {"document": _document_payload(document)}


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_document(
    document_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> None:
    _store_call(store.soft_delete_document, user_id=user_id, document_ref=document_id)


@router.post("/documents/{document_id}/restore")
def restore_knowledge_document(
    document_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> dict:
    document = _store_call(store.restore_document, user_id=user_id, document_ref=document_id)
    return {"document": _document_payload(document)}


@router.delete("/deleted/{document_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
def purge_knowledge_document(
    document_id: str,
    body: KnowledgePurgeRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> None:
    if body.confirm_document_id != document_id:
        raise HTTPException(status_code=400, detail="confirm_document_id 必须完整匹配")
    _store_call(
        store.purge_document,
        user_id=user_id,
        document_ref=document_id,
        confirm_document_id=body.confirm_document_id,
    )


@router.post("/documents/{document_id}/versions/{version_id}/restore")
async def restore_knowledge_version(
    document_id: str,
    version_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    indexer: Annotated[
        KnowledgeEmbeddingIndexer, Depends(get_knowledge_embedding_indexer)
    ],
) -> dict:
    result = await anyio.to_thread.run_sync(
        partial(
            _store_call,
            store.restore_version,
            user_id=user_id,
            document_ref=document_id,
            version_ref=version_id,
        )
    )
    return await _commit_and_embed(
        store=store, indexer=indexer, user_id=user_id, result=result
    )


@router.post("/documents/{document_id}/versions/{version_id}/reindex")
async def reindex_knowledge_version(
    document_id: str,
    version_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    indexer: Annotated[
        KnowledgeEmbeddingIndexer, Depends(get_knowledge_embedding_indexer)
    ],
) -> dict:
    result = await anyio.to_thread.run_sync(
        partial(
            _store_call,
            store.reindex_version,
            user_id=user_id,
            document_ref=document_id,
            version_ref=version_id,
        )
    )
    return await _commit_and_embed(
        store=store, indexer=indexer, user_id=user_id, result=result
    )


@router.post("/search")
async def search_knowledge(
    body: KnowledgeSearchRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    agent: Annotated[KnowledgeSearchAgent, Depends(get_knowledge_search_agent)],
) -> dict:
    scope_requested = bool(
        body.document_refs or body.tags or body.metadata_filter
    )
    scoped_refs = await anyio.to_thread.run_sync(
        partial(
            store.resolve_document_refs,
            user_id=user_id,
            document_refs=body.document_refs,
            tags=body.tags,
            metadata_filter=body.metadata_filter,
            include_sensitive=body.include_sensitive,
        )
    )
    if scope_requested and not scoped_refs:
        return _empty_search_payload(body.request, fallback_reason="scope_empty")
    try:
        result = await agent.search(
            request=body.request,
            user_id=user_id,
            limit=body.limit,
            document_refs=scoped_refs if scope_requested else [],
            quality=body.quality,
            include_sensitive=body.include_sensitive,
        )
        selected = await anyio.to_thread.run_sync(
            partial(
                store.get_chunks_by_refs,
                user_id=user_id,
                chunk_refs=result.selected_refs,
                include_sensitive=body.include_sensitive,
            )
        )
    except Exception as exc:
        _raise_store_error(exc)
    by_ref = {item.chunk_ref: item for item in selected}
    ordered = [by_ref[ref] for ref in result.selected_refs if ref in by_ref]
    hits = _bounded_search_hit_payloads(ordered)
    local_candidates = _search_candidate_payloads(list(result.baseline_candidates))
    metadata = result.metadata.model_dump()
    return {
        "request": body.request,
        "data": hits,
        "results": hits,
        "local_candidates": local_candidates,
        "metadata": metadata,
        "agent_used": metadata["agent_used"],
        "agent_model": metadata["model"] or None,
        "agent_rounds": metadata["rounds"],
        "upgraded": metadata["escalated"],
        "fallback_reason": metadata["fallback_reason"] or None,
        "elapsed_ms": metadata["elapsed_ms"],
        "steps": metadata["tool_steps"],
    }


@router.post("/read")
def read_knowledge(
    body: KnowledgeReadRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    return _store_call(
        store.read_reference,
        user_id=user_id,
        reference=body.reference,
        cursor=body.cursor,
        max_chars=body.max_chars,
        include_sensitive=body.include_sensitive,
        signing_key=settings.gateway_api_key,
    )


@router.get("/export")
def export_knowledge(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
) -> dict:
    return _store_call(build_knowledge_export, store=store, user_id=user_id)


@router.post("/restore")
async def restore_knowledge(
    body: KnowledgeRestoreRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    indexer: Annotated[
        KnowledgeEmbeddingIndexer, Depends(get_knowledge_embedding_indexer)
    ],
) -> dict:
    result = await anyio.to_thread.run_sync(
        partial(
            _store_call,
            restore_knowledge_export,
            store=store,
            user_id=user_id,
            export_data=body.data,
        )
    )
    embedding_results = []
    for document_ref in result.get("document_refs", []):
        detail = await anyio.to_thread.run_sync(
            partial(
                store.get_document_detail,
                user_id=user_id,
                document_ref=document_ref,
            )
        )
        document = detail["document"]
        if document.current_version_ref:
            embedding_results.append(
                await indexer.index_version(
                    user_id=user_id,
                    version_ref=document.current_version_ref,
                )
            )
    result["embeddings_rebuilt"] = sum(
        1 for item in embedding_results if item.get("status") in {"ready", "partial"}
    )
    result["embedding_failures"] = sum(
        1 for item in embedding_results if item.get("status") == "failed"
    )
    return result


def _commit_text(
    *,
    store: KnowledgeStore,
    user_id: str,
    title: str,
    source_name: str,
    content_type: str,
    sensitivity: KnowledgeSensitivity,
    confirm_sensitivity_override: bool,
    replace_document_ref: str,
    tags: Sequence[str],
    metadata: dict[str, Any],
    text: str,
):
    session = store.begin_upload(
        user_id=user_id,
        title=title,
        content_type=content_type,
        source_name=source_name,
        replace_document_ref=replace_document_ref,
        sensitivity=sensitivity,
        tags=tags,
        metadata=metadata,
    )
    try:
        part_size = 1_000_000
        parts = [
            text[index : index + part_size]
            for index in range(0, len(text), part_size)
        ]
        for sequence, part in enumerate(parts):
            store.append_upload(
                user_id=user_id,
                upload_id=session.id,
                sequence=sequence,
                text=part,
            )
        return store.commit_upload(
            user_id=user_id,
            upload_id=session.id,
            expected_parts=len(parts),
            confirm_sensitivity_override=confirm_sensitivity_override,
        )
    except Exception:
        try:
            store.cancel_upload(user_id=user_id, upload_id=session.id)
        except KnowledgeError:
            pass
        raise


async def _commit_and_embed(
    *,
    store: KnowledgeStore,
    indexer: KnowledgeEmbeddingIndexer,
    user_id: str,
    result,
) -> dict:
    embedding = await indexer.index_version(
        user_id=user_id,
        version_ref=result.version.ref,
    )
    refreshed = await anyio.to_thread.run_sync(
        partial(
            store.get_version,
            user_id=user_id,
            version_id=result.version.ref,
        )
    )
    payload = _commit_payload(result)
    payload["version"] = _version_payload(refreshed)
    payload["embedding"] = embedding
    return payload


def _metadata_query(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="metadata_json 必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="metadata_json 必须是 JSON 对象")
    return parsed


def _empty_search_payload(request: str, *, fallback_reason: str) -> dict:
    metadata = {
        "agent_used": False,
        "agent_attempted": False,
        "model": "",
        "rounds": 0,
        "flash_rounds": 0,
        "pro_rounds": 0,
        "escalated": False,
        "fallback_reason": fallback_reason,
        "elapsed_ms": 0,
        "baseline_count": 0,
        "baseline_refs": [],
        "tool_steps": [],
    }
    return {
        "request": request,
        "data": [],
        "results": [],
        "local_candidates": [],
        "metadata": metadata,
        "agent_used": False,
        "agent_model": None,
        "agent_rounds": 0,
        "upgraded": False,
        "fallback_reason": fallback_reason,
        "elapsed_ms": 0,
        "steps": [],
    }


def _store_call(function, /, **kwargs):
    try:
        return function(**kwargs)
    except Exception as exc:
        _raise_store_error(exc)


def _raise_store_error(exc: Exception) -> None:
    if isinstance(exc, KnowledgeNotFoundError):
        raise HTTPException(status_code=404, detail="知识文档或引用不存在") from exc
    if isinstance(exc, KnowledgeSensitivityConfirmationRequired):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "sensitivity_confirmation_required",
                "message": (
                    "本地规则认为该文档比你选择的敏感级别更高。"
                    "请检查后明确确认，系统才会按你的选择导入。"
                ),
                "declared_sensitivity": exc.declared_sensitivity,
                "detected_sensitivity": exc.detected_sensitivity,
            },
        ) from exc
    if isinstance(exc, KnowledgeConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, KnowledgeValidationError) or isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=503, detail="知识库暂时不可用") from exc


def _model_payload(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    return value.model_dump()


def _version_payload(version: KnowledgeVersion | dict) -> dict:
    payload = _model_payload(version)
    payload.update(
        {
            "version_ref": payload.get("ref", payload.get("version_ref", "")),
            "document_ref": payload.get("document_ref", ""),
            "sha256": payload.get("content_sha256", payload.get("sha256", "")),
            "size_bytes": payload.get("byte_size", payload.get("size_bytes", 0)),
        }
    )
    return payload


def _document_payload(document: KnowledgeDocument | dict) -> dict:
    payload = _model_payload(document)
    payload.update(
        {
            "document_ref": payload.get("ref", payload.get("document_ref", "")),
            "size_bytes": payload.get("byte_size", payload.get("size_bytes", 0)),
        }
    )
    return payload


def _commit_payload(result) -> dict:
    payload = _model_payload(result)
    return {
        **payload,
        "document": _document_payload(payload["document"]),
        "version": _version_payload(payload["version"]),
        "duplicate": bool(payload.get("deduplicated", False)),
    }


def _search_hit_payload(hit: KnowledgeSearchHit | dict) -> dict:
    payload = _model_payload(hit)
    payload["heading_path"] = payload.get("title_path", [])
    payload["start_char"] = payload.get("char_start", 0)
    payload["end_char"] = payload.get("char_end", 0)
    payload["start_line"] = payload.get("line_start", 1)
    payload["end_line"] = payload.get("line_end", 1)
    return payload


def _bounded_search_hit_payloads(
    hits: list[KnowledgeSearchHit | dict],
    *,
    excerpt_limit: int = 800,
    total_limit: int = 8000,
) -> list[dict]:
    """Return bounded verbatim excerpts while keeping ranges truthful."""

    remaining = total_limit
    result: list[dict] = []
    for hit in hits:
        if remaining <= 0:
            break
        payload = _search_hit_payload(hit)
        original = str(payload.get("excerpt") or "")
        excerpt = original[: min(excerpt_limit, remaining)]
        if not excerpt and original:
            break
        payload["excerpt"] = excerpt
        start = int(payload.get("char_start") or 0)
        payload["char_end"] = start + len(excerpt)
        payload["end_char"] = payload["char_end"]
        line_start = int(payload.get("line_start") or 1)
        touched_newlines = excerpt[:-1].count("\n") if excerpt else 0
        payload["line_end"] = line_start + touched_newlines
        payload["end_line"] = payload["line_end"]
        remaining -= len(excerpt)
        result.append(payload)
    return result


def _search_candidate_payloads(hits: list[KnowledgeSearchHit | dict]) -> list[dict]:
    """Expose auditable local ranking metadata without duplicating response text."""

    result: list[dict] = []
    for hit in hits[:20]:
        payload = _search_hit_payload(hit)
        payload.pop("excerpt", None)
        result.append(payload)
    return result
