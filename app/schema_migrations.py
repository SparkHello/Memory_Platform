from collections.abc import Callable, Sequence
import sqlite3


SchemaMigration = tuple[int, Callable[[sqlite3.Connection], None]]


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
