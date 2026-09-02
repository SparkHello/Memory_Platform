"""context_quote / source_quote tolerance and the affirmation grounding rule.

Reproduces the 2026-09-02 Android diagnostics: the extractor quoted the
assistant's "那我改押：**青海大学**？😋" without markdown/emoji and the whole
candidate was rejected, including one whose fact sat in the user's own words.
"""
from __future__ import annotations

import json

import pytest

from app.memory.search import NullEmbeddingClient
from app.memory.extractor import (
    _is_pure_affirmation,
    _last_assistant_turn,
    _normalized_for_quote_match,
)
from app.memory.ingest import MemoryIngestService
from app.memory.store import MemoryStore

ASSISTANT_GUESS = "那我改押：**青海大学**？😋\n\n如果还不对，你就告诉我：  \n**是在新疆还是宁夏？** 🌚"
SUMMARY_CONTEXT = (
    "<compressed_summary_non_authoritative>\n用户让助手猜自己在哪里上大学\n"
    "</compressed_summary_non_authoritative>"
)
QUOTE_SOURCE = (
    "用户：猜错了，另外是 211🌚\n"
    "助手：那我这次押：**长安大学**！😎\n\n如果还不对，你就偷偷告诉我：  \n**是不是在陕西？** 🌚\n"
    "用户：不是陕西😎\n"
    f"助手：{ASSISTANT_GUESS}\n"
    "用户：哈哈，对了🤓"
)


def _service(memory_store: MemoryStore, fake_llm) -> MemoryIngestService:
    return MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
    )


def _extraction(**candidate) -> str:
    base = {
        "action": "create",
        "type": "semantic",
        "importance": 8,
        "confidence": 0.9,
        "context_quote": "",
    }
    base.update(candidate)
    return json.dumps(
        {"memories": [base], "reason_code": "has_candidates", "reason": "test"},
        ensure_ascii=False,
    )


def test_normalization_drops_markdown_whitespace_and_emoji() -> None:
    # NFKC also folds full-width punctuation, so both sides compare equal.
    assert _normalized_for_quote_match(ASSISTANT_GUESS).startswith("那我改押:青海大学?如果还不对")
    assert _normalized_for_quote_match("**CS 专业**🥴") == "cs专业"


def test_last_assistant_turn_is_the_block_before_the_current_user_message() -> None:
    assert _last_assistant_turn(QUOTE_SOURCE) == ASSISTANT_GUESS
    assert _last_assistant_turn("用户：你好") == ""


@pytest.mark.parametrize(
    "message, expected",
    [
        ("哈哈，对了🤓", True),
        ("对！", True),
        ("没错没错", False),
        ("是的呀", True),
        ("Yes!", True),
        ("对，我在西宁", False),
        ("哈哈，你再猜猜", False),
        ("", False),
    ],
)
def test_pure_affirmation_detection(message: str, expected: bool) -> None:
    assert _is_pure_affirmation(message) is expected


@pytest.mark.asyncio
async def test_context_quote_without_markdown_or_emoji_still_verifies(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户是 CS（计算机科学）专业的学生。",
        source_quote="CS 专业",
        context_quote="那我改押：青海大学？",
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="CS 专业，不过虽然我很喜欢 CS，但是不玩 CS🥴",
        conversation_context=QUOTE_SOURCE,
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 0
    memories = memory_store.list_memories(user_id="default")
    assert [m.content for m in memories] == ["用户是 CS（计算机科学）专业的学生。"]


@pytest.mark.asyncio
async def test_unverifiable_context_quote_is_dropped_when_memory_stands_on_user_words(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户是 CS（计算机科学）专业的学生。",
        source_quote="CS 专业",
        context_quote="这句话从未出现在对话里",
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="CS 专业，不过虽然我很喜欢 CS，但是不玩 CS🥴",
        conversation_context=QUOTE_SOURCE,
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 0
    assert len(memory_store.list_memories(user_id="default")) == 1


@pytest.mark.asyncio
async def test_bare_age_answer_still_needs_a_real_context_quote(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户现在 18 岁。",
        importance=7,
        confidence=0.85,
        source_quote="18",
        context_quote="你今年到底多少岁",
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="18",
        conversation_context="用户：你喜欢什么颜色",
        context_quote_source="用户：你喜欢什么颜色",
    )
    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "不是较早对话原文" in memory_store.list_decision_logs()[0].reason


@pytest.mark.asyncio
async def test_affirmation_adopts_fact_from_the_confirmed_assistant_turn(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户就读于青海大学。",
        source_quote="对了",
        context_quote="那我改押：青海大学？",
        entities=["青海大学"],
        topics=["教育背景"],
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="哈哈，对了🤓",
        conversation_context=f"{SUMMARY_CONTEXT}\n\n<recent_dialogue_quote_source>\n{QUOTE_SOURCE}\n</recent_dialogue_quote_source>",
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 0, memory_store.list_decision_logs()
    memories = memory_store.list_memories(user_id="default")
    assert [m.content for m in memories] == ["用户就读于青海大学。"]


@pytest.mark.asyncio
async def test_affirmation_cannot_reach_back_past_the_last_assistant_turn(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户就读于长安大学。",
        source_quote="对了",
        context_quote="那我这次押：长安大学！",
        entities=["长安大学"],
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="哈哈，对了🤓",
        conversation_context=QUOTE_SOURCE,
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []


@pytest.mark.asyncio
async def test_non_affirmation_cannot_adopt_assistant_facts(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户就读于青海大学。",
        source_quote="你再猜猜",
        context_quote="那我改押：青海大学？",
        entities=["青海大学"],
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="哈哈，你再猜猜",
        conversation_context=QUOTE_SOURCE,
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []


@pytest.mark.asyncio
async def test_affirmation_does_not_launder_values_missing_from_the_assistant_turn(
    memory_store: MemoryStore, fake_llm
) -> None:
    fake_llm.extraction_content = _extraction(
        memory="用户就读于青海大学，该校为 211 高校。",
        source_quote="对了",
        context_quote="那我改押：青海大学？",
        entities=["青海大学"],
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="哈哈，对了🤓",
        conversation_context=QUOTE_SOURCE,
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []


@pytest.mark.asyncio
async def test_affirmation_still_needs_the_relation_to_be_evidenced_somewhere(
    memory_store: MemoryStore, fake_llm
) -> None:
    """"青海大学？" / "对了" alone does not say the user *studies* there."""
    fake_llm.extraction_content = _extraction(
        memory="用户就读于青海大学。",
        source_quote="对了",
        context_quote="那我改押：青海大学？",
        entities=["青海大学"],
    )
    result = await _service(memory_store, fake_llm).ingest(
        user_id="default",
        text="哈哈，对了🤓",
        conversation_context=QUOTE_SOURCE,
        context_quote_source=QUOTE_SOURCE,
    )
    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []
