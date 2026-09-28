"""Grounding false negatives from the 2026-09-28 Android diagnostics.

Replays candidates the extractor produced from real user turns that the
grounding gate rejected although the user stated the fact verbatim, plus the
adversarial neighbours those relaxations must keep rejecting.
"""
from __future__ import annotations

import pytest

from app.memory.extractor import CandidateMemory, _grounding_gate_reason


def _gate(memory: str, quote: str, entities: list[str] | None = None) -> str | None:
    candidate = CandidateMemory.model_validate(
        {
            "action": "create",
            "memory": memory,
            "source_quote": quote,
            "entities": entities or [],
            "type": "semantic",
        }
    )
    return _grounding_gate_reason(candidate, quote=quote)


@pytest.mark.parametrize(
    ("memory", "quote", "entities"),
    [
        # "我的 X" asserts ownership; the negation is about 烦恼, and the
        # device noun after the product name is only a descriptor.
        (
            "用户拥有一台 MacBook Air M5 笔记本电脑。",
            "我的 MacBook Air M5 倒没有这个烦恼，毕竟没有风扇😂",
            ["MacBook Air M5"],
        ),
        # "读 CS" is reading a major.
        (
            "用户就读于青海大学计算机（CS）专业。",
            "青海大学，虽说末二，但是读 CS 真费分",
            ["青海大学"],
        ),
        ("用户读计算机专业。", "我在读计算机", []),
    ],
)
def test_verbatim_facts_are_accepted(
    memory: str, quote: str, entities: list[str]
) -> None:
    assert _gate(memory, quote, entities) is None


def test_entity_spacing_and_case_are_not_facts() -> None:
    reason = _gate("用户使用枪神 9 Plus。", "我在用枪神 9plus", ["枪神 9 Plus"])

    assert reason is None


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        # The possessive binds to the product right after it, not to others.
        ("用户拥有 Pixel。", "我的 iPhone 比 Pixel 好用"),
        # Loss of ownership is not ownership.
        ("用户拥有 MacBook。", "我的 MacBook 丢了"),
        ("用户拥有 MacBook。", "我的 MacBook 已经没了"),
        ("用户拥有 MacBook。", "我的 MacBook 卖掉了"),
        # Someone else's product.
        ("用户拥有 MacBook。", "我朋友的 MacBook 很好"),
        ("用户拥有 MacBook。", "我没有 MacBook"),
        # "我的 X" is ownership, not a primary-device choice or a preference.
        ("用户的主力电脑是 MacBook。", "我的 MacBook 倒没有这个烦恼"),
        ("用户喜欢 MacBook。", "我的 MacBook 倒没有这个烦恼"),
        # A bare device noun still asserts tool_choice.
        ("用户的手机是 iPhone。", "我的 iPhone 很好用"),
        # Reading a document is not studying a major.
        ("用户就读于 pdf 专业。", "每天读 pdf 好累"),
    ],
)
def test_adjacent_unsupported_claims_stay_rejected(memory: str, quote: str) -> None:
    assert _gate(memory, quote) is not None


def test_entity_must_still_appear_in_quote() -> None:
    reason = _gate("用户使用枪神 10。", "我在用枪神 9plus", ["枪神 10"])

    assert reason == "candidate.entities 中有值未出现在 source_quote，疑似模型编造"
