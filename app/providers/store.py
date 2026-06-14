from pathlib import Path
import sqlite3
from uuid import uuid4

from app.memory.models import utc_now_iso
from app.providers.models import (
    BalanceAdjustment,
    BalanceRecord,
    ProviderConfig,
    ProviderModelConfig,
    ProvidersConfig,
    RouteConfig,
    RouterConfig,
    UsageEvent,
)


class ProviderStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_balances (
                    provider TEXT PRIMARY KEY,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_usage_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    conversation_id TEXT,
                    virtual_model TEXT,
                    provider TEXT,
                    upstream_model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    input_cost REAL,
                    output_cost REAL,
                    total_cost REAL,
                    currency TEXT,
                    estimated INTEGER DEFAULT 0,
                    status TEXT,
                    error_type TEXT,
                    created_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider_usage_created
                ON provider_usage_events(created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider_usage_provider_model_status
                ON provider_usage_events(provider, virtual_model, status, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_balance_adjustments (
                    id TEXT PRIMARY KEY,
                    provider TEXT,
                    amount_delta REAL,
                    balance_after REAL,
                    currency TEXT,
                    reason TEXT,
                    created_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider_adjustments_provider_created
                ON provider_balance_adjustments(provider, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_configs (
                    provider TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    timeout_seconds REAL NOT NULL DEFAULT 60,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_model_configs (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    api_format TEXT NOT NULL DEFAULT 'openai_compatible',
                    pricing_mode TEXT NOT NULL DEFAULT 'flat',
                    pricing_tiers_json TEXT NOT NULL DEFAULT '',
                    input_price_per_million REAL NOT NULL DEFAULT 0,
                    output_price_per_million REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider_model_configs_provider
                ON provider_model_configs(provider, enabled)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_model_configs_provider_upstream
                ON provider_model_configs(provider, upstream_model)
                """
            )
            _ensure_column(
                connection,
                "provider_model_configs",
                "api_format",
                "TEXT NOT NULL DEFAULT 'openai_compatible'",
            )
            _ensure_column(
                connection,
                "provider_model_configs",
                "pricing_mode",
                "TEXT NOT NULL DEFAULT 'flat'",
            )
            _ensure_column(
                connection,
                "provider_model_configs",
                "pricing_tiers_json",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_route_configs (
                    id TEXT PRIMARY KEY,
                    virtual_model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    provider_model_id TEXT,
                    priority INTEGER NOT NULL DEFAULT 100,
                    input_price_per_million REAL NOT NULL DEFAULT 0,
                    output_price_per_million REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    min_balance REAL NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            _ensure_column(connection, "provider_route_configs", "provider_model_id", "TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider_route_configs_virtual_enabled_priority
                ON provider_route_configs(virtual_model, enabled, priority)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provider_route_configs_provider
                ON provider_route_configs(provider)
                """
            )

    def load_sqlite_providers_config(self) -> ProvidersConfig:
        providers = {provider.id: provider for provider in self.list_provider_configs()}
        provider_models = {model.id: model for model in self.list_provider_model_configs()}
        routes = self.list_route_configs()
        enabled = _has_enabled_provider_route(providers, provider_models, routes)
        default_model = _first_enabled_route_model(providers, provider_models, routes)
        return ProvidersConfig(
            enabled=enabled,
            path=self.database_path,
            source="sqlite" if enabled else "legacy",
            router=RouterConfig(default_model=default_model, fallback_enabled=True),
            providers=providers,
            provider_models=provider_models,
            routes=routes,
        )

    def list_provider_configs(self) -> list[ProviderConfig]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_configs ORDER BY provider"
            ).fetchall()
        return [_provider_config_from_row(row) for row in rows]

    def get_provider_config(self, provider: str) -> ProviderConfig | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_configs WHERE provider = ?",
                (provider,),
            ).fetchone()
        return _provider_config_from_row(row) if row else None

    def upsert_provider_config(
        self,
        *,
        provider: str,
        name: str,
        base_url: str,
        api_key: str | None = None,
        enabled: bool = True,
        timeout_seconds: float = 60.0,
    ) -> ProviderConfig:
        now = utc_now_iso()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM provider_configs WHERE provider = ?",
                (provider,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            next_api_key = existing["api_key"] if existing and api_key is None else api_key
            connection.execute(
                """
                INSERT INTO provider_configs(
                    provider, name, base_url, api_key, enabled,
                    timeout_seconds, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    api_key = excluded.api_key,
                    enabled = excluded.enabled,
                    timeout_seconds = excluded.timeout_seconds,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    name,
                    base_url,
                    next_api_key,
                    1 if enabled else 0,
                    timeout_seconds,
                    created_at,
                    now,
                ),
            )
        return self.get_provider_config(provider)  # type: ignore[return-value]

    def patch_provider_config(
        self,
        *,
        provider: str,
        name: str | None = None,
        base_url: str | None = None,
        api_key_update: str | None = None,
        update_api_key: bool = False,
        enabled: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderConfig | None:
        current = self.get_provider_config(provider)
        if current is None:
            return None
        api_key = current.api_key
        if update_api_key:
            api_key = api_key_update or ""
        return self.upsert_provider_config(
            provider=provider,
            name=name if name is not None else current.name,
            base_url=base_url if base_url is not None else current.base_url,
            api_key=api_key,
            enabled=enabled if enabled is not None else current.enabled,
            timeout_seconds=(
                timeout_seconds if timeout_seconds is not None else current.timeout_seconds
            ),
        )

    def disable_provider_config(self, provider: str) -> ProviderConfig | None:
        return self.patch_provider_config(provider=provider, enabled=False)

    def list_provider_model_configs(self, provider: str | None = None) -> list[ProviderModelConfig]:
        query = "SELECT * FROM provider_model_configs"
        params: tuple[object, ...] = ()
        if provider:
            query += " WHERE provider = ?"
            params = (provider,)
        query += " ORDER BY provider, upstream_model"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_provider_model_config_from_row(row) for row in rows]

    def get_provider_model_config(self, model_id: str) -> ProviderModelConfig | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_model_configs WHERE id = ?",
                (model_id,),
            ).fetchone()
        return _provider_model_config_from_row(row) if row else None

    def create_provider_model_config(
        self,
        *,
        provider: str,
        upstream_model: str,
        display_name: str = "",
        api_format: str = "openai_compatible",
        pricing_mode: str = "flat",
        pricing_tiers_json: str = "",
        input_price_per_million: float = 0.0,
        output_price_per_million: float = 0.0,
        currency: str = "CNY",
        enabled: bool = True,
        model_id: str | None = None,
    ) -> ProviderModelConfig:
        now = utc_now_iso()
        model_id = model_id or str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_model_configs(
                    id, provider, upstream_model, display_name,
                    api_format, pricing_mode, pricing_tiers_json,
                    input_price_per_million, output_price_per_million,
                    currency, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_id,
                    provider,
                    upstream_model,
                    display_name,
                    api_format,
                    pricing_mode,
                    pricing_tiers_json,
                    input_price_per_million,
                    output_price_per_million,
                    currency,
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
        return self.get_provider_model_config(model_id)  # type: ignore[return-value]

    def patch_provider_model_config(
        self,
        *,
        model_id: str,
        provider: str | None = None,
        upstream_model: str | None = None,
        display_name: str | None = None,
        api_format: str | None = None,
        pricing_mode: str | None = None,
        pricing_tiers_json: str | None = None,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        currency: str | None = None,
        enabled: bool | None = None,
    ) -> ProviderModelConfig | None:
        current = self.get_provider_model_config(model_id)
        if current is None:
            return None
        now = utc_now_iso()
        next_provider = provider if provider is not None else current.provider
        next_upstream_model = (
            upstream_model if upstream_model is not None else current.upstream_model
        )
        next_display_name = display_name if display_name is not None else current.display_name
        next_api_format = api_format if api_format is not None else current.api_format
        next_pricing_mode = pricing_mode if pricing_mode is not None else current.pricing_mode
        next_pricing_tiers_json = (
            pricing_tiers_json
            if pricing_tiers_json is not None
            else current.pricing_tiers_json
        )
        next_input_price = (
            input_price_per_million
            if input_price_per_million is not None
            else current.input_price_per_million
        )
        next_output_price = (
            output_price_per_million
            if output_price_per_million is not None
            else current.output_price_per_million
        )
        next_currency = currency if currency is not None else current.currency
        next_enabled = enabled if enabled is not None else current.enabled
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE provider_model_configs
                SET provider = ?, upstream_model = ?, display_name = ?,
                    api_format = ?, pricing_mode = ?, pricing_tiers_json = ?,
                    input_price_per_million = ?, output_price_per_million = ?,
                    currency = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_provider,
                    next_upstream_model,
                    next_display_name,
                    next_api_format,
                    next_pricing_mode,
                    next_pricing_tiers_json,
                    next_input_price,
                    next_output_price,
                    next_currency,
                    1 if next_enabled else 0,
                    now,
                    model_id,
                ),
            )
            connection.execute(
                """
                UPDATE provider_route_configs
                SET provider = ?, upstream_model = ?,
                    input_price_per_million = ?, output_price_per_million = ?,
                    currency = ?, updated_at = ?
                WHERE provider_model_id = ?
                """,
                (
                    next_provider,
                    next_upstream_model,
                    next_input_price,
                    next_output_price,
                    next_currency,
                    now,
                    model_id,
                ),
            )
        return self.get_provider_model_config(model_id)

    def disable_provider_model_config(self, model_id: str) -> ProviderModelConfig | None:
        return self.patch_provider_model_config(model_id=model_id, enabled=False)

    def upsert_provider_model_by_identity(
        self,
        model: ProviderModelConfig,
    ) -> ProviderModelConfig:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM provider_model_configs
                WHERE provider = ? AND upstream_model = ?
                """,
                (model.provider, model.upstream_model),
            ).fetchone()
        if row:
            updated = self.patch_provider_model_config(
                model_id=row["id"],
                display_name=model.display_name,
                api_format=model.api_format,
                pricing_mode=model.pricing_mode,
                pricing_tiers_json=model.pricing_tiers_json,
                input_price_per_million=model.input_price_per_million,
                output_price_per_million=model.output_price_per_million,
                currency=model.currency,
                enabled=model.enabled,
            )
            return updated  # type: ignore[return-value]
        return self.create_provider_model_config(
            model_id=model.id,
            provider=model.provider,
            upstream_model=model.upstream_model,
            display_name=model.display_name,
            api_format=model.api_format,
            pricing_mode=model.pricing_mode,
            pricing_tiers_json=model.pricing_tiers_json,
            input_price_per_million=model.input_price_per_million,
            output_price_per_million=model.output_price_per_million,
            currency=model.currency,
            enabled=model.enabled,
        )

    def list_route_configs(self) -> list[RouteConfig]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_route_configs ORDER BY virtual_model, priority DESC"
            ).fetchall()
        return [_route_config_from_row(row) for row in rows]

    def get_route_config(self, route_id: str) -> RouteConfig | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_route_configs WHERE id = ?",
                (route_id,),
            ).fetchone()
        return _route_config_from_row(row) if row else None

    def create_route_config(
        self,
        *,
        virtual_model: str,
        provider: str,
        upstream_model: str,
        provider_model_id: str | None = None,
        priority: int = 100,
        input_price_per_million: float = 0.0,
        output_price_per_million: float = 0.0,
        currency: str = "CNY",
        min_balance: float = 0.0,
        enabled: bool = True,
        route_id: str | None = None,
    ) -> RouteConfig:
        now = utc_now_iso()
        route_id = route_id or str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_route_configs(
                    id, virtual_model, provider, upstream_model, provider_model_id, priority,
                    input_price_per_million, output_price_per_million,
                    currency, min_balance, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    virtual_model,
                    provider,
                    upstream_model,
                    provider_model_id,
                    priority,
                    input_price_per_million,
                    output_price_per_million,
                    currency,
                    min_balance,
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
        return self.get_route_config(route_id)  # type: ignore[return-value]

    def patch_route_config(
        self,
        *,
        route_id: str,
        virtual_model: str | None = None,
        provider: str | None = None,
        upstream_model: str | None = None,
        provider_model_id: str | None = None,
        priority: int | None = None,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        currency: str | None = None,
        min_balance: float | None = None,
        enabled: bool | None = None,
    ) -> RouteConfig | None:
        current = self.get_route_config(route_id)
        if current is None:
            return None
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE provider_route_configs
                SET virtual_model = ?, provider = ?, upstream_model = ?, provider_model_id = ?,
                    priority = ?,
                    input_price_per_million = ?, output_price_per_million = ?,
                    currency = ?, min_balance = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    virtual_model if virtual_model is not None else current.virtual_model,
                    provider if provider is not None else current.provider,
                    upstream_model if upstream_model is not None else current.upstream_model,
                    (
                        provider_model_id
                        if provider_model_id is not None
                        else current.provider_model_id
                    ),
                    priority if priority is not None else current.priority,
                    (
                        input_price_per_million
                        if input_price_per_million is not None
                        else current.input_price_per_million
                    ),
                    (
                        output_price_per_million
                        if output_price_per_million is not None
                        else current.output_price_per_million
                    ),
                    currency if currency is not None else current.currency,
                    min_balance if min_balance is not None else current.min_balance,
                    1 if (enabled if enabled is not None else current.enabled) else 0,
                    now,
                    route_id,
                ),
            )
        return self.get_route_config(route_id)

    def delete_route_config(self, route_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM provider_route_configs WHERE id = ?",
                (route_id,),
            )
        return cursor.rowcount > 0

    def upsert_route_by_identity(self, route: RouteConfig) -> RouteConfig:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM provider_route_configs
                WHERE virtual_model = ? AND provider = ? AND upstream_model = ?
                """,
                (route.virtual_model, route.provider, route.upstream_model),
            ).fetchone()
        if row:
            updated = self.patch_route_config(
                route_id=row["id"],
                priority=route.priority,
                input_price_per_million=route.input_price_per_million,
                output_price_per_million=route.output_price_per_million,
                currency=route.currency,
                min_balance=route.min_balance,
                enabled=route.enabled,
            )
            return updated  # type: ignore[return-value]
        return self.create_route_config(
            virtual_model=route.virtual_model,
            provider=route.provider,
            upstream_model=route.upstream_model,
            provider_model_id=route.provider_model_id,
            priority=route.priority,
            input_price_per_million=route.input_price_per_million,
            output_price_per_million=route.output_price_per_million,
            currency=route.currency,
            min_balance=route.min_balance,
            enabled=route.enabled,
        )

    def get_balance(self, provider: str) -> BalanceRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_balances WHERE provider = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return BalanceRecord(provider=provider, balance=0.0, currency="CNY")
        return BalanceRecord(**dict(row))

    def list_balances(self, provider_ids: list[str] | None = None) -> list[BalanceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_balances ORDER BY provider"
            ).fetchall()
        by_provider = {row["provider"]: BalanceRecord(**dict(row)) for row in rows}
        if provider_ids is None:
            return list(by_provider.values())

        ordered: list[BalanceRecord] = []
        seen: set[str] = set()
        for provider in provider_ids:
            seen.add(provider)
            ordered.append(by_provider.get(provider) or BalanceRecord(provider=provider))
        for provider, record in by_provider.items():
            if provider not in seen:
                ordered.append(record)
        return ordered

    def adjust_balance(
        self,
        *,
        provider: str,
        amount_delta: float,
        currency: str = "CNY",
        reason: str = "",
    ) -> tuple[BalanceRecord, BalanceAdjustment]:
        now = utc_now_iso()
        adjustment = BalanceAdjustment(
            provider=provider,
            amount_delta=amount_delta,
            balance_after=0.0,
            currency=currency,
            reason=reason,
            created_at=now,
        )
        with self._connect() as connection:
            current = connection.execute(
                "SELECT balance FROM provider_balances WHERE provider = ?",
                (provider,),
            ).fetchone()
            current_balance = float(current["balance"]) if current else 0.0
            balance_after = current_balance + amount_delta
            adjustment.balance_after = balance_after
            connection.execute(
                """
                INSERT INTO provider_balances(provider, currency, balance, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    currency = excluded.currency,
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (provider, currency, balance_after, now),
            )
            connection.execute(
                """
                INSERT INTO provider_balance_adjustments(
                    id, provider, amount_delta, balance_after, currency, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    adjustment.id,
                    adjustment.provider,
                    adjustment.amount_delta,
                    adjustment.balance_after,
                    adjustment.currency,
                    adjustment.reason,
                    adjustment.created_at,
                ),
            )
        return BalanceRecord(
            provider=provider,
            currency=currency,
            balance=balance_after,
            updated_at=now,
        ), adjustment

    def deduct_balance(self, *, provider: str, amount: float, currency: str) -> BalanceRecord:
        if amount <= 0:
            return self.get_balance(provider)
        now = utc_now_iso()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT balance FROM provider_balances WHERE provider = ?",
                (provider,),
            ).fetchone()
            current_balance = float(current["balance"]) if current else 0.0
            balance_after = current_balance - amount
            connection.execute(
                """
                INSERT INTO provider_balances(provider, currency, balance, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    currency = excluded.currency,
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (provider, currency, balance_after, now),
            )
        return BalanceRecord(
            provider=provider,
            currency=currency,
            balance=balance_after,
            updated_at=now,
        )

    def record_usage_event(self, event: UsageEvent) -> UsageEvent:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_usage_events(
                    id, user_id, conversation_id, virtual_model, provider, upstream_model,
                    prompt_tokens, completion_tokens, total_tokens,
                    input_cost, output_cost, total_cost, currency,
                    estimated, status, error_type, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.user_id,
                    event.conversation_id,
                    event.virtual_model,
                    event.provider,
                    event.upstream_model,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.total_tokens,
                    event.input_cost,
                    event.output_cost,
                    event.total_cost,
                    event.currency,
                    1 if event.estimated else 0,
                    event.status,
                    event.error_type,
                    event.created_at,
                ),
            )
        return event

    def list_usage_events(
        self,
        *,
        limit: int = 100,
        provider: str | None = None,
        virtual_model: str | None = None,
        status: str | None = None,
    ) -> list[UsageEvent]:
        query = "SELECT * FROM provider_usage_events"
        params: list[object] = []
        conditions: list[str] = []
        if provider:
            conditions.append("provider = ?")
            params.append(provider)
        if virtual_model:
            conditions.append("virtual_model = ?")
            params.append(virtual_model)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_usage_event_from_row(row) for row in rows]

    def usage_summary(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    provider,
                    virtual_model,
                    COUNT(*) AS calls,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(total_tokens) AS total_tokens,
                    SUM(input_cost) AS input_cost,
                    SUM(output_cost) AS output_cost,
                    SUM(total_cost) AS total_cost,
                    currency
                FROM provider_usage_events
                GROUP BY provider, virtual_model, currency
                ORDER BY provider, virtual_model
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _usage_event_from_row(row: sqlite3.Row) -> UsageEvent:
    data = dict(row)
    data["estimated"] = bool(data.get("estimated"))
    return UsageEvent(**data)


def _provider_config_from_row(row: sqlite3.Row) -> ProviderConfig:
    data = dict(row)
    return ProviderConfig(
        id=data["provider"],
        name=data["name"],
        base_url=data["base_url"],
        api_key=data.get("api_key") or "",
        enabled=bool(data.get("enabled")),
        timeout_seconds=float(data.get("timeout_seconds") or 60.0),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _provider_model_config_from_row(row: sqlite3.Row) -> ProviderModelConfig:
    data = dict(row)
    return ProviderModelConfig(
        id=data["id"],
        provider=data["provider"],
        upstream_model=data["upstream_model"],
        display_name=data.get("display_name") or "",
        api_format=data.get("api_format") or "openai_compatible",
        pricing_mode=data.get("pricing_mode") or "flat",
        pricing_tiers_json=data.get("pricing_tiers_json") or "",
        input_price_per_million=float(data.get("input_price_per_million") or 0.0),
        output_price_per_million=float(data.get("output_price_per_million") or 0.0),
        currency=data.get("currency") or "CNY",
        enabled=bool(data.get("enabled")),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _route_config_from_row(row: sqlite3.Row) -> RouteConfig:
    data = dict(row)
    return RouteConfig(
        id=data["id"],
        virtual_model=data["virtual_model"],
        provider=data["provider"],
        upstream_model=data["upstream_model"],
        provider_model_id=data.get("provider_model_id"),
        priority=int(data.get("priority") or 0),
        input_price_per_million=float(data.get("input_price_per_million") or 0.0),
        output_price_per_million=float(data.get("output_price_per_million") or 0.0),
        currency=data.get("currency") or "CNY",
        min_balance=float(data.get("min_balance") or 0.0),
        enabled=bool(data.get("enabled")),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _has_enabled_provider_route(
    providers: dict[str, ProviderConfig],
    provider_models: dict[str, ProviderModelConfig],
    routes: list[RouteConfig],
) -> bool:
    return any(
        route.enabled
        and (provider := providers.get(route.provider)) is not None
        and provider.enabled
        and _route_model_enabled(route, provider_models, provider.id)
        for route in routes
    )


def _first_enabled_route_model(
    providers: dict[str, ProviderConfig],
    provider_models: dict[str, ProviderModelConfig],
    routes: list[RouteConfig],
) -> str | None:
    enabled_routes = [
        route
        for route in routes
        if route.enabled
        and (provider := providers.get(route.provider)) is not None
        and provider.enabled
        and _route_model_enabled(route, provider_models, provider.id)
    ]
    enabled_routes.sort(key=lambda route: route.priority, reverse=True)
    return enabled_routes[0].virtual_model if enabled_routes else None


def _route_model_enabled(
    route: RouteConfig,
    provider_models: dict[str, ProviderModelConfig],
    provider: str,
) -> bool:
    if not route.provider_model_id:
        return True
    model = provider_models.get(route.provider_model_id)
    return (
        model is not None
        and model.enabled
        and model.provider == provider
        and model.api_format == "openai_compatible"
    )


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
