from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
import re
import threading

from app.memory.models import MemoryRelation


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


def _set_jaccard(left: Collection[str], right: Collection[str]) -> float:
    if not left or not right:
        return 0.0
    left_set = set(left)
    right_set = set(right)
    return len(left_set & right_set) / len(left_set | right_set)


def _term_jaccard(left: str, right: str) -> float:
    return _set_jaccard(_terms(left), _terms(right))


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
    return _set_jaccard(
        {char.lower() for char in left if not char.isspace()},
        {char.lower() for char in right if not char.isspace()},
    )


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


def _utc_now(now: datetime | None) -> datetime:
    """统一的当前时间归一化：None 取当前 UTC；naive 视为 UTC；aware 转到 UTC。"""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _ordered_unique(values: list[str]) -> list[str]:
    """保序去重，丢弃空值。"""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


@dataclass(frozen=True)
class PairTextSignals:
    """pair-relation 判定所需的文本预处理信号。

    review.py 对同一批记忆做 O(n²) pair 扫描，预先计算一次信号以避免重复
    normalize/terms/否定检测；一次性判定直接用 pair_relation 即可。
    """

    normalized: str
    terms: frozenset[str]
    chars: frozenset[str]
    has_negation: bool


def pair_text_signals(text: str) -> PairTextSignals:
    return PairTextSignals(
        normalized=_normalize(text),
        terms=frozenset(_terms(text)),
        chars=frozenset(char.lower() for char in text if not char.isspace()),
        has_negation=_has_negation(text),
    )


def pair_relation(
    left: str,
    right: str,
    *,
    similarity_threshold: float,
) -> tuple[MemoryRelation, float]:
    """判定两条文本的 pair-relation，返回 (relation, 相似度分数)。

    流程（review 体检与 review_revision 规则关联曾各自抄写一份，已收敛于此）：

    1. 任一 normalize 后为空 → ("none", 0.0)
    2. 完全一致 → ("same", 1.0)
    3. 互为包含 → ("supplement", 0.92)
    4. max(term jaccard, char overlap) 低于阈值 → ("none", 0.0)
    5. 否定极性不同 → ("conflict", score)，否则 → ("supersede", score)

    各调用方阈值保持现状，勿单方收紧或放宽：

    - review.py 体检 pair 建议 0.65：只把高度相似的同类型记忆交给用户确认，
      避免体检噪音；
    - review_revision.py 规则关联候选 0.45：召回更多关联记忆供 AI 修改预览
      参考，最终仍由模型与用户确认。

    resolver.py 的冲突判定语义不同（只看 char overlap、不先排除
    same/supplement），见 pair_conflict。
    """
    return pair_relation_from_signals(
        pair_text_signals(left),
        pair_text_signals(right),
        similarity_threshold=similarity_threshold,
    )


def pair_relation_from_signals(
    left: PairTextSignals,
    right: PairTextSignals,
    *,
    similarity_threshold: float,
) -> tuple[MemoryRelation, float]:
    """pair_relation 的预处理信号版本，供 O(n²) pair 扫描复用。"""
    if not left.normalized or not right.normalized:
        return "none", 0.0
    if left.normalized == right.normalized:
        return "same", 1.0
    if left.normalized in right.normalized or right.normalized in left.normalized:
        return "supplement", 0.92
    score = max(
        _set_jaccard(left.terms, right.terms),
        _set_jaccard(left.chars, right.chars),
    )
    if score < similarity_threshold:
        return "none", 0.0
    if left.has_negation != right.has_negation:
        return "conflict", score
    return "supersede", score


def pair_conflict(
    left: str,
    right: str,
    *,
    similarity_threshold: float,
) -> bool:
    """否定极性不同且字符重叠达到阈值时判定两条内容冲突。

    resolver.py 专用：其调用点已通过 embedding/jaccard 确认两条记忆相关，
    只需再区分冲突/替代/补充；与 pair_relation 第 5 步不同，这里只看
    char overlap（不取 term jaccard 的 max），也不排除 same/supplement。
    调用方阈值现状 0.45。
    """
    if _has_negation(left) == _has_negation(right):
        return False
    return _char_overlap(left, right) >= similarity_threshold
