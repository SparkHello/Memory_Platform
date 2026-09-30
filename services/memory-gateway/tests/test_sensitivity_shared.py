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


@pytest.mark.parametrize(
    ("text", "memory_level", "knowledge_level"),
    (
        ("如果忘记了登录密码，可以点击忘记密码重新设置", "sensitive", "normal"),
        ("请输入短信验证码后登录", "sensitive", "normal"),
        ("The password must contain letters and digits", "sensitive", "normal"),
        ("我的密码是 Hunter2!2026", "sensitive", "sensitive"),
        ("密码：Abc12345", "sensitive", "sensitive"),
        ("password: correct-horse-battery", "sensitive", "sensitive"),
        ("api_key=sk-abcdefghijklmnop", "sensitive", "sensitive"),
        ("登录时输入手机号和验证码", "sensitive", "normal"),
        ("客服手机号 13800138000", "private", "private"),
        ("联系邮箱 user@example.com", "private", "private"),
    ),
)
def test_knowledge_credentials_require_a_value_while_memory_keeps_mentions(
    text: str,
    memory_level: str,
    knowledge_level: str,
) -> None:
    assert detect_text_sensitivity(text) == memory_level
    assert detect_knowledge_text_sensitivity(text) == knowledge_level
