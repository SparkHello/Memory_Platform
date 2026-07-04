from datetime import UTC, datetime, timedelta

from app.memory.decay import score_memory
from app.memory.store import MemoryStore


def test_decay_score_keeps_unused_memory_nonzero(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=8,
    )
    now = datetime.now(UTC)

    score = score_memory(memory, now=now)

    assert score.activation_count == 0
    assert score.final_score > 0
    assert score.last_active_at == memory.updated_at


def test_freshness_bonus_rewards_new_memory(memory_store: MemoryStore) -> None:
    new_memory = memory_store.create_memory(
        user_id="default",
        content="用户最近在开发 My_Memory。",
        type="semantic",
        importance=5,
    )
    old_memory = memory_store.create_memory(
        user_id="default",
        content="用户以前在开发 My_Memory。",
        type="semantic",
        importance=5,
    )
    now = datetime.now(UTC)
    old_time = (now - timedelta(days=10)).isoformat()
    _set_memory_times(memory_store, old_memory.id, old_time, old_time)
    old_memory = memory_store.get_memory(memory_id=old_memory.id, user_id="default")
    assert old_memory is not None

    new_score = score_memory(new_memory, now=now)
    old_score = score_memory(old_memory, now=now)

    assert new_score.freshness_bonus > old_score.freshness_bonus
    assert new_score.final_score > old_score.final_score


def test_activation_count_slows_decay(memory_store: MemoryStore) -> None:
    unused = memory_store.create_memory(
        user_id="default",
        content="用户常用 FastAPI。",
        type="semantic",
        importance=5,
    )
    active = memory_store.create_memory(
        user_id="default",
        content="用户常用 Python。",
        type="semantic",
        importance=5,
    )
    now = datetime.now(UTC)
    old_time = (now - timedelta(days=20)).isoformat()
    _set_memory_times(memory_store, unused.id, old_time, old_time, usage_count=0)
    _set_memory_times(memory_store, active.id, old_time, old_time, usage_count=8)
    unused = memory_store.get_memory(memory_id=unused.id, user_id="default")
    active = memory_store.get_memory(memory_id=active.id, user_id="default")
    assert unused is not None
    assert active is not None

    unused_score = score_memory(unused, now=now)
    active_score = score_memory(active, now=now)

    assert active_score.activation_count == 8
    assert active_score.final_score > unused_score.final_score


def test_first_activation_increases_score(memory_store: MemoryStore) -> None:
    unused = memory_store.create_memory(
        user_id="default",
        content="用户使用 FastAPI。",
        type="semantic",
        importance=6,
    )
    used = memory_store.create_memory(
        user_id="default",
        content="用户使用 Python。",
        type="semantic",
        importance=6,
    )
    now = datetime.now(UTC)
    timestamp = now.isoformat()
    _set_memory_times(memory_store, unused.id, timestamp, timestamp, usage_count=0)
    _set_memory_times(memory_store, used.id, timestamp, timestamp, usage_count=1)
    unused = memory_store.get_memory(memory_id=unused.id, user_id="default")
    used = memory_store.get_memory(memory_id=used.id, user_id="default")
    assert unused is not None
    assert used is not None

    assert score_memory(used, now=now).final_score > score_memory(unused, now=now).final_score


def test_long_inactive_memory_decays(memory_store: MemoryStore) -> None:
    recent = memory_store.create_memory(
        user_id="default",
        content="用户正在练习跑步。",
        type="semantic",
        importance=6,
    )
    old = memory_store.create_memory(
        user_id="default",
        content="用户曾经练习跑步。",
        type="semantic",
        importance=6,
    )
    now = datetime.now(UTC)
    old_time = (now - timedelta(days=120)).isoformat()
    _set_memory_times(memory_store, old.id, old_time, old_time)
    old = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old is not None

    assert score_memory(recent, now=now).final_score > score_memory(old, now=now).final_score


def _set_memory_times(
    memory_store: MemoryStore,
    memory_id: str,
    created_at: str,
    updated_at: str,
    *,
    usage_count: int | None = None,
) -> None:
    assignments = "created_at = ?, updated_at = ?"
    params: list[object] = [created_at, updated_at]
    if usage_count is not None:
        assignments += ", usage_count = ?"
        params.append(usage_count)
    params.append(memory_id)
    with memory_store._connect() as connection:
        connection.execute(
            f"""
            UPDATE memories
            SET {assignments}
            WHERE id = ?
            """,
            params,
        )

