from collections import OrderedDict
from datetime import UTC, datetime
import json
import re


# 进程内有界缓存：(memory_id, updated_at) -> 解析后的 embedding 向量。
# key 里带 updated_at，记忆更新后旧 key 自然失效；记忆删除后条目最多占一个坑位，
# 由 LRU 上限挤出，不会读到旧向量。
_EMBEDDING_VECTOR_CACHE_MAX = 2048
_embedding_vector_cache: OrderedDict[tuple[str, str], list[float] | None] = OrderedDict()


def parse_embedding_vector(raw_json: str | None) -> list[float] | None:
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    try:
        return [float(value) for value in data]
    except (TypeError, ValueError):
        return None


def _cached_embedding_vector(
    *,
    memory_id: str,
    updated_at: str | None,
    embedding_json: str | None,
) -> list[float] | None:
    key = (memory_id, updated_at or "")
    if key in _embedding_vector_cache:
        _embedding_vector_cache.move_to_end(key)
        return _embedding_vector_cache[key]
    vector = parse_embedding_vector(embedding_json)
    if len(_embedding_vector_cache) >= _EMBEDDING_VECTOR_CACHE_MAX:
        _embedding_vector_cache.popitem(last=False)
    _embedding_vector_cache[key] = vector
    return vector


def _memory_embedding_vector(memory) -> list[float] | None:
    """按 (id, updated_at) 缓存解析记忆的 embedding，避免每次查询重复 json.loads。"""
    return _cached_embedding_vector(
        memory_id=str(memory.id),
        updated_at=memory.updated_at,
        embedding_json=memory.embedding_json,
    )


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
    lowered = text.lower()
    markers = (
        "不",
        "不是",
        "不再",
        "没有",
        "没",
        "停止",
        "戒掉",
        "讨厌",
        "不喜欢",
        "不喝",
        "no longer",
        "not",
    )
    return any(marker in lowered for marker in markers)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。.!?！？").lower()
