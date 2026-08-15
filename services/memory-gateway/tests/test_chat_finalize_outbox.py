"""聊天 finalize outbox 状态机测试。

覆盖：崩溃恢复（stale-running 重放）、重复投递（done 不回翻）、retryable
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
async def test_crash_recovery_replays_stale_running_job(
    memory_store: MemoryStore, stub_ingest
) -> None:
    job_id, _key = _enqueue(memory_store)
    # 模拟崩溃现场：job 已置 running 后进程死亡，进程内 claim 随进程消失。
    memory_store.mark_chat_finalize_job(job_id=job_id, status="running")
    stale = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    with sqlite3.connect(memory_store.database_path) as connection:
        connection.execute(
            "UPDATE chat_finalize_jobs SET updated_at = ? WHERE id = ?",
            (stale, job_id),
        )
    chat_gateway.clear_chat_gateway_state()

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
    _, _ = _enqueue(memory_store)  # 该 pending job 的同轮副作用正在本进程内执行
    jobs = memory_store.list_recoverable_chat_finalize_jobs(limit=10)
    key = str(jobs[0]["claim_key"])
    assert chat_gateway._INGESTED_TURNS.claim(key, 3600.0)
    try:
        recovered = await chat_gateway.recover_pending_chat_finalize_jobs(
            store=memory_store,
            embedding_client=NullEmbeddingClient(),
            llm_client=None,
            settings=_settings(),
        )
    finally:
        chat_gateway._INGESTED_TURNS.release(key)

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


@pytest.mark.asyncio
async def test_drainer_resolves_fresh_embedding_client_for_each_pass(
    monkeypatch,
) -> None:
    clients = [object(), object()]
    resolved: list[object] = []
    sleep_calls = 0

    def fake_get_embedding_client(*, settings):
        assert settings is test_settings
        return clients[len(resolved)]

    async def fake_recover(*, embedding_client, **kwargs):
        del kwargs
        resolved.append(embedding_client)
        return 0

    async def fake_run_sync(function, *args):
        del function, args
        return 0

    async def fake_sleep(seconds):
        nonlocal sleep_calls
        assert seconds == 30.0
        sleep_calls += 1
        if sleep_calls == 2:
            raise RuntimeError("stop after two passes")

    test_settings = _settings()
    monkeypatch.setattr(chat_gateway, "get_embedding_client", fake_get_embedding_client)
    monkeypatch.setattr(
        chat_gateway,
        "recover_pending_chat_finalize_jobs",
        fake_recover,
    )
    monkeypatch.setattr(chat_gateway.anyio.to_thread, "run_sync", fake_run_sync)
    monkeypatch.setattr(chat_gateway.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop after two passes"):
        await chat_gateway.chat_finalize_outbox_drainer(
            store=SimpleNamespace(prune_chat_finalize_jobs=lambda: 0),
            llm_client=None,
            settings=test_settings,
            interval_seconds=0.0,
        )

    assert resolved == clients
