"""Unit tests for the local extraction pre-filter.

The filter must stay conservative: it may only skip closed classes of text and
must never skip explicit memory directives, short answers to an assistant
question, or short first-person facts.
"""

from __future__ import annotations

import pytest

from app.memory.extraction_prefilter import (
    ACKNOWLEDGEMENT_LEXICON,
    compact_text,
    prefilter_extraction_turn,
)


def _decide(text: str, *, last_assistant: str | None = None, has_context: bool = False):
    return prefilter_extraction_turn(
        user_text=text,
        last_assistant_text=last_assistant,
        has_context=has_context,
    )


def test_compact_text_keeps_cjk_letters_digits_and_drops_punctuation_and_emoji() -> None:
    assert compact_text("谢谢～！") == "谢谢"
    assert compact_text("OK 👍") == "ok"
    assert compact_text("我姓王。") == "我姓王"
    assert compact_text("18") == "18"
    assert compact_text("Thank you!!") == "thankyou"


def test_lexicon_is_stored_in_compact_form_without_polar_answers() -> None:
    assert "thankyou" in ACKNOWLEDGEMENT_LEXICON
    assert "谢谢" in ACKNOWLEDGEMENT_LEXICON
    for polar in ("是的", "对", "不是", "yes", "no"):
        assert compact_text(polar) not in ACKNOWLEDGEMENT_LEXICON


@pytest.mark.parametrize(
    "text",
    ["你好", "谢谢！", "OK 👍", "好的。", "thanks", "辛苦了～", "👍", "Thank you!!", "继续"],
)
def test_greetings_and_acknowledgements_are_skipped(text: str) -> None:
    decision = _decide(text)
    assert decision.skip is True
    assert decision.rule == "greeting"
    assert decision.reason.startswith("本地预过滤：")


@pytest.mark.parametrize("text", ["不吃辣", "我姓王", "18", "我住上海", "I'm vegan"])
def test_short_facts_are_never_skipped_by_length(text: str) -> None:
    assert _decide(text).skip is False


@pytest.mark.parametrize(
    "text",
    [
        "我哪天运动？",
        "请问我上次去哪里旅游",
        "我的猫叫什么",
        "能不能帮我看看这个？",
        "What's my coffee preference?",
        "为什么天是蓝的？我该怎么解释给孩子？",
        "那我应该买哪个型号呢",
    ],
)
def test_question_only_turns_are_skipped(text: str) -> None:
    decision = _decide(text)
    assert decision.skip is True
    assert decision.rule == "question_only"


@pytest.mark.parametrize(
    "text",
    [
        "我养了猫，我哪天运动？",
        "我今年35，是不是该换工作？",
        "我喜欢什么都不加的黑咖啡",
        "我不知道为什么他这么说，但我信他",
    ],
)
def test_fact_plus_question_turns_are_extracted(text: str) -> None:
    assert _decide(text).skip is False


@pytest.mark.parametrize(
    "text",
    [
        "记住我哪天运动？",
        "请记住：我不吃辣",
        "别忘了我姓王",
        "remember I'm vegan",
        "谢谢，记住这条信息",
        "Please remember this: thanks",
    ],
)
def test_explicit_memory_directives_are_never_skipped(text: str) -> None:
    assert _decide(text).skip is False


def test_code_only_turns_are_skipped() -> None:
    fenced = "```python\nprint('hi')\n```"
    assert _decide(fenced).rule == "code_only"
    assert _decide(f"帮我看看\n{fenced}").rule == "code_only"
    assert _decide(f"这个报错怎么修？\n{fenced}").rule == "code_only"
    # Unterminated fence at the end still counts as code.
    assert _decide("```js\nconsole.log(1)").rule == "code_only"


def test_code_with_a_statement_is_extracted() -> None:
    fenced = "```python\nprint('hi')\n```"
    assert _decide(f"我用 Python 3.12\n{fenced}").skip is False


def test_assistant_question_guard_protects_elided_answers() -> None:
    # "好的" is a lexicon hit, but it may be answering the assistant.
    assert _decide("好的", last_assistant="要不要我记下你喝美式？", has_context=True).skip is False
    assert _decide("18", last_assistant="你今年多大了?", has_context=True).skip is False


def test_short_reply_with_context_is_treated_as_possible_answer() -> None:
    # Not an acknowledgement and very short: could be an elided value.
    assert _decide("18", last_assistant="我猜是 20 岁。", has_context=True).skip is False
    assert _decide("上海", has_context=True).skip is False


def test_lexicon_acknowledgement_with_context_is_still_skipped() -> None:
    decision = _decide("谢谢", last_assistant="推荐黑咖啡。", has_context=True)
    assert decision.skip is True
    assert decision.rule == "greeting"


def test_empty_text_is_left_to_the_caller() -> None:
    assert _decide("").skip is False
    assert _decide("   ").skip is False
