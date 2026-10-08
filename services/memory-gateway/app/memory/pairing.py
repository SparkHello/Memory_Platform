"""Live memories that may be versions of the same fact, for 体检 and recall.

When a new statement is semantically close to a live fact but carries no
transition the resolver can trust, both rows stay live and the resolver files
the pair for 体检.  The lexical 体检 scan cannot see paraphrased pairs
("在读 CSAPP 第 8 章" / "在读 OSTEP"), so these helpers find them again by
embedding similarity.  Recall uses the same pairs to tell the chat model that
two injected facts may be an old and a new version; nothing here changes data.
"""

from __future__ import annotations

from collections.abc import Sequence
import math
import operator

from app.memory.extractor import (
    grounding_subjects_compatible_both_ways,
    shared_relation_families,
)
from app.memory.models import MemoryContextPair, MemoryRecord
from app.memory.resolver import (
    _ADDITIVE_RELATION_FAMILIES,
    _AUTO_SUPERSEDE_TYPES,
    CONFLICT_CHAR_OVERLAP_THRESHOLD,
    EMBEDDING_SIMILARITY_THRESHOLD,
    _attribute_relations,
    _looks_superseding,
)
from app.memory.temporal import is_current_temporal_memory
from app.memory.utils import (
    _memory_embedding_space_id,
    _memory_embedding_vector,
    _memory_embeddings_share_space,
    pair_conflict,
    pair_relation,
)

# 与体检 pair 建议同一字面门槛（review.py）：无向量时只认高度相似的说法。
LEXICAL_PAIR_SIMILARITY_THRESHOLD = 0.65


def unit_memory_vectors(memories: Sequence[MemoryRecord]) -> dict[str, list[float]]:
    """Normalised embedding per memory id; rows without a usable vector are absent."""
    vectors: dict[str, list[float]] = {}
    for memory in memories:
        if not _memory_embedding_space_id(memory):
            continue
        vector = _memory_embedding_vector(memory)
        if not vector:
            continue
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            continue
        vectors[memory.id] = [value / norm for value in vector]
    return vectors


def semantic_similarity(
    left: MemoryRecord,
    right: MemoryRecord,
    vectors: dict[str, list[float]],
) -> float | None:
    """Cosine of two memories in the same embedding space, else None."""
    if not _memory_embeddings_share_space(left, right):
        return None
    left_vector = vectors.get(left.id)
    right_vector = vectors.get(right.id)
    if left_vector is None or right_vector is None or len(left_vector) != len(right_vector):
        return None
    return sum(map(operator.mul, left_vector, right_vector))


def older_and_newer(
    left: MemoryRecord,
    right: MemoryRecord,
) -> tuple[MemoryRecord, MemoryRecord]:
    """Order by when each statement was recorded; edits do not make a fact newer."""
    if (left.created_at, left.id) <= (right.created_at, right.id):
        return left, right
    return right, left


def may_be_versions_of_same_fact(older: MemoryRecord, newer: MemoryRecord) -> bool:
    """Whether two related statements may set the same attribute of one subject.

    Looser than the resolver's auto-supersede gate because the result is only a
    suggestion or a note, but it keeps the guards that separate distinct facts:

    * only sectors the resolver may supersede (events and insights accumulate);
    * the same actor (never 用户 vs 用户的猫/朋友);
    * attribute relations, when either side asserts one, must overlap unless
      the polarity flips over mostly the same words, so "养了一只叫年糕的橘猫"
      and "年糕现在三岁" stay apart while a compound "住在北京，在阿里工作"
      still pairs with "在字节工作";
    * additive families (liking tea does not end liking coffee) pair only when
      the newer statement marks a transition or the polarity flips.
    """
    if older.type != newer.type or older.type not in _AUTO_SUPERSEDE_TYPES:
        return False
    old_text, new_text = older.content, newer.content
    if not grounding_subjects_compatible_both_ways(new_text, old_text):
        return False
    old_attributes = _attribute_relations(old_text)
    new_attributes = _attribute_relations(new_text)
    if (old_attributes or new_attributes) and not (old_attributes & new_attributes):
        # A negated verb can read as another relation ("没有坚持" → 有/possession);
        # a polarity flip over mostly the same words is still the same fact.
        return polarity_conflict(older, newer)
    families = shared_relation_families(new_text, old_text)
    if families and families <= _ADDITIVE_RELATION_FAMILIES:
        return _looks_superseding(new_text) or polarity_conflict(older, newer)
    return True


def polarity_conflict(older: MemoryRecord, newer: MemoryRecord) -> bool:
    return pair_conflict(
        newer.content,
        older.content,
        similarity_threshold=CONFLICT_CHAR_OVERLAP_THRESHOLD,
    )


def context_pairs(memories: Sequence[MemoryRecord]) -> list[MemoryContextPair]:
    """Pairs an injected memory list should annotate for the chat model."""
    by_id = {memory.id: memory for memory in memories}
    pairs = [
        MemoryContextPair(older_id=memory.id, newer_id=memory.superseded_by, kind="superseded")
        for memory in memories
        if memory.superseded_by and memory.superseded_by in by_id
    ]
    live = [memory for memory in memories if is_current_temporal_memory(memory)]
    vectors = unit_memory_vectors(live)
    for index, left in enumerate(live):
        for right in live[index + 1 :]:
            if not _related(left, right, vectors):
                continue
            older, newer = older_and_newer(left, right)
            if may_be_versions_of_same_fact(older, newer):
                pairs.append(
                    MemoryContextPair(older_id=older.id, newer_id=newer.id, kind="parallel")
                )
    return pairs


def _related(
    left: MemoryRecord,
    right: MemoryRecord,
    vectors: dict[str, list[float]],
) -> bool:
    score = semantic_similarity(left, right, vectors)
    if score is not None and score >= EMBEDDING_SIMILARITY_THRESHOLD:
        return True
    relation, _score = pair_relation(
        left.content,
        right.content,
        similarity_threshold=LEXICAL_PAIR_SIMILARITY_THRESHOLD,
    )
    return relation in {"conflict", "supersede"}
