from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sqlite3
import sys
from typing import Mapping
from urllib.parse import quote


SECTOR_TYPES = {"episodic", "semantic", "procedural", "emotional", "reflective"}
LEGACY_TYPES = {"project", "preference", "fact", "learning", "style", "person", "relationship"}
VALID_STATUSES = {"dynamic", "resolved", "archived", "pinned"}

REQUIRED_MEMORY_COLUMNS = [
    "id",
    "user_id",
    "content",
    "type",
    "importance",
    "confidence",
    "usage_count",
    "status",
    "digested",
    "decay_lambda",
    "valid_from",
    "valid_until",
    "temporal_subject",
    "temporal_predicate",
    "supersedes",
    "superseded_by",
    "topics_json",
    "entities_json",
]

JSON_MEMORY_COLUMNS = [
    "embedding_json",
    "evidence_memory_ids_json",
    "topics_json",
    "entities_json",
]

@dataclass
class Finding:
    severity: str
    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


def run_audit(
    database: str | Path = "data/memory.db",
    *,
    env_file: str | Path | None = ".env",
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Run a read-only audit of the memory SQLite database."""
    database_path = Path(database)
    result: dict[str, object] = {
        "database": str(database_path),
        "status": "ok",
        "findings": [],
        "counts": {},
        "config": {},
    }
    findings: list[Finding] = []

    if not database_path.exists():
        findings.append(
            Finding(
                severity="error",
                code="database_not_found",
                message=f"Database file does not exist: {database_path}",
            )
        )
        return _finalize_result(result, findings)

    try:
        connection = _connect_readonly(database_path)
    except sqlite3.Error as exc:
        findings.append(
            Finding(
                severity="error",
                code="database_open_failed",
                message=f"Could not open database in read-only mode: {exc}",
            )
        )
        return _finalize_result(result, findings)

    try:
        connection.row_factory = sqlite3.Row
        if not _table_exists(connection, "memories"):
            findings.append(
                Finding(
                    severity="error",
                    code="memories_table_missing",
                    message="Required table memories is missing.",
                )
            )
            return _finalize_result(result, findings)

        columns = _table_columns(connection, "memories")
        missing_columns = [name for name in REQUIRED_MEMORY_COLUMNS if name not in columns]
        counts = result["counts"]
        assert isinstance(counts, dict)
        counts["column_count"] = len(columns)

        if missing_columns:
            findings.append(
                Finding(
                    severity="error",
                    code="missing_required_columns",
                    message="The memories table is missing required P0/P1 columns.",
                    details={"columns": missing_columns},
                )
            )

        counts["memory_count"] = _count(connection, "SELECT COUNT(*) FROM memories")
        _audit_types(connection, columns, counts, findings)
        _audit_statuses(connection, columns, counts, findings)
        _audit_usage_count(connection, columns, counts, findings)
        _audit_temporal_fields(connection, columns, counts, findings)
        _audit_json_columns(connection, columns, counts, findings)
        _audit_decision_logs(connection, counts)
    finally:
        connection.close()

    return _finalize_result(result, findings)


def exit_code_for_result(result: Mapping[str, object], *, strict: bool = False) -> int:
    findings = result.get("findings", [])
    has_errors = any(item.get("severity") == "error" for item in findings if isinstance(item, dict))
    has_warnings = any(item.get("severity") == "warning" for item in findings if isinstance(item, dict))
    if has_errors or (strict and has_warnings):
        return 1
    return 0


def format_text_report(result: Mapping[str, object]) -> str:
    lines = [
        "memory-gateway database audit",
        f"Database: {result.get('database')}",
        f"Status: {str(result.get('status')).upper()}",
    ]
    counts = result.get("counts", {})
    if isinstance(counts, dict) and counts:
        lines.extend(["", "Counts:"])
        for key in sorted(counts):
            lines.append(f"- {key}: {counts[key]}")

    findings = result.get("findings", [])
    lines.extend(["", "Findings:"])
    if not findings:
        lines.append("- none")
    else:
        for item in findings:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "info")).upper()
            code = item.get("code")
            message = item.get("message")
            details = item.get("details") or {}
            suffix = f" details={json.dumps(details, ensure_ascii=False, sort_keys=True)}" if details else ""
            lines.append(f"- [{severity}] {code}: {message}{suffix}")
    return "\n".join(lines)


def _connect_readonly(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:")
    return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _count(connection: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = connection.execute(sql, params).fetchone()
    return int(row[0] or 0)


def _audit_types(
    connection: sqlite3.Connection,
    columns: set[str],
    counts: dict[str, object],
    findings: list[Finding],
) -> None:
    if "type" not in columns:
        return
    rows = connection.execute(
        "SELECT type, COUNT(*) AS count FROM memories GROUP BY type ORDER BY count DESC"
    ).fetchall()
    counts["type_counts"] = {str(row["type"]): int(row["count"]) for row in rows}

    placeholders = ",".join("?" for _ in LEGACY_TYPES)
    legacy_count = _count(
        connection,
        f"SELECT COUNT(*) FROM memories WHERE type IN ({placeholders})",
        tuple(sorted(LEGACY_TYPES)),
    )
    counts["legacy_type_count"] = legacy_count
    if legacy_count:
        findings.append(
            Finding(
                severity="warning",
                code="legacy_memory_types",
                message="Legacy business memory types are still present; migrated rows should be semantic.",
                details={"count": legacy_count},
            )
        )

    allowed = tuple(sorted(SECTOR_TYPES | LEGACY_TYPES))
    placeholders = ",".join("?" for _ in allowed)
    invalid_count = _count(
        connection,
        f"SELECT COUNT(*) FROM memories WHERE type IS NULL OR type NOT IN ({placeholders})",
        allowed,
    )
    counts["invalid_type_count"] = invalid_count
    if invalid_count:
        findings.append(
            Finding(
                severity="error",
                code="invalid_memory_types",
                message="Memory rows contain type values outside sector and legacy type sets.",
                details={"count": invalid_count},
            )
        )


def _audit_statuses(
    connection: sqlite3.Connection,
    columns: set[str],
    counts: dict[str, object],
    findings: list[Finding],
) -> None:
    if "status" not in columns:
        return
    rows = connection.execute(
        "SELECT status, COUNT(*) AS count FROM memories GROUP BY status ORDER BY count DESC"
    ).fetchall()
    counts["status_counts"] = {str(row["status"]): int(row["count"]) for row in rows}

    placeholders = ",".join("?" for _ in VALID_STATUSES)
    invalid_count = _count(
        connection,
        f"SELECT COUNT(*) FROM memories WHERE status IS NULL OR status NOT IN ({placeholders})",
        tuple(sorted(VALID_STATUSES)),
    )
    counts["invalid_status_count"] = invalid_count
    if invalid_count:
        findings.append(
            Finding(
                severity="error",
                code="invalid_memory_statuses",
                message="Memory rows contain lifecycle status values outside dynamic/resolved/archived/pinned.",
                details={"count": invalid_count},
            )
        )

    if "digested" in columns:
        invalid_digested = _count(
            connection,
            "SELECT COUNT(*) FROM memories WHERE digested IS NOT NULL AND digested NOT IN (0, 1)",
        )
        counts["invalid_digested_count"] = invalid_digested
        if invalid_digested:
            findings.append(
                Finding(
                    severity="error",
                    code="invalid_digested_values",
                    message="Memory rows contain digested values outside 0/1.",
                    details={"count": invalid_digested},
                )
            )


def _audit_usage_count(
    connection: sqlite3.Connection,
    columns: set[str],
    counts: dict[str, object],
    findings: list[Finding],
) -> None:
    if "usage_count" not in columns:
        return
    counts["max_usage_count"] = connection.execute("SELECT MAX(usage_count) FROM memories").fetchone()[0]
    null_count = _count(connection, "SELECT COUNT(*) FROM memories WHERE usage_count IS NULL")
    negative_count = _count(connection, "SELECT COUNT(*) FROM memories WHERE usage_count < 0")
    fractional_count = _count(
        connection,
        "SELECT COUNT(*) FROM memories WHERE usage_count != CAST(usage_count AS INTEGER)",
    )
    counts["null_usage_count"] = null_count
    counts["negative_usage_count"] = negative_count
    counts["fractional_usage_count"] = fractional_count

    if null_count:
        findings.append(
            Finding(
                severity="error",
                code="null_usage_count",
                message="Memory rows contain NULL usage_count values.",
                details={"count": null_count},
            )
        )
    if negative_count:
        findings.append(
            Finding(
                severity="error",
                code="negative_usage_count",
                message="Memory rows contain negative usage_count values.",
                details={"count": negative_count},
            )
        )
    if fractional_count:
        findings.append(
            Finding(
                severity="warning",
                code="fractional_usage_count",
                message="Some usage_count values are fractional; this can be expected only after Time Ripple activation.",
                details={"count": fractional_count},
            )
        )


def _audit_temporal_fields(
    connection: sqlite3.Connection,
    columns: set[str],
    counts: dict[str, object],
    findings: list[Finding],
) -> None:
    if "valid_from" in columns:
        counts["valid_from_count"] = _count(
            connection, "SELECT COUNT(*) FROM memories WHERE valid_from IS NOT NULL"
        )
    if {"temporal_subject", "temporal_predicate"}.issubset(columns):
        counts["temporal_key_count"] = _count(
            connection,
            """
            SELECT COUNT(*) FROM memories
            WHERE temporal_subject IS NOT NULL OR temporal_predicate IS NOT NULL
            """,
        )
        partial_key_count = _count(
            connection,
            """
            SELECT COUNT(*) FROM memories
            WHERE (temporal_subject IS NULL AND temporal_predicate IS NOT NULL)
               OR (temporal_subject IS NOT NULL AND temporal_predicate IS NULL)
            """,
        )
        counts["partial_temporal_key_count"] = partial_key_count
        if partial_key_count:
            findings.append(
                Finding(
                    severity="warning",
                    code="partial_temporal_keys",
                    message="Some rows have only one side of the temporal subject/predicate key.",
                    details={"count": partial_key_count},
                )
            )
    if {"supersedes", "superseded_by"}.issubset(columns):
        counts["supersession_link_count"] = _count(
            connection,
            "SELECT COUNT(*) FROM memories WHERE supersedes IS NOT NULL OR superseded_by IS NOT NULL",
        )


def _audit_json_columns(
    connection: sqlite3.Connection,
    columns: set[str],
    counts: dict[str, object],
    findings: list[Finding],
) -> None:
    invalid_total = 0
    invalid_by_column: dict[str, int] = {}
    for column in JSON_MEMORY_COLUMNS:
        if column not in columns:
            continue
        rows = connection.execute(
            f"SELECT id, {column} AS value FROM memories WHERE {column} IS NOT NULL AND {column} != ''"
        ).fetchall()
        invalid_count = 0
        for row in rows:
            try:
                json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                invalid_count += 1
        counts[f"{column}_non_empty_count"] = len(rows)
        counts[f"{column}_invalid_count"] = invalid_count
        if invalid_count:
            invalid_by_column[column] = invalid_count
            invalid_total += invalid_count
    counts["invalid_json_count"] = invalid_total
    if invalid_total:
        findings.append(
            Finding(
                severity="error",
                code="invalid_memory_json",
                message="Memory rows contain invalid JSON in structured columns.",
                details=invalid_by_column,
            )
        )


def _audit_decision_logs(connection: sqlite3.Connection, counts: dict[str, object]) -> None:
    if not _table_exists(connection, "memory_decision_logs"):
        counts["decision_log_table"] = "missing"
        return
    counts["decision_log_table"] = "present"
    counts["decision_log_count"] = _count(connection, "SELECT COUNT(*) FROM memory_decision_logs")
    counts["temporal_decision_log_count"] = _count(
        connection,
        "SELECT COUNT(*) FROM memory_decision_logs WHERE candidate_json LIKE '%temporal_%'",
    )


def _finalize_result(result: dict[str, object], findings: list[Finding]) -> dict[str, object]:
    serialized_findings = [asdict(finding) for finding in findings]
    result["findings"] = serialized_findings
    if any(finding.severity == "error" for finding in findings):
        result["status"] = "error"
    elif any(finding.severity == "warning" for finding in findings):
        result["status"] = "warning"
    else:
        result["status"] = "ok"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit for memory-gateway SQLite memory data."
    )
    parser.add_argument("--database", default="data/memory.db", help="SQLite database path.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional .env path kept for CLI compatibility; ignored by the audit.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when warnings are present.",
    )
    args = parser.parse_args(argv)

    env_file = args.env_file if args.env_file else None
    result = run_audit(args.database, env_file=env_file)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text_report(result))
    return exit_code_for_result(result, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
