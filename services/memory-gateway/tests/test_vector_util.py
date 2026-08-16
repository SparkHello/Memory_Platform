from __future__ import annotations

import pytest

from app.vector_util import cosine_similarity, try_cosine_similarity


def test_try_cosine_similarity_distinguishes_incomparable_vectors() -> None:
    assert try_cosine_similarity([], []) is None
    assert try_cosine_similarity([1.0], [1.0, 0.0]) is None
    assert try_cosine_similarity([0.0, 0.0], [1.0, 0.0]) is None


def test_cosine_similarity_preserves_neutral_memory_fallback() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_uses_norm_division_for_non_unit_vectors() -> None:
    # dot = 10, both norms sqrt(14): true similarity 10/14, not 10 * 14.
    assert try_cosine_similarity([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(
        10 / 14
    )
    assert try_cosine_similarity([2.0, 0.0], [0.0, 9.0]) == 0.0
    assert try_cosine_similarity([1.0, 2.0], [-2.0, -1.0]) == pytest.approx(-4 / 5)


def test_cosine_similarity_clamps_float_drift_into_unit_range() -> None:
    # [3, 3] vs itself: dot / (sqrt(18) * sqrt(18)) rounds to
    # 1.0000000000000002, so the clamp must pin the result to exactly 1.0.
    assert try_cosine_similarity([3.0, 3.0], [3.0, 3.0]) == 1.0
    assert try_cosine_similarity([3.0, 3.0], [-3.0, -3.0]) == -1.0
