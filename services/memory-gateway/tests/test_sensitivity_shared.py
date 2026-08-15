from __future__ import annotations

import pytest

from app.knowledge.store import detect_knowledge_text_sensitivity
from app.memory.redaction import detect_text_sensitivity


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("我喜欢黑咖啡", "normal"),
        ("邮箱 user@example.com", "private"),
        ("refresh_token=abcdefghijklmnop", "sensitive"),
        ("银行卡号 6222 0202 0000 1234 567", "sensitive"),
    ),
)
def test_memory_and_knowledge_share_sensitivity_floor(
    text: str,
    expected: str,
) -> None:
    assert detect_text_sensitivity(text) == expected
    assert detect_knowledge_text_sensitivity(text) == expected
