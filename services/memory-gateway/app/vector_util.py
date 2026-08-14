"""Neutral vector math helpers shared by the memory and knowledge subsystems."""

from __future__ import annotations

from collections.abc import Sequence
import math


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Returns 0.0 when either vector is empty, their lengths differ, or a norm is
    zero.  The result is clamped to [-1, 1].
    """
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))
