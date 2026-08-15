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
