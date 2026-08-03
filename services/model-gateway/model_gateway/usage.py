from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from model_gateway.routing import RouteTarget
from model_gateway.models import PricingConfig, PricingTier


_INIT_LOCK = threading.Lock()


@dataclass(slots=True)
class UsageCapture:
    usage: dict[str, Any] | None = None
    response_model: str = ""
    request_id: str = ""
    saw_done: bool = False
    malformed: bool = False
    _buffer: bytes = field(default=b"", init=False, repr=False)

    def feed(self, chunk: bytes) -> None:
        if self.malformed:
            return
        self._buffer += chunk
        if len(self._buffer) > 1_000_000:
            self._buffer = b""
            self.malformed = True
            return
        while True:
            boundary = _first_sse_boundary(self._buffer)
            if boundary is None:
                break
            index, separator = boundary
            event = self._buffer[:index]
            self._buffer = self._buffer[index + len(separator) :]
            data_lines = [
                line[5:].lstrip(b" ")
                for line in event.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
                if line.startswith(b"data:")
            ]
            if not data_lines:
                continue
            data = b"\n".join(data_lines).strip()
            if data == b"[DONE]":
                self.saw_done = True
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                self.malformed = True
                continue
            if isinstance(payload, dict):
                self._extract(payload)

    def from_non_stream(self, content: bytes) -> None:
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            self.malformed = True
            return
        if isinstance(payload, dict):
            self._extract(payload)

    def _extract(self, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage = dict(usage)
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            self.response_model = _safe_metadata_id(model, max_length=300, allow_slash=True)
        request_id = payload.get("request_id") or payload.get("id")
        if isinstance(request_id, str) and request_id.strip():
            self.request_id = _safe_metadata_id(
                request_id, max_length=300, allow_slash=False
            )


class UsageStore:
    """Metadata-only usage storage. Request and response bodies are never accepted."""

    def __init__(self, path: Path):
        self.path = path

    def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _INIT_LOCK, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    channel_operator TEXT NOT NULL,
                    model_author TEXT NOT NULL DEFAULT '',
                    upstream_model TEXT NOT NULL,
                    response_model TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    request_id TEXT NOT NULL,
                    pricing_id TEXT NOT NULL DEFAULT '',
                    pricing_snapshot TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT '',
                    estimated_cost TEXT NOT NULL DEFAULT '',
                    cost_complete INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._ensure_columns(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_client ON usage_events(client_id, created_at DESC)"
            )

    def record(
        self,
        *,
        client_id: str,
        kind: str,
        route_id: str,
        target: RouteTarget | None,
        status_code: int,
        latency_ms: int,
        attempts: int,
        complete: bool,
        capture: UsageCapture,
        pricing_id: str = "",
        pricing: PricingConfig | None = None,
    ) -> str:
        usage = _parse_usage(capture.usage)
        estimated_cost, cost_complete = estimate_cost(usage, pricing)
        pricing_snapshot = (
            pricing.model_dump_json(exclude_none=True) if pricing is not None else ""
        )
        event_id = f"use_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_events (
                    id, created_at, client_id, kind, route_id,
                    deployment_id, connection_id, channel_operator, model_author,
                    upstream_model,
                    response_model, status_code, latency_ms, attempts, complete,
                    input_tokens, cached_input_tokens, output_tokens, total_tokens,
                    request_id, pricing_id, pricing_snapshot, currency,
                    estimated_cost, cost_complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    datetime.now(UTC).isoformat(),
                    client_id[:120],
                    "embedding" if kind == "embedding" else "chat",
                    route_id[:120],
                    target.deployment_id[:120] if target else "",
                    target.connection_id[:120] if target else "",
                    target.connection.channel_operator[:120] if target else "",
                    target.deployment.model_author[:300] if target else "",
                    target.deployment.upstream_model[:300] if target else "",
                    capture.response_model[:300],
                    int(status_code),
                    max(0, int(latency_ms)),
                    max(0, int(attempts)),
                    int(bool(complete)),
                    usage["input_tokens"],
                    usage["cached_input_tokens"],
                    usage["output_tokens"],
                    usage["total_tokens"],
                    capture.request_id,
                    pricing_id[:120],
                    pricing_snapshot,
                    pricing.currency if pricing is not None else "",
                    str(estimated_cost) if estimated_cost is not None else "",
                    int(cost_complete),
                ),
            )
        return event_id

    def summary(self, *, days: int = 30) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=max(1, days))
        with self._connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN complete = 1 THEN 1 ELSE 0 END) AS complete_calls,
                       SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                       SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                       SUM(COALESCE(total_tokens, 0)) AS total_tokens
                FROM usage_events WHERE created_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT deployment_id, connection_id, channel_operator, model_author,
                       upstream_model,
                       COUNT(*) AS calls, SUM(COALESCE(total_tokens, 0)) AS total_tokens
                FROM usage_events WHERE created_at >= ?
                GROUP BY deployment_id, connection_id, channel_operator, model_author,
                         upstream_model
                ORDER BY calls DESC
                """,
                (since.isoformat(),),
            ).fetchall()
            cost_rows = connection.execute(
                """
                SELECT currency, estimated_cost, cost_complete, status_code
                FROM usage_events
                WHERE created_at >= ?
                """,
                (since.isoformat(),),
            ).fetchall()
        costs: dict[str, Decimal] = {}
        incomplete_cost_calls = 0
        for row in cost_rows:
            if 200 <= int(row["status_code"] or 0) < 300 and not row["cost_complete"]:
                incomplete_cost_calls += 1
            if not row["estimated_cost"] or not row["cost_complete"]:
                continue
            currency = str(row["currency"] or "")
            try:
                value = Decimal(str(row["estimated_cost"]))
            except Exception:
                continue
            costs[currency] = costs.get(currency, Decimal("0")) + value
        return {
            "days": max(1, days),
            "calls": int(totals["calls"] or 0),
            "complete_calls": int(totals["complete_calls"] or 0),
            "input_tokens": int(totals["input_tokens"] or 0),
            "output_tokens": int(totals["output_tokens"] or 0),
            "total_tokens": int(totals["total_tokens"] or 0),
            "estimated_costs": {
                currency: str(value) for currency, value in sorted(costs.items()) if currency
            },
            "incomplete_cost_calls": incomplete_cost_calls,
            "deployments": [dict(row) for row in rows],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=_ClosingSQLiteConnection)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(usage_events)")
        }
        definitions = {
            "model_author": "TEXT NOT NULL DEFAULT ''",
            "pricing_id": "TEXT NOT NULL DEFAULT ''",
            "pricing_snapshot": "TEXT NOT NULL DEFAULT ''",
            "currency": "TEXT NOT NULL DEFAULT ''",
            "estimated_cost": "TEXT NOT NULL DEFAULT ''",
            "cost_complete": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in definitions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE usage_events ADD COLUMN {name} {definition}"
                )


class _ClosingSQLiteConnection(sqlite3.Connection):
    """Make ``with`` commit/rollback and close, not just commit/rollback."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def _parse_usage(raw: dict[str, Any] | None) -> dict[str, int | None]:
    usage = raw or {}
    prompt = _integer(usage, "prompt_tokens", "input_tokens")
    completion = _integer(usage, "completion_tokens", "output_tokens")
    total = _integer(usage, "total_tokens")
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    cached = None
    if isinstance(details, dict):
        cached = _integer(details, "cached_tokens", "cache_read_input_tokens")
    if cached is None:
        cached = _integer(
            usage,
            "cache_read_input_tokens",
            "prompt_cache_hit_tokens",
            "cached_tokens",
        )
    return {
        "input_tokens": prompt,
        "cached_input_tokens": cached,
        "output_tokens": completion,
        "total_tokens": total,
    }


def estimate_cost(
    usage: dict[str, int | None],
    pricing: PricingConfig | None,
) -> tuple[Decimal | None, bool]:
    """Return an auditable estimate; missing usage/rates stay explicitly incomplete."""

    if pricing is None or pricing.mode != "per_token" or not pricing.tiers:
        return None, False
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None, False
    tier = _select_tier(pricing.tiers, input_tokens)
    if tier is None:
        return None, False
    unit = Decimal(pricing.unit_tokens)
    total = Decimal("0")
    complete = True

    cached_tokens = usage.get("cached_input_tokens")
    if input_tokens is not None:
        if cached_tokens is not None and cached_tokens > input_tokens:
            return None, False
        if cached_tokens is not None and cached_tokens > 0:
            uncached_tokens = max(0, input_tokens - cached_tokens)
            if tier.input is None or tier.cached_input is None:
                complete = False
            else:
                total += Decimal(uncached_tokens) * tier.input / unit
                total += Decimal(cached_tokens) * tier.cached_input / unit
        elif tier.input is None:
            complete = False
        else:
            total += Decimal(input_tokens) * tier.input / unit
    else:
        complete = False

    if output_tokens is None:
        complete = False
    elif tier.output is None:
        complete = False
    else:
        total += Decimal(output_tokens) * tier.output / unit
    return total, complete


def _select_tier(
    tiers: list[PricingTier], input_tokens: int | None
) -> PricingTier | None:
    if input_tokens is None:
        return tiers[0] if len(tiers) == 1 else None
    for tier in tiers:
        if tier.max_input_tokens is None or input_tokens <= tier.max_input_tokens:
            return tier
    return None


def _integer(mapping: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = mapping.get(name)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 9_223_372_036_854_775_807
        ):
            return value
    return None


def _first_sse_boundary(buffer: bytes) -> tuple[int, bytes] | None:
    matches = [
        (index, separator)
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := buffer.find(separator)) >= 0
    ]
    return min(matches, key=lambda item: item[0]) if matches else None


def _safe_metadata_id(value: str, *, max_length: int, allow_slash: bool) -> str:
    normalized = value.strip()
    character_class = r"A-Za-z0-9._:/-" if allow_slash else r"A-Za-z0-9._:-"
    if len(normalized) > max_length or not re.fullmatch(
        rf"[{character_class}]+", normalized
    ):
        return ""
    return normalized
