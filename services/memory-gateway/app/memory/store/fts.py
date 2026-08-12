"""memories 的 FTS5 关键词候选索引。

大库场景下，keyword 通道不再"全表分页扫描 + Python 逐条打分"，而是先用
FTS5 索引把候选缩小到"至少共享一个查询词"的记忆，再交给原有打分逻辑
精排。索引存的是与打分层完全一致的 ``_terms`` 预分词结果（CJK 2/3-gram +
英文词），因此候选集是打分层 shared_terms 通道的精确超集；单字 CJK、
类别标记等不走 term 匹配的通道由调用方负责回退到全表扫描。

同步策略（不侵入分散的写路径、不依赖触发器）：

- FTS 行的 rowid 与 memories 行的 rowid 一一对应；
- 每次候选查询前按 ``updated_at`` 水位增量刷新变更行；
- 增量后行数仍对不上（物理删除、未触发 updated_at 的写、VACUUM 重排
  rowid 等）时整用户重建，自愈；
- 库低于 ``FTS_MIN_CORPUS_ROWS`` 行时不维护索引，直接走全表扫描，
  既有小库行为完全不变。
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from app.memory.models import MemoryRecord
from app.memory.utils import _terms

if TYPE_CHECKING:
    from app.memory.store._monolith import MemoryStore

# 低于此规模，全表扫描 + 全库 IDF 更简单也更准；索引只在大库时启用。
FTS_MIN_CORPUS_ROWS = 2000
# 单次 MATCH 返回的候选上限；bm25 排序保证截断时保留共享词最多的记忆。
FTS_CANDIDATE_LIMIT = 512
_ACTIVE_WHERE = "archived = 0 AND (status IS NULL OR status != 'archived')"


def _row_terms(row: sqlite3.Row) -> str:
    import json

    def _joined(raw: str | None) -> str:
        if not raw:
            return ""
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        if not isinstance(values, list):
            return ""
        return " ".join(str(value) for value in values)

    terms = (
        _terms(str(row["content"] or ""))
        | _terms(_joined(row["topics_json"]))
        | _terms(_joined(row["entities_json"]))
    )
    return " ".join(sorted(terms))


def _ensure_fts_table(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                terms,
                user_id UNINDEXED,
                indexed_updated_at UNINDEXED,
                tokenize='unicode61'
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        # SQLite 编译时未启用 FTS5；调用方回退全表扫描。
        return False


def _upsert_rows(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    for row in rows:
        connection.execute(
            "DELETE FROM memories_fts WHERE rowid = ?", (int(row["fts_rowid"]),)
        )
        if not (
            int(row["archived"] or 0) == 0
            and (row["status"] is None or row["status"] != "archived")
        ):
            continue
        connection.execute(
            """
            INSERT INTO memories_fts(rowid, terms, user_id, indexed_updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(row["fts_rowid"]),
                _row_terms(row),
                str(row["user_id"] or ""),
                str(row["updated_at"] or ""),
            ),
        )


def _rebuild_user_index(connection: sqlite3.Connection, user_id: str) -> None:
    connection.execute("DELETE FROM memories_fts WHERE user_id = ?", (user_id,))
    rows = connection.execute(
        f"""
        SELECT rowid AS fts_rowid, user_id, content, topics_json, entities_json,
               updated_at, archived, status
        FROM memories
        WHERE user_id = ? AND {_ACTIVE_WHERE}
        """,
        (user_id,),
    ).fetchall()
    _upsert_rows(connection, rows)


def _refresh_user_index(connection: sqlite3.Connection, user_id: str) -> None:
    active_row = connection.execute(
        f"""
        SELECT COUNT(*) AS c, MAX(updated_at) AS w FROM memories
        WHERE user_id = ? AND {_ACTIVE_WHERE}
        """,
        (user_id,),
    ).fetchone()
    indexed_row = connection.execute(
        "SELECT COUNT(*) AS c, MAX(indexed_updated_at) AS w FROM memories_fts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    active_count = int(active_row["c"] or 0)
    active_watermark = str(active_row["w"] or "")
    indexed_count = int(indexed_row["c"] or 0)
    indexed_watermark = str(indexed_row["w"] or "")

    if indexed_count == active_count and indexed_watermark == active_watermark:
        return
    if indexed_count == 0 or indexed_watermark > active_watermark:
        _rebuild_user_index(connection, user_id)
        return

    # 增量：>= 水位的行（含刚归档的行）重建索引条目；重复 upsert 幂等。
    changed = connection.execute(
        """
        SELECT rowid AS fts_rowid, user_id, content, topics_json, entities_json,
               updated_at, archived, status
        FROM memories
        WHERE user_id = ? AND updated_at >= ?
        """,
        (user_id, indexed_watermark),
    ).fetchall()
    _upsert_rows(connection, changed)

    remaining = connection.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE user_id = ?", (user_id,)
    ).fetchone()
    if int(remaining[0] or 0) != active_count:
        # 物理删除或绕过 updated_at 的写路径造成漂移：整用户重建自愈。
        _rebuild_user_index(connection, user_id)


def keyword_candidate_memories(
    store: "MemoryStore",
    *,
    user_id: str,
    terms: list[str],
    limit: int | None = None,
    min_corpus_rows: int | None = None,
) -> list[MemoryRecord] | None:
    """返回共享至少一个查询词的候选记忆；返回 None 表示走全表扫描。"""
    bounded_limit = FTS_CANDIDATE_LIMIT if limit is None else limit
    threshold = FTS_MIN_CORPUS_ROWS if min_corpus_rows is None else min_corpus_rows
    safe_terms = [term for term in terms if term and '"' not in term]
    if not safe_terms:
        return None
    match_expression = " OR ".join(f'"{term}"' for term in safe_terms)
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _ensure_fts_table(connection):
            return None
        active_count_row = connection.execute(
            f"SELECT COUNT(*) FROM memories WHERE user_id = ? AND {_ACTIVE_WHERE}",
            (user_id,),
        ).fetchone()
        if int(active_count_row[0] or 0) < max(1, int(threshold)):
            return None
        _refresh_user_index(connection, user_id)
        candidate_rows = connection.execute(
            """
            SELECT rowid FROM memories_fts
            WHERE memories_fts MATCH ? AND user_id = ?
            ORDER BY rank LIMIT ?
            """,
            (match_expression, user_id, max(1, int(bounded_limit))),
        ).fetchall()
        rowids = [int(row[0]) for row in candidate_rows]
        memories: list[MemoryRecord] = []
        for start in range(0, len(rowids), 500):
            batch = rowids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT rowid AS recall_rowid, * FROM memories
                WHERE user_id = ? AND {_ACTIVE_WHERE}
                  AND rowid IN ({placeholders})
                """,
                (user_id, *batch),
            ).fetchall()
            memories.extend(
                store._rows_to_memories_on_connection(connection=connection, rows=rows)
            )
        return memories
