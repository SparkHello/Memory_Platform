"""聊天 finalize outbox 状态机测试。

覆盖：崩溃恢复（持久 claim 残留）、重复投递（done 不回翻）、retryable
重试、done 时 payload 清理，以及终态行数裁剪。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.api.chat_gateway as chat_gateway
from app.memory.search import NullEmbeddingClient
from app.memory.store import MemoryStore


@dataclass
class _StubIngestResult:
    retryable: bool = False
    reason: str | None = None
    created: int = 0


class _StubIngestService:
    """按预设脚本响应，记录调用次数。"""

    calls = 0
    script: list[_StubIngestResult] = []

    def __init__(self, **kwargs) -> None:
        del kwargs

    async def ingest(self, **kwargs):
        del kwargs
        cls = type(self)
        cls.calls += 1
        if cls.script:
            return cls.script.pop(0)
        return _StubIngestResult()


@pytest.fixture
def stub_ingest(monkeypatch) -> type[_StubIngestService]:
    _StubIngestService.calls = 0
    _StubIngestService.script = []
    monkeypatch.setattr(chat_gateway, "MemoryIngestService", _StubIngestService)
    return _StubIngestService


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        chat_gateway_turn_ttl_seconds=600.0,
        allow_sensitive_egress=False,
    )


def _enqueue(store: MemoryStore, *, user_id: str = "default") -> tuple[str, str]:
    key = f"turn-{uuid4().hex}"
    job_id = f"job-{uuid4().hex}"
    store.enqueue_chat_finalize_job(
        job_id=job_id,
        user_id=user_id,
        kind="ingest",
        claim_key=key,
        payload={"user_text": "我喜欢喝美式咖啡", "assistant_text": "好的"},
    )
    return job_id, key


def _job_row(store: MemoryStore, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(store.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM chat_finalize_jobs WHERE id = ?", (job_id,)
        ).fetchone()


async def _run_job(
    store: MemoryStore,
    job_id: str,
    key: str,
    *,
    force_reclaim: bool = False,
) -> bool:
    return await chat_gateway._run_ingest_finalize_job(
        store=store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
        job_id=job_id,
        user_id="default",
        ingest_key=key,
        user_text="我喜欢喝美式咖啡",
        assistant_text="好的",
        conversation_id=None,
        extraction_context=None,
        context_quote_source=None,
        force_reclaim=force_reclaim,
    )


@pytest.mark.asyncio
async def test_done_job_clears_payload_and_never_flips_back(
    memory_store: MemoryStore, stub_ingest
) -> None:
    job_id, key = _enqueue(memory_store)

    assert await _run_job(memory_store, job_id, key) is True
    row = _job_row(memory_store, job_id)
    assert row["status"] == "done"
    assert row["payload_json"] == ""
    assert stub_ingest.calls == 1

    # 迟到的重复投递不得把 done 翻回 running/pending。
    assert (
        memory_store.mark_chat_finalize_job(job_id=job_id, status="running")
        is False
    )
    assert (
        memory_store.mark_chat_finalize_job(job_id=job_id, status="pending")
        is False
    )
    assert _job_row(memory_store, job_id)["status"] == "done"

    # 同一轮次的重复执行被幂等 claim 挡住，不会二次提取。
    assert await _run_job(memory_store, job_id, key) is False
    assert stub_ingest.calls == 1


@pytest.mark.asyncio
async def test_crash_recovery_bypasses_stale_persistent_claim(
    memory_store: MemoryStore, stub_ingest
) -> None:
    job_id, key = _enqueue(memory_store)
    # 模拟崩溃现场：worker 已拿到持久 claim 并置 running，然后进程死亡。
    assert memory_store.claim_chat_side_effect(
        kind="ingest", key=key, user_id="default", ttl_seconds=3600.0
    )
    memory_store.mark_chat_finalize_job(job_id=job_id, status="running")
    stale = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    with sqlite3.connect(memory_store.database_path) as connection:
        connection.execute(
            "UPDATE chat_finalize_jobs SET updated_at = ? WHERE id = ?",
            (stale, job_id),
        )

    recovered = await chat_gateway.recover_pending_chat_finalize_jobs(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
    )

    assert recovered == 1
    assert stub_ingest.calls == 1
    assert _job_row(memory_store, job_id)["status"] == "done"


@pytest.mark.asyncio
async def test_recovered_counts_only_actually_executed_jobs(
    memory_store: MemoryStore, stub_ingest
) -> None:
    _, _ = _enqueue(memory_store)  # 该 pending job 的 claim 被"另一 worker"占用
    jobs = memory_store.list_recoverable_chat_finalize_jobs(limit=10)
    key = str(jobs[0]["claim_key"])
    assert memory_store.claim_chat_side_effect(
        kind="ingest", key=key, user_id="default", ttl_seconds=3600.0
    )

    recovered = await chat_gateway.recover_pending_chat_finalize_jobs(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
    )

    assert recovered == 0
    assert stub_ingest.calls == 0


@pytest.mark.asyncio
async def test_retryable_result_requeues_then_succeeds(
    memory_store: MemoryStore, stub_ingest
) -> None:
    job_id, key = _enqueue(memory_store)
    stub_ingest.script = [_StubIngestResult(retryable=True, reason="upstream_503")]

    assert await _run_job(memory_store, job_id, key) is True
    row = _job_row(memory_store, job_id)
    assert row["status"] == "pending"
    assert row["last_error"] == "upstream_503"
    assert row["attempts"] == 1

    # 周期 drainer 拾起 pending 任务并成功完成。
    recovered = await chat_gateway.recover_pending_chat_finalize_jobs(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
    )
    assert recovered == 1
    row = _job_row(memory_store, job_id)
    assert row["status"] == "done"
    assert row["attempts"] == 2
    assert stub_ingest.calls == 2


def test_prune_caps_terminal_rows_per_user(memory_store: MemoryStore) -> None:
    ids: list[str] = []
    for _ in range(5):
        job_id, _key = _enqueue(memory_store)
        memory_store.mark_chat_finalize_job(job_id=job_id, status="done")
        ids.append(job_id)
    keep_pending, _ = _enqueue(memory_store)

    removed = memory_store.prune_chat_finalize_jobs(keep_per_user=2)

    assert removed == 3
    with sqlite3.connect(memory_store.database_path) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM chat_finalize_jobs"
            ).fetchall()
        }
    # pending 永不被裁剪；终态行只保留最新 2 条。
    assert keep_pending in remaining
    assert set(ids[-2:]).issubset(remaining)
    assert not set(ids[:3]) & remaining
