from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import sqlite3

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import GatewayConfig, PricingConfig
from model_gateway.routing import Router
from model_gateway.usage import (
    AttemptTrace,
    UsageCapture,
    UsageMetadata,
    UsageStore,
    estimate_cost,
)


def pricing() -> PricingConfig:
    return PricingConfig.model_validate(
        {
            "mode": "per_token",
            "currency": "USD",
            "tiers": [
                {"max_input_tokens": 100, "input": "1", "cached_input": "0.1", "output": "2"},
                {"input": "3", "cached_input": "0.3", "output": "4"},
            ],
            "source_url": "https://vendor.example/pricing",
        }
    )


def embedding_pricing() -> PricingConfig:
    return PricingConfig.model_validate(
        {
            "mode": "per_token",
            "currency": "USD",
            "tiers": [{"input": "1"}],
            "source_url": "https://vendor.example/embedding-pricing",
        }
    )


def test_usage_capture_only_extracts_metadata() -> None:
    capture = UsageCapture()
    capture.feed(
        b'data: {"choices":[{"delta":{"content":"do-not-store"}}]}\n\n'
        b'data: {"model":"m","usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n\n'
        b'data: [DONE]\n\n'
    )
    assert capture.usage == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    assert capture.response_model == "m"
    assert capture.saw_done is True
    assert not hasattr(capture, "content")


def test_usage_capture_handles_mixed_valid_sse_line_endings_in_order() -> None:
    capture = UsageCapture()
    capture.feed(
        b'data: {"model":"m"}\n\n'
        b'data: {"usage":{"prompt_tokens":4,"completion_tokens":2}}\r\n\r\n'
        b'data: [DONE]\r\r'
    )

    assert capture.response_model == "m"
    assert capture.usage == {"prompt_tokens": 4, "completion_tokens": 2}
    assert capture.saw_done is True
    assert capture.malformed is False


def test_usage_capture_rejects_body_text_disguised_as_metadata() -> None:
    capture = UsageCapture()
    capture.from_non_stream(
        b'{"id":"this contains user private prose",'
        b'"model":"also contains private prose","usage":{"total_tokens":1}}'
    )
    assert capture.request_id == ""
    assert capture.response_model == ""


def test_tiered_cost_uses_actual_input_size_and_cache_rate() -> None:
    cost, complete = estimate_cost(
        {
            "input_tokens": 200,
            "cached_input_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 210,
        },
        pricing(),
    )
    assert cost == Decimal("0.000505")
    assert complete is True


def test_cost_rejects_cached_tokens_greater_than_total_input() -> None:
    cost, complete = estimate_cost(
        {
            "input_tokens": 10,
            "cached_input_tokens": 11,
            "output_tokens": 1,
            "total_tokens": 11,
        },
        pricing(),
    )

    assert cost is None
    assert complete is False


def test_embedding_cost_is_complete_with_input_usage_only() -> None:
    cost, complete = estimate_cost(
        {
            "input_tokens": 10,
            "cached_input_tokens": None,
            "output_tokens": None,
            "total_tokens": 10,
        },
        embedding_pricing(),
        kind="embedding",
    )

    assert cost == Decimal("0.00001")
    assert complete is True


def test_embedding_usage_row_marks_input_only_price_complete(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    capture = UsageCapture()
    capture.from_non_stream(b'{"usage":{"prompt_tokens":10,"total_tokens":10}}')
    store.record(
        client_id="client",
        kind="embedding",
        route_id="memory.embedding",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=capture,
        pricing_id="embedding-price-v1",
        pricing=embedding_pricing(),
    )

    with sqlite3.connect(tmp_path / "usage.db") as connection:
        row = connection.execute(
            "SELECT estimated_cost, cost_complete FROM usage_events"
        ).fetchone()
    assert row == ("0.00001", 1)
    summary = store.summary()
    assert summary["estimated_costs"] == {"USD": "0.00001"}
    assert summary["incomplete_cost_calls"] == 0


def test_usage_db_stores_price_snapshot_but_never_bodies(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    capture = UsageCapture()
    capture.from_non_stream(
        b'{"model":"m","choices":[{"message":{"content":"sensitive-marker"}}],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}'
    )
    store.record(
        client_id="client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=12,
        attempts=1,
        complete=True,
        capture=capture,
        pricing_id="price-v1",
        pricing=pricing(),
    )

    assert b"sensitive-marker" not in (tmp_path / "usage.db").read_bytes()
    with sqlite3.connect(tmp_path / "usage.db") as connection:
        row = connection.execute(
            "SELECT pricing_id, pricing_snapshot, estimated_cost, cost_complete FROM usage_events"
        ).fetchone()
    assert row[0] == "price-v1"
    assert "vendor.example/pricing" in row[1]
    assert row[2] == "0.000014"
    assert row[3] == 1


def test_summary_counts_success_without_price_as_incomplete(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    capture = UsageCapture()
    capture.from_non_stream(b'{"usage":{"prompt_tokens":1,"completion_tokens":1}}')
    store.record(
        client_id="client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=capture,
    )
    assert store.summary()["incomplete_cost_calls"] == 1


def test_deepseek_cache_hit_alias_is_recorded_and_priced(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    capture = UsageCapture()
    capture.from_non_stream(
        b'{"usage":{"prompt_tokens":10,"prompt_cache_hit_tokens":4,'
        b'"completion_tokens":2,"total_tokens":12}}'
    )
    store.record(
        client_id="client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=capture,
        pricing_id="price-v1",
        pricing=pricing(),
    )

    with sqlite3.connect(tmp_path / "usage.db") as connection:
        row = connection.execute(
            "SELECT cached_input_tokens, estimated_cost, cost_complete FROM usage_events"
        ).fetchone()
    assert row == (4, "0.0000104", 1)


def test_summary_does_not_mix_partial_costs_into_billable_total(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    capture = UsageCapture()
    capture.from_non_stream(b'{"usage":{"prompt_tokens":10}}')
    store.record(
        client_id="client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=capture,
        pricing_id="price-v1",
        pricing=pricing(),
    )

    summary = store.summary()
    assert summary["estimated_costs"] == {}
    assert summary["incomplete_cost_calls"] == 1


def test_oversized_provider_token_integer_never_reaches_sqlite(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    capture = UsageCapture()
    capture.from_non_stream(
        b'{"usage":{"prompt_tokens":999999999999999999999999,'
        b'"completion_tokens":1}}'
    )
    store.record(
        client_id="client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=capture,
    )

    with sqlite3.connect(tmp_path / "usage.db") as connection:
        row = connection.execute(
            "SELECT input_tokens, output_tokens FROM usage_events"
        ).fetchone()
    assert row == (None, 1)


def test_usage_schema_migration_adds_model_author_column(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE usage_events (id TEXT PRIMARY KEY)")
        UsageStore._ensure_columns(connection)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(usage_events)")
        }

    assert "model_author" in columns


def test_attempt_ledger_records_each_send_and_sums_attempt_costs(
    tmp_path: Path,
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    router = Router()
    route = router.resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    first_capture = UsageCapture()
    first_capture.from_non_stream(
        b'{"usage":{"prompt_tokens":10,"completion_tokens":1,"total_tokens":11}}'
    )
    final_capture = UsageCapture()
    final_capture.from_non_stream(
        b'{"id":"final","usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}'
    )
    traces = (
        AttemptTrace(
            attempt_index=1,
            target=route.targets[0],
            status_code=429,
            outcome="http_error",
            failure_class="http_rate_limit",
            request_sent=True,
            response_complete=True,
            capture=first_capture,
        ),
        AttemptTrace(
            attempt_index=2,
            target=route.targets[1],
            status_code=200,
            outcome="success",
            failure_class="none",
            request_sent=True,
            response_complete=True,
            capture=final_capture,
        ),
    )
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    event_id = store.record(
        client_id="memory-gateway",
        kind="chat",
        route_id="memory.chat",
        target=route.targets[1],
        status_code=200,
        latency_ms=10,
        attempts=2,
        complete=True,
        capture=final_capture,
        attempt_traces=traces,
        pricing_catalog=gateway_config.pricing,
    )

    with sqlite3.connect(tmp_path / "usage.db") as connection:
        rows = connection.execute(
            "SELECT usage_event_id, attempt_index, failure_class, request_sent, "
            "estimated_cost, cost_complete FROM attempt_events ORDER BY attempt_index"
        ).fetchall()
    assert rows == [
        (event_id, 1, "http_rate_limit", 1, "0.000012", 1),
        (event_id, 2, "none", 1, "", 0),
    ]
    summary = store.summary()
    assert summary["calls"] == 1
    assert summary["estimated_costs"] == {"USD": "0.000012"}
    assert summary["attempts"]["recorded"] == 2
    assert summary["attempts"]["known_cost_attempts"] == 1
    assert summary["attempts"]["unknown_cost_attempts"] == 1
    assert summary["attempts"]["legacy_logical_events_without_attempts"] == 0


def test_attempt_ledger_never_accepts_error_or_body_text(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    target = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    ).targets[0]
    trace = AttemptTrace(
        attempt_index=1,
        target=target,
        failure_class="connect_timeout",
    )

    assert not hasattr(trace, "error")
    assert not hasattr(trace, "content")


def test_attempt_table_migration_is_additive_and_preserves_legacy_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.db"
    store = UsageStore(path)
    store.init_db()
    store.record(
        client_id="legacy-client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=UsageCapture(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE attempt_events")

    store.init_db()

    with sqlite3.connect(path) as connection:
        legacy = connection.execute(
            "SELECT client_id, route_id FROM usage_events"
        ).fetchone()
        attempt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(attempt_events)")
        }
    assert legacy == ("legacy-client", "memory.chat")
    assert {"usage_event_id", "attempt_index", "failure_class"} <= attempt_columns


def test_usage_metadata_filters_and_retention_roll_up_attempt_costs(
    tmp_path: Path,
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    route = Router().resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
    )
    capture = UsageCapture()
    capture.from_non_stream(
        b'{"id":"request-safe","usage":{"prompt_tokens":10,'
        b'"completion_tokens":2,"total_tokens":12}}'
    )
    trace = AttemptTrace(
        attempt_index=1,
        target=route.targets[0],
        status_code=200,
        outcome="success",
        failure_class="none",
        request_sent=True,
        response_complete=True,
        capture=capture,
    )
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    event_id = store.record(
        client_id="memory-gateway",
        kind="chat",
        route_id="memory.chat",
        target=route.targets[0],
        status_code=200,
        latency_ms=7,
        attempts=1,
        complete=True,
        capture=capture,
        pricing_id="official-chat-2026-08",
        pricing=gateway_config.pricing["official-chat-2026-08"],
        attempt_traces=(trace,),
        pricing_catalog=gateway_config.pricing,
        metadata=UsageMetadata(
            correlation_id="turn:retention-1",
            operation="memory.audit.answer",
            user_tag="user:opaque-1",
        ),
    )
    old_time = datetime.now(UTC) - timedelta(days=100)
    with sqlite3.connect(tmp_path / "usage.db") as connection:
        connection.execute(
            "UPDATE usage_events SET created_at = ? WHERE id = ?",
            (old_time.isoformat(), event_id),
        )

    result = store.prune(now=datetime.now(UTC), vacuum=True)
    assert result["raw_events_pruned"] == 1
    assert result["vacuumed"] is True
    assert store.events(event_id=event_id) == []
    summary = store.summary(
        days=365,
        client_id="memory-gateway",
        operation="memory.audit.answer",
        user_tag="user:opaque-1",
    )
    assert summary["calls"] == 1
    assert summary["total_tokens"] == 12
    assert summary["estimated_costs"] == {"USD": "0.000014"}
    assert summary["attempts"]["recorded"] == 1
    assert summary["attempts"]["known_cost_attempts"] == 1
    assert summary["retention"] == {"raw_days": 90, "daily_days": 365}

    with sqlite3.connect(tmp_path / "usage.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM attempt_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM usage_daily").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cost_daily").fetchone()[0] == 1


def test_daily_usage_retention_deletes_rows_older_than_365_days(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    stale_day = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    with sqlite3.connect(tmp_path / "usage.db") as connection:
        connection.execute(
            """
            INSERT INTO usage_daily (
                day, client_id, kind, route_id, deployment_id, connection_id,
                channel_operator, model_author, upstream_model, operation,
                user_tag, calls, complete_calls, input_tokens, output_tokens,
                total_tokens, incomplete_cost_calls
            ) VALUES (?, 'client', 'chat', 'memory.chat', '', '', '', '', '',
                      '', '', 1, 1, 1, 1, 2, 0)
            """,
            (stale_day,),
        )
        connection.execute(
            """
            INSERT INTO cost_daily (
                day, client_id, operation, user_tag, currency, estimated_cost,
                known_attempts, unknown_attempts, not_sent_attempts
            ) VALUES (?, 'client', '', '', 'USD', '0.1', 1, 0, 0)
            """,
            (stale_day,),
        )

    result = store.prune(now=datetime.now(UTC))
    assert result["daily_usage_deleted"] == 1
    assert result["daily_cost_deleted"] == 1


def test_concurrent_prune_never_double_counts_the_same_raw_event(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    event_id = store.record(
        client_id="client",
        kind="chat",
        route_id="memory.chat",
        target=None,
        status_code=200,
        latency_ms=1,
        attempts=1,
        complete=True,
        capture=UsageCapture(),
        metadata=UsageMetadata(operation="memory.concurrent"),
    )
    old_time = datetime.now(UTC) - timedelta(days=100)
    with sqlite3.connect(tmp_path / "usage.db") as connection:
        connection.execute(
            "UPDATE usage_events SET created_at = ? WHERE id = ?",
            (old_time.isoformat(), event_id),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.prune(), range(2)))

    assert sum(int(result["raw_events_pruned"]) for result in results) == 1
    assert store.summary(days=365, operation="memory.concurrent")["calls"] == 1
