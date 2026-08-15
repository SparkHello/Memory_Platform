from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from model_gateway.routing import RouteTarget
from model_gateway_contracts import PricingConfig, PricingTier


_INIT_LOCK = threading.Lock()
RAW_RETENTION_DAYS = 90
DAILY_RETENTION_DAYS = 365
USAGE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


@dataclass(frozen=True, slots=True)
class UsageMetadata:
    correlation_id: str = ""
    operation: str = ""
    user_tag: str = ""

    def __post_init__(self) -> None:
        for label, value in (
            ("correlation_id", self.correlation_id),
            ("operation", self.operation),
            ("user_tag", self.user_tag),
        ):
            if value and not USAGE_TAG_PATTERN.fullmatch(value):
                raise ValueError(
                    f"{label} 必须是 1-120 字符的无空白 opaque ASCII ID"
                )


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
            self.response_model = safe_metadata_id(model, max_length=300, allow_slash=True)
        request_id = payload.get("request_id") or payload.get("id")
        if isinstance(request_id, str) and request_id.strip():
            self.request_id = safe_metadata_id(
                request_id, max_length=300, allow_slash=False
            )


ATTEMPT_OUTCOMES = frozenset(
    {"success", "http_error", "connect_failure", "ambiguous_failure"}
)
ATTEMPT_FAILURE_CLASSES = frozenset(
    {
        "none",
        "connect_error",
        "connect_timeout",
        "pool_timeout",
        "read_timeout",
        "write_timeout",
        "read_error",
        "write_error",
        "protocol_error",
        "empty_stream",
        "response_too_large",
        "invalid_embedding_response",
        "http_auth",
        "http_billing",
        "http_model_not_found",
        "http_rate_limit",
        "http_server",
        "http_redirect",
        "http_other",
        "other_network",
    }
)


@dataclass(slots=True)
class AttemptTrace:
    """Metadata-only trace for one actual upstream HTTP send.

    The type deliberately has no field for request/response bodies or exception
    text. ``failure_class`` and ``outcome`` are finite labels so provider error
    prose cannot accidentally enter the ledger.
    """

    attempt_index: int
    target: RouteTarget
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status_code: int | None = None
    latency_ms: int = 0
    outcome: str = "connect_failure"
    failure_class: str = "other_network"
    request_sent: bool = False
    billable_unknown: bool = False
    response_complete: bool = False
    capture: UsageCapture = field(default_factory=UsageCapture)

    def __post_init__(self) -> None:
        if self.attempt_index < 1:
            raise ValueError("attempt_index 必须从 1 开始")
        if self.outcome not in ATTEMPT_OUTCOMES:
            raise ValueError("未知 attempt outcome")
        if self.failure_class not in ATTEMPT_FAILURE_CLASSES:
            raise ValueError("未知 attempt failure_class")


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
                    correlation_id TEXT NOT NULL DEFAULT '',
                    operation TEXT NOT NULL DEFAULT '',
                    user_tag TEXT NOT NULL DEFAULT '',
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
                """
                CREATE TABLE IF NOT EXISTS attempt_events (
                    id TEXT PRIMARY KEY,
                    usage_event_id TEXT NOT NULL,
                    attempt_index INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    channel_operator TEXT NOT NULL,
                    model_author TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    response_model TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    status_code INTEGER,
                    latency_ms INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    failure_class TEXT NOT NULL,
                    request_sent INTEGER NOT NULL,
                    billable_unknown INTEGER NOT NULL,
                    response_complete INTEGER NOT NULL,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    pricing_id TEXT NOT NULL,
                    pricing_snapshot TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    estimated_cost TEXT NOT NULL,
                    cost_complete INTEGER NOT NULL,
                    UNIQUE(usage_event_id, attempt_index),
                    FOREIGN KEY(usage_event_id) REFERENCES usage_events(id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_client ON usage_events(client_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_correlation "
                "ON usage_events(client_id, correlation_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_operation_tag "
                "ON usage_events(client_id, operation, user_tag, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempt_created "
                "ON attempt_events(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempt_usage "
                "ON attempt_events(usage_event_id, attempt_index)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_daily (
                    day TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    channel_operator TEXT NOT NULL,
                    model_author TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    user_tag TEXT NOT NULL,
                    calls INTEGER NOT NULL,
                    complete_calls INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    incomplete_cost_calls INTEGER NOT NULL,
                    PRIMARY KEY (
                        day, client_id, kind, route_id, deployment_id,
                        connection_id, channel_operator, model_author,
                        upstream_model, operation, user_tag
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cost_daily (
                    day TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    user_tag TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    estimated_cost TEXT NOT NULL,
                    known_attempts INTEGER NOT NULL,
                    unknown_attempts INTEGER NOT NULL,
                    not_sent_attempts INTEGER NOT NULL,
                    PRIMARY KEY (day, client_id, operation, user_tag, currency)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_daily_lookup "
                "ON usage_daily(client_id, operation, user_tag, day DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_daily_lookup "
                "ON cost_daily(client_id, operation, user_tag, day DESC)"
            )
        self.prune(vacuum=False)

    def probe_writable(self) -> None:
        """Verify that the ledger can acquire a writer transaction.

        The transaction is rolled back without changing logical data.  Opening
        the database file itself is checked separately by the storage monitor;
        this catches SQLite-level read-only, corruption and writer failures.
        """

        with sqlite3.connect(
            self.path,
            timeout=0.1,
            factory=_ClosingSQLiteConnection,
        ) as connection:
            connection.execute("PRAGMA busy_timeout=100")
            result = connection.execute("PRAGMA quick_check(1)").fetchone()
            if result is None or str(result[0]).lower() != "ok":
                raise sqlite3.DatabaseError("usage ledger integrity check failed")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required = {
                "usage_events",
                "attempt_events",
                "usage_daily",
                "cost_daily",
            }
            if not required.issubset(tables):
                raise sqlite3.DatabaseError("usage ledger schema is incomplete")
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                if (
                    isinstance(code, int)
                    and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                ) or "locked" in str(exc).casefold():
                    # A concurrent short writer proves neither disk exhaustion
                    # nor a read-only ledger. The real record path retains its
                    # normal 30-second busy timeout.
                    return
                raise
            connection.rollback()

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
        attempt_traces: Sequence[AttemptTrace] = (),
        pricing_catalog: Mapping[str, PricingConfig] | None = None,
        metadata: UsageMetadata | None = None,
    ) -> str:
        usage_metadata = metadata or UsageMetadata()
        usage = _parse_usage(capture.usage)
        estimated_cost, cost_complete = estimate_cost(usage, pricing, kind=kind)
        pricing_snapshot = (
            pricing.model_dump_json(exclude_none=True) if pricing is not None else ""
        )
        event_id = f"use_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_events (
                    id, created_at, client_id, kind, route_id,
                    correlation_id, operation, user_tag,
                    deployment_id, connection_id, channel_operator, model_author,
                    upstream_model,
                    response_model, status_code, latency_ms, attempts, complete,
                    input_tokens, cached_input_tokens, output_tokens, total_tokens,
                    request_id, pricing_id, pricing_snapshot, currency,
                    estimated_cost, cost_complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    datetime.now(UTC).isoformat(),
                    client_id[:120],
                    "embedding" if kind == "embedding" else "chat",
                    route_id[:120],
                    usage_metadata.correlation_id,
                    usage_metadata.operation,
                    usage_metadata.user_tag,
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
            for trace in attempt_traces:
                self._record_attempt(
                    connection,
                    usage_event_id=event_id,
                    client_id=client_id,
                    kind=kind,
                    route_id=route_id,
                    trace=trace,
                    pricing_catalog=pricing_catalog or {},
                )
        return event_id

    @staticmethod
    def _record_attempt(
        connection: sqlite3.Connection,
        *,
        usage_event_id: str,
        client_id: str,
        kind: str,
        route_id: str,
        trace: AttemptTrace,
        pricing_catalog: Mapping[str, PricingConfig],
    ) -> None:
        if trace.outcome not in ATTEMPT_OUTCOMES:
            raise ValueError("未知 attempt outcome")
        if trace.failure_class not in ATTEMPT_FAILURE_CLASSES:
            raise ValueError("未知 attempt failure_class")
        target = trace.target
        usage = _parse_usage(trace.capture.usage)
        pricing_id = target.deployment.pricing or ""
        pricing = pricing_catalog.get(pricing_id) if pricing_id else None
        if not trace.request_sent:
            estimated_cost: Decimal | None = Decimal("0")
            cost_complete = True
        else:
            estimated_cost, cost_complete = estimate_cost(
                usage,
                pricing,
                kind="embedding" if kind == "embedding" else "chat",
            )
        pricing_snapshot = (
            pricing.model_dump_json(exclude_none=True) if pricing is not None else ""
        )
        connection.execute(
            """
            INSERT INTO attempt_events (
                id, usage_event_id, attempt_index, created_at, client_id, kind,
                route_id, deployment_id, connection_id, channel_operator,
                model_author, upstream_model, response_model, request_id,
                status_code, latency_ms, outcome, failure_class, request_sent,
                billable_unknown, response_complete, input_tokens,
                cached_input_tokens, output_tokens, total_tokens, pricing_id,
                pricing_snapshot, currency, estimated_cost, cost_complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"att_{uuid4().hex}",
                usage_event_id,
                trace.attempt_index,
                trace.created_at,
                client_id[:120],
                "embedding" if kind == "embedding" else "chat",
                route_id[:120],
                target.deployment_id[:120],
                target.connection_id[:120],
                target.connection.channel_operator[:120],
                target.deployment.model_author[:300],
                target.deployment.upstream_model[:300],
                trace.capture.response_model[:300],
                trace.capture.request_id[:300],
                trace.status_code,
                max(0, int(trace.latency_ms)),
                trace.outcome,
                trace.failure_class,
                int(trace.request_sent),
                int(trace.billable_unknown or (trace.request_sent and not cost_complete)),
                int(trace.response_complete),
                usage["input_tokens"],
                usage["cached_input_tokens"],
                usage["output_tokens"],
                usage["total_tokens"],
                pricing_id[:120],
                pricing_snapshot,
                pricing.currency if pricing is not None else "",
                str(estimated_cost) if estimated_cost is not None else "",
                int(cost_complete),
            ),
        )

    def summary(
        self,
        *,
        days: int = 30,
        client_id: str = "",
        operation: str = "",
        user_tag: str = "",
    ) -> dict[str, Any]:
        selected_days = min(DAILY_RETENTION_DAYS, max(1, int(days)))
        metadata = UsageMetadata(operation=operation, user_tag=user_tag)
        if client_id and not USAGE_TAG_PATTERN.fullmatch(client_id):
            raise ValueError("client_id 格式无效")
        since = datetime.now(UTC) - timedelta(days=selected_days)
        raw_where, raw_parameters = _usage_filters(
            since=since.isoformat(),
            client_id=client_id,
            operation=metadata.operation,
            user_tag=metadata.user_tag,
        )
        joined_where, _ = _usage_filters(
            since=since.isoformat(),
            client_id=client_id,
            operation=metadata.operation,
            user_tag=metadata.user_tag,
            alias="u",
        )
        daily_where, daily_parameters = _daily_filters(
            since=since.date().isoformat(),
            client_id=client_id,
            operation=metadata.operation,
            user_tag=metadata.user_tag,
        )
        with self._connect() as connection:
            totals = connection.execute(
                f"""
                SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN complete = 1 THEN 1 ELSE 0 END) AS complete_calls,
                       SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                       SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                       SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                       SUM(CASE WHEN status_code BETWEEN 200 AND 299
                                     AND cost_complete = 0 THEN 1 ELSE 0 END)
                           AS incomplete_cost_calls
                FROM usage_events WHERE {raw_where}
                """,
                raw_parameters,
            ).fetchone()
            daily_totals = connection.execute(
                f"""
                SELECT SUM(calls) AS calls, SUM(complete_calls) AS complete_calls,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(incomplete_cost_calls) AS incomplete_cost_calls
                FROM usage_daily WHERE {daily_where}
                """,
                daily_parameters,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT deployment_id, connection_id, channel_operator, model_author,
                       upstream_model, COUNT(*) AS calls,
                       SUM(COALESCE(total_tokens, 0)) AS total_tokens
                FROM usage_events WHERE {raw_where}
                GROUP BY deployment_id, connection_id, channel_operator, model_author,
                         upstream_model
                """,
                raw_parameters,
            ).fetchall()
            daily_rows = connection.execute(
                f"""
                SELECT deployment_id, connection_id, channel_operator, model_author,
                       upstream_model, SUM(calls) AS calls,
                       SUM(total_tokens) AS total_tokens
                FROM usage_daily WHERE {daily_where}
                GROUP BY deployment_id, connection_id, channel_operator, model_author,
                         upstream_model
                """,
                daily_parameters,
            ).fetchall()
            legacy_cost_rows = connection.execute(
                f"""
                SELECT u.currency, u.estimated_cost, u.cost_complete
                FROM usage_events AS u
                WHERE {joined_where}
                  AND u.attempts > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM attempt_events AS a
                    WHERE a.usage_event_id = u.id
                  )
                """,
                raw_parameters,
            ).fetchall()
            attempt_rows = connection.execute(
                f"""
                SELECT a.currency, a.estimated_cost, a.cost_complete,
                       a.request_sent, a.billable_unknown
                FROM attempt_events AS a
                JOIN usage_events AS u ON u.id = a.usage_event_id
                WHERE {joined_where}
                """,
                raw_parameters,
            ).fetchall()
            daily_cost_rows = connection.execute(
                f"""
                SELECT currency, estimated_cost, known_attempts,
                       unknown_attempts, not_sent_attempts
                FROM cost_daily WHERE {daily_where}
                """,
                daily_parameters,
            ).fetchall()

        costs: dict[str, Decimal] = {}
        for row in legacy_cost_rows:
            value = _decimal_or_none(row["estimated_cost"])
            currency = str(row["currency"] or "")
            if row["cost_complete"] and value is not None and currency:
                costs[currency] = costs.get(currency, Decimal("0")) + value
        currency_attempts: dict[str, dict[str, Any]] = {}
        known_attempts = 0
        unknown_attempts = 0
        not_sent_attempts = 0
        for row in attempt_rows:
            currency = str(row["currency"] or "") or "UNPRICED"
            bucket = _currency_bucket(currency_attempts, currency)
            if not row["request_sent"]:
                not_sent_attempts += 1
            value = _decimal_or_none(row["estimated_cost"])
            if row["cost_complete"] and value is not None:
                known_attempts += 1
                bucket["known_attempts"] += 1
                bucket["estimated_cost"] += value
                if currency != "UNPRICED":
                    costs[currency] = costs.get(currency, Decimal("0")) + value
            elif row["request_sent"] or row["billable_unknown"]:
                unknown_attempts += 1
                bucket["unknown_attempts"] += 1
        for row in daily_cost_rows:
            currency = str(row["currency"] or "UNPRICED")
            bucket = _currency_bucket(currency_attempts, currency)
            value = _decimal_or_none(row["estimated_cost"]) or Decimal("0")
            known = int(row["known_attempts"] or 0)
            unknown = int(row["unknown_attempts"] or 0)
            not_sent = int(row["not_sent_attempts"] or 0)
            known_attempts += known
            unknown_attempts += unknown
            not_sent_attempts += not_sent
            bucket["known_attempts"] += known
            bucket["unknown_attempts"] += unknown
            bucket["estimated_cost"] += value
            if currency != "UNPRICED":
                costs[currency] = costs.get(currency, Decimal("0")) + value

        deployments: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in [*rows, *daily_rows]:
            key = tuple(
                str(row[name] or "")
                for name in (
                    "deployment_id",
                    "connection_id",
                    "channel_operator",
                    "model_author",
                    "upstream_model",
                )
            )
            bucket = deployments.setdefault(
                key,
                {
                    "deployment_id": key[0],
                    "connection_id": key[1],
                    "channel_operator": key[2],
                    "model_author": key[3],
                    "upstream_model": key[4],
                    "calls": 0,
                    "total_tokens": 0,
                },
            )
            bucket["calls"] += int(row["calls"] or 0)
            bucket["total_tokens"] += int(row["total_tokens"] or 0)

        def combined(name: str) -> int:
            return int(totals[name] or 0) + int(daily_totals[name] or 0)

        return {
            "days": selected_days,
            "filters": {
                "client_id": client_id,
                "operation": metadata.operation,
                "user_tag": metadata.user_tag,
            },
            "calls": combined("calls"),
            "complete_calls": combined("complete_calls"),
            "input_tokens": combined("input_tokens"),
            "output_tokens": combined("output_tokens"),
            "total_tokens": combined("total_tokens"),
            "estimated_costs": {
                currency: str(value)
                for currency, value in sorted(costs.items())
                if currency
            },
            "incomplete_cost_calls": combined("incomplete_cost_calls"),
            "attempts": {
                "recorded": len(attempt_rows)
                + sum(
                    int(row["known_attempts"] or 0)
                    + int(row["unknown_attempts"] or 0)
                    for row in daily_cost_rows
                ),
                "known_cost_attempts": known_attempts,
                "unknown_cost_attempts": unknown_attempts,
                "not_sent_attempts": not_sent_attempts,
                "legacy_logical_events_without_attempts": len(legacy_cost_rows),
                "by_currency": {
                    currency: {
                        "known_attempts": values["known_attempts"],
                        "unknown_attempts": values["unknown_attempts"],
                        "estimated_cost": str(values["estimated_cost"]),
                    }
                    for currency, values in sorted(currency_attempts.items())
                },
            },
            "deployments": sorted(
                deployments.values(), key=lambda row: (-row["calls"], row["deployment_id"])
            ),
            "retention": {
                "raw_days": RAW_RETENTION_DAYS,
                "daily_days": DAILY_RETENTION_DAYS,
            },
        }

    def events(
        self,
        *,
        client_id: str = "",
        event_id: str = "",
        correlation_id: str = "",
        operation: str = "",
        user_tag: str = "",
        days: int = RAW_RETENTION_DAYS,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        metadata = UsageMetadata(
            correlation_id=correlation_id,
            operation=operation,
            user_tag=user_tag,
        )
        for label, value in (("client_id", client_id), ("event_id", event_id)):
            if value and not USAGE_TAG_PATTERN.fullmatch(value):
                raise ValueError(f"{label} 格式无效")
        since = datetime.now(UTC) - timedelta(
            days=min(RAW_RETENTION_DAYS, max(1, int(days)))
        )
        clauses = ["created_at >= ?"]
        parameters: list[Any] = [since.isoformat()]
        for name, value in (
            ("id", event_id),
            ("client_id", client_id),
            ("correlation_id", metadata.correlation_id),
            ("operation", metadata.operation),
            ("user_tag", metadata.user_tag),
        ):
            if value:
                clauses.append(f"{name} = ?")
                parameters.append(value)
        parameters.append(min(500, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, created_at, client_id, kind, route_id,
                       correlation_id, operation, user_tag, deployment_id,
                       connection_id, channel_operator, model_author,
                       upstream_model, response_model, status_code, latency_ms,
                       attempts, complete, input_tokens, cached_input_tokens,
                       output_tokens, total_tokens, request_id, pricing_id,
                       currency, estimated_cost, cost_complete
                FROM usage_events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
            attempt_rows = (
                connection.execute(
                    f"""
                    SELECT usage_event_id, currency, estimated_cost,
                           cost_complete, request_sent, billable_unknown
                    FROM attempt_events
                    WHERE usage_event_id IN ({','.join('?' for _ in rows)})
                    ORDER BY usage_event_id, attempt_index
                    """,
                    [str(row["id"]) for row in rows],
                ).fetchall()
                if rows
                else []
            )
        attempts_by_event: dict[str, list[sqlite3.Row]] = {}
        for attempt in attempt_rows:
            attempts_by_event.setdefault(str(attempt["usage_event_id"]), []).append(
                attempt
            )
        result: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            attempts = attempts_by_event.get(str(raw["id"]), [])
            costs: dict[str, Decimal] = {}
            unknown = 0
            if attempts:
                for attempt in attempts:
                    currency = str(attempt["currency"] or "") or "UNPRICED"
                    value = _decimal_or_none(attempt["estimated_cost"])
                    if attempt["cost_complete"] and value is not None:
                        if currency != "UNPRICED":
                            costs[currency] = costs.get(currency, Decimal("0")) + value
                    elif attempt["request_sent"] or attempt["billable_unknown"]:
                        unknown += 1
            else:
                currency = str(raw["currency"] or "")
                value = _decimal_or_none(raw["estimated_cost"])
                if raw["cost_complete"] and value is not None and currency:
                    costs[currency] = value
                elif int(raw["attempts"] or 0) > 0:
                    unknown = 1
            row["attempt_costs"] = {
                currency: str(value) for currency, value in sorted(costs.items())
            }
            row["unknown_cost_attempts"] = unknown
            result.append(row)
        return result

    def prune(
        self,
        *,
        raw_days: int = RAW_RETENTION_DAYS,
        daily_days: int = DAILY_RETENTION_DAYS,
        vacuum: bool = False,
        now: datetime | None = None,
    ) -> dict[str, int | bool]:
        selected_raw_days = max(1, int(raw_days))
        selected_daily_days = max(selected_raw_days, int(daily_days))
        current = (now or datetime.now(UTC)).astimezone(UTC)
        raw_cutoff = current - timedelta(days=selected_raw_days)
        daily_cutoff = (current - timedelta(days=selected_daily_days)).date().isoformat()
        logical_groups: dict[tuple[str, ...], dict[str, int]] = {}
        cost_groups: dict[tuple[str, ...], dict[str, Any]] = {}
        with self._connect() as connection:
            # Select, roll up and delete must be one writer transaction. Without
            # this lock, two gateway processes starting together could both
            # aggregate the same raw rows before either one deletes them.
            connection.execute("BEGIN IMMEDIATE")
            old_events = connection.execute(
                "SELECT * FROM usage_events WHERE created_at < ? ORDER BY created_at",
                (raw_cutoff.isoformat(),),
            ).fetchall()
            if old_events:
                attempt_rows = connection.execute(
                    """
                    SELECT a.*, u.client_id AS logical_client_id,
                           u.operation AS logical_operation,
                           u.user_tag AS logical_user_tag,
                           u.created_at AS logical_created_at
                    FROM attempt_events AS a
                    JOIN usage_events AS u ON u.id = a.usage_event_id
                    WHERE u.created_at < ?
                    """,
                    (raw_cutoff.isoformat(),),
                ).fetchall()
                event_attempt_ids = {
                    str(row["usage_event_id"]) for row in attempt_rows
                }
                for row in old_events:
                    day = str(row["created_at"])[:10]
                    key = (
                        day,
                        str(row["client_id"]),
                        str(row["kind"]),
                        str(row["route_id"]),
                        str(row["deployment_id"]),
                        str(row["connection_id"]),
                        str(row["channel_operator"]),
                        str(row["model_author"]),
                        str(row["upstream_model"]),
                        str(row["operation"]),
                        str(row["user_tag"]),
                    )
                    bucket = logical_groups.setdefault(
                        key,
                        {
                            "calls": 0,
                            "complete_calls": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "incomplete_cost_calls": 0,
                        },
                    )
                    bucket["calls"] += 1
                    bucket["complete_calls"] += int(bool(row["complete"]))
                    bucket["input_tokens"] += int(row["input_tokens"] or 0)
                    bucket["output_tokens"] += int(row["output_tokens"] or 0)
                    bucket["total_tokens"] += int(row["total_tokens"] or 0)
                    bucket["incomplete_cost_calls"] += int(
                        200 <= int(row["status_code"] or 0) < 300
                        and not row["cost_complete"]
                    )
                    if (
                        str(row["id"]) not in event_attempt_ids
                        and int(row["attempts"] or 0) > 0
                    ):
                        currency = str(row["currency"] or "") or "UNPRICED"
                        cost_key = (
                            day,
                            str(row["client_id"]),
                            str(row["operation"]),
                            str(row["user_tag"]),
                            currency,
                        )
                        cost_bucket = _cost_bucket(cost_groups, cost_key)
                        value = _decimal_or_none(row["estimated_cost"])
                        if row["cost_complete"] and value is not None:
                            cost_bucket["estimated_cost"] += value
                            cost_bucket["known_attempts"] += 1
                        else:
                            cost_bucket["unknown_attempts"] += 1
                for row in attempt_rows:
                    currency = str(row["currency"] or "") or "UNPRICED"
                    cost_key = (
                        str(row["logical_created_at"])[:10],
                        str(row["logical_client_id"]),
                        str(row["logical_operation"]),
                        str(row["logical_user_tag"]),
                        currency,
                    )
                    cost_bucket = _cost_bucket(cost_groups, cost_key)
                    if not row["request_sent"]:
                        cost_bucket["not_sent_attempts"] += 1
                    value = _decimal_or_none(row["estimated_cost"])
                    if row["cost_complete"] and value is not None:
                        cost_bucket["estimated_cost"] += value
                        cost_bucket["known_attempts"] += 1
                    elif row["request_sent"] or row["billable_unknown"]:
                        cost_bucket["unknown_attempts"] += 1

                for key, values in logical_groups.items():
                    connection.execute(
                        """
                        INSERT INTO usage_daily (
                            day, client_id, kind, route_id, deployment_id,
                            connection_id, channel_operator, model_author,
                            upstream_model, operation, user_tag, calls,
                            complete_calls, input_tokens, output_tokens,
                            total_tokens, incomplete_cost_calls
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT DO UPDATE SET
                            calls = calls + excluded.calls,
                            complete_calls = complete_calls + excluded.complete_calls,
                            input_tokens = input_tokens + excluded.input_tokens,
                            output_tokens = output_tokens + excluded.output_tokens,
                            total_tokens = total_tokens + excluded.total_tokens,
                            incomplete_cost_calls = incomplete_cost_calls
                                + excluded.incomplete_cost_calls
                        """,
                        (*key, *values.values()),
                    )
                for key, values in cost_groups.items():
                    existing = connection.execute(
                        """
                        SELECT estimated_cost, known_attempts, unknown_attempts,
                               not_sent_attempts
                        FROM cost_daily
                        WHERE day = ? AND client_id = ? AND operation = ?
                          AND user_tag = ? AND currency = ?
                        """,
                        key,
                    ).fetchone()
                    previous_cost = (
                        _decimal_or_none(existing["estimated_cost"])
                        if existing is not None
                        else None
                    ) or Decimal("0")
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO cost_daily (
                            day, client_id, operation, user_tag, currency,
                            estimated_cost, known_attempts, unknown_attempts,
                            not_sent_attempts
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            *key,
                            str(previous_cost + values["estimated_cost"]),
                            int(values["known_attempts"])
                            + int(existing["known_attempts"] if existing else 0),
                            int(values["unknown_attempts"])
                            + int(existing["unknown_attempts"] if existing else 0),
                            int(values["not_sent_attempts"])
                            + int(existing["not_sent_attempts"] if existing else 0),
                        ),
                    )
                connection.execute(
                    """
                    DELETE FROM attempt_events WHERE usage_event_id IN (
                        SELECT id FROM usage_events WHERE created_at < ?
                    )
                    """,
                    (raw_cutoff.isoformat(),),
                )
                connection.execute(
                    "DELETE FROM usage_events WHERE created_at < ?",
                    (raw_cutoff.isoformat(),),
                )
            daily_usage_deleted = connection.execute(
                "DELETE FROM usage_daily WHERE day < ?", (daily_cutoff,)
            ).rowcount
            daily_cost_deleted = connection.execute(
                "DELETE FROM cost_daily WHERE day < ?", (daily_cutoff,)
            ).rowcount
        if vacuum:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
        return {
            "raw_events_pruned": len(old_events),
            "daily_usage_deleted": max(0, int(daily_usage_deleted)),
            "daily_cost_deleted": max(0, int(daily_cost_deleted)),
            "vacuumed": bool(vacuum),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            factory=_ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(usage_events)")
        }
        definitions = {
            "model_author": "TEXT NOT NULL DEFAULT ''",
            "correlation_id": "TEXT NOT NULL DEFAULT ''",
            "operation": "TEXT NOT NULL DEFAULT ''",
            "user_tag": "TEXT NOT NULL DEFAULT ''",
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


def _usage_filters(
    *,
    since: str,
    client_id: str,
    operation: str,
    user_tag: str,
    alias: str = "",
) -> tuple[str, tuple[Any, ...]]:
    prefix = f"{alias}." if alias else ""
    clauses = [f"{prefix}created_at >= ?"]
    parameters: list[Any] = [since]
    for name, value in (
        ("client_id", client_id),
        ("operation", operation),
        ("user_tag", user_tag),
    ):
        if value:
            clauses.append(f"{prefix}{name} = ?")
            parameters.append(value)
    return " AND ".join(clauses), tuple(parameters)


def _daily_filters(
    *, since: str, client_id: str, operation: str, user_tag: str
) -> tuple[str, tuple[Any, ...]]:
    clauses = ["day >= ?"]
    parameters: list[Any] = [since]
    for name, value in (
        ("client_id", client_id),
        ("operation", operation),
        ("user_tag", user_tag),
    ):
        if value:
            clauses.append(f"{name} = ?")
            parameters.append(value)
    return " AND ".join(clauses), tuple(parameters)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _currency_bucket(
    values: dict[str, dict[str, Any]], currency: str
) -> dict[str, Any]:
    return values.setdefault(
        currency,
        {
            "known_attempts": 0,
            "unknown_attempts": 0,
            "estimated_cost": Decimal("0"),
        },
    )


def _cost_bucket(
    values: dict[tuple[str, ...], dict[str, Any]], key: tuple[str, ...]
) -> dict[str, Any]:
    return values.setdefault(
        key,
        {
            "estimated_cost": Decimal("0"),
            "known_attempts": 0,
            "unknown_attempts": 0,
            "not_sent_attempts": 0,
        },
    )


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
    *,
    kind: Literal["chat", "embedding"] = "chat",
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

    if kind == "embedding":
        return total, complete

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
        result = _usage_counter(mapping.get(name))
        if result is not None:
            return result
    return None


def _usage_counter(value: Any) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 9_223_372_036_854_775_807
    ):
        return value
    return None


_METADATA_USAGE_SCALARS = (
    "prompt_tokens",
    "input_tokens",
    "completion_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "prompt_cache_hit_tokens",
    "cached_tokens",
)
_METADATA_USAGE_DETAILS = ("prompt_tokens_details", "input_tokens_details")
_METADATA_USAGE_DETAIL_KEYS = ("cached_tokens", "cache_read_input_tokens")


def metadata_only_usage(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Whitelist scalar token counters from an untrusted ``usage`` object.

    The pricing-research ledger records this shape; ``UsageStore.record`` then
    normalizes it through ``_parse_usage`` like any other capture.
    """

    cleaned: dict[str, Any] = {
        name: value
        for name in _METADATA_USAGE_SCALARS
        if (value := _usage_counter(raw.get(name))) is not None
    }
    for detail_name in _METADATA_USAGE_DETAILS:
        details = raw.get(detail_name)
        if not isinstance(details, dict):
            continue
        clean_details = {
            name: value
            for name in _METADATA_USAGE_DETAIL_KEYS
            if (value := _usage_counter(details.get(name))) is not None
        }
        if clean_details:
            cleaned[detail_name] = clean_details
    return cleaned or None


def _first_sse_boundary(buffer: bytes) -> tuple[int, bytes] | None:
    matches = [
        (index, separator)
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := buffer.find(separator)) >= 0
    ]
    return min(matches, key=lambda item: item[0]) if matches else None


def safe_metadata_id(value: str, *, max_length: int, allow_slash: bool) -> str:
    normalized = value.strip()
    character_class = r"A-Za-z0-9._:/-" if allow_slash else r"A-Za-z0-9._:-"
    if len(normalized) > max_length or not re.fullmatch(
        rf"[{character_class}]+", normalized
    ):
        return ""
    return normalized
