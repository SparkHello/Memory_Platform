from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import shutil
import sys
from urllib.parse import quote

from app.memory.redaction import redact_memory_payload
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    RECALL_CANDIDATE_POOL,
    _memory_is_locally_sensitive,
)
from app.memory.store import MemoryStore, ClosingSQLiteConnection


DEFAULT_EVAL_DIR = "eval"
SNAPSHOT_NAME = "eval_snapshot.db"
SNAPSHOT_PREFIX = "eval_snapshot_"
SNAPSHOT_POINTER_NAME = "current_snapshot.txt"
PREVIEW_NAME = "memories_preview.tsv"
LABELS_NAME = "labels.jsonl"
KEYWORD_RESULT_NAME = "last_keyword_result.json"
EMBEDDING_RESULT_NAME = "last_embedding_result.json"
USER_WORKSPACES_NAME = "users"

LABEL_JUDGMENTS = {"unlabeled", "relevant", "no_answer"}
BLOCKING_LABEL_ISSUE_CODES = {
    "blank_query",
    "duplicate_label_id",
    "duplicate_query_conflict",
    "invalid_judgment",
    "missing_relevant_ids",
    "no_answer_with_relevant_ids",
    "unlabeled_with_relevant_ids",
    "unknown_memory_id",
}

SECTOR_TYPES = ("episodic", "semantic", "procedural", "emotional", "reflective")
DEGENERATE_TYPE_SHARE = 0.90
SKEWED_TYPE_SHARE = 0.70
SPARSE_TAG_COVERAGE = 0.20
DEFAULT_AFFECT_DEGENERATE_SHARE = 0.90
DEFAULT_AFFECT_SKEWED_SHARE = 0.70
RECALL_CONCENTRATION_WARN = 0.5
MIN_MEANINGFUL_COUNT = 10
TARGET_LABEL_MIN = 20
TARGET_LABEL_MAX = 30
MAX_RECALL_EVAL_K = 20

LABELS_TEMPLATE = """# memory-gateway 召回评测标注文件
# 每行一个 JSON 对象：{"id": "q001", "query": "一句话检索意图", "judgment": "relevant|no_answer|unlabeled", "relevant_ids": ["应被召回的 memory id"], "note": "可选说明"}
# - query 用自然的检索意图，模拟客户端调用 search_memory 时的 query。
# - relevant_ids 从 memories_preview.tsv 或 Web 评测闭环页里挑选你认为这个 query 应该命中的记忆 id。
# - 没有相关记忆时，将 judgment 明确设为 no_answer；空 relevant_ids 本身仍表示尚未标注。
# - 以 # 开头的行和空行会被忽略。
{"id": "q001", "query": "用户的饮食偏好", "judgment": "unlabeled", "relevant_ids": [], "note": "示例，请完成标注"}
"""


@dataclass
class Verdict:
    mechanism: str
    state: str
    message: str
    metrics: dict[str, object] = field(default_factory=dict)


class EvaluationError(ValueError):
    pass


class _EvaluationMemoryStore(MemoryStore):
    """Read-only store whose context-managed connections actually close."""

    def _connect(self) -> sqlite3.Connection:
        resolved = Path(self.database_path).resolve()
        uri_path = quote(resolved.as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def delete_user_eval_workspace(
    eval_dir: str | Path,
    *,
    user_id: str,
) -> dict[str, int | bool]:
    """Remove snapshots and labels that may retain a permanently deleted memory."""
    eval_path = Path(eval_dir)
    user_path = _user_eval_dir(eval_path, user_id=user_id)
    workspace_removed = user_path.exists()
    if workspace_removed:
        shutil.rmtree(user_path)

    legacy_names = {
        SNAPSHOT_POINTER_NAME,
        PREVIEW_NAME,
        LABELS_NAME,
        KEYWORD_RESULT_NAME,
        EMBEDDING_RESULT_NAME,
    }
    legacy_removed = 0
    for path in (eval_path / name for name in legacy_names):
        if not path.is_file():
            continue
        path.unlink()
        legacy_removed += 1

    legacy_databases = {eval_path / SNAPSHOT_NAME}
    for path in eval_path.glob(f"{SNAPSHOT_PREFIX}*.db*"):
        raw_path = str(path)
        for suffix in ("-wal", "-shm", "-journal"):
            if raw_path.endswith(suffix):
                raw_path = raw_path[: -len(suffix)]
                break
        legacy_databases.add(Path(raw_path))
    for database_path in legacy_databases:
        legacy_removed += _unlink_sqlite_database(
            database_path,
            ignore_permission_error=False,
        )
    return {
        "workspace_removed": workspace_removed,
        "legacy_artifacts_removed": legacy_removed,
    }


def run_diagnosis(
    database: str | Path = "data/memory.db",
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """只读评估各记忆机制是否被真实数据激活。"""
    database_path = Path(database)
    result: dict[str, object] = {
        "database": str(database_path),
        "user_id": user_id,
        "memory_count": 0,
        "metrics": {},
        "verdicts": [],
    }

    if not database_path.exists():
        result["error"] = f"Database file does not exist: {database_path}"
        return result

    try:
        connection = _connect_readonly(database_path)
    except sqlite3.Error as exc:
        result["error"] = f"Could not open database in read-only mode: {exc}"
        return result

    try:
        connection.row_factory = sqlite3.Row
        columns = _table_columns(connection, "memories")
        if not columns:
            result["error"] = "Required table memories is missing."
            return result

        where_sql, params = _active_memory_scope(user_id)
        total = _count(connection, f"SELECT COUNT(*) FROM memories WHERE {where_sql}", params)
        result["memory_count"] = total

        metrics: dict[str, object] = result["metrics"]  # type: ignore[assignment]
        verdicts: list[Verdict] = []

        type_dist = _group_counts(connection, "type", user_id=user_id)
        status_dist = _group_counts(connection, "status", user_id=user_id)
        metrics["type_distribution"] = type_dist
        metrics["status_distribution"] = status_dist
        metrics["tag_coverage"] = _tag_coverage(connection, columns, total, user_id=user_id)
        metrics["graph"] = _graph_metrics(
            connection,
            columns,
            total,
            tag_coverage=metrics["tag_coverage"],
            user_id=user_id,
        )
        metrics["temporal"] = _temporal_metrics(connection, columns, user_id=user_id)
        metrics["importance"] = _numeric_summary(connection, "importance", user_id=user_id)
        metrics["usage_count"] = _numeric_summary(connection, "usage_count", user_id=user_id)
        never_recalled = _count(
            connection,
            f"SELECT COUNT(*) FROM memories WHERE {where_sql} AND COALESCE(usage_count, 0) = 0",
            params,
        )
        metrics["never_recalled_count"] = never_recalled
        metrics["affect"] = _affect_metrics(connection, columns, total, type_dist, user_id=user_id)
        metrics["recall"] = _recall_metrics(connection, total, never_recalled, user_id=user_id)

        verdicts.append(_sector_verdict(type_dist, total))
        verdicts.append(_lifecycle_verdict(status_dist, total))
        verdicts.append(_temporal_verdict(metrics["temporal"]))
        verdicts.append(_graph_verdict(metrics["graph"], total))
        verdicts.append(_affect_verdict(metrics["affect"], total))
        verdicts.append(_recall_verdict(metrics["recall"], total))

        result["verdicts"] = [asdict(verdict) for verdict in verdicts]
    finally:
        connection.close()

    return result


def init_eval(
    *,
    source_db: str | Path,
    eval_dir: str | Path = DEFAULT_EVAL_DIR,
    user_id: str = "default",
) -> dict[str, object]:
    """创建只包含单个用户数据的快照，并生成该用户独立的评测工作区。"""
    source_path = Path(source_db)
    if not source_path.exists():
        return {"error": f"Source database does not exist: {source_path}"}

    out_dir = _user_eval_dir(eval_dir, user_id=user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = _new_snapshot_path(out_dir)
    preview_path = out_dir / PREVIEW_NAME
    labels_path = out_dir / LABELS_NAME

    _snapshot_readonly(source_path, snapshot_path, user_id=user_id)

    user_counts, preview_rows = _read_snapshot_overview(
        snapshot_path,
        user_id=user_id,
    )
    _write_preview(preview_path, preview_rows)
    _write_current_snapshot_pointer(out_dir, snapshot_path)
    _cleanup_old_snapshots(out_dir, current_snapshot=snapshot_path)
    _invalidate_eval_results(out_dir)

    labels_created = False
    if not labels_path.exists():
        labels_path.write_text(LABELS_TEMPLATE, encoding="utf-8")
        labels_created = True

    return {
        "snapshot": str(snapshot_path),
        "preview": str(preview_path),
        "labels": str(labels_path),
        "labels_created": labels_created,
        "memory_count": len(preview_rows),
        "user_counts": user_counts,
        "user_id": user_id,
    }


def load_labels(labels_path: str | Path) -> list[dict[str, object]]:
    path = Path(labels_path)
    labels: list[dict[str, object]] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise EvaluationError(f"Labels file is not valid UTF-8: {path}: {exc}") from exc
    for index, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"Invalid label JSON on line {index}: {exc}") from exc
        try:
            labels.append(_normalize_label_entry(entry, index=index))
        except EvaluationError as exc:
            raise EvaluationError(f"Invalid label on line {index}: {exc}") from exc
    return labels


def save_labels(
    *,
    eval_dir: str | Path,
    labels: list[dict[str, object]],
    user_id: str,
) -> dict[str, object]:
    eval_path = _user_eval_dir(eval_dir, user_id=user_id)
    snapshot_path = _current_snapshot_path(eval_path)
    labels_path = eval_path / LABELS_NAME
    valid_ids = _snapshot_memory_ids(snapshot_path, user_id=user_id)
    normalized = _validate_labels(labels, valid_ids=valid_ids)
    _write_labels_atomic(labels_path, normalized)
    return {
        "labels": normalized,
        "summary": _label_summary(normalized),
        "validation_issues": _label_validation_issues(normalized, valid_ids=valid_ids),
    }


def build_recall_workbench(
    *,
    eval_dir: str | Path,
    user_id: str,
    redact_sensitive: bool = True,
) -> dict[str, object]:
    eval_path = _user_eval_dir(eval_dir, user_id=user_id)
    snapshot_path = _current_snapshot_path(eval_path)
    labels_path = eval_path / LABELS_NAME
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}. Run recall init first.")
    if not labels_path.exists():
        raise FileNotFoundError(f"Labels not found: {labels_path}. Run recall init first.")

    memories = _snapshot_memories(snapshot_path, user_id=user_id, redact_sensitive=redact_sensitive)
    labels = load_labels(labels_path)
    valid_ids = {str(memory["id"]) for memory in memories}
    return {
        "snapshot": str(snapshot_path),
        "labels_path": str(labels_path),
        "user_id": user_id,
        "target_label_min": TARGET_LABEL_MIN,
        "target_label_max": TARGET_LABEL_MAX,
        "labels": labels,
        "summary": _label_summary(labels),
        "validation_issues": _label_validation_issues(labels, valid_ids=valid_ids),
        "candidates": memories,
        "last_results": load_last_results(eval_path, snapshot_path=snapshot_path),
    }


class _TrackingEmbeddingClient(EmbeddingClient):
    def __init__(self, delegate: EmbeddingClient):
        self.delegate = delegate
        self.embedding_space_id = getattr(delegate, "embedding_space_id", "")
        self.available = False

    def reset(self) -> None:
        self.available = False

    async def embed(self, text: str) -> list[float] | None:
        vector = await self.delegate.embed(text)
        self.available = bool(vector)
        return vector


def run_eval(
    *,
    snapshot_db: str | Path,
    labels: list[dict[str, object]],
    user_id: str = "default",
    k: int = 8,
    embedding_client: EmbeddingClient | None = None,
    requested_mode: str | None = None,
) -> dict[str, object]:
    """对快照库跑召回评测，返回 per-query 指标和汇总。"""
    if not 1 <= k <= MAX_RECALL_EVAL_K:
        raise EvaluationError(f"k must be between 1 and {MAX_RECALL_EVAL_K}")
    effective_k = k
    normalized_labels = [
        _normalize_label_entry(label, index=index)
        for index, label in enumerate(labels, start=1)
    ]
    input_query_count = len(normalized_labels)
    normalized_labels = _deduplicate_identical_queries(normalized_labels)
    requested_mode = requested_mode or (
        "keyword" if embedding_client is None or isinstance(embedding_client, NullEmbeddingClient) else "embedding"
    )
    relevant_labels = [label for label in normalized_labels if label["judgment"] == "relevant"]
    no_answer_labels = [label for label in normalized_labels if label["judgment"] == "no_answer"]
    graded_count = len(relevant_labels) + len(no_answer_labels)
    store = _EvaluationMemoryStore(str(snapshot_db))
    tracking_embedding_client = _TrackingEmbeddingClient(embedding_client or NullEmbeddingClient())
    service = MemorySearchService(
        store=store,
        embedding_client=tracking_embedding_client,
        # 关闭进程级缓存：否则 keyword/embedding 两次基线会命中同一个
        # (user, query, limit) 缓存 key，第二次直接复用第一次的结果，
        # 还可能与线上实时检索互相串味。
        enable_cache=False,
    )
    per_query = asyncio.run(
        _search_all(
            service,
            normalized_labels,
            user_id=user_id,
            k=effective_k,
            requested_mode=requested_mode,
            embedding_tracker=tracking_embedding_client,
        )
    )

    summary: dict[str, object] = {
        "queries_input": input_query_count,
        "queries_total": len(normalized_labels),
        "duplicate_queries_collapsed": input_query_count - len(normalized_labels),
        "queries_graded": graded_count,
        "queries_relevant": len(relevant_labels),
        "queries_no_answer": len(no_answer_labels),
        "queries_unlabeled": len(normalized_labels) - graded_count,
        "requested_k": k,
        "k": effective_k,
        "effective_k": effective_k,
        "requested_mode": requested_mode,
    }
    if relevant_labels:
        relevant_results = [row for row in per_query if row["judgment"] == "relevant"]
        summary["hit_rate"] = round(_mean(row["hit"] for row in relevant_results), 4)
        summary["precision_at_k"] = round(_mean(row["precision"] for row in relevant_results), 4)
        summary["returned_precision"] = round(
            _mean(row["returned_precision"] for row in relevant_results),
            4,
        )
        summary["recall_at_k"] = round(_mean(row["recall"] for row in relevant_results), 4)
        summary["mrr"] = round(_mean(row["reciprocal_rank"] for row in relevant_results), 4)
        summary["ndcg_at_k"] = round(_mean(row["ndcg"] for row in relevant_results), 4)
    if no_answer_labels:
        no_answer_results = [row for row in per_query if row["judgment"] == "no_answer"]
        summary["no_answer_false_positive_rate"] = round(
            _mean(1.0 if row["false_positive"] else 0.0 for row in no_answer_results),
            4,
        )
        summary["no_answer_abstention_rate"] = round(
            _mean(1.0 if row["retrieved"] == 0 else 0.0 for row in no_answer_results),
            4,
        )
        summary["no_answer_mean_retrieved"] = round(
            _mean(float(row["retrieved"]) for row in no_answer_results),
            4,
        )
    retrieval_mode_counts: dict[str, int] = {}
    for row in per_query:
        retrieval_mode = str(row["retrieval_mode"])
        retrieval_mode_counts[retrieval_mode] = retrieval_mode_counts.get(retrieval_mode, 0) + 1
    summary["retrieval_mode_counts"] = retrieval_mode_counts
    summary["fallback_queries"] = sum(1 for row in per_query if row["fallback_reason"] is not None)
    return {"summary": summary, "per_query": per_query}


def run_recall_eval(
    *,
    eval_dir: str | Path,
    user_id: str,
    mode: str = "keyword",
    k: int = 8,
    embedding_client: EmbeddingClient | None = None,
) -> dict[str, object]:
    eval_path = _user_eval_dir(eval_dir, user_id=user_id)
    snapshot_path = _current_snapshot_path(eval_path)
    labels_path = eval_path / LABELS_NAME
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}. Run recall init first.")
    if not labels_path.exists():
        raise FileNotFoundError(f"Labels not found: {labels_path}. Run recall init first.")
    if mode not in {"keyword", "embedding"}:
        raise EvaluationError("mode must be keyword or embedding")

    labels = load_labels(labels_path)
    valid_ids = _snapshot_memory_ids(snapshot_path, user_id=user_id)
    issues = _label_validation_issues(labels, valid_ids=valid_ids)
    blocking = [issue for issue in issues if issue["code"] in BLOCKING_LABEL_ISSUE_CODES]
    if blocking:
        raise EvaluationError("; ".join(str(issue["message"]) for issue in blocking))

    result = run_eval(
        snapshot_db=snapshot_path,
        labels=labels,
        user_id=user_id,
        k=k,
        embedding_client=embedding_client if mode == "embedding" else NullEmbeddingClient(),
        requested_mode=mode,
    )
    result["mode"] = mode
    result["user_id"] = user_id
    result["snapshot"] = str(snapshot_path)
    result["validation_issues"] = issues
    save_eval_result(eval_path, mode=mode, result=result)
    return result


async def _search_all(
    service: MemorySearchService,
    labels: list[dict[str, object]],
    *,
    user_id: str,
    k: int,
    requested_mode: str,
    embedding_tracker: _TrackingEmbeddingClient,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label in labels:
        query = str(label["query"])
        relevant = [str(memory_id) for memory_id in label.get("relevant_ids", [])]
        embedding_tracker.reset()
        hits = await service.search_hits(
            query=query,
            user_id=user_id,
            limit=k,
            record_usage=False,
        )
        predicted = [hit.memory.id for hit in hits]
        predicted_channels = {
            hit.memory.id: list(hit.channels)
            for hit in hits
        }
        predicted_scores = {
            hit.memory.id: {
                "relevance": hit.relevance,
                "topic_score": hit.topic_score,
                "total_score": hit.total_score,
                "score_breakdown": dict(hit.score_breakdown),
            }
            for hit in hits
        }
        retrieval_mode, fallback_reason = _actual_retrieval_mode(
            requested_mode=requested_mode,
            embedding_available=embedding_tracker.available,
            predicted_channels=predicted_channels,
        )
        row = _score_query(
            query,
            relevant,
            predicted,
            k=k,
            label_id=str(label.get("id") or ""),
            judgment=str(label.get("judgment") or "unlabeled"),
        )
        row.update(
            {
                "requested_mode": requested_mode,
                "retrieval_mode": retrieval_mode,
                "fallback_reason": fallback_reason,
                "embedding_available": embedding_tracker.available if requested_mode == "embedding" else None,
                "predicted_channels": predicted_channels,
                "predicted_scores": predicted_scores,
            }
        )
        rows.append(row)
    return rows


def _actual_retrieval_mode(
    *,
    requested_mode: str,
    embedding_available: bool,
    predicted_channels: dict[str, list[str]],
) -> tuple[str, str | None]:
    channels = {
        channel
        for hit_channels in predicted_channels.values()
        for channel in hit_channels
    }
    if requested_mode == "keyword":
        return ("keyword" if "keyword" in channels else "none", None)
    if not embedding_available:
        return (
            "keyword_fallback" if "keyword" in channels else "none",
            "embedding_unavailable",
        )
    if "embedding" in channels and "keyword" in channels:
        return "hybrid", None
    if "embedding" in channels:
        return "embedding", None
    if "keyword" in channels:
        return "keyword_fallback", "no_embedding_hits"
    return "none", "no_candidates_scored"


def _score_query(
    query: str,
    relevant: list[str],
    predicted: list[str],
    *,
    k: int,
    label_id: str = "",
    judgment: str | None = None,
) -> dict[str, object]:
    relevant_set = set(relevant)
    top_k = predicted[:k]
    retrieved = len(top_k)
    hit_positions = [i for i, memory_id in enumerate(top_k, start=1) if memory_id in relevant_set]
    relevant_hits = len(hit_positions)

    precision_at_k = relevant_hits / k
    returned_precision = relevant_hits / retrieved if retrieved else 0.0
    recall = relevant_hits / len(relevant_set) if relevant_set else 0.0
    reciprocal_rank = 1.0 / hit_positions[0] if hit_positions else 0.0
    dcg = sum(1.0 / math.log2(pos + 1) for pos in hit_positions)
    ideal_hits = min(k, len(relevant_set))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    ndcg = dcg / idcg if idcg else 0.0
    normalized_judgment = judgment or ("relevant" if relevant_set else "unlabeled")

    return {
        "id": label_id,
        "query": query,
        "judgment": normalized_judgment,
        "graded": normalized_judgment in {"relevant", "no_answer"},
        "relevant_count": len(relevant_set),
        "retrieved": retrieved,
        "relevant_hits": relevant_hits,
        "hit": 1.0 if relevant_hits else 0.0,
        # Keep the historical key for API/UI compatibility, but make it a real
        # P@k.  The former denominator (number actually returned) is exposed
        # separately so an abstaining retriever is not mislabeled as P@k=1.
        "precision": round(precision_at_k, 4),
        "returned_precision": round(returned_precision, 4),
        "recall": round(recall, 4),
        "reciprocal_rank": round(reciprocal_rank, 4),
        "ndcg": round(ndcg, 4),
        "false_positive": normalized_judgment == "no_answer" and retrieved > 0,
        "predicted_ids": top_k,
    }


def save_eval_result(eval_dir: str | Path, *, mode: str, result: dict[str, object]) -> Path:
    path = Path(eval_dir) / _result_name(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, result)
    return path


def load_last_results(
    eval_dir: str | Path,
    *,
    snapshot_path: str | Path | None = None,
) -> dict[str, object]:
    eval_path = Path(eval_dir)
    expected_snapshot = str(Path(snapshot_path)) if snapshot_path is not None else None
    results: dict[str, object] = {}
    for mode in ("keyword", "embedding"):
        path = eval_path / _result_name(mode)
        if not path.exists():
            results[mode] = None
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            results[mode] = None
            continue
        if not isinstance(result, dict) or (
            expected_snapshot is not None and result.get("snapshot") != expected_snapshot
        ):
            results[mode] = None
            continue
        results[mode] = result
    return results


def format_text_report(result: dict[str, object]) -> str:
    summary = result.get("summary", {})
    lines = ["memory-gateway recall evaluation", ""]
    if isinstance(summary, dict):
        graded = summary.get("queries_graded", 0)
        lines.append(
            f"Queries: {summary.get('queries_total')} total, {graded} graded "
            f"({summary.get('queries_relevant', 0)} relevant, "
            f"{summary.get('queries_no_answer', 0)} no-answer, "
            f"k={summary.get('effective_k', summary.get('k'))})"
        )
        if summary.get("duplicate_queries_collapsed"):
            lines.append(
                f"- duplicate queries collapsed: {summary.get('duplicate_queries_collapsed')} "
                f"(input={summary.get('queries_input')})"
            )
        if summary.get("queries_relevant"):
            lines.append(f"- hit_rate@k:    {summary.get('hit_rate')}")
            lines.append(f"- precision@k:   {summary.get('precision_at_k')}")
            lines.append(f"- returned precision: {summary.get('returned_precision')}")
            lines.append(f"- recall@k:      {summary.get('recall_at_k')}")
            lines.append(f"- MRR:           {summary.get('mrr')}")
            lines.append(f"- nDCG@k:        {summary.get('ndcg_at_k')}")
        if summary.get("queries_no_answer"):
            lines.append(
                f"- no-answer false-positive rate: {summary.get('no_answer_false_positive_rate')}"
            )
            lines.append(f"- no-answer abstention rate:     {summary.get('no_answer_abstention_rate')}")
        if not graded:
            lines.append("No graded queries yet. Set judgment to relevant or no_answer, then re-run.")
    lines.append("")
    lines.append("Per-query:")
    for row in result.get("per_query", []):
        if not isinstance(row, dict):
            continue
        if row.get("judgment") == "no_answer":
            lines.append(
                f"- no-answer false_positive={str(bool(row.get('false_positive'))).lower()} "
                f"retrieved={row['retrieved']} mode={row.get('retrieval_mode')} :: {row['query']}"
            )
            continue
        if row.get("judgment") == "unlabeled":
            lines.append(f"- (ungraded) {row['query']}")
            continue
        lines.append(
            f"- hit={int(row['hit'])} p={row['precision']} r={row['recall']} "
            f"rr={row['reciprocal_rank']} ndcg={row['ndcg']} :: {row['query']}"
        )
    return "\n".join(lines)


_STATE_LABELS = {
    "active": "OK",
    "degenerate": "DEGENERATE",
    "dormant": "DORMANT",
    "sparse": "SPARSE",
    "insufficient_data": "NO DATA",
}


def format_diagnosis_text_report(result: dict[str, object]) -> str:
    lines = [
        "memory-gateway mechanism health diagnosis",
        f"Database: {result.get('database')}",
        f"User: {result.get('user_id') or 'all'}",
        f"Active memories: {result.get('memory_count')}",
    ]
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
        return "\n".join(lines)

    lines.append("")
    lines.append("Mechanism verdicts:")
    for verdict in result.get("verdicts", []):
        if not isinstance(verdict, dict):
            continue
        label = _STATE_LABELS.get(str(verdict.get("state")), str(verdict.get("state")).upper())
        lines.append(f"- [{label}] {verdict.get('mechanism')}: {verdict.get('message')}")

    metrics = result.get("metrics", {})
    if isinstance(metrics, dict):
        lines.extend(["", "Key metrics:"])
        lines.append(f"- type_distribution: {json.dumps(metrics.get('type_distribution'), ensure_ascii=False)}")
        lines.append(f"- status_distribution: {json.dumps(metrics.get('status_distribution'), ensure_ascii=False)}")
        lines.append(f"- tag_coverage: {json.dumps(metrics.get('tag_coverage'), ensure_ascii=False)}")
        lines.append(f"- graph: {json.dumps(metrics.get('graph'), ensure_ascii=False)}")
        lines.append(f"- temporal: {json.dumps(metrics.get('temporal'), ensure_ascii=False)}")
        lines.append(f"- never_recalled_count: {metrics.get('never_recalled_count')}")
        lines.append(f"- affect: {json.dumps(metrics.get('affect'), ensure_ascii=False)}")
        lines.append(f"- recall: {json.dumps(metrics.get('recall'), ensure_ascii=False)}")
    return "\n".join(lines)


_ZH_KEY_TEXT = {
    "episodic": "事件",
    "semantic": "语义",
    "procedural": "流程",
    "emotional": "情绪",
    "reflective": "反思",
    "dynamic": "活跃",
    "resolved": "已解决",
    "archived": "归档",
    "pinned": "钉选",
}


def _zh_key(key: str) -> str:
    return _ZH_KEY_TEXT.get(key, key)


def _sector_verdict(type_dist: dict[str, int], total: int) -> Verdict:
    metrics = {"distinct_types": len(type_dist), "distribution": type_dist}
    if total < MIN_MEANINGFUL_COUNT:
        return Verdict(
            "sector_typing",
            "insufficient_data",
            f"仅 {total} 条记忆，扇区分布还不具参考意义。",
            metrics,
        )
    top_share = max(type_dist.values()) / total if type_dist else 0.0
    metrics["top_share"] = round(top_share, 3)
    if len(type_dist) <= 1 or top_share >= DEGENERATE_TYPE_SHARE:
        top_type = max(type_dist, key=type_dist.get) if type_dist else "?"
        return Verdict(
            "sector_typing",
            "degenerate",
            f"{top_share:.0%} 的记忆都是「{_zh_key(top_type)}」类型，"
            f"五扇区划分和各扇区独立的衰减系数已退化为单一扇区。",
            metrics,
        )
    if top_share >= SKEWED_TYPE_SHARE:
        return Verdict(
            "sector_typing",
            "active",
            f"扇区分类已在使用，但分布偏斜（最大占比 {top_share:.0%}）。",
            metrics,
        )
    return Verdict("sector_typing", "active", "扇区分类已真实分化。", metrics)


def _lifecycle_verdict(status_dist: dict[str, int], total: int) -> Verdict:
    metrics = {"distinct_statuses": len(status_dist), "distribution": status_dist}
    if total == 0:
        return Verdict("lifecycle_status", "insufficient_data", "暂无可评估的记忆。", metrics)
    if len(status_dist) <= 1:
        only = next(iter(status_dist), "dynamic")
        return Verdict(
            "lifecycle_status",
            "dormant",
            f"所有记忆都处于「{_zh_key(only)}」状态，"
            f"生命周期因子（已解决/钉选加权）从未参与区分打分。",
            metrics,
        )
    return Verdict("lifecycle_status", "active", "多种生命周期状态已在使用。", metrics)


def _temporal_verdict(temporal: dict[str, int]) -> Verdict:
    key_count = int(temporal.get("temporal_key_count", 0))
    supersession = int(temporal.get("supersession_link_count", 0))
    active_edges = int(temporal.get("active_supersession_edge_count", supersession))
    dangling = int(temporal.get("dangling_supersession_reference_count", 0))
    if key_count == 0:
        return Verdict(
            "temporal_kg",
            "dormant",
            "没有记忆携带时间主语/谓语键，双时态知识图谱（失效、时间线、替代）"
            "从未在真实数据上被触发。",
            temporal,
        )
    if supersession == 0:
        return Verdict(
            "temporal_kg",
            "active",
            f"{key_count} 条记忆携带时间键，但尚未发生任何替代。",
            temporal,
        )
    if dangling:
        return Verdict(
            "temporal_kg",
            "degenerate",
            f"检测到 {dangling} 个活跃但不互为反向引用、跨时间键或指向回收站的替代引用，"
            "时间版本链需要修复。",
            temporal,
        )
    if active_edges == 0:
        return Verdict(
            "temporal_kg",
            "sparse",
            "时间键已出现，但替代链仅存在于回收站/历史行，当前活跃版本图没有有效边。",
            temporal,
        )
    return Verdict("temporal_kg", "active", "活跃时间键与双向一致的替代链路均已出现。", temporal)


def _graph_verdict(tag_coverage: dict[str, object], total: int) -> Verdict:
    if total < MIN_MEANINGFUL_COUNT:
        return Verdict(
            "graph_structure",
            "insufficient_data",
            f"仅 {total} 条记忆，标签/空间覆盖率还不具参考意义。",
            tag_coverage,
        )
    topic_cov = float(tag_coverage.get("topic_coverage", 0.0))  # type: ignore[arg-type]
    edge_count = int(tag_coverage.get("edge_count", 0))
    connected_share = float(tag_coverage.get("connected_node_share", 0.0))
    if edge_count == 0 or connected_share < SPARSE_TAG_COVERAGE:
        return Verdict(
            "graph_structure",
            "sparse",
            f"检测到 {edge_count} 条实际关系边，覆盖 {connected_share:.0%} 的记忆；"
            f"主题覆盖率为 {topic_cov:.0%}，图遍历仍较稀疏。",
            tag_coverage,
        )
    return Verdict(
        "graph_structure",
        "active",
        f"检测到 {edge_count} 条 evidence、temporal 或共享标签/空间关系边。",
        tag_coverage,
    )


def _affect_metrics(
    connection: sqlite3.Connection,
    columns: set[str],
    total: int,
    type_dist: dict[str, int],
    *,
    user_id: str | None,
) -> dict[str, object]:
    """情绪 affect 通道的激活度：emotional 扇区计数、valence/arousal 分布、卡在默认值的占比。"""
    if not {"valence", "arousal"}.issubset(columns):
        return {"available": False}
    where_sql, params = _active_memory_scope(user_id)
    emotional = int(type_dist.get("emotional", 0))
    # 提取 prompt 对"无法判断"的中性事实写死 valence=0.5、arousal=0.3；两者同时贴默认
    # 值的占比越高，说明情绪坐标越接近常量，emotion_factor 与 mode=emotional 越无区分度。
    default_pair = _count(
        connection,
        f"SELECT COUNT(*) FROM memories WHERE {where_sql} "
        "AND valence IS NOT NULL AND arousal IS NOT NULL "
        "AND ABS(valence - 0.5) < 1e-6 AND ABS(arousal - 0.3) < 1e-6",
        params,
    )
    distinct_valence = _count(
        connection,
        f"SELECT COUNT(DISTINCT valence) FROM memories WHERE {where_sql} AND valence IS NOT NULL",
        params,
    )
    distinct_arousal = _count(
        connection,
        f"SELECT COUNT(DISTINCT arousal) FROM memories WHERE {where_sql} AND arousal IS NOT NULL",
        params,
    )
    return {
        "available": True,
        "emotional_sector_count": emotional,
        "emotional_sector_share": round(emotional / total, 3) if total else 0.0,
        "valence": _numeric_summary(connection, "valence", user_id=user_id),
        "arousal": _numeric_summary(connection, "arousal", user_id=user_id),
        "default_affect_count": default_pair,
        "default_affect_share": round(default_pair / total, 3) if total else 0.0,
        "distinct_valence": distinct_valence,
        "distinct_arousal": distinct_arousal,
    }


def _affect_verdict(affect: dict[str, object], total: int) -> Verdict:
    if not affect.get("available", False):
        return Verdict(
            "emotion_affect",
            "insufficient_data",
            "缺少 valence/arousal 字段，无法诊断情绪通道。",
            affect,
        )
    if total < MIN_MEANINGFUL_COUNT:
        return Verdict(
            "emotion_affect",
            "insufficient_data",
            f"仅 {total} 条记忆，情绪分布还不具参考意义。",
            affect,
        )
    emotional = int(affect.get("emotional_sector_count", 0))  # type: ignore[arg-type]
    default_share = float(affect.get("default_affect_share", 0.0))  # type: ignore[arg-type]
    distinct_valence = int(affect.get("distinct_valence", 0))  # type: ignore[arg-type]
    distinct_arousal = int(affect.get("distinct_arousal", 0))  # type: ignore[arg-type]
    constant_affect = distinct_valence <= 1 and distinct_arousal <= 1
    if constant_affect or (emotional == 0 and default_share >= DEFAULT_AFFECT_DEGENERATE_SHARE):
        return Verdict(
            "emotion_affect",
            "degenerate",
            f"情绪扇区仅 {emotional} 条，{default_share:.0%} 的记忆停在默认情绪值"
            f"（正向度≈0.5 / 唤起度≈0.3），情绪加权衰减和情绪浮现模式无从区分。",
            affect,
        )
    if default_share >= DEFAULT_AFFECT_SKEWED_SHARE:
        return Verdict(
            "emotion_affect",
            "active",
            f"情绪坐标已在使用，但大多停留在默认值"
            f"（{default_share:.0%} 为 0.5/0.3，情绪扇区 {emotional} 条）。",
            affect,
        )
    return Verdict(
        "emotion_affect",
        "active",
        f"情绪坐标已真实散开（情绪扇区 {emotional} 条，默认值占 {default_share:.0%}）。",
        affect,
    )


def _recall_metrics(
    connection: sqlite3.Connection,
    total: int,
    never_recalled: int,
    *,
    user_id: str | None,
) -> dict[str, object]:
    """无需标注的激活健康度。

    ``usage_count`` 同时包含直接召回与 Time Ripple 的小数增量，所以它是
    activation_count，而不是精确的召回次数。保留旧键仅用于 API 兼容。
    """
    where_sql, params = _active_memory_scope(user_id)
    row = connection.execute(
        f"SELECT COALESCE(SUM(usage_count), 0) AS total_recalls, "
        f"COALESCE(MAX(usage_count), 0) AS max_recalls "
        f"FROM memories WHERE {where_sql}",
        params,
    ).fetchone()
    total_activation = float(row["total_recalls"] or 0.0)
    max_activation = float(row["max_recalls"] or 0.0)
    recalled = max(0, total - int(never_recalled))
    return {
        "activated_memory_count": recalled,
        "activated_memory_share": round(recalled / total, 3) if total else 0.0,
        "never_activated_count": int(never_recalled),
        "total_activation_count": total_activation,
        "max_activation_count": max_activation,
        "top1_concentration": (
            round(max_activation / total_activation, 3) if total_activation else 0.0
        ),
        # Backward-compatible aliases. These values are activation counts, not
        # literal search-event counts.
        "recalled_count": recalled,
        "recalled_share": round(recalled / total, 3) if total else 0.0,
        "never_recalled_count": int(never_recalled),
        "total_recalls": total_activation,
        "max_recalls": max_activation,
    }


def _recall_verdict(recall: dict[str, object], total: int) -> Verdict:
    if total == 0:
        return Verdict("recall_health", "insufficient_data", "暂无可评估的记忆。", recall)
    recalled = int(recall.get("activated_memory_count", 0))  # type: ignore[arg-type]
    total_activation = float(recall.get("total_activation_count", 0.0))  # type: ignore[arg-type]
    concentration = float(recall.get("top1_concentration", 0.0))  # type: ignore[arg-type]
    if recalled == 0 or total_activation <= 0.0:
        return Verdict(
            "recall_health",
            "dormant",
            "还没有记忆被激活过，搜索/浮现尚未带来任何激活。",
            recall,
        )
    if concentration >= RECALL_CONCENTRATION_WARN:
        return Verdict(
            "recall_health",
            "active",
            f"召回高度集中：最热的一条记忆占了全部激活的 {concentration:.0%}"
            f"（{recalled}/{total} 条被激活过）。",
            recall,
        )
    return Verdict(
        "recall_health",
        "active",
        f"{recalled}/{total} 条记忆被激活过，激活分布比较均匀。",
        recall,
    )


def _group_counts(connection: sqlite3.Connection, column: str, *, user_id: str | None) -> dict[str, int]:
    where_sql, params = _active_memory_scope(user_id)
    rows = connection.execute(
        f"SELECT {column} AS key, COUNT(*) AS count FROM memories "
        f"WHERE {where_sql} GROUP BY {column} ORDER BY count DESC",
        params,
    ).fetchall()
    return {str(row["key"]): int(row["count"]) for row in rows}


def _tag_coverage(
    connection: sqlite3.Connection,
    columns: set[str],
    total: int,
    *,
    user_id: str | None,
) -> dict[str, object]:
    if total <= 0:
        return {"topic_coverage": 0.0, "entity_coverage": 0.0, "space_coverage": 0.0}
    where_sql, params = _active_memory_scope(user_id)
    topics = _count(
        connection,
        f"SELECT COUNT(*) FROM memories WHERE {where_sql} "
        "AND topics_json IS NOT NULL AND topics_json != '' AND topics_json != '[]'",
        params,
    )
    entities = _count(
        connection,
        f"SELECT COUNT(*) FROM memories WHERE {where_sql} "
        "AND entities_json IS NOT NULL AND entities_json != '' AND entities_json != '[]'",
        params,
    )
    coverage: dict[str, object] = {
        "topic_coverage": round(topics / total, 3),
        "entity_coverage": round(entities / total, 3),
        "topic_tagged": topics,
        "entity_tagged": entities,
    }
    if _table_exists(connection, "memory_space_links"):
        link_scope = "COALESCE(memory.archived, 0) = 0"
        link_params: tuple[object, ...] = ()
        if user_id is not None:
            link_scope += " AND COALESCE(memory.user_id, 'default') = ? AND link.user_id = ?"
            link_params = (user_id, user_id)
        linked = _count(
            connection,
            "SELECT COUNT(DISTINCT link.memory_id) "
            "FROM memory_space_links AS link "
            "JOIN memories AS memory ON memory.id = link.memory_id "
            f"WHERE {link_scope}",
            link_params,
        )
        coverage["space_coverage"] = round(linked / total, 3)
        coverage["space_linked"] = linked
    else:
        coverage["space_coverage"] = 0.0
        coverage["space_linked"] = 0
    return coverage


def _graph_metrics(
    connection: sqlite3.Connection,
    columns: set[str],
    total: int,
    *,
    tag_coverage: object,
    user_id: str | None,
) -> dict[str, object]:
    """Measure relationships that actually connect active memory rows."""
    metrics = dict(tag_coverage) if isinstance(tag_coverage, dict) else {}
    if total <= 0:
        metrics.update(
            {
                "edge_count": 0,
                "connected_node_count": 0,
                "connected_node_share": 0.0,
                "edge_kinds": {},
            }
        )
        return metrics

    optional_columns = [
        name
        for name in (
            "topics_json",
            "entities_json",
            "evidence_memory_ids_json",
            "supersedes",
        )
        if name in columns
    ]
    where_sql, params = _active_memory_scope(user_id)
    rows = connection.execute(
        f"SELECT id{''.join(f', {name}' for name in optional_columns)} "
        f"FROM memories WHERE {where_sql}",
        params,
    ).fetchall()
    active_ids = {str(row["id"]) for row in rows}
    edge_kinds: dict[str, set[tuple[str, str]]] = {
        "evidence": set(),
        "temporal": set(),
        "topic": set(),
        "entity": set(),
        "space": set(),
    }
    groups: dict[tuple[str, str], list[str]] = {}

    def json_strings(raw: object, *, normalize_label: bool = True) -> list[str]:
        try:
            values = json.loads(str(raw)) if raw else []
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        return [value.casefold() for value in cleaned] if normalize_label else cleaned

    def edge(left_id: str, right_id: str) -> tuple[str, str] | None:
        if left_id == right_id or right_id not in active_ids:
            return None
        return tuple(sorted((left_id, right_id)))

    for row in rows:
        memory_id = str(row["id"])
        if "evidence_memory_ids_json" in optional_columns:
            for evidence_id in json_strings(
                row["evidence_memory_ids_json"],
                normalize_label=False,
            ):
                pair = edge(memory_id, evidence_id)
                if pair:
                    edge_kinds["evidence"].add(pair)
        if "supersedes" in optional_columns and row["supersedes"]:
            pair = edge(memory_id, str(row["supersedes"]))
            if pair:
                edge_kinds["temporal"].add(pair)
        for column_name, kind in (
            ("topics_json", "topic"),
            ("entities_json", "entity"),
        ):
            if column_name not in optional_columns:
                continue
            for label in json_strings(row[column_name]):
                groups.setdefault((kind, label), []).append(memory_id)

    if _table_exists(connection, "memory_space_links") and active_ids:
        link_query = "SELECT memory_id, space_id FROM memory_space_links"
        link_params: tuple[object, ...] = ()
        if user_id is not None:
            link_query += " WHERE user_id = ?"
            link_params = (user_id,)
        for row in connection.execute(link_query, link_params).fetchall():
            memory_id = str(row["memory_id"])
            if memory_id in active_ids:
                groups.setdefault(("space", str(row["space_id"])), []).append(memory_id)

    # A spanning star per shared label proves connectivity without materializing
    # every O(n²) clique edge in large libraries.
    for (kind, _), member_ids in groups.items():
        unique_members = list(dict.fromkeys(member_ids))
        if len(unique_members) < 2:
            continue
        anchor = unique_members[0]
        for member_id in unique_members[1:]:
            pair = edge(anchor, member_id)
            if pair:
                edge_kinds[kind].add(pair)

    all_edges = set().union(*edge_kinds.values())
    connected_ids = {memory_id for pair in all_edges for memory_id in pair}
    metrics.update(
        {
            "edge_count": len(all_edges),
            "connected_node_count": len(connected_ids),
            "connected_node_share": round(len(connected_ids) / total, 3),
            "edge_kinds": {kind: len(edges) for kind, edges in edge_kinds.items()},
        }
    )
    return metrics


def _temporal_metrics(
    connection: sqlite3.Connection,
    columns: set[str],
    *,
    user_id: str | None,
) -> dict[str, int]:
    metrics: dict[str, int] = {}
    where_sql, params = _active_memory_scope(user_id)
    if {"temporal_subject", "temporal_predicate"}.issubset(columns):
        metrics["temporal_key_count"] = _count(
            connection,
            f"SELECT COUNT(*) FROM memories WHERE {where_sql} "
            "AND temporal_subject IS NOT NULL AND temporal_predicate IS NOT NULL",
            params,
        )
    if {"supersedes", "superseded_by"}.issubset(columns):
        scope_sql = "1 = 1" if user_id is None else "user_id = ?"
        scope_params: tuple[object, ...] = () if user_id is None else (user_id,)
        rows = connection.execute(
            f"SELECT id, user_id, archived, temporal_subject, temporal_predicate, "
            f"supersedes, superseded_by FROM memories WHERE {scope_sql}",
            scope_params,
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        linked_rows = [
            row for row in rows if row["supersedes"] or row["superseded_by"]
        ]
        active_rows = [row for row in rows if not int(row["archived"] or 0)]
        valid_edges: set[tuple[str, str]] = set()
        valid_active_ids: set[str] = set()
        dangling_references = 0

        def same_temporal_key(left: sqlite3.Row, right: sqlite3.Row) -> bool:
            return bool(
                left["temporal_subject"]
                and left["temporal_predicate"]
                and left["temporal_subject"] == right["temporal_subject"]
                and left["temporal_predicate"] == right["temporal_predicate"]
            )

        for row in active_rows:
            memory_id = str(row["id"])
            for field_name, reciprocal_name in (
                ("supersedes", "superseded_by"),
                ("superseded_by", "supersedes"),
            ):
                target_id = str(row[field_name] or "")
                if not target_id:
                    continue
                target = by_id.get(target_id)
                valid = bool(
                    target is not None
                    and not int(target["archived"] or 0)
                    and str(target["user_id"]) == str(row["user_id"])
                    and str(target[reciprocal_name] or "") == memory_id
                    and same_temporal_key(row, target)
                )
                if not valid:
                    dangling_references += 1
                    continue
                edge = tuple(sorted((memory_id, target_id)))
                valid_edges.add(edge)
                valid_active_ids.update(edge)

        metrics.update(
            {
                "supersession_link_count": len(linked_rows),
                "active_supersession_link_count": len(valid_active_ids),
                "active_supersession_edge_count": len(valid_edges),
                "trashed_supersession_link_count": sum(
                    1 for row in linked_rows if int(row["archived"] or 0)
                ),
                "dangling_supersession_reference_count": dangling_references,
            }
        )
    if "valid_from" in columns:
        metrics["valid_from_count"] = _count(
            connection,
            f"SELECT COUNT(*) FROM memories WHERE {where_sql} "
            "AND valid_from IS NOT NULL AND valid_from != ''",
            params,
        )
    return metrics


def _numeric_summary(connection: sqlite3.Connection, column: str, *, user_id: str | None) -> dict[str, object]:
    where_sql, params = _active_memory_scope(user_id)
    row = connection.execute(
        f"SELECT MIN({column}) AS lo, MAX({column}) AS hi, AVG({column}) AS avg "
        f"FROM memories WHERE {where_sql} AND {column} IS NOT NULL",
        params,
    ).fetchone()
    if row is None or row["lo"] is None:
        return {"min": None, "max": None, "avg": None}
    return {
        "min": row["lo"],
        "max": row["hi"],
        "avg": round(float(row["avg"]), 2),
    }


def _active_memory_scope(user_id: str | None) -> tuple[str, tuple[object, ...]]:
    if user_id is None:
        return "COALESCE(archived, 0) = 0", ()
    return "COALESCE(archived, 0) = 0 AND COALESCE(user_id, 'default') = ?", (user_id,)


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


def _new_snapshot_path(eval_dir: Path) -> Path:
    while True:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        path = eval_dir / f"{SNAPSHOT_PREFIX}{stamp}.db"
        if not path.exists():
            return path


def _user_eval_dir(eval_dir: str | Path, *, user_id: str) -> Path:
    normalized_user_id = user_id or "default"
    digest = hashlib.sha256(normalized_user_id.encode("utf-8")).hexdigest()
    return Path(eval_dir) / USER_WORKSPACES_NAME / digest


def _current_snapshot_path(eval_dir: str | Path) -> Path:
    eval_path = Path(eval_dir)
    pointer_path = eval_path / SNAPSHOT_POINTER_NAME
    try:
        pointed_name = pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        pointed_name = ""

    if pointed_name:
        pointed_path = eval_path / pointed_name
        if pointed_path.exists():
            return pointed_path

    legacy_path = eval_path / SNAPSHOT_NAME
    if legacy_path.exists():
        return legacy_path

    snapshots = sorted(
        eval_path.glob(f"{SNAPSHOT_PREFIX}*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return snapshots[0] if snapshots else legacy_path


def _write_current_snapshot_pointer(eval_dir: Path, snapshot_path: Path) -> None:
    pointer_path = eval_dir / SNAPSHOT_POINTER_NAME
    tmp_path = pointer_path.with_name(pointer_path.name + ".tmp")
    tmp_path.write_text(snapshot_path.name, encoding="utf-8")
    tmp_path.replace(pointer_path)


def _cleanup_old_snapshots(eval_dir: Path, *, current_snapshot: Path, keep: int = 3) -> None:
    snapshots = [
        path
        for path in eval_dir.glob(f"{SNAPSHOT_PREFIX}*.db")
        if path.resolve() != current_snapshot.resolve()
    ]
    legacy_path = eval_dir / SNAPSHOT_NAME
    if legacy_path.exists() and legacy_path.resolve() != current_snapshot.resolve():
        snapshots.append(legacy_path)

    snapshots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for snapshot_path in snapshots[keep:]:
        _unlink_sqlite_database(snapshot_path)


def _invalidate_eval_results(eval_dir: Path) -> None:
    for name in (KEYWORD_RESULT_NAME, EMBEDDING_RESULT_NAME):
        (eval_dir / name).unlink(missing_ok=True)


def _unlink_sqlite_database(
    path: Path,
    *,
    ignore_permission_error: bool = True,
) -> int:
    removed = 0
    for target in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        try:
            if target.is_file():
                target.unlink()
                removed += 1
        except PermissionError:
            if ignore_permission_error:
                continue
            raise
    return removed


def _snapshot_readonly(source_path: Path, snapshot_path: Path, *, user_id: str) -> None:
    """用 backup API 建立临时副本，过滤完成后再原子发布单用户快照。"""
    resolved = source_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:")
    temp_path = snapshot_path.with_name(f".{snapshot_path.name}.tmp")
    _unlink_sqlite_database(temp_path)
    source = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(temp_path))
        try:
            source.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")
            _filter_snapshot_to_user(dest, user_id=user_id)
        finally:
            dest.close()
        # Filtering is committed into the main temp file before publication. Any
        # empty/stale sidecars must keep the temporary name and never accompany
        # the atomically replaced snapshot.
        for sidecar in (
            Path(str(temp_path) + "-wal"),
            Path(str(temp_path) + "-shm"),
            Path(str(temp_path) + "-journal"),
        ):
            sidecar.unlink(missing_ok=True)
        temp_path.replace(snapshot_path)
    except Exception:
        _unlink_sqlite_database(temp_path)
        raise
    finally:
        source.close()


def _filter_snapshot_to_user(connection: sqlite3.Connection, *, user_id: str) -> None:
    connection.execute("PRAGMA secure_delete = ON")
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in table_rows:
        table_name = str(row[0])
        quoted_table = _quote_identifier(table_name)
        columns = {
            str(column[1])
            for column in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        }
        if "user_id" in columns:
            connection.execute(
                f"DELETE FROM {quoted_table} WHERE COALESCE(user_id, 'default') <> ?",
                (user_id,),
            )
            connection.execute(
                f"UPDATE {quoted_table} SET user_id = 'default' WHERE user_id IS NULL"
            )
        else:
            # 未声明用户边界的辅助表不能安全带入用户快照；保留 schema，清空其数据。
            connection.execute(f"DELETE FROM {quoted_table}")
    connection.commit()
    connection.execute("VACUUM")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _read_snapshot_overview(
    snapshot_path: Path,
    *,
    user_id: str,
) -> tuple[dict[str, int], list[tuple[str, str, str]]]:
    connection = sqlite3.connect(str(snapshot_path))
    try:
        connection.row_factory = sqlite3.Row
        user_rows = connection.execute(
            "SELECT COALESCE(user_id, 'default') AS user_id, COUNT(*) AS count "
            "FROM memories WHERE COALESCE(archived, 0) = 0 GROUP BY user_id ORDER BY count DESC"
        ).fetchall()
        user_counts = {str(row["user_id"]): int(row["count"]) for row in user_rows}
    finally:
        connection.close()
    memories = _eligible_snapshot_memories(snapshot_path, user_id=user_id)
    preview = [
        (memory.id, memory.type, _one_line(memory.content))
        for memory in memories
    ]
    return user_counts, preview


def _write_preview(preview_path: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["id\ttype\tcontent_preview"]
    lines.extend(f"{memory_id}\t{memory_type}\t{content}" for memory_id, memory_type, content in rows)
    preview_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _snapshot_memories(
    snapshot_path: Path,
    *,
    user_id: str,
    redact_sensitive: bool,
) -> list[dict[str, object]]:
    memories = _eligible_snapshot_memories(snapshot_path, user_id=user_id)
    payloads: list[dict[str, object]] = []
    for memory in memories:
        payload = memory.model_dump(exclude={"embedding_json"})
        payloads.append(redact_memory_payload(payload, redact_sensitive=redact_sensitive))
    return payloads


def _eligible_snapshot_memories(snapshot_path: Path, *, user_id: str):
    """Mirror the default search candidate pool before scoring."""
    store = _EvaluationMemoryStore(str(snapshot_path))
    memories = store.list_memories(
        user_id=user_id,
        limit=RECALL_CANDIDATE_POOL,
        include_lifecycle_archived=False,
    )
    return [
        memory
        for memory in memories
        if memory.origin == "user_asserted"
        and not _memory_is_locally_sensitive(memory)
    ]


def _snapshot_memory_ids(snapshot_path: Path, *, user_id: str) -> set[str]:
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}. Run recall init first.")
    return {str(memory["id"]) for memory in _snapshot_memories(snapshot_path, user_id=user_id, redact_sensitive=False)}


def _normalize_label_entry(entry: object, *, index: int) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise EvaluationError(f"Label line must be a JSON object: {entry!r}")
    label_id = _one_line(str(entry.get("id") or f"q{index:03d}"), limit=80)
    query = str(entry.get("query") or "").strip()
    relevant_raw = entry.get("relevant_ids", [])
    if not isinstance(relevant_raw, list):
        raise EvaluationError(f"Label relevant_ids must be a list: {entry!r}")
    relevant_ids = [str(memory_id).strip() for memory_id in relevant_raw if str(memory_id).strip()]
    judgment_raw = entry.get("judgment")
    if judgment_raw is None or not str(judgment_raw).strip():
        judgment = "relevant" if relevant_ids else "unlabeled"
    else:
        judgment = str(judgment_raw).strip().lower()
    note_raw = entry.get("note")
    label: dict[str, object] = {
        "id": label_id or f"q{index:03d}",
        "query": query,
        "judgment": judgment,
        "relevant_ids": list(dict.fromkeys(relevant_ids)),
    }
    if note_raw is not None:
        label["note"] = str(note_raw)
    return label


def _validate_labels(labels: list[dict[str, object]], *, valid_ids: set[str]) -> list[dict[str, object]]:
    normalized = [_normalize_label_entry(label, index=index) for index, label in enumerate(labels, start=1)]
    issues = _label_validation_issues(normalized, valid_ids=valid_ids)
    blocking = [issue for issue in issues if issue["code"] in BLOCKING_LABEL_ISSUE_CODES]
    if blocking:
        raise EvaluationError("; ".join(str(issue["message"]) for issue in blocking))
    return normalized


def _label_summary(labels: list[dict[str, object]]) -> dict[str, int]:
    relevant = sum(1 for label in labels if label.get("judgment") == "relevant")
    no_answer = sum(1 for label in labels if label.get("judgment") == "no_answer")
    graded = relevant + no_answer
    return {
        "queries_total": len(labels),
        "queries_graded": graded,
        "queries_relevant": relevant,
        "queries_no_answer": no_answer,
        "queries_unlabeled": len(labels) - graded,
        "target_min": TARGET_LABEL_MIN,
        "target_max": TARGET_LABEL_MAX,
    }


def _label_validation_issues(
    labels: list[dict[str, object]],
    *,
    valid_ids: set[str],
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    seen: set[str] = set()
    seen_queries: dict[str, tuple[str, tuple[str, ...], str]] = {}
    for label in labels:
        label_id = str(label.get("id") or "")
        query = str(label.get("query") or "").strip()
        if not query:
            issues.append({"code": "blank_query", "label_id": label_id, "message": "query 不能为空。"})
        if label_id in seen:
            issues.append({"code": "duplicate_label_id", "label_id": label_id, "message": f"标注 ID 重复:{label_id}"})
        seen.add(label_id)
        judgment = str(label.get("judgment") or "unlabeled")
        relevant_ids = list(label.get("relevant_ids", []))
        normalized_query = " ".join(query.casefold().split())
        query_signature = (
            judgment,
            tuple(sorted(str(memory_id) for memory_id in relevant_ids)),
            label_id,
        )
        previous = seen_queries.get(normalized_query)
        if normalized_query and previous is not None:
            previous_judgment, previous_ids, previous_label_id = previous
            same_annotation = (
                judgment == previous_judgment
                and query_signature[1] == previous_ids
            )
            issues.append(
                {
                    "code": "duplicate_query" if same_annotation else "duplicate_query_conflict",
                    "label_id": label_id,
                    "other_label_id": previous_label_id,
                    "message": (
                        f"{label_id} 与 {previous_label_id} 的 query 重复；评测时会折叠重复样本。"
                        if same_annotation
                        else f"{label_id} 与 {previous_label_id} 的 query 相同但标注冲突。"
                    ),
                }
            )
        elif normalized_query:
            seen_queries[normalized_query] = query_signature
        if judgment not in LABEL_JUDGMENTS:
            issues.append(
                {
                    "code": "invalid_judgment",
                    "label_id": label_id,
                    "message": f"{label_id} 的标注判断无效:{judgment}",
                }
            )
        elif judgment == "relevant" and not relevant_ids:
            issues.append(
                {
                    "code": "missing_relevant_ids",
                    "label_id": label_id,
                    "message": f"{label_id} 标为「有相关记忆」时至少要选择一条记忆。",
                }
            )
        elif judgment == "no_answer" and relevant_ids:
            issues.append(
                {
                    "code": "no_answer_with_relevant_ids",
                    "label_id": label_id,
                    "message": f"{label_id} 标为「无答案」时不能勾选相关记忆。",
                }
            )
        elif judgment == "unlabeled" and relevant_ids:
            issues.append(
                {
                    "code": "unlabeled_with_relevant_ids",
                    "label_id": label_id,
                    "message": f"{label_id} 尚未标注，不应勾选相关记忆。",
                }
            )
        for memory_id in relevant_ids:
            memory_id_text = str(memory_id)
            if memory_id_text not in valid_ids:
                issues.append(
                    {
                        "code": "unknown_memory_id",
                        "label_id": label_id,
                        "memory_id": memory_id_text,
                        "message": f"记忆 ID 不在当前快照/用户范围内:{memory_id_text}",
                    }
                )
    return issues


def _deduplicate_identical_queries(
    labels: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Collapse exact duplicate samples so one query cannot silently reweight metrics."""
    unique: list[dict[str, object]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for label in labels:
        signature = (
            " ".join(str(label.get("query") or "").casefold().split()),
            str(label.get("judgment") or "unlabeled"),
            tuple(sorted(str(value) for value in label.get("relevant_ids", []))),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(label)
    return unique


def _write_labels_atomic(labels_path: Path, labels: list[dict[str, object]]) -> None:
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(label, ensure_ascii=False, sort_keys=True)
        for label in labels
    ]
    tmp_path = labels_path.with_suffix(labels_path.suffix + ".tmp")
    tmp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp_path.replace(labels_path)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _result_name(mode: str) -> str:
    return EMBEDDING_RESULT_NAME if mode == "embedding" else KEYWORD_RESULT_NAME


def _one_line(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def _mean(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _build_embedding_client() -> EmbeddingClient:
    from app.api.deps import get_embedding_client
    from app.config import get_settings

    return get_embedding_client(get_settings())


def recall_cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Micro recall evaluation for memory-gateway search.")
    parser.add_argument("--init", action="store_true", help="Snapshot the real DB and scaffold labels.")
    parser.add_argument("--run", action="store_true", help="Run the evaluation against the snapshot.")
    parser.add_argument("--database", default="data/memory.db", help="Real SQLite database path (read-only).")
    parser.add_argument("--eval-dir", default=DEFAULT_EVAL_DIR, help="Directory for snapshot/preview/labels.")
    parser.add_argument("--user-id", default="default", help="X-User-Id scope to evaluate.")
    parser.add_argument(
        "--k",
        type=int,
        default=8,
        help=f"Top-k cutoff (1-{MAX_RECALL_EVAL_K}).",
    )
    parser.add_argument("--use-embedding", action="store_true", help="Use the real embedding provider for queries.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if not args.init and not args.run:
        parser.error("Specify --init or --run.")

    eval_dir = Path(args.eval_dir)

    if args.init:
        result = init_eval(source_db=args.database, eval_dir=eval_dir, user_id=args.user_id)
        if result.get("error"):
            print(result["error"])
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"Snapshot: {result['snapshot']} ({result['memory_count']} active memories)")
            print(f"Preview:  {result['preview']}")
            print(f"Labels:   {result['labels']} ({'created template' if result['labels_created'] else 'kept existing'})")
            print(f"User scopes: {json.dumps(result['user_counts'], ensure_ascii=False)}")
            print("\nNext: edit labels.jsonl to fill relevant_ids, then run --run.")
        if not args.run:
            return 0

    mode = "embedding" if args.use_embedding else "keyword"
    try:
        result = run_recall_eval(
            eval_dir=eval_dir,
            user_id=args.user_id,
            mode=mode,
            k=args.k,
            embedding_client=_build_embedding_client() if args.use_embedding else NullEmbeddingClient(),
        )
    except (FileNotFoundError, EvaluationError) as exc:
        print(str(exc))
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text_report(result))
    return 0


def diagnosis_cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only diagnosis of whether memory mechanisms are activated by real data."
    )
    parser.add_argument("--database", default="data/memory.db", help="SQLite database path.")
    parser.add_argument("--user-id", default=None, help="Optional user scope.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    result = run_diagnosis(args.database, user_id=args.user_id)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_diagnosis_text_report(result))
    return 1 if result.get("error") else 0
