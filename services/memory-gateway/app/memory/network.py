from itertools import combinations

from app.memory.core import safe_core_memory_sections
from app.memory.models import MemoryRecord
from app.memory.redaction import redact_memory_payload
from app.memory.search import cosine_similarity
from app.memory.store import MemoryStore
from app.memory.utils import (
    _char_overlap,
    _memory_embedding_vector,
    _memory_embeddings_share_space,
    _term_jaccard,
)


CORE_SECTION_TITLES = {
    "profile": "稳定背景",
    "preferences": "偏好",
    "relationships": "关系",
    "routines": "习惯",
    "goals": "目标",
    "communication": "沟通风格",
}


def build_memory_network(
    *,
    store: MemoryStore,
    user_id: str,
    limit: int = 80,
    similarity_threshold: float = 0.42,
    max_similarity_edges: int = 80,
    space_id: str | None = None,
    memory_type: str | None = None,
    sensitivity: str | None = None,
    valence_min: float | None = None,
    valence_max: float | None = None,
    arousal_min: float | None = None,
    arousal_max: float | None = None,
    redact_sensitive: bool = False,
) -> dict:
    capped_limit = max(1, min(limit, 150))
    capped_edges = max(0, min(max_similarity_edges, 200))
    threshold = max(0.0, min(1.0, similarity_threshold))

    memories = _filter_memories(
        store.list_memories(user_id=user_id, limit=10000),
        space_id=space_id,
        memory_type=memory_type,
        sensitivity=sensitivity,
        valence_min=valence_min,
        valence_max=valence_max,
        arousal_min=arousal_min,
        arousal_max=arousal_max,
    )[:capped_limit]
    memory_by_id = {memory.id: memory for memory in memories}
    # 两两比较前一次性解析全部候选向量（带缓存），避免 O(n²) 次重复 json.loads。
    vectors = {memory.id: _memory_embedding_vector(memory) for memory in memories}
    # Core summaries are denormalized model output. In redacted views, revalidate
    # their evidence so a later sensitivity/temporal change cannot expose stale
    # core text. The explicit unredacted management view keeps the full graph.
    core_sections = (
        safe_core_memory_sections(store=store, user_id=user_id)
        if redact_sensitive
        else store.list_core_memory_sections(user_id=user_id)
    )

    nodes = [_core_node(section) for section in core_sections]
    nodes.extend(
        _memory_node(memory, redact_sensitive=redact_sensitive)
        for memory in memories
    )

    edges: list[dict] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for section in core_sections:
        source = f"core:{section.section}"
        for memory_id in section.evidence_memory_ids:
            if memory_id not in memory_by_id:
                continue
            _append_edge(
                edges,
                edge_keys,
                {
                    "id": f"core_evidence:{section.section}:{memory_id}",
                    "source": source,
                    "target": memory_id,
                    "kind": "core_evidence",
                    "weight": 1.0,
                    "label": "核心证据",
                },
            )

    for memory in memories:
        if memory.supersedes and memory.supersedes in memory_by_id:
            _append_edge(
                edges,
                edge_keys,
                {
                    "id": f"temporal:{memory.id}:{memory.supersedes}",
                    "source": memory.id,
                    "target": memory.supersedes,
                    "kind": "temporal",
                    "weight": 1.0,
                    "label": "时间替代",
                },
            )
        for evidence_id in memory.evidence_memory_ids:
            if evidence_id == memory.id or evidence_id not in memory_by_id:
                continue
            _append_edge(
                edges,
                edge_keys,
                {
                    "id": f"memory_evidence:{memory.id}:{evidence_id}",
                    "source": memory.id,
                    "target": evidence_id,
                    "kind": "memory_evidence",
                    "weight": 0.95,
                    "label": "证据",
                },
            )

    similarity_edges = []
    for left, right in combinations(memories, 2):
        score = memory_similarity(left, right, vectors=vectors)
        if score < threshold:
            continue
        similarity_edges.append((score, left.id, right.id))
    similarity_edges.sort(reverse=True)

    for score, left_id, right_id in similarity_edges[:capped_edges]:
        _append_edge(
            edges,
            edge_keys,
            {
                "id": f"similarity:{left_id}:{right_id}",
                "source": left_id,
                "target": right_id,
                "kind": "similarity",
                "weight": round(score, 3),
                "label": "相似",
            },
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "memory_count": len(memories),
            "core_count": len(core_sections),
            "similarity_threshold": threshold,
            "max_similarity_edges": capped_edges,
            "filters": {
                "space_id": space_id,
                "type": memory_type,
                "sensitivity": sensitivity,
                "valence_min": valence_min,
                "valence_max": valence_max,
                "arousal_min": arousal_min,
                "arousal_max": arousal_max,
                "redact_sensitive": redact_sensitive,
            },
        },
    }


def _core_node(section) -> dict:
    title = CORE_SECTION_TITLES.get(section.section, section.section)
    return {
        "id": f"core:{section.section}",
        "kind": "core",
        "label": title,
        "section": section.section,
        "content": section.content,
        "confidence": section.confidence,
        "version": section.version,
        "evidence_memory_ids": section.evidence_memory_ids,
        "updated_at": section.updated_at,
    }


def _memory_node(memory: MemoryRecord, *, redact_sensitive: bool = False) -> dict:
    payload = {
        "id": memory.id,
        "kind": "memory",
        "label": _memory_label(memory),
        "content": memory.content,
        "type": memory.type,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "valence": memory.valence,
        "arousal": memory.arousal,
        "stability": memory.stability,
        "sensitivity": memory.sensitivity,
        "usage_count": memory.usage_count,
        "last_used_at": memory.last_used_at,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "source_message": memory.source_message,
        "source_conversation_id": memory.source_conversation_id,
        "evidence_memory_ids": memory.evidence_memory_ids,
        "topics": memory.topics,
        "entities": memory.entities,
        "space_ids": memory.space_ids,
    }
    return redact_memory_payload(payload, redact_sensitive=redact_sensitive)


def _filter_memories(
    memories: list[MemoryRecord],
    *,
    space_id: str | None,
    memory_type: str | None,
    sensitivity: str | None,
    valence_min: float | None,
    valence_max: float | None,
    arousal_min: float | None,
    arousal_max: float | None,
) -> list[MemoryRecord]:
    filtered: list[MemoryRecord] = []
    for memory in memories:
        if space_id and space_id not in memory.space_ids:
            continue
        if memory_type and memory.type != memory_type:
            continue
        if sensitivity and memory.sensitivity != sensitivity:
            continue
        if valence_min is not None and memory.valence < valence_min:
            continue
        if valence_max is not None and memory.valence > valence_max:
            continue
        if arousal_min is not None and memory.arousal < arousal_min:
            continue
        if arousal_max is not None and memory.arousal > arousal_max:
            continue
        filtered.append(memory)
    return filtered


def _memory_label(memory: MemoryRecord) -> str:
    if memory.sensitivity in {"private", "sensitive"}:
        return "私密记忆" if memory.sensitivity == "private" else "敏感记忆"
    return _preview(memory.content, 32)


def _preview(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _append_edge(edges: list[dict], edge_keys: set[tuple[str, str, str]], edge: dict) -> None:
    key = (str(edge["source"]), str(edge["target"]), str(edge["kind"]))
    reverse_key = (key[1], key[0], key[2])
    if key in edge_keys or reverse_key in edge_keys:
        return
    edge_keys.add(key)
    edges.append(edge)


def memory_similarity(
    left: MemoryRecord,
    right: MemoryRecord,
    *,
    vectors: dict[str, list[float] | None] | None = None,
) -> float:
    if vectors is None:
        left_vector = _memory_embedding_vector(left)
        right_vector = _memory_embedding_vector(right)
    else:
        left_vector = vectors.get(left.id)
        right_vector = vectors.get(right.id)
    if (
        _memory_embeddings_share_space(left, right)
        and left_vector is not None
        and right_vector is not None
        and len(left_vector) == len(right_vector)
        and bool(left_vector)
        and any(value != 0.0 for value in left_vector)
        and any(value != 0.0 for value in right_vector)
    ):
        return max(0.0, cosine_similarity(left_vector, right_vector))

    text_score = max(
        _term_jaccard(left.content, right.content),
        _char_overlap(left.content, right.content),
    )
    if left.type == right.type:
        text_score += 0.06
    return min(1.0, text_score)
