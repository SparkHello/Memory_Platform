from collections import OrderedDict
from datetime import UTC, datetime
import json
import math
import re
import threading


# 进程内有界缓存：(memory_id, updated_at, embedding_space_id) -> 向量。
# key 里同时带更新时间和空间，记忆更新/重新向量化后旧 key 自然失效；
# 记忆删除后条目最多占一个坑位，由 LRU 上限挤出。
_EMBEDDING_VECTOR_CACHE_MAX = 2048
_embedding_vector_cache: OrderedDict[tuple[str, str, str], list[float] | None] = OrderedDict()
_embedding_vector_cache_lock = threading.Lock()


def parse_embedding_vector(raw_json: str | None) -> list[float] | None:
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in data
    ):
        return None
    try:
        vector = [float(value) for value in data]
    except (OverflowError, TypeError, ValueError):
        return None
    return vector if all(math.isfinite(value) for value in vector) else None


def _cached_embedding_vector(
    *,
    memory_id: str,
    updated_at: str | None,
    embedding_json: str | None,
    embedding_space_id: str | None,
) -> list[float] | None:
    key = (memory_id, updated_at or "", embedding_space_id or "")
    with _embedding_vector_cache_lock:
        if key in _embedding_vector_cache:
            _embedding_vector_cache.move_to_end(key)
            return _embedding_vector_cache[key]
        vector = parse_embedding_vector(embedding_json)
        if len(_embedding_vector_cache) >= _EMBEDDING_VECTOR_CACHE_MAX:
            _embedding_vector_cache.popitem(last=False)
        _embedding_vector_cache[key] = vector
        return vector


def _memory_embedding_vector(
    memory,
    *,
    expected_space_id: str | None = None,
) -> list[float] | None:
    """解析记忆向量；指定空间时，未知或不同空间一律不可用。"""
    memory_space_id = _memory_embedding_space_id(memory)
    if expected_space_id is not None and (
        not expected_space_id or memory_space_id != expected_space_id
    ):
        return None
    return _cached_embedding_vector(
        memory_id=str(memory.id),
        updated_at=memory.updated_at,
        embedding_json=memory.embedding_json,
        embedding_space_id=memory_space_id,
    )


def _memory_embedding_space_id(memory) -> str:
    value = getattr(memory, "embedding_space_id", None)
    return str(value).strip() if value else ""


def _memory_embeddings_share_space(left, right) -> bool:
    left_space = _memory_embedding_space_id(left)
    return bool(left_space and left_space == _memory_embedding_space_id(right))


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_json_object(text: str) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"```\s*$", "", stripped)
    for candidate_text in (stripped, _first_json_block(stripped)):
        if not candidate_text:
            continue
        try:
            data = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _first_json_block(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group() if match else None


def _term_jaccard(left: str, right: str) -> float:
    left_terms = _terms(left)
    right_terms = _terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _terms(text: str) -> set[str]:
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+", text)}
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            terms.add(run)
            continue
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
        if len(run) >= 3:
            terms.update(run[index : index + 3] for index in range(len(run) - 2))
    return terms


def _char_overlap(left: str, right: str) -> float:
    left_chars = {char.lower() for char in left if not char.isspace()}
    right_chars = {char.lower() for char in right if not char.isspace()}
    if not left_chars or not right_chars:
        return 0.0
    return len(left_chars & right_chars) / len(left_chars | right_chars)


def _has_negation(text: str) -> bool:
    """Return whether text carries an explicit negative polarity.

    Chinese negators are characters/short words, while English negators must be
    matched on token boundaries.  A raw substring check for ``not`` both missed
    common forms such as ``no``/``never`` and incorrectly marked words such as
    ``notable`` as negative.
    """
    if re.search(r"从未|并非|绝不|不再|没有|未|无|不|没|停止|戒掉|讨厌", text):
        return True
    return bool(
        re.search(
            r"\b(?:no|not|never|without|none|neither|nor|cannot|"
            r"dislike(?:s|d)?|hate(?:s|d)?)\b"
            r"|\b(?:isn|aren|wasn|weren|don|doesn|didn|can|couldn|"
            r"won|wouldn|shouldn|hasn|haven|hadn)['’]t\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。.!?！？").lower()
