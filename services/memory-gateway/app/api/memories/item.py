"""/memories routes for individual memory items (/{memory_id})."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from app.api.deps import get_memory_store, get_user_id
from app.api.memories.common import (
    MemorySpacesUpdateRequest,
    MemoryUpdateRequest,
    _classification_payload,
    _memory_to_response,
    _raise_revision_conflict,
    _revision_etag,
    _write_classification_log,
)
from app.memory.classification import classify_memory
from app.memory.models import (
    CandidateMemory,
    normalize_iso_text,
    normalize_optional_text,
)
from app.memory.redaction import redact_memory_payload
from app.memory.store import MemoryStore, RevisionConflictError


router = APIRouter()

@router.patch("/{memory_id}/spaces")
def update_memory_spaces(
    memory_id: str,
    body: MemorySpacesUpdateRequest,
    response: Response,
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
            expected_revision=body.expected_revision,
        )
    except RevisionConflictError as exc:
        _raise_revision_conflict(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
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
    response.headers["ETag"] = _revision_etag("memory", memory.id, memory.revision)
    return {"updated": True, "memory": memory.model_dump(exclude={"embedding_json"})}

@router.patch("/{memory_id}")
def update_memory(
    memory_id: str,
    body: MemoryUpdateRequest,
    response: Response,
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
    expected_revision = updates.pop("expected_revision", None)
    updates.pop("preserve_metadata", None)
    if "content" in body.model_fields_set:
        if body.content is None:
            raise HTTPException(
                status_code=422,
                detail="content 不能为 null",
            )
        content = body.content.strip()
        if not content:
            raise HTTPException(
                status_code=422,
                detail="content 不能为空",
            )
        updates["content"] = content
    if "topics" in body.model_fields_set and body.topics is None:
        raise HTTPException(
            status_code=422,
            detail="topics 不能为 null",
        )
    if "entities" in body.model_fields_set and body.entities is None:
        raise HTTPException(
            status_code=422,
            detail="entities 不能为 null",
        )

    if "valid_from" in body.model_fields_set:
        try:
            updates["valid_from"] = normalize_iso_text(body.valid_from)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="valid_from must be an ISO date or datetime",
            ) from exc
    if "temporal_subject" in body.model_fields_set:
        updates["temporal_subject"] = normalize_optional_text(body.temporal_subject)
    if "temporal_predicate" in body.model_fields_set:
        updates["temporal_predicate"] = normalize_optional_text(body.temporal_predicate)

    content = updates.get("content", existing.content)
    content_changed = content != existing.content
    embedding_json = None if content_changed else existing.embedding_json
    before = _classification_payload(existing)

    derived_classification = None
    if content_changed and not body.preserve_metadata:
        candidate = CandidateMemory(
            action="update",
            memory=content,
            type=updates.get("type", existing.type),
            importance=updates.get("importance", existing.importance),
            confidence=updates.get("confidence", existing.confidence),
            valence=updates.get("valence", existing.valence),
            arousal=updates.get("arousal", existing.arousal),
            stability=updates.get("stability", existing.stability),
            sensitivity=updates.get("sensitivity", existing.sensitivity),
            temporal_subject=updates.get("temporal_subject", existing.temporal_subject),
            temporal_predicate=updates.get("temporal_predicate", existing.temporal_predicate),
            source_quote=content,
        )
        derived_classification = classify_memory(candidate, source_text=content)
        if "topics" not in body.model_fields_set:
            updates["topics"] = derived_classification.topics
        if "entities" not in body.model_fields_set:
            updates["entities"] = derived_classification.entities

    # 校验 status 值
    if "status" in body.model_fields_set and body.status not in {"dynamic", "resolved", "archived", "pinned"}:
        raise HTTPException(
            status_code=422,
            detail=f"无效的 status 值: {body.status}，仅支持 dynamic/resolved/archived/pinned",
        )
    if updates.get("status") == "archived":
        # 归档必须走完整语义（置 archived 标志并脱离时间链）；只写 status 列
        # 会让记忆从列表消失却进不了回收站，也无法恢复或清除。
        if len(updates) > 1:
            raise HTTPException(
                status_code=422,
                detail="归档记忆时请仅提交 status 字段",
            )
        try:
            archived = store.archive_memory(
                memory_id=memory_id,
                user_id=user_id,
                expected_revision=expected_revision,
                return_revision=True,
            )
        except RevisionConflictError as exc:
            _raise_revision_conflict(exc)
        if not archived:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="记忆不存在或已删除",
            )
        archived_revision = int(archived)
        response.headers["ETag"] = _revision_etag(
            "memory",
            memory_id,
            archived_revision,
        )
        return {
            "updated": True,
            "archived": True,
            "memory_id": memory_id,
            "revision": archived_revision,
        }

    temporal_updates = {
        field_name: updates[field_name]
        for field_name in (
            "valid_from",
            "valid_until",
            "temporal_subject",
            "temporal_predicate",
        )
        if field_name in body.model_fields_set
    }
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
            review_after=updates.get("review_after", existing.review_after),
            sensitivity=updates.get("sensitivity", existing.sensitivity),
            evidence_memory_ids=existing.evidence_memory_ids,
            topics=updates.get("topics", existing.topics),
            entities=updates.get("entities", existing.entities),
            status=updates.get("status", None),
            expected_revision=expected_revision,
            replacement_space_ids=[] if derived_classification is not None else None,
            replacement_space_names=(
                derived_classification.space_names
                if derived_classification is not None
                else None
            ),
            **temporal_updates,
        )
    except RevisionConflictError as exc:
        _raise_revision_conflict(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
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
    response.headers["ETag"] = _revision_etag("memory", memory.id, memory.revision)
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
    response: Response,
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
    response.headers["ETag"] = _revision_etag("memory", memory.id, memory.revision)
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
