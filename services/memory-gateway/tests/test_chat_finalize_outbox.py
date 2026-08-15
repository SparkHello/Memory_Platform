"""Durable chat finalize outbox state-machine tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.api.chat_gateway as chat_gateway
from app.memory.search import NullEmbeddingClient
from app.memory.store import MemoryStore
from app.memory.store.chat_finalize import ChatFinalizeQueueFullError


@dataclass
class _StubIngestResult:
    retryable: bool = False
    reason: str | None = None
    created: int = 0


class _StubIngestService:
    calls = 0
    script: list[object] = []

    def __init__(self, **kwargs) -> None:
        del kwargs

    async def ingest(self, **kwargs):
        del kwargs
        cls = type(self)
        cls.calls += 1
        result = cls.script.pop(0) if cls.script else _StubIngestResult()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def stub_ingest(monkeypatch) -> type[_StubIngestService]:
    _StubIngestService.calls = 0
    _StubIngestService.script = []
    monkeypatch.setattr(chat_gateway, "MemoryIngestService", _StubIngestService)
    return _StubIngestService


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        chat_gateway_turn_ttl_seconds=600.0,
        chat_gateway_extraction_context_turns=4,
        chat_gateway_extraction_context_max_chars=4000,
        allow_sensitive_egress=False,
    )


def _enqueue(
    store: MemoryStore,
    *,
    user_id: str = "default",
    payload: dict | None = None,
) -> tuple[str, str]:
    key = f"turn-{uuid4().hex}"
    job_id = f"job-{uuid4().hex}"
    store.enqueue_chat_finalize_job(
        job_id=job_id,
        user_id=user_id,
        kind="ingest",
        claim_key=key,
        payload=payload
        or {"user_text": "我喜欢喝美式咖啡", "assistant_text": "好的"},
    )
    return job_id, key


def _job_row(store: MemoryStore, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(store.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM chat_finalize_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return row


async def _run_job(store: MemoryStore, job_id: str) -> str | None:
    return await chat_gateway._run_ingest_finalize_job(
        store=store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
        job_id=job_id,
    )


@pytest.mark.asyncio
async def test_done_job_clears_payload_and_duplicate_does_not_run(
    memory_store: MemoryStore,
    stub_ingest,
) -> None:
    job_id, _ = _enqueue(memory_store)

    assert await _run_job(memory_store, job_id) == job_id
    row = _job_row(memory_store, job_id)
    assert row["status"] == "done"
    assert row["payload_json"] == ""
    assert row["lease_token"] is None
    assert row["lease_expires_at"] is None
    assert row["attempts"] == 1
    assert stub_ingest.calls == 1

    assert await _run_job(memory_store, job_id) is None
    assert stub_ingest.calls == 1


def test_claim_is_atomic_across_store_instances(memory_store: MemoryStore) -> None:
    job_id, _ = _enqueue(memory_store)
    barrier = Barrier(2)

    def claim() -> dict[str, object] | None:
        contender = MemoryStore(memory_store.database_path)
        barrier.wait(timeout=5)
        return contender.claim_chat_finalize_job(job_id=job_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["id"] == job_id
    assert _job_row(memory_store, job_id)["attempts"] == 1


def test_expired_lease_can_be_reclaimed_and_old_token_loses_cas(
    memory_store: MemoryStore,
) -> None:
    job_id, _ = _enqueue(memory_store)
    first = memory_store.claim_chat_finalize_job(job_id=job_id)
    assert first is not None
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(memory_store.database_path) as connection:
        connection.execute(
            "UPDATE chat_finalize_jobs SET lease_expires_at = ? WHERE id = ?",
            (expired, job_id),
        )

    second = memory_store.claim_chat_finalize_job(job_id=job_id)
    assert second is not None
    assert second["lease_token"] != first["lease_token"]
    assert second["attempts"] == 2
    assert (
        memory_store.mark_chat_finalize_job(
            job_id=job_id,
            lease_token=str(first["lease_token"]),
            status="done",
        )
        is False
    )
    assert memory_store.mark_chat_finalize_job(
        job_id=job_id,
        lease_token=str(second["lease_token"]),
        status="done",
    )
    row = _job_row(memory_store, job_id)
    assert row["status"] == "done"
    assert row["payload_json"] == ""


@pytest.mark.asyncio
async def test_recovery_reclaims_expired_running_lease(
    memory_store: MemoryStore,
    stub_ingest,
) -> None:
    job_id, _ = _enqueue(memory_store)
    crashed_claim = memory_store.claim_chat_finalize_job(job_id=job_id)
    assert crashed_claim is not None
    with sqlite3.connect(memory_store.database_path) as connection:
        connection.execute(
            "UPDATE chat_finalize_jobs SET lease_expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job_id),
        )

    recovered = await chat_gateway.recover_pending_chat_finalize_jobs(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
    )

    assert recovered == 1
    assert stub_ingest.calls == 1
    row = _job_row(memory_store, job_id)
    assert row["status"] == "done"
    assert row["attempts"] == 2


@pytest.mark.asyncio
async def test_retryable_result_requeues_then_succeeds(
    memory_store: MemoryStore,
    stub_ingest,
) -> None:
    job_id, _ = _enqueue(memory_store)
    stub_ingest.script = [_StubIngestResult(retryable=True, reason="upstream_503")]

    assert await _run_job(memory_store, job_id) == job_id
    row = _job_row(memory_store, job_id)
    assert row["status"] == "pending"
    assert row["last_error"] == "upstream_503"
    assert row["attempts"] == 1
    assert row["payload_json"]
    assert row["lease_token"] is None

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        _StubIngestResult(retryable=True, reason="still_unavailable"),
        RuntimeError("provider failed"),
    ],
)
async def test_eighth_attempt_is_terminal_and_clears_payload(
    memory_store: MemoryStore,
    stub_ingest,
    outcome: object,
) -> None:
    job_id, _ = _enqueue(memory_store)
    with sqlite3.connect(memory_store.database_path) as connection:
        connection.execute(
            "UPDATE chat_finalize_jobs SET attempts = 7 WHERE id = ?",
            (job_id,),
        )
    stub_ingest.script = [outcome]

    assert await _run_job(memory_store, job_id) == job_id
    row = _job_row(memory_store, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 8
    assert row["payload_json"] == ""
    assert row["lease_token"] is None
    assert memory_store.claim_chat_finalize_job(job_id=job_id) is None


def test_prune_terminates_jobs_older_than_24_hours(
    memory_store: MemoryStore,
) -> None:
    job_id, _ = _enqueue(memory_store)
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    with sqlite3.connect(memory_store.database_path) as connection:
        connection.execute(
            """
            UPDATE chat_finalize_jobs
            SET created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (old, old, job_id),
        )

    memory_store.prune_chat_finalize_jobs()

    row = _job_row(memory_store, job_id)
    assert row["status"] == "failed"
    assert row["last_error"] == "max_age_exceeded"
    assert row["payload_json"] == ""


def test_enqueue_caps_nonterminal_jobs_per_user(
    memory_store: MemoryStore,
) -> None:
    for _ in range(100):
        _enqueue(memory_store, user_id="bounded")

    with pytest.raises(ChatFinalizeQueueFullError):
        _enqueue(memory_store, user_id="bounded")

    with sqlite3.connect(memory_store.database_path) as connection:
        count = connection.execute(
            """
            SELECT COUNT(*) FROM chat_finalize_jobs
            WHERE user_id = 'bounded' AND status IN ('pending', 'running')
            """
        ).fetchone()[0]
    assert count == 100


def test_prune_caps_terminal_rows_per_user(memory_store: MemoryStore) -> None:
    for _ in range(5):
        job_id, _ = _enqueue(memory_store)
        claim = memory_store.claim_chat_finalize_job(job_id=job_id)
        assert claim is not None
        assert memory_store.mark_chat_finalize_job(
            job_id=job_id,
            lease_token=str(claim["lease_token"]),
            status="done",
        )
    pending_id, _ = _enqueue(memory_store)

    removed = memory_store.prune_chat_finalize_jobs(keep_per_user=2)

    assert removed == 3
    with sqlite3.connect(memory_store.database_path) as connection:
        terminal_count = connection.execute(
            """
            SELECT COUNT(*) FROM chat_finalize_jobs
            WHERE user_id = 'default' AND status IN ('done', 'failed')
            """
        ).fetchone()[0]
        pending = connection.execute(
            "SELECT status FROM chat_finalize_jobs WHERE id = ?",
            (pending_id,),
        ).fetchone()
    assert terminal_count == 2
    assert pending == ("pending",)


@pytest.mark.asyncio
async def test_enqueue_failure_calls_pure_ingest_exactly_once(
    memory_store: MemoryStore,
    stub_ingest,
    monkeypatch,
) -> None:
    def fail_enqueue(**kwargs):
        del kwargs
        raise ChatFinalizeQueueFullError("full")

    monkeypatch.setattr(memory_store, "enqueue_chat_finalize_job", fail_enqueue)
    monkeypatch.setattr(
        chat_gateway,
        "_completed_branch_history_fingerprint",
        lambda **kwargs: "",
    )

    await chat_gateway._finalize_turn(
        key="turn-key",
        assistant_text="我记住了",
        memory_mode="read-write",
        user_id="alice",
        user_text="我喜欢美式咖啡",
        extraction_context_messages=[],
        conversation_id=None,
        previous_context=None,
        branch_state="root",
        parent_history_fingerprint="",
        branch_messages=[],
        turn_fingerprint="turn",
        memory_ids=[],
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=None,
        settings=_settings(),
    )

    assert stub_ingest.calls == 1
    with sqlite3.connect(memory_store.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_finalize_jobs"
        ).fetchone()[0] == 0


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
