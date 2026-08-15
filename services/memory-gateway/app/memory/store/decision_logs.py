"""Decision log helpers."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any

from app.memory.models import DecisionLog, DecisionLogAction, new_memory_id, utc_now_iso
from app.memory.store.constants import _DECISION_LOG_RETENTION_LIMIT
from app.memory.store.helpers import ConnectionProvider

def _insert_decision_log(
    *,
    connection: sqlite3.Connection,
    user_id: str = "default",
    conversation_id: str | None,
    candidate_json: str,
    decision: DecisionLogAction,
    reason: str,
    created_at: str | None = None,
) -> DecisionLog:
    """写入一条决策日志并按用户裁剪到最近 _DECISION_LOG_RETENTION_LIMIT 条。

    所有 memory_decision_logs 写入路径（ingest/temporal/purge 审计）都必须
    走这里，保证裁剪一致；purge 审计通过 created_at 复用事务时间戳。
    """
    log = DecisionLog(
        id=new_memory_id(),
        user_id=user_id,
        conversation_id=conversation_id,
        candidate_json=candidate_json,
        decision=decision,
        reason=reason,
        created_at=created_at or utc_now_iso(),
    )
    connection.execute(
        """
        INSERT INTO memory_decision_logs (
            id, user_id, conversation_id, candidate_json, decision, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log.id,
            log.user_id,
            log.conversation_id,
            log.candidate_json,
            log.decision,
            log.reason,
            log.created_at,
        ),
    )
    # 每用户只保留最近 _DECISION_LOG_RETENTION_LIMIT 条，防止日志表无界增长。
    connection.execute(
        """
        DELETE FROM memory_decision_logs
        WHERE user_id = ?
          AND id NOT IN (
              SELECT id FROM memory_decision_logs
              WHERE user_id = ?
              ORDER BY created_at DESC, rowid DESC
              LIMIT ?
          )
        """,
        (user_id, user_id, _DECISION_LOG_RETENTION_LIMIT),
    )
    return log

def create_decision_log(
    store: ConnectionProvider,
    *,
    user_id: str = "default",
    conversation_id: str | None,
    candidate_json: str,
    decision: DecisionLogAction,
    reason: str,
) -> DecisionLog:
    with store._connect() as connection:
        return _insert_decision_log(
            connection=connection,
            user_id=user_id,
            conversation_id=conversation_id,
            candidate_json=candidate_json,
            decision=decision,
            reason=reason,
        )

_DECISION_LOG_MEMORY_REFERENCE_KEYS = {
    "allowed_memory_ids",
    "archived_memory_ids",
    "evidence_memory_ids",
    "memory_id",
    "memory_ids",
    "new_memory_id",
    "previous_superseded_by",
    "primary_superseded_id",
    "resolved_ids",
    "source_ids",
    "superseded_by",
    "superseded_memory_ids",
    "supersedes",
    "target_memory_id",
}


def _decision_log_referenced_memory_ids(value: object) -> set[str]:
    """提取决策日志 payload 引用的全部 memory id。

    口径（purge 脱敏与健康巡检共用这一份，不再各自维护）：dict key 大小写
    不敏感地命中 _DECISION_LOG_MEMORY_REFERENCE_KEYS，或以
    ``_memory_id`` / ``_memory_ids`` 结尾时，其 str 值及 list 中的 str 项
    视为引用；其余结构递归扫描。
    """
    references: set[str] = set()
    _collect_memory_id_references(value, references=references, reference_context=False)
    return references


def _collect_memory_id_references(
    value: object,
    *,
    references: set[str],
    reference_context: bool,
) -> None:
    if reference_context:
        if isinstance(value, str):
            if value:
                references.add(value)
            return
        if isinstance(value, list):
            for item in value:
                _collect_memory_id_references(
                    item,
                    references=references,
                    reference_context=True,
                )
            return
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).casefold()
            is_reference = (
                key in _DECISION_LOG_MEMORY_REFERENCE_KEYS
                or key.endswith("_memory_id")
                or key.endswith("_memory_ids")
            )
            _collect_memory_id_references(
                item,
                references=references,
                reference_context=is_reference,
            )
    elif isinstance(value, list):
        for item in value:
            _collect_memory_id_references(
                item,
                references=references,
                reference_context=False,
            )


def _decision_log_references_memory_ids(raw_json: str, memory_ids: set[str]) -> bool:
    try:
        payload = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(_decision_log_referenced_memory_ids(payload) & memory_ids)

def list_decision_logs(
    store: ConnectionProvider,
    *,
    user_id: str | None = None,
    conversation_id: str | None = None,
    memory_id: str | None = None,
    limit: int | None = 100,
) -> list[DecisionLog]:
    query = "SELECT * FROM memory_decision_logs"
    params: list[object] = []
    conditions: list[str] = []
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if conversation_id:
        conditions.append("conversation_id = ?")
        params.append(conversation_id)
    if memory_id:
        conditions.append("memory_log_references(candidate_json) = 1")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
    with store._connect() as connection:
        if memory_id:
            references = {memory_id}
            connection.create_function(
                "memory_log_references",
                1,
                lambda raw_json: int(
                    _decision_log_references_memory_ids(raw_json, references)
                ),
                deterministic=True,
            )
        rows = connection.execute(query, params).fetchall()
    return [DecisionLog(**dict(row)) for row in rows]

def _ensure_decision_logs_user_id(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(memory_decision_logs)").fetchall()
    }
    if "user_id" not in columns:
        connection.execute(
            "ALTER TABLE memory_decision_logs ADD COLUMN user_id TEXT DEFAULT 'default'"
        )

