"""Unit tests for sentence-level egress partitioning and local directive saves."""

from __future__ import annotations

import pytest

from app.memory.egress import (
    classify_sentence,
    local_directive_candidate,
    local_directive_content,
    partition_for_egress,
    split_sentences,
    withheld_sentence_has_scoped_directive,
)


@pytest.mark.parametrize(
    "text",
    [
        "我喜欢黑咖啡。我有糖尿病。我住在上海。",
        "I drink coffee. My weight is 3.5 kg, e.g. small! Right?",
        "第一句\n第二句没有句号",
        "没有任何终止符",
        "结尾带空格。  ",
        "",
    ],
)
def test_split_sentences_reconstructs_text(text: str) -> None:
    assert "".join(split_sentences(text)) == text


def test_split_sentences_keeps_terminators_and_does_not_split_decimals() -> None:
    sentences = split_sentences("体重 3.5 公斤。血压正常! Next one? end")
    assert sentences == ["体重 3.5 公斤。", "血压正常!", " Next one?", " end"]


def test_partition_withholds_only_sentences_above_the_ceiling() -> None:
    text = "我喜欢黑咖啡。我有糖尿病。我的银行卡密码是 123456。"

    private_ceiling = partition_for_egress(text, ceiling="private")
    assert [span.stripped for span in private_ceiling.kept] == ["我喜欢黑咖啡。", "我有糖尿病。"]
    assert [span.stripped for span in private_ceiling.withheld] == ["我的银行卡密码是 123456。"]
    assert private_ceiling.withheld[0].level == "sensitive"
    # 银行卡 + 密码 hit two sensitive categories at once.
    assert private_ceiling.withheld[0].categories == ["credential", "financial_account"]
    assert private_ceiling.egress_text == "我喜欢黑咖啡。我有糖尿病。"

    normal_ceiling = partition_for_egress(text, ceiling="normal")
    assert [span.stripped for span in normal_ceiling.kept] == ["我喜欢黑咖啡。"]
    assert [span.level for span in normal_ceiling.withheld] == ["private", "sensitive"]
    assert normal_ceiling.withheld[0].categories == ["health"]


def test_classify_sentence_reports_tier_and_categories() -> None:
    span = classify_sentence("我的邮箱是 user@example.com，手机号 13800138000")
    assert span.level == "private"
    assert span.categories == ["contact"]
    assert classify_sentence("今天天气不错").level == "normal"


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("记住，我的身份证号是 123456789012345678。", True),
        ("请记住我的身份证号是 123456789012345678。", True),
        ("我的身份证号是 123456789012345678，请记住。", True),
        ("Please remember my passport number is E12345678.", True),
        ("记住我喜欢咖啡，我的身份证号是 123456789012345678。", False),
        ("我记得我的身份证号是 123456789012345678。", False),
        ("我的身份证号是 123456789012345678。", False),
    ],
)
def test_withheld_sentence_directive_scoping(sentence: str, expected: bool) -> None:
    assert withheld_sentence_has_scoped_directive(sentence) is expected


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("记住，我有糖尿病。", "我有糖尿病。"),
        ("我有糖尿病，请记住。", "我有糖尿病。"),
        ("请记住我有糖尿病。", "我有糖尿病。"),
        ("Please remember I am allergic to peanuts.", "I am allergic to peanuts."),
        ("记住我的手机号是 13800138000，邮箱是 a@b.com。", "我的手机号是 13800138000，邮箱是 a@b.com。"),
    ],
)
def test_local_directive_content_strips_directive_wording(sentence: str, expected: str) -> None:
    assert local_directive_content(sentence) == expected


def test_local_directive_candidate_is_verbatim_and_conservative() -> None:
    span = classify_sentence("记住，我的银行卡密码是 123456。")
    candidate = local_directive_candidate(span)

    assert candidate.action == "create"
    assert candidate.memory == "我的银行卡密码是 123456。"
    assert candidate.source_quote == "记住，我的银行卡密码是 123456。"
    assert candidate.sensitivity == "sensitive"
    assert candidate.type == "semantic"
    assert candidate.importance == 8
    assert candidate.confidence == 0.9
    assert candidate.topics == []
    assert candidate.entities == []
