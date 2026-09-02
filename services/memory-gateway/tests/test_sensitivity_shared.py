from __future__ import annotations

import pytest

from app.knowledge.store import detect_knowledge_text_sensitivity
from app.memory.redaction import detect_text_sensitivity, higher_sensitivity
from app.sensitivity import SENSITIVITY_RANK


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("我喜欢黑咖啡", "normal"),
        ("邮箱 user@example.com", "private"),
        ("refresh_token=abcdefghijklmnop", "sensitive"),
        ("银行卡号 6222 0202 0000 1234 567", "sensitive"),
        ("需要持续控制血糖", "private"),
        ("my home address is 12 Main Street", "private"),
    ),
)
def test_memory_and_knowledge_share_sensitivity_floor(
    text: str,
    expected: str,
) -> None:
    assert detect_text_sensitivity(text) == expected
    assert detect_knowledge_text_sensitivity(text) == expected


def test_sensitivity_rank_orders_every_level_pair() -> None:
    assert SENSITIVITY_RANK == {"normal": 0, "private": 1, "sensitive": 2}
    assert higher_sensitivity("normal", "private") == "private"
    assert higher_sensitivity("private", "normal") == "private"
    assert higher_sensitivity("normal", "sensitive") == "sensitive"
    assert higher_sensitivity("private", "sensitive") == "sensitive"
    assert higher_sensitivity("sensitive", "sensitive") == "sensitive"
    assert higher_sensitivity("normal", "normal") == "normal"
