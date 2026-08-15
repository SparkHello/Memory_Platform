"""Neutral vector math helpers shared by the memory and knowledge subsystems."""

from __future__ import annotations

from collections.abc import Sequence
import math


def try_cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
) -> float | None:
    """Return cosine similarity, or ``None`` for incomparable vectors."""
    if len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, using 0.0 for incomparable vectors.

    Memory ranking historically treats an unavailable comparison as neutral.
    Callers that must distinguish invalid vectors should use
    :func:`try_cosine_similarity` instead.
    """
    result = try_cosine_similarity(left, right)
    return 0.0 if result is None else result
