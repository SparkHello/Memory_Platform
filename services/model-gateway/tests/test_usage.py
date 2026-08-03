from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3

from model_gateway.models import PricingConfig
from model_gateway.usage import UsageCapture, UsageStore, estimate_cost


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
