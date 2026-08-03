import pytest

from app.memory.conversation_context import (
    append_and_compact_recent_context,
    safe_extraction_context,
)
from app.memory.models import RecentContextTurn
from app.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_recent_context_compacts_older_turns_and_keeps_latest_two(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    for index in range(1, 5):
        await append_and_compact_recent_context(
            store=memory_store,
            llm_client=fake_llm,
            user_id="default",
            conversation_id="rolling",
            user_text=f"第 {index} 个问题",
            assistant_text=f"第 {index} 个回答",
            allow_sensitive_egress=False,
            keep_recent_turns=2,
            compact_after_turns=4,
            compact_after_chars=100_000,
            summary_max_chars=4000,
        )

    state = memory_store.get_recent_context_summary_for_conversation(
        user_id="default",
        conversation_id="rolling",
    )
    assert state is not None
    assert state.turn_count == 4
    assert len(state.recent_turns) == 2
    assert state.recent_turns[0].user == "第 3 个问题"
    assert state.compressed_summary == "较早对话的测试压缩摘要。"
    assert fake_llm.context_compaction_calls == 1
    compaction_payload = str(fake_llm.context_compaction_messages)
    assert "第 1 个问题" in compaction_payload
    assert "第 2 个问题" in compaction_payload


@pytest.mark.asyncio
async def test_sensitive_old_turn_stays_local_and_is_not_compacted_or_extracted(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    sensitive_value = "110101199001011234"
    turns = [
        (f"身份证号是 {sensitive_value}", "收到"),
        ("普通问题二", "普通回答二"),
        ("普通问题三", "普通回答三"),
        ("普通问题四", "普通回答四"),
    ]
    for user_text, assistant_text in turns:
        await append_and_compact_recent_context(
            store=memory_store,
            llm_client=fake_llm,
            user_id="default",
            conversation_id="sensitive",
            user_text=user_text,
            assistant_text=assistant_text,
            allow_sensitive_egress=False,
            keep_recent_turns=2,
            compact_after_turns=4,
            compact_after_chars=100_000,
            summary_max_chars=4000,
        )

    state = memory_store.get_recent_context_summary_for_conversation(
        user_id="default",
        conversation_id="sensitive",
    )
    assert state is not None
    assert sensitive_value in state.summary
    assert sensitive_value not in str(fake_llm.context_compaction_messages)

    outbound = safe_extraction_context(
        state=state,
        request_messages=[],
        allow_sensitive_egress=False,
        recent_turn_limit=2,
        max_chars=8000,
    )
    assert outbound is not None
    assert sensitive_value not in outbound


@pytest.mark.asyncio
async def test_mislabeled_imported_sensitive_turn_is_locally_rechecked_before_egress(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    sensitive_value = "110101199001011234"
    memory_store.upsert_recent_context_state(
        user_id="default",
        conversation_id="imported-sensitive",
        summary="",
        compressed_summary="",
        recent_turns=[
            RecentContextTurn(
                user=f"身份证号是 {sensitive_value}",
                assistant="收到",
                sensitivity="normal",
            ),
            RecentContextTurn(user="普通问题二", assistant="普通回答二"),
            RecentContextTurn(user="普通问题三", assistant="普通回答三"),
        ],
        turn_count=3,
    )

    await append_and_compact_recent_context(
        store=memory_store,
        llm_client=fake_llm,
        user_id="default",
        conversation_id="imported-sensitive",
        user_text="普通问题四",
        assistant_text="普通回答四",
        allow_sensitive_egress=False,
        keep_recent_turns=1,
        compact_after_turns=4,
        compact_after_chars=100_000,
        summary_max_chars=4000,
    )
    state = memory_store.get_recent_context_summary_for_conversation(
        user_id="default",
        conversation_id="imported-sensitive",
    )
    assert state is not None
    assert sensitive_value not in str(fake_llm.context_compaction_messages)

    outbound = safe_extraction_context(
        state=state,
        request_messages=[],
        allow_sensitive_egress=False,
        recent_turn_limit=5,
        max_chars=8000,
    )
    assert sensitive_value not in (outbound or "")


@pytest.mark.asyncio
async def test_recent_context_has_a_hard_local_turn_bound(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory_store.upsert_recent_context_state(
        user_id="default",
        conversation_id="bounded",
        summary="",
        compressed_summary="",
        recent_turns=[
            RecentContextTurn(
                user=f"身份证号是 11010119900101{index:04d}",
                assistant="收到",
                sensitivity="sensitive",
            )
            for index in range(200)
        ],
        turn_count=200,
    )

    state = await append_and_compact_recent_context(
        store=memory_store,
        llm_client=fake_llm,
        user_id="default",
        conversation_id="bounded",
        user_text="身份证号是 110101199001019999",
        assistant_text="收到",
        allow_sensitive_egress=False,
        keep_recent_turns=2,
        compact_after_turns=4,
        compact_after_chars=100,
        summary_max_chars=4000,
    )

    assert state is not None
    assert state.turn_count == 201
    assert len(state.recent_turns) == 200
    assert state.recent_turns[-1].user.endswith("9999")
