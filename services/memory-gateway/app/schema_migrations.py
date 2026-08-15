from collections.abc import Callable, Sequence
import sqlite3
import time


SchemaMigration = tuple[int, Callable[[sqlite3.Connection], None]]


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    column_defs: dict[str, str],
) -> None:
    """Add missing columns to a table without touching existing ones."""
    existing = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in column_defs.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def enable_wal_with_retry(
    connection: sqlite3.Connection,
    *,
    attempts: int = 20,
    initial_delay_seconds: float = 0.01,
) -> str:
    """Enable WAL while tolerating the brief lock race during concurrent startup.

    ``PRAGMA journal_mode=WAL`` may raise ``database is locked`` immediately even
    when ``busy_timeout`` is configured.  Only that transient lock/busy case is
    retried; configuration and filesystem errors still fail fast.
    """
    bounded_attempts = max(1, int(attempts))
    delay = max(0.0, float(initial_delay_seconds))
    for attempt in range(bounded_attempts):
        try:
            row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            return str(row[0]) if row else ""
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            retryable = "locked" in message or "busy" in message
            if not retryable or attempt + 1 >= bounded_attempts:
                raise
            time.sleep(min(delay * (2**attempt), 0.1))
    raise RuntimeError("unreachable WAL initialization state")


def validated_schema_version(
    connection: sqlite3.Connection,
    migrations: Sequence[SchemaMigration],
    *,
    schema_name: str,
) -> int:
    """Validate the migration declaration and reject unsupported future databases."""
    versions = [version for version, _ in migrations]
    if any(type(version) is not int or version <= 0 for version in versions):
        raise RuntimeError(
            f"{schema_name} migration versions must be positive integer literals"
        )
    if any(left >= right for left, right in zip(versions, versions[1:])):
        raise RuntimeError(
            f"{schema_name} migration versions must be unique and strictly increasing"
        )

    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    latest = versions[-1] if versions else 0
    if current > latest:
        raise RuntimeError(
            f"{schema_name} schema version {current} is newer than supported version {latest}"
        )
    return current


def apply_schema_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[SchemaMigration],
    *,
    schema_name: str,
) -> None:
    current = validated_schema_version(
        connection,
        migrations,
        schema_name=schema_name,
    )
    for version, step in migrations:
        if current >= version:
            continue
        step(connection)
        connection.execute(f"PRAGMA user_version = {version}")
