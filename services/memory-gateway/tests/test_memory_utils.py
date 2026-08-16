import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.memory import utils as memory_utils
from app.memory.utils import (
    _cached_embedding_vector,
    _char_overlap,
    _has_negation,
    _memory_embedding_vector,
    _memory_embeddings_share_space,
    _ordered_unique,
    _parse_iso_datetime,
    _set_jaccard,
    _terms,
    _utc_now,
    pair_conflict,
    pair_relation,
    pair_text_signals,
    parse_embedding_vector,
)


@pytest.mark.parametrize(
    "raw_json",
    [
        "[NaN, 0.0]",
        "[Infinity, 0.0]",
        "[-Infinity, 0.0]",
        '["nan", 0.0]',
        "[true, 0.0]",
        "[]",
        f"[{10**400}]",
    ],
)
def test_parse_embedding_vector_rejects_non_numeric_or_non_finite_values(
    raw_json: str,
) -> None:
    assert parse_embedding_vector(raw_json) is None


def test_parse_embedding_vector_accepts_finite_json_numbers() -> None:
    assert parse_embedding_vector("[1, -0.25, 3.5]") == [1.0, -0.25, 3.5]


def test_terms_builds_ascii_words_and_exact_cjk_ngram_windows() -> None:
    assert _terms("Hello world_2") == {"hello", "world_2"}
    assert _terms("好") == {"好"}
    assert _terms("世界") == {"世界"}
    assert _terms("你好世") == {"你好", "好世", "你好世"}
    assert _terms("你好世界") == {
        "你好",
        "好世",
        "世界",
        "你好世",
        "好世界",
    }


def test_set_jaccard_returns_exact_fraction_and_zero_for_empty() -> None:
    assert _set_jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert _set_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert _set_jaccard(set(), {"a"}) == 0.0
    assert _set_jaccard({"a"}, set()) == 0.0


def test_char_overlap_ignores_case_and_whitespace() -> None:
    assert _char_overlap("A b", "ab") == 1.0
    assert _char_overlap("xyz", "abc") == 0.0


def test_negation_detection_covers_cn_and_en_word_boundaries() -> None:
    assert _has_negation("我不再喝咖啡") is True
    assert _has_negation("讨厌跑步") is True
    assert _has_negation("我喜欢咖啡") is False
    assert _has_negation("I do not like it") is True
    assert _has_negation("she can't swim") is True
    assert _has_negation("never again") is True
    assert _has_negation("notable growth") is False
    assert _has_negation("cannon and nutmeg") is False


def test_parse_iso_datetime_normalizes_naive_and_aware_to_utc() -> None:
    naive = _parse_iso_datetime("2026-01-02T03:04:05")
    assert naive == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    aware = _parse_iso_datetime("2026-01-02T11:04:05+08:00")
    assert aware == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert _parse_iso_datetime("not-a-date") is None
    assert _parse_iso_datetime("") is None
    assert _parse_iso_datetime(None) is None


def test_utc_now_normalizes_naive_and_aware_inputs() -> None:
    assert _utc_now(datetime(2026, 1, 2, 3, 4, 5)) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    plus8 = timezone(timedelta(hours=8))
    assert _utc_now(datetime(2026, 1, 2, 11, 4, 5, tzinfo=plus8)) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    assert _utc_now(None).tzinfo is UTC


def test_ordered_unique_preserves_order_and_drops_empty() -> None:
    assert _ordered_unique(["b", "", "a", "b"]) == ["b", "a"]
    assert _ordered_unique([]) == []


def test_embedding_vector_cache_evicts_lru_beyond_capacity() -> None:
    cache = memory_utils._embedding_vector_cache
    cache.clear()
    try:
        for index in range(memory_utils._EMBEDDING_VECTOR_CACHE_MAX + 1):
            _cached_embedding_vector(
                memory_id=f"m{index}",
                updated_at="t",
                embedding_json="[1.0]",
                embedding_space_id="s",
            )
        assert len(cache) == memory_utils._EMBEDDING_VECTOR_CACHE_MAX
        assert ("m0", "t", "s") not in cache
        assert ("m1", "t", "s") in cache

        assert (
            _cached_embedding_vector(
                memory_id="m1",
                updated_at="t",
                embedding_json="[2.0]",
                embedding_space_id="s",
            )
            == [1.0]
        )
        _cached_embedding_vector(
            memory_id="fresh", updated_at="t", embedding_json="[3.0]", embedding_space_id="s"
        )
        assert ("m1", "t", "s") in cache
        assert ("m2", "t", "s") not in cache
    finally:
        cache.clear()


def test_embedding_cache_keys_normalize_falsy_parts_and_keep_distinct_values() -> None:
    cache = memory_utils._embedding_vector_cache
    cache.clear()
    try:
        assert (
            _cached_embedding_vector(
                memory_id="m", updated_at=None, embedding_json="[1.0]", embedding_space_id=None
            )
            == [1.0]
        )
        assert (
            _cached_embedding_vector(
                memory_id="m", updated_at="", embedding_json="[9.0]", embedding_space_id=""
            )
            == [1.0]
        )

        assert (
            _cached_embedding_vector(
                memory_id="n", updated_at="2026-01", embedding_json="[1.0]", embedding_space_id="s"
            )
            == [1.0]
        )
        assert (
            _cached_embedding_vector(
                memory_id="n", updated_at="2026-02", embedding_json="[2.0]", embedding_space_id="s"
            )
            == [2.0]
        )
    finally:
        cache.clear()


class _FakeMemory:
    def __init__(self, memory_id, updated_at, embedding_json, space_id):
        self.id = memory_id
        self.updated_at = updated_at
        self.embedding_json = embedding_json
        self.embedding_space_id = space_id


def test_memory_embedding_vector_enforces_expected_space_gate() -> None:
    memory_utils._embedding_vector_cache.clear()
    memory = _FakeMemory("m1", "t", "[1.0]", "space-a")
    assert _memory_embedding_vector(memory) == [1.0]
    assert _memory_embedding_vector(memory, expected_space_id="space-a") == [1.0]
    assert _memory_embedding_vector(memory, expected_space_id="space-b") is None

    unspaced = _FakeMemory("m2", "t", "[2.0]", None)
    assert _memory_embedding_vector(unspaced) == [2.0]
    assert _memory_embedding_vector(unspaced, expected_space_id="space-a") is None
    assert _memory_embedding_vector(unspaced, expected_space_id="") is None


def test_memory_embeddings_share_space_requires_same_nonempty_space() -> None:
    a = _FakeMemory("a", "t", None, "s1")
    b = _FakeMemory("b", "t", None, "s1")
    c = _FakeMemory("c", "t", None, "s2")
    empty = _FakeMemory("e", "t", None, "")
    assert _memory_embeddings_share_space(a, b) is True
    assert _memory_embeddings_share_space(a, c) is False
    assert _memory_embeddings_share_space(a, empty) is False
    assert _memory_embeddings_share_space(empty, empty) is False


def test_pair_relation_decision_ladder_is_exact() -> None:
    assert pair_relation("我喜欢咖啡", "我喜欢咖啡", similarity_threshold=0.5) == ("same", 1.0)
    assert pair_relation("我喜欢咖啡", "", similarity_threshold=0.5) == ("none", 0.0)
    assert pair_relation("", "我喜欢咖啡", similarity_threshold=0.5) == ("none", 0.0)
    assert pair_relation("我喜欢咖啡", "我喜欢咖啡和茶", similarity_threshold=0.5) == (
        "supplement",
        0.92,
    )
    assert pair_relation("我喜欢咖啡和茶", "我喜欢咖啡", similarity_threshold=0.5) == (
        "supplement",
        0.92,
    )


def test_pair_relation_threshold_is_exclusive_boundary() -> None:
    # terms {aa,bb,cc} vs {aa,bb,dd} and chars {a,b,c} vs {a,b,d} both give 0.5.
    relation, score = pair_relation("aa bb cc", "aa bb dd", similarity_threshold=0.5)
    assert relation == "supersede"
    assert score == pytest.approx(0.5)

    relation, score = pair_relation("aa bb cc", "aa bb dd", similarity_threshold=0.6)
    assert relation == "none"
    assert score == 0.0


def test_pair_relation_separates_conflict_from_supersede_by_negation() -> None:
    relation, score = pair_relation("i love tea", "i never love tea", similarity_threshold=0.3)
    assert relation == "conflict"
    assert score == pytest.approx(7 / 9)

    relation, score = pair_relation("i love tea", "i also love tea", similarity_threshold=0.3)
    assert relation == "supersede"


def test_pair_conflict_requires_polarity_gap_and_char_overlap_boundary() -> None:
    assert pair_conflict("ab12", "ab12 never", similarity_threshold=0.5) is True
    assert pair_conflict("i love tea", "i love coffee", similarity_threshold=0.3) is False
    assert pair_conflict("ab12x", "ab99 never", similarity_threshold=0.5) is False


def test_pair_text_signals_precomputes_normalized_terms_and_is_frozen() -> None:
    signals = pair_text_signals("Hello 世界")
    assert signals.normalized == "hello世界"
    assert signals.terms == frozenset({"hello", "世界"})
    assert signals.chars == frozenset("hello世界")
    assert signals.has_negation is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        signals.has_negation = True
