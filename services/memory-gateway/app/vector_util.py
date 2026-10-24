"""Neutral vector math helpers shared by the memory and knowledge subsystems.

Similarity for recall is a dot product of L2-normalized vectors. The pairwise
helpers below stay in pure Python so existing callers keep their historical
results. Batch scoring uses NumPy when it is installed and the same dot
product otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
import math
import struct


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


def _numpy():
    """Return NumPy, or ``None`` when this runtime does not have it.

    Android's embedded dependency list does not ship a NumPy wheel. Recall
    still uses normalized vectors there; only the matmul is replaced by a
    pure-Python dot product.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    return np


def unit_vector(values: Sequence[float]) -> list[float] | None:
    """Return an L2 unit vector, or ``None`` when the input cannot be normalized."""
    if not values:
        return None
    norm_sq = 0.0
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            return None
        norm_sq += number * number
    if norm_sq == 0.0 or not math.isfinite(norm_sq):
        return None
    norm = math.sqrt(norm_sq)
    return [float(value) / norm for value in values]


def pack_unit_vector(values: Sequence[float]) -> bytes | None:
    """Pack ``values`` as a little-endian float64 unit vector."""
    unit = unit_vector(values)
    if unit is None:
        return None
    return struct.pack("<" + "d" * len(unit), *unit)


def unpack_unit_vector(blob: bytes | memoryview | None) -> list[float] | None:
    """Unpack a unit vector stored by :func:`pack_unit_vector`."""
    if not blob:
        return None
    raw = bytes(blob)
    if len(raw) < 8 or len(raw) % 8:
        return None
    count = len(raw) // 8
    values = list(struct.unpack("<" + "d" * count, raw))
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def batch_max_cosine(
    query_vectors: Sequence[Sequence[float]],
    matrix,
) -> list[float]:
    """Max cosine of each matrix row against ``query_vectors``.

    Matrix rows are already unit length. Each query is normalized here, so
    the score is a dot product. Incomparable queries are ignored. An empty
    query set yields zeros, matching :func:`cosine_similarity`.
    """
    row_count = _matrix_row_count(matrix)
    units = []
    for query in query_vectors:
        unit = unit_vector(query)
        if unit is not None:
            units.append(unit)
    if row_count == 0:
        return []
    if not units:
        return [0.0] * row_count
    np = _numpy()
    if np is not None and isinstance(matrix, np.ndarray):
        if matrix.size == 0:
            return []
        query_matrix = np.asarray(units, dtype=np.float64)
        if query_matrix.shape[1] != matrix.shape[1]:
            return [0.0] * row_count
        dots = query_matrix @ matrix.T
        clipped = np.clip(dots.max(axis=0), -1.0, 1.0)
        return [float(value) for value in clipped]
    scores: list[float] = []
    for row in _matrix_rows(matrix):
        best = -2.0
        comparable = False
        for unit in units:
            if len(unit) != len(row):
                continue
            comparable = True
            dot = 0.0
            for left, right in zip(unit, row, strict=True):
                dot += left * right
            if dot > best:
                best = dot
        if not comparable:
            scores.append(0.0)
        else:
            scores.append(max(-1.0, min(1.0, best)))
    return scores


def _matrix_row_count(matrix) -> int:
    shape = getattr(matrix, "shape", None)
    if shape is not None:
        return int(shape[0]) if shape else 0
    return len(matrix)


def _matrix_rows(matrix):
    if hasattr(matrix, "shape"):
        return (row.tolist() for row in matrix)
    return matrix
