from __future__ import annotations

import json

from app.api.chat_gateway import _fit_memory_context
from app.llm.prompts import render_memory_context
from app.memory.models import MemoryContextPair
from app.memory.pairing import context_pairs
from app.memory.store import MemoryStore


def _memory(store: MemoryStore, content: str, *, vector=None, space="test-space", **overrides):
    payload = {"user_id": "default", "content": content, "type": "semantic", **overrides}
    if vector is not None:
        payload["embedding_json"] = json.dumps(vector)
        payload["embedding_space_id"] = space
    return store.create_memory(**payload)


def test_render_labels_each_memory_with_its_record_date(memory_store: MemoryStore) -> None:
    memory = _memory(memory_store, "用户住在西宁。")

    rendered = render_memory_context([memory])

    assert f"1. 用户住在西宁。（记录于：{memory.created_at[:10]}）" in rendered
    assert "生效于" not in rendered


def test_render_notes_name_the_newer_of_two_live_versions(memory_store: MemoryStore) -> None:
    older = _memory(memory_store, "用户住在常德。")
    newer = _memory(memory_store, "用户住在西宁。")
    unrelated = _memory(memory_store, "用户喜欢黑咖啡。")
    pairs = [
        MemoryContextPair(older_id=older.id, newer_id=newer.id, kind="parallel"),
        MemoryContextPair(older_id=unrelated.id, newer_id="not-recalled", kind="superseded"),
    ]

    rendered = render_memory_context([newer, unrelated, older], pairs=pairs)

    assert rendered.splitlines()[-1] == (
        "注：第 1、3 条主题相近且都仍有效，第 1 条记录更晚；两者可能并存，也可能后者已取代前者。"
    )
    assert "not-recalled" not in rendered
    assert rendered.count("注：") == 1


def test_render_notes_explicit_supersede_link(memory_store: MemoryStore) -> None:
    older = _memory(memory_store, "用户住在常德。")
    newer = _memory(memory_store, "用户住在西宁。")

    rendered = render_memory_context(
        [older, newer],
        pairs=[MemoryContextPair(older_id=older.id, newer_id=newer.id, kind="superseded")],
    )

    assert rendered.splitlines()[-1] == "注：第 1 条已被第 2 条取代。"


def test_context_pairs_never_compare_vectors_across_embedding_spaces(
    memory_store: MemoryStore,
) -> None:
    older = _memory(memory_store, "用户住在常德。", vector=[1.0, 0.0], space="old-space")
    newer = _memory(memory_store, "用户住在西宁。", vector=[1.0, 0.0], space="new-space")

    assert context_pairs([older, newer]) == []


def test_context_pairs_fall_back_to_lexical_similarity_without_vectors(
    memory_store: MemoryStore,
) -> None:
    older = _memory(memory_store, "用户平时用 iPhone 15 Pro 手机。")
    newer = _memory(memory_store, "用户平时用 iPhone 16 Pro 手机。")

    assert context_pairs([newer, older]) == [
        MemoryContextPair(older_id=older.id, newer_id=newer.id, kind="parallel")
    ]


def test_chat_context_keeps_pair_notes_within_the_budget(memory_store: MemoryStore) -> None:
    older = _memory(memory_store, "用户住在常德。", vector=[1.0, 0.0])
    newer = _memory(memory_store, "用户住在西宁。", vector=[0.98, 0.2])

    rendered, selected = _fit_memory_context([newer, older], max_chars=2000)

    assert [memory.id for memory in selected] == [newer.id, older.id]
    # Both rows can share a timestamp on coarse clocks, so only the pair is fixed.
    assert "注：第 1、2 条主题相近且都仍有效" in rendered
