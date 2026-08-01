import json
from typing import Any

from app.memory.models import (
    DatabaseHealthIssue,
    DatabaseHealthResult,
    DatabaseHealthSeverity,
    DatabaseHealthSummary,
    DatabaseHealthStatus,
    utc_now_iso,
)
from app.memory.report import build_memory_export
from app.memory.search import SEARCH_CACHE
from app.memory.store import MemoryStore
from app.memory.utils import parse_embedding_vector


class MemoryHealthChecker:
    """Read-only database consistency checks for memory maintenance views."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        expected_embedding_dimensions: int,
        embedding_enabled: bool,
    ):
        self.store = store
        self.expected_embedding_dimensions = expected_embedding_dimensions
        self.embedding_enabled = embedding_enabled

    def check(self, *, user_id: str) -> DatabaseHealthResult:
        issues: list[DatabaseHealthIssue] = []
        with self.store._connect() as connection:
            memory_rows = connection.execute(
                """
                SELECT id, archived, embedding_json
                FROM memories
                WHERE user_id = ?
                ORDER BY updated_at DESC, id ASC
                """,
                (user_id,),
            ).fetchall()
            active_memory_ids = {
                str(row["id"]) for row in memory_rows if int(row["archived"] or 0) == 0
            }
            archived_memory_ids = {
                str(row["id"]) for row in memory_rows if int(row["archived"] or 0) == 1
            }
            all_memory_ids = active_memory_ids | archived_memory_ids

            core_rows = connection.execute(
                """
                SELECT id, section, evidence_memory_ids_json
                FROM core_memory_sections
                WHERE user_id = ? AND archived = 0
                ORDER BY section ASC, updated_at DESC
                """,
                (user_id,),
            ).fetchall()
            history_rows = connection.execute(
                """
                SELECT id, section, evidence_memory_ids_json
                FROM core_memory_section_history
                WHERE user_id = ?
                ORDER BY replaced_at DESC, id ASC
                """,
                (user_id,),
            ).fetchall()
            space_rows = connection.execute(
                """
                SELECT id
                FROM memory_spaces
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
            space_ids = {str(row["id"]) for row in space_rows}
            link_rows = connection.execute(
                """
                SELECT memory_id, space_id
                FROM memory_space_links
                WHERE user_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (user_id,),
            ).fetchall()
            decision_rows = connection.execute(
                """
                SELECT id, candidate_json
                FROM memory_decision_logs
                WHERE user_id = ?
                ORDER BY created_at DESC, id ASC
                LIMIT 10000
                """,
                (user_id,),
            ).fetchall()

        issues.extend(
            self._core_evidence_issues(
                core_rows=core_rows,
                active_memory_ids=active_memory_ids,
                archived_memory_ids=archived_memory_ids,
            )
        )
        issues.extend(
            self._space_link_issues(
                link_rows=link_rows,
                memory_ids=all_memory_ids,
                space_ids=space_ids,
            )
        )
        issues.extend(
            self._embedding_issues(
                memory_rows=memory_rows,
                active_memory_ids=active_memory_ids,
            )
        )
        issues.extend(
            self._export_issues(
                user_id=user_id,
                space_ids=space_ids,
            )
        )
        issues.extend(
            self._search_cache_issues(
                user_id=user_id,
                active_memory_ids=active_memory_ids,
            )
        )
        issues.extend(
            self._core_history_issues(
                history_rows=history_rows,
                memory_ids=all_memory_ids,
            )
        )
        issues.extend(
            self._decision_log_issues(
                decision_rows=decision_rows,
                memory_ids=all_memory_ids,
            )
        )

        return _result(issues)

    def _core_evidence_issues(
        self,
        *,
        core_rows: list[Any],
        active_memory_ids: set[str],
        archived_memory_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        issues: list[DatabaseHealthIssue] = []
        for row in core_rows:
            object_id = f"core:{row['section']}"
            for memory_id in _string_list_json(row["evidence_memory_ids_json"]):
                if memory_id in active_memory_ids:
                    continue
                if memory_id in archived_memory_ids:
                    issues.append(
                        _issue(
                            issue_type="archived_core_evidence",
                            severity="warning",
                            object_id=object_id,
                            related_id=memory_id,
                            message="Core memory evidence references a memory in the trash.",
                            recommended_action="Review the core section before restoring or reconsolidating evidence.",
                        )
                    )
                    continue
                issues.append(
                    _issue(
                        issue_type="orphan_core_evidence",
                        severity="warning",
                        object_id=object_id,
                        related_id=memory_id,
                        message="Core memory evidence references a missing memory.",
                        recommended_action="Review the core section and decide whether to reconsolidate.",
                    )
                )
        return issues

    def _space_link_issues(
        self,
        *,
        link_rows: list[Any],
        memory_ids: set[str],
        space_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        issues: list[DatabaseHealthIssue] = []
        for row in link_rows:
            memory_id = str(row["memory_id"])
            space_id = str(row["space_id"])
            object_id = f"memory_space_link:{memory_id}:{space_id}"
            if memory_id not in memory_ids:
                issues.append(
                    _issue(
                        issue_type="orphan_space_link_memory",
                        severity="error",
                        object_id=object_id,
                        related_id=memory_id,
                        message="Memory-space link points to a missing memory.",
                        recommended_action="Inspect the link and remove or recreate it through a future repair tool.",
                    )
                )
            if space_id not in space_ids:
                issues.append(
                    _issue(
                        issue_type="orphan_space_link_space",
                        severity="error",
                        object_id=object_id,
                        related_id=space_id,
                        message="Memory-space link points to a missing space.",
                        recommended_action="Inspect the link and recreate the space or remove the stale link through a future repair tool.",
                    )
                )
        return issues

    def _embedding_issues(
        self,
        *,
        memory_rows: list[Any],
        active_memory_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        issues: list[DatabaseHealthIssue] = []
        missing_severity: DatabaseHealthSeverity = (
            "warning" if self.embedding_enabled else "info"
        )
        for row in memory_rows:
            memory_id = str(row["id"])
            if memory_id not in active_memory_ids:
                continue
            raw_embedding = row["embedding_json"]
            object_id = f"memory:{memory_id}"
            if not raw_embedding:
                issues.append(
                    _issue(
                        issue_type="embedding_missing",
                        severity=missing_severity,
                        object_id=object_id,
                        related_id=memory_id,
                        message="Active memory has no embedding vector.",
                        recommended_action="Regenerate embeddings if semantic search quality matters.",
                    )
                )
                continue
            vector = parse_embedding_vector(raw_embedding)
            if vector is None:
                issues.append(
                    _issue(
                        issue_type="embedding_invalid",
                        severity="warning",
                        object_id=object_id,
                        related_id=memory_id,
                        message="Active memory embedding is not valid numeric JSON.",
                        recommended_action="Regenerate the memory embedding.",
                    )
                )
                continue
            if len(vector) != self.expected_embedding_dimensions:
                issues.append(
                    _issue(
                        issue_type="embedding_dimension_mismatch",
                        severity="warning",
                        object_id=object_id,
                        related_id=memory_id,
                        message="Active memory embedding dimension does not match current configuration.",
                        recommended_action="Regenerate embeddings with the configured embedding dimension.",
                    )
                )
        return issues

    def _export_issues(
        self,
        *,
        user_id: str,
        space_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        try:
            export_data = build_memory_export(
                store=self.store,
                user_id=user_id,
                include_deleted=True,
            )
        except Exception as exc:
            return [
                _issue(
                    issue_type="export_consistency_error",
                    severity="error",
                    object_id=f"export:{user_id}",
                    related_id=None,
                    message=f"Memory export failed during health check: {exc}",
                    recommended_action="Run the export endpoint and inspect the server log before relying on backups.",
                )
            ]

        issues: list[DatabaseHealthIssue] = []
        exported_spaces = {
            str(space.get("id"))
            for space in export_data.get("memory_spaces", [])
            if isinstance(space, dict) and space.get("id")
        }
        known_export_space_ids = exported_spaces | space_ids
        for collection_name in ("memories", "deleted_memories"):
            for memory in export_data.get(collection_name, []):
                if not isinstance(memory, dict):
                    continue
                memory_id = str(memory.get("id") or "")
                for space_id in _string_values(memory.get("space_ids")):
                    if space_id in known_export_space_ids:
                        continue
                    issues.append(
                        _issue(
                            issue_type="export_space_reference_missing",
                            severity="warning",
                            object_id=f"export_memory:{memory_id}",
                            related_id=space_id,
                            message="Exported memory references a space that is not present in the export.",
                            recommended_action="Review memory space links before using the export for migration.",
                        )
                    )
        return issues

    def _search_cache_issues(
        self,
        *,
        user_id: str,
        active_memory_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        issues: list[DatabaseHealthIssue] = []
        for key, value in list(SEARCH_CACHE.items()):
            if not isinstance(key, tuple) or not key or key[0] != user_id:
                continue
            if not isinstance(value, tuple) or len(value) < 4:
                continue
            payloads = value[3]
            if not isinstance(payloads, list):
                continue
            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                memory_id = payload.get("id")
                if not isinstance(memory_id, str) or memory_id in active_memory_ids:
                    continue
                issues.append(
                    _issue(
                        issue_type="stale_search_cache_reference",
                        severity="info",
                        object_id=f"search_cache:{key[1] if len(key) > 1 else ''}",
                        related_id=memory_id,
                        message="Search cache references a memory that is no longer active.",
                        recommended_action="No immediate action is required; cache expiry should clear this naturally.",
                    )
                )
        return issues

    def _core_history_issues(
        self,
        *,
        history_rows: list[Any],
        memory_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        issues: list[DatabaseHealthIssue] = []
        for row in history_rows:
            object_id = f"core_history:{row['id']}"
            for memory_id in _string_list_json(row["evidence_memory_ids_json"]):
                if memory_id in memory_ids:
                    continue
                issues.append(
                    _issue(
                        issue_type="orphan_core_history_evidence",
                        severity="info",
                        object_id=object_id,
                        related_id=memory_id,
                        message="Core memory history references a missing memory.",
                        recommended_action="No automatic repair is needed; keep this as historical audit context.",
                    )
                )
        return issues

    def _decision_log_issues(
        self,
        *,
        decision_rows: list[Any],
        memory_ids: set[str],
    ) -> list[DatabaseHealthIssue]:
        issues: list[DatabaseHealthIssue] = []
        for row in decision_rows:
            object_id = f"decision_log:{row['id']}"
            try:
                payload = json.loads(row["candidate_json"] or "")
            except json.JSONDecodeError:
                issues.append(
                    _issue(
                        issue_type="invalid_decision_log_json",
                        severity="info",
                        object_id=object_id,
                        related_id=None,
                        message="Decision log candidate_json is not valid JSON.",
                        recommended_action="No automatic repair is needed; inspect the audit record if it matters.",
                    )
                )
                continue
            for memory_id in sorted(_extract_memory_references(payload)):
                if memory_id in memory_ids:
                    continue
                issues.append(
                    _issue(
                        issue_type="orphan_decision_log_reference",
                        severity="info",
                        object_id=object_id,
                        related_id=memory_id,
                        message="Decision log references a memory that is no longer present.",
                        recommended_action="No automatic repair is needed; decision logs are historical audit records.",
                    )
                )
        return issues


def _result(issues: list[DatabaseHealthIssue]) -> DatabaseHealthResult:
    summary = DatabaseHealthSummary(
        errors=sum(1 for issue in issues if issue.severity == "error"),
        warnings=sum(1 for issue in issues if issue.severity == "warning"),
        info=sum(1 for issue in issues if issue.severity == "info"),
    )
    status_value: DatabaseHealthStatus = "ok"
    if summary.errors:
        status_value = "error"
    elif summary.warnings:
        status_value = "warning"
    return DatabaseHealthResult(
        status=status_value,
        checked_at=utc_now_iso(),
        summary=summary,
        issues=issues,
    )


def _issue(
    *,
    issue_type: str,
    severity: DatabaseHealthSeverity,
    object_id: str,
    related_id: str | None,
    message: str,
    recommended_action: str,
) -> DatabaseHealthIssue:
    return DatabaseHealthIssue(
        type=issue_type,
        severity=severity,
        object_id=object_id,
        related_id=related_id,
        message=message,
        recommended_action=recommended_action,
    )


def _string_list_json(raw_value: str | None) -> list[str]:
    try:
        values = json.loads(raw_value) if raw_value else []
    except json.JSONDecodeError:
        return []
    return _string_values(values)


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _extract_memory_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "memory_id",
                "target_memory_id",
                "source_memory_id",
                "confirm_memory_id",
            } and isinstance(item, str):
                references.add(item)
            elif key in {
                "memory_ids",
                "merged_memory_ids",
                "archived_memory_ids",
                "evidence_memory_ids",
            }:
                references.update(_string_values(item))
            else:
                references.update(_extract_memory_references(item))
    elif isinstance(value, list):
        for item in value:
            references.update(_extract_memory_references(item))
    return {reference for reference in references if reference}
