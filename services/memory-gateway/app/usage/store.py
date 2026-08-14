from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from app.schema_migrations import enable_wal_with_retry
from app.usage.pricing import (
    ModelPrice,
    normalize_model_name,
    price_for,
    pricing_catalog,
    provider_label,
)


_NANOS_PER_UNIT = Decimal("1000000000")
_TOKENS_PER_MILLION = Decimal("1000000")
_USAGE_DB_INIT_LOCK = threading.Lock()

# Raw usage events power the Console cost views (30/90 day windows). One year
# keeps every view working while bounding growth for always-on deployments.
EVENT_RETENTION_DAYS = 365


from app.sqlite_util import ClosingSQLiteConnection as _ClosingSQLiteConnection


class UsageStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with _USAGE_DB_INIT_LOCK:
            with self._connect() as connection:
                enable_wal_with_retry(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS model_usage_events (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        provider_code TEXT DEFAULT '',
                        model TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        input_tokens INTEGER,
                        cached_input_tokens INTEGER,
                        output_tokens INTEGER,
                        total_tokens INTEGER,
                        usage_available INTEGER DEFAULT 0,
                        price_available INTEGER DEFAULT 0,
                        cost_nanos INTEGER,
                        currency TEXT DEFAULT 'CNY',
                        price_key TEXT DEFAULT '',
                        input_cache_hit_per_million TEXT DEFAULT '',
                        input_cache_miss_per_million TEXT DEFAULT '',
                        output_per_million TEXT DEFAULT '',
                        pricing_as_of TEXT DEFAULT '',
                        pricing_source_url TEXT DEFAULT '',
                        source_request_id TEXT DEFAULT '',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_model_usage_user_created
                    ON model_usage_events(user_id, created_at DESC)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_model_usage_user_model_created
                    ON model_usage_events(user_id, provider, model, created_at DESC)
                    """
                )

    def record_response(
        self,
        *,
        user_id: str,
        operation: str,
        provider: str,
        provider_code: str,
        model: str,
        kind: str,
        payload: dict[str, Any],
        use_local_pricing: bool = True,
    ) -> str:
        usage = parse_usage(payload.get("usage"))
        price = (
            price_for(
                provider=provider,
                model=model,
                kind=kind,
                input_tokens=(
                    int(usage["input_tokens"])
                    if usage["input_tokens"] is not None
                    else None
                ),
            )
            if use_local_pricing
            else None
        )
        cost_nanos = (
            calculate_cost_nanos(usage=usage, price=price)
            if usage["available"] and price is not None
            else None
        )
        event_id = f"use_{uuid4().hex}"
        request_id = str(
            payload.get("request_id") or payload.get("id") or ""
        ).strip()[:300]
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_usage_events (
                    id, user_id, operation, provider, provider_code, model, kind,
                    input_tokens, cached_input_tokens, output_tokens, total_tokens,
                    usage_available, price_available, cost_nanos, currency,
                    price_key, input_cache_hit_per_million,
                    input_cache_miss_per_million, output_per_million,
                    pricing_as_of, pricing_source_url, source_request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(user_id or "default"),
                    _bounded_text(operation or "unspecified", 120),
                    _bounded_text(provider or "custom", 60),
                    _bounded_text(provider_code, 10),
                    _bounded_text(normalize_model_name(model) or "unknown", 200),
                    "embedding" if kind == "embedding" else "chat",
                    usage["input_tokens"],
                    usage["cached_input_tokens"],
                    usage["output_tokens"],
                    usage["total_tokens"],
                    int(bool(usage["available"])),
                    int(price is not None),
                    cost_nanos,
                    price.currency if price is not None else "CNY",
                    price.key if price is not None else "",
                    (
                        str(price.input_cache_hit_per_million)
                        if price is not None
                        else ""
                    ),
                    (
                        str(price.input_cache_miss_per_million)
                        if price is not None
                        else ""
                    ),
                    str(price.output_per_million) if price is not None else "",
                    price.as_of if price is not None else "",
                    price.source_url if price is not None else "",
                    request_id,
                    now,
                ),
            )
        return event_id

    def prune(
        self,
        *,
        retention_days: int = EVENT_RETENTION_DAYS,
        now: datetime | None = None,
    ) -> int:
        """Delete usage events older than the retention window; returns count."""
        cutoff = (
            (now or datetime.now(UTC)) - timedelta(days=max(1, int(retention_days)))
        ).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM model_usage_events WHERE created_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

    def summary(self, *, user_id: str, days: int | None = 30) -> dict[str, Any]:
        end = datetime.now(UTC)
        start = end - timedelta(days=days) if days is not None else None
        sql = """
            SELECT *
            FROM model_usage_events
            WHERE user_id = ?
        """
        params: list[Any] = [user_id]
        if start is not None:
            sql += " AND created_at >= ?"
            params.append(start.isoformat())
        sql += " ORDER BY created_at DESC, rowid DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        events = [dict(row) for row in rows]
        totals = _empty_totals()
        by_model: dict[tuple[str, str, str], dict[str, Any]] = {}
        by_operation: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, Any]] = {}
        for event in events:
            _accumulate(totals, event)
            model_key = (event["provider"], event["model"], event["kind"])
            model_bucket = by_model.setdefault(
                model_key,
                {
                    **_empty_totals(),
                    "provider": event["provider"],
                    "provider_label": provider_label(event["provider"]),
                    "model": event["model"],
                    "kind": event["kind"],
                },
            )
            _accumulate(model_bucket, event)
            operation = str(event["operation"])
            operation_bucket = by_operation.setdefault(
                operation,
                {**_empty_totals(), "operation": operation},
            )
            _accumulate(operation_bucket, event)
            day = str(event["created_at"])[:10]
            daily_bucket = by_day.setdefault(
                day,
                {**_empty_totals(), "date": day},
            )
            _accumulate(daily_bucket, event)

        recent = [_public_event(event) for event in events[:100]]
        return {
            "range": {
                "days": days,
                "start": start.isoformat() if start is not None else None,
                "end": end.isoformat(),
            },
            "totals": _finalize_totals(totals),
            "by_model": [
                _finalize_totals(bucket)
                for bucket in sorted(
                    by_model.values(),
                    key=lambda item: (
                        -int(item["cost_nanos"]),
                        -int(item["total_tokens"]),
                        str(item["model"]),
                    ),
                )
            ],
            "by_operation": [
                _finalize_totals(bucket)
                for bucket in sorted(
                    by_operation.values(),
                    key=lambda item: (
                        -int(item["cost_nanos"]),
                        -int(item["total_tokens"]),
                        str(item["operation"]),
                    ),
                )
            ],
            "daily": [
                _finalize_totals(by_day[day])
                for day in sorted(by_day)
            ],
            "recent": recent,
            "pricing": pricing_catalog(),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            factory=_ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def parse_usage(raw_usage: Any) -> dict[str, int | bool | None]:
    if not isinstance(raw_usage, dict):
        return {
            "available": False,
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    input_tokens = _first_int(
        raw_usage,
        "prompt_tokens",
        "input_tokens",
    )
    output_tokens = _first_int(
        raw_usage,
        "completion_tokens",
        "output_tokens",
    )
    details = raw_usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = raw_usage.get("input_tokens_details")
    cached_input_tokens = (
        _first_int(details, "cached_tokens", "cache_read_tokens")
        if isinstance(details, dict)
        else None
    )
    cached_input_tokens = _first_not_none(
        cached_input_tokens,
        _first_int(
            raw_usage,
            "prompt_cache_hit_tokens",
            "cache_read_input_tokens",
            "cached_tokens",
        ),
    )
    cache_miss_tokens = _first_int(
        raw_usage,
        "prompt_cache_miss_tokens",
        "cache_miss_input_tokens",
    )
    if input_tokens is None and (
        cached_input_tokens is not None or cache_miss_tokens is not None
    ):
        input_tokens = int(cached_input_tokens or 0) + int(cache_miss_tokens or 0)
    total_tokens = _first_int(raw_usage, "total_tokens")
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    if input_tokens is not None:
        cached_input_tokens = max(
            0,
            min(int(cached_input_tokens or 0), input_tokens),
        )
    available = any(
        key in raw_usage
        for key in (
            "prompt_tokens",
            "input_tokens",
            "completion_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
    )
    return {
        "available": available,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def calculate_cost_nanos(
    *,
    usage: dict[str, int | bool | None],
    price: ModelPrice,
) -> int:
    input_tokens = Decimal(int(usage["input_tokens"] or 0))
    cached_tokens = Decimal(int(usage["cached_input_tokens"] or 0))
    output_tokens = Decimal(int(usage["output_tokens"] or 0))
    uncached_tokens = max(Decimal(0), input_tokens - cached_tokens)
    amount = (
        cached_tokens * price.input_cache_hit_per_million
        + uncached_tokens * price.input_cache_miss_per_million
        + output_tokens * price.output_per_million
    ) / _TOKENS_PER_MILLION
    return int(
        (amount * _NANOS_PER_UNIT).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _first_int(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)) and raw >= 0:
            return int(raw)
    return None


def _first_not_none(*values: int | None) -> int | None:
    return next((value for value in values if value is not None), None)


def _empty_totals() -> dict[str, Any]:
    return {
        "calls": 0,
        "measured_calls": 0,
        "priced_calls": 0,
        "unmeasured_calls": 0,
        "unpriced_calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_nanos": 0,
    }


def _accumulate(bucket: dict[str, Any], event: dict[str, Any]) -> None:
    bucket["calls"] += 1
    usage_available = bool(event["usage_available"])
    price_available = bool(event["price_available"])
    cost_available = event["cost_nanos"] is not None
    bucket["measured_calls"] += int(usage_available)
    bucket["priced_calls"] += int(cost_available)
    bucket["unmeasured_calls"] += int(not usage_available)
    bucket["unpriced_calls"] += int(usage_available and not price_available)
    for field in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_nanos",
    ):
        bucket[field] += int(event[field] or 0)


def _finalize_totals(bucket: dict[str, Any]) -> dict[str, Any]:
    result = dict(bucket)
    result["cost_cny"] = round(int(result.pop("cost_nanos", 0)) / 1_000_000_000, 9)
    input_tokens = int(result.get("input_tokens", 0))
    cached_tokens = int(result.get("cached_input_tokens", 0))
    result["cache_hit_rate"] = (
        round(cached_tokens / input_tokens, 4) if input_tokens else None
    )
    return result


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    cost = (
        round(int(event["cost_nanos"]) / 1_000_000_000, 9)
        if event["cost_nanos"] is not None
        else None
    )
    return {
        "id": event["id"],
        "operation": event["operation"],
        "provider": event["provider"],
        "provider_label": provider_label(event["provider"]),
        "provider_code": event["provider_code"],
        "model": event["model"],
        "kind": event["kind"],
        "input_tokens": event["input_tokens"],
        "cached_input_tokens": event["cached_input_tokens"],
        "output_tokens": event["output_tokens"],
        "total_tokens": event["total_tokens"],
        "usage_available": bool(event["usage_available"]),
        "price_available": bool(event["price_available"]),
        "cost_cny": cost,
        "currency": event["currency"],
        "price_key": event["price_key"],
        "pricing_as_of": event["pricing_as_of"],
        "pricing_source_url": event["pricing_source_url"],
        "created_at": event["created_at"],
    }


def _bounded_text(value: str, max_chars: int) -> str:
    return str(value or "").strip()[:max_chars]
