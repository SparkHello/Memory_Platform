import pytest

from app.memory.utils import parse_embedding_vector


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
