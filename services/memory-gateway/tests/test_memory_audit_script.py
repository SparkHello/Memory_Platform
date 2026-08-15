from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from app.memory.store import MemoryStore


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_memory_db.py"
SPEC = importlib.util.spec_from_file_location("audit_memory_db", SCRIPT_PATH)
assert SPEC is not None
audit_memory_db = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = audit_memory_db
SPEC.loader.exec_module(audit_memory_db)


def _store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    return store


def _audit(db_path: str | Path, *, environ: dict[str, str] | None = None) -> dict:
    return audit_memory_db.run_audit(db_path, env_file=None, environ=environ or {})


def _codes(result: dict, *, severity: str | None = None) -> set[str]:
    findings = result["findings"]
    if severity is None:
        return {finding["code"] for finding in findings}
    return {finding["code"] for finding in findings if finding["severity"] == severity}


def test_clean_database_has_no_findings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_memory(
        user_id="default",
        content="User uses memory-gateway for local memory.",
        type="semantic",
        importance=7,
        confidence=0.9,
    )

    result = _audit(store.database_path)

    assert result["status"] == "ok"
    assert result["findings"] == []
    assert result["counts"]["memory_count"] == 1
    assert result["counts"]["legacy_type_count"] == 0


def test_missing_required_columns_are_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-columns.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")

    result = _audit(db_path)

    assert result["status"] == "error"
    assert "missing_required_columns" in _codes(result, severity="error")
    assert audit_memory_db.exit_code_for_result(result) == 1


def test_legacy_memory_type_is_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = store.create_memory(
        user_id="default",
        content="User prefers quiet work sessions.",
        type="semantic",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE memories SET type = 'preference' WHERE id = ?", (memory.id,))

    result = _audit(store.database_path)

    assert result["status"] == "warning"
    assert "legacy_memory_types" in _codes(result, severity="warning")
    assert result["counts"]["legacy_type_count"] == 1
    assert audit_memory_db.exit_code_for_result(result) == 0
    assert audit_memory_db.exit_code_for_result(result, strict=True) == 1


def test_invalid_status_is_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = store.create_memory(
        user_id="default",
        content="User is testing lifecycle audits.",
        type="semantic",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE memories SET status = 'stale' WHERE id = ?", (memory.id,))

    result = _audit(store.database_path)

    assert result["status"] == "error"
    assert "invalid_memory_statuses" in _codes(result, severity="error")
    assert result["counts"]["invalid_status_count"] == 1


def test_negative_and_fractional_usage_counts_are_reported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    negative = store.create_memory(
        user_id="default",
        content="User is testing negative usage.",
        type="semantic",
    )
    fractional = store.create_memory(
        user_id="default",
        content="User is testing fractional usage.",
        type="semantic",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE memories SET usage_count = -1 WHERE id = ?", (negative.id,))
        connection.execute("UPDATE memories SET usage_count = 1.25 WHERE id = ?", (fractional.id,))

    result = _audit(store.database_path)

    assert result["status"] == "error"
    assert "negative_usage_count" in _codes(result, severity="error")
    assert "fractional_usage_count" in _codes(result, severity="warning")
    assert result["counts"]["negative_usage_count"] == 1
    assert result["counts"]["fractional_usage_count"] == 1
