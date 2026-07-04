from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

from app.memory.redaction import redact_memory_payload
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    RECALL_CANDIDATE_POOL,
)
from app.memory.store import MemoryStore


DEFAULT_EVAL_DIR = "eval"
SNAPSHOT_NAME = "eval_snapshot.db"
SNAPSHOT_PREFIX = "eval_snapshot_"
SNAPSHOT_POINTER_NAME = "current_snapshot.txt"
PREVIEW_NAME = "memories_preview.tsv"
LABELS_NAME = "labels.jsonl"
KEYWORD_RESULT_NAME = "last_keyword_result.json"
EMBEDDING_RESULT_NAME = "last_embedding_result.json"

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

LABELS_TEMPLATE = """# memory-gateway 召回评测标注文件
# 每行一个 JSON 对象：{"id": "q001", "query": "一句话检索意图", "relevant_ids": ["应被召回的 memory id"], "note": "可选说明"}
# - query 用自然的检索意图，模拟客户端调用 search_memory 时的 query。
# - relevant_ids 从 memories_preview.tsv 或 Web 评测闭环页里挑选你认为这个 query 应该命中的记忆 id。
# - 以 # 开头的行和空行会被忽略。
{"id": "q001", "query": "用户的饮食偏好", "relevant_ids": [], "note": "示例，请填入真实 id"}
"""


@dataclass
class Verdict:
    mechanism: str
    state: str
    message: str
    metrics: dict[str, object] = field(default_factory=dict)


class EvaluationError(ValueError):
    pass


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
        verdicts.append(_graph_verdict(metrics["tag_coverage"], total))
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
) -> dict[str, object]:
    """把真实库只读快照到 eval 目录，并生成预览和标注模板。"""
    source_path = Path(source_db)
    if not source_path.exists():
        return {"error": f"Source database does not exist: {source_path}"}

    out_dir = Path(eval_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = _new_snapshot_path(out_dir)
    preview_path = out_dir / PREVIEW_NAME
    labels_path = out_dir / LABELS_NAME

    _snapshot_readonly(source_path, snapshot_path)

    user_counts, preview_rows = _read_snapshot_overview(snapshot_path)
    _write_preview(preview_path, preview_rows)
    _write_current_snapshot_pointer(out_dir, snapshot_path)
    _cleanup_old_snapshots(out_dir, current_snapshot=snapshot_path)

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
    }


def load_labels(labels_path: str | Path) -> list[dict[str, object]]:
    path = Path(labels_path)
    labels: list[dict[str, object]] = []
    for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid label line: {line!r} ({exc})") from exc
        labels.append(_normalize_label_entry(entry, index=index))
    return labels


def save_labels(
    *,
    eval_dir: str | Path,
    labels: list[dict[str, object]],
    user_id: str,
) -> dict[str, object]:
    snapshot_path = _current_snapshot_path(eval_dir)
    labels_path = Path(eval_dir) / LABELS_NAME
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
    eval_path = Path(eval_dir)
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
        "last_results": load_last_results(eval_path),
    }


def run_eval(
    *,
    snapshot_db: str | Path,
    labels: list[dict[str, object]],
    user_id: str = "default",
    k: int = 8,
    embedding_client: EmbeddingClient | None = None,
) -> dict[str, object]:
    """对快照库跑召回评测，返回 per-query 指标和汇总。"""
    graded = [label for label in labels if label.get("relevant_ids")]
    store = MemoryStore(str(snapshot_db))
    service = MemorySearchService(
        store=store,
        embedding_client=embedding_client or NullEmbeddingClient(),
        # 关闭进程级缓存：否则 keyword/embedding 两次基线会命中同一个
        # (user, query, limit) 缓存 key，第二次直接复用第一次的结果，
        # 还可能与线上实时检索互相串味。
        enable_cache=False,
    )
    per_query = asyncio.run(_search_all(service, labels, user_id=user_id, k=k))

    summary: dict[str, object] = {
        "queries_total": len(labels),
        "queries_graded": len(graded),
        "k": k,
    }
    if graded:
        graded_results = [row for row in per_query if row["relevant_count"] > 0]
        summary["hit_rate"] = round(_mean(row["hit"] for row in graded_results), 4)
        summary["precision_at_k"] = round(_mean(row["precision"] for row in graded_results), 4)
        summary["recall_at_k"] = round(_mean(row["recall"] for row in graded_results), 4)
        summary["mrr"] = round(_mean(row["reciprocal_rank"] for row in graded_results), 4)
        summary["ndcg_at_k"] = round(_mean(row["ndcg"] for row in graded_results), 4)
    return {"summary": summary, "per_query": per_query}


def run_recall_eval(
    *,
    eval_dir: str | Path,
    user_id: str,
    mode: str = "keyword",
    k: int = 8,
    embedding_client: EmbeddingClient | None = None,
) -> dict[str, object]:
    eval_path = Path(eval_dir)
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
    blocking = [
        issue
        for issue in issues
        if issue["code"] in {"blank_query", "unknown_memory_id", "duplicate_label_id"}
    ]
    if blocking:
        raise EvaluationError("; ".join(str(issue["message"]) for issue in blocking))

    result = run_eval(
        snapshot_db=snapshot_path,
        labels=labels,
        user_id=user_id,
        k=k,
        embedding_client=embedding_client if mode == "embedding" else NullEmbeddingClient(),
    )
    result["mode"] = mode
    result["user_id"] = user_id
    result["validation_issues"] = issues
    save_eval_result(eval_path, mode=mode, result=result)
    return result


async def _search_all(
    service: MemorySearchService,
    labels: list[dict[str, object]],
    *,
    user_id: str,
    k: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label in labels:
        query = str(label["query"])
        relevant = [str(memory_id) for memory_id in label.get("relevant_ids", [])]
        hits = await service.search_hits(
            query=query,
            user_id=user_id,
            limit=k,
            record_usage=False,
        )
        predicted = [hit.memory.id for hit in hits]
        rows.append(_score_query(query, relevant, predicted, k=k, label_id=str(label.get("id") or "")))
    return rows


def _score_query(
    query: str,
    relevant: list[str],
    predicted: list[str],
    *,
    k: int,
    label_id: str = "",
) -> dict[str, object]:
    relevant_set = set(relevant)
    top_k = predicted[:k]
    retrieved = len(top_k)
    hit_positions = [i for i, memory_id in enumerate(top_k, start=1) if memory_id in relevant_set]
    relevant_hits = len(hit_positions)

    precision = relevant_hits / retrieved if retrieved else 0.0
    recall = relevant_hits / len(relevant_set) if relevant_set else 0.0
    reciprocal_rank = 1.0 / hit_positions[0] if hit_positions else 0.0
    dcg = sum(1.0 / math.log2(pos + 1) for pos in hit_positions)
    ideal_hits = min(k, len(relevant_set))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    ndcg = dcg / idcg if idcg else 0.0

    return {
        "id": label_id,
        "query": query,
        "relevant_count": len(relevant_set),
        "retrieved": retrieved,
        "relevant_hits": relevant_hits,
        "hit": 1.0 if relevant_hits else 0.0,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "reciprocal_rank": round(reciprocal_rank, 4),
        "ndcg": round(ndcg, 4),
        "predicted_ids": top_k,
    }


def save_eval_result(eval_dir: str | Path, *, mode: str, result: dict[str, object]) -> Path:
    path = Path(eval_dir) / _result_name(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, result)
    return path


def load_last_results(eval_dir: str | Path) -> dict[str, object]:
    eval_path = Path(eval_dir)
    results: dict[str, object] = {}
    for mode in ("keyword", "embedding"):
        path = eval_path / _result_name(mode)
        if not path.exists():
            results[mode] = None
            continue
        try:
            results[mode] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            results[mode] = None
    return results


def format_text_report(result: dict[str, object]) -> str:
    summary = result.get("summary", {})
    lines = ["memory-gateway recall evaluation", ""]
    if isinstance(summary, dict):
        graded = summary.get("queries_graded", 0)
        lines.append(f"Queries: {summary.get('queries_total')} total, {graded} graded (k={summary.get('k')})")
        if graded:
            lines.append(f"- hit_rate@k:    {summary.get('hit_rate')}")
            lines.append(f"- precision@k:   {summary.get('precision_at_k')}")
            lines.append(f"- recall@k:      {summary.get('recall_at_k')}")
            lines.append(f"- MRR:           {summary.get('mrr')}")
            lines.append(f"- nDCG@k:        {summary.get('ndcg_at_k')}")
        else:
            lines.append("No graded queries yet. Fill relevant_ids in labels.jsonl, then re-run.")
    lines.append("")
    lines.append("Per-query:")
    for row in result.get("per_query", []):
        if not isinstance(row, dict):
            continue
        if row["relevant_count"] == 0:
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
        lines.append(f"- temporal: {json.dumps(metrics.get('temporal'), ensure_ascii=False)}")
        lines.append(f"- never_recalled_count: {metrics.get('never_recalled_count')}")
        lines.append(f"- affect: {json.dumps(metrics.get('affect'), ensure_ascii=False)}")
        lines.append(f"- recall: {json.dumps(metrics.get('recall'), ensure_ascii=False)}")
    return "\n".join(lines)


def _sector_verdict(type_dist: dict[str, int], total: int) -> Verdict:
    metrics = {"distinct_types": len(type_dist), "distribution": type_dist}
    if total < MIN_MEANINGFUL_COUNT:
        return Verdict(
            "sector_typing",
            "insufficient_data",
            f"Only {total} memories; sector distribution is not yet meaningful.",
            metrics,
        )
    top_share = max(type_dist.values()) / total if type_dist else 0.0
    metrics["top_share"] = round(top_share, 3)
    if len(type_dist) <= 1 or top_share >= DEGENERATE_TYPE_SHARE:
        top_type = max(type_dist, key=type_dist.get) if type_dist else "?"
        return Verdict(
            "sector_typing",
            "degenerate",
            f"{top_share:.0%} of memories are '{top_type}'. The five-sector split and "
            f"per-sector decay lambdas collapse to a single sector.",
            metrics,
        )
    if top_share >= SKEWED_TYPE_SHARE:
        return Verdict(
            "sector_typing",
            "active",
            f"Sector typing is in use but skewed (top share {top_share:.0%}).",
            metrics,
        )
    return Verdict("sector_typing", "active", "Sector typing shows real diversity.", metrics)


def _lifecycle_verdict(status_dist: dict[str, int], total: int) -> Verdict:
    metrics = {"distinct_statuses": len(status_dist), "distribution": status_dist}
    if total == 0:
        return Verdict("lifecycle_status", "insufficient_data", "No memories to evaluate.", metrics)
    if len(status_dist) <= 1:
        only = next(iter(status_dist), "dynamic")
        return Verdict(
            "lifecycle_status",
            "dormant",
            f"All memories are '{only}'. Lifecycle status factors "
            f"(resolved/pinned weighting) never differentiate scoring.",
            metrics,
        )
    return Verdict("lifecycle_status", "active", "Multiple lifecycle states are in use.", metrics)


def _temporal_verdict(temporal: dict[str, int]) -> Verdict:
    key_count = int(temporal.get("temporal_key_count", 0))
    supersession = int(temporal.get("supersession_link_count", 0))
    if key_count == 0:
        return Verdict(
            "temporal_kg",
            "dormant",
            "No memory carries a temporal subject/predicate key. The bi-temporal KG "
            "(invalidation, timeline, supersession) has never been triggered on real data.",
            temporal,
        )
    if supersession == 0:
        return Verdict(
            "temporal_kg",
            "active",
            f"{key_count} memories carry temporal keys but no supersession has occurred yet.",
            temporal,
        )
    return Verdict("temporal_kg", "active", "Temporal keys and supersession links are present.", temporal)


def _graph_verdict(tag_coverage: dict[str, object], total: int) -> Verdict:
    if total < MIN_MEANINGFUL_COUNT:
        return Verdict(
            "graph_structure",
            "insufficient_data",
            f"Only {total} memories; tag/space coverage is not yet meaningful.",
            tag_coverage,
        )
    topic_cov = float(tag_coverage.get("topic_coverage", 0.0))  # type: ignore[arg-type]
    if topic_cov < SPARSE_TAG_COVERAGE:
        return Verdict(
            "graph_structure",
            "sparse",
            f"Only {topic_cov:.0%} of memories have topics. Network graph, traversal, "
            f"and Time Ripple have almost no edges to work with.",
            tag_coverage,
        )
    return Verdict("graph_structure", "active", "Topic/space tagging gives the graph real edges.", tag_coverage)


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
            "valence/arousal columns are missing; affect cannot be diagnosed.",
            affect,
        )
    if total < MIN_MEANINGFUL_COUNT:
        return Verdict(
            "emotion_affect",
            "insufficient_data",
            f"Only {total} memories; affect distribution is not yet meaningful.",
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
            f"Emotional sector={emotional}; {default_share:.0%} of memories sit at default affect "
            f"(valence≈0.5/arousal≈0.3). Emotion-weighted decay and mode=emotional surfacing "
            f"never differentiate.",
            affect,
        )
    if default_share >= DEFAULT_AFFECT_SKEWED_SHARE:
        return Verdict(
            "emotion_affect",
            "active",
            f"Affect is in use but mostly default ({default_share:.0%} at 0.5/0.3, "
            f"emotional sector={emotional}).",
            affect,
        )
    return Verdict(
        "emotion_affect",
        "active",
        f"Affect shows real spread (emotional sector={emotional}, {default_share:.0%} at default).",
        affect,
    )


def _recall_metrics(
    connection: sqlite3.Connection,
    total: int,
    never_recalled: int,
    *,
    user_id: str | None,
) -> dict[str, object]:
    """无需标注的召回健康度：被召回比例、总召回量、单条记忆的召回集中度。"""
    where_sql, params = _active_memory_scope(user_id)
    row = connection.execute(
        f"SELECT COALESCE(SUM(usage_count), 0) AS total_recalls, "
        f"COALESCE(MAX(usage_count), 0) AS max_recalls "
        f"FROM memories WHERE {where_sql}",
        params,
    ).fetchone()
    total_recalls = int(row["total_recalls"] or 0)
    max_recalls = int(row["max_recalls"] or 0)
    recalled = max(0, total - int(never_recalled))
    return {
        "recalled_count": recalled,
        "recalled_share": round(recalled / total, 3) if total else 0.0,
        "never_recalled_count": int(never_recalled),
        "total_recalls": total_recalls,
        "max_recalls": max_recalls,
        "top1_concentration": round(max_recalls / total_recalls, 3) if total_recalls else 0.0,
    }


def _recall_verdict(recall: dict[str, object], total: int) -> Verdict:
    if total == 0:
        return Verdict("recall_health", "insufficient_data", "No memories to evaluate.", recall)
    recalled = int(recall.get("recalled_count", 0))  # type: ignore[arg-type]
    total_recalls = int(recall.get("total_recalls", 0))  # type: ignore[arg-type]
    concentration = float(recall.get("top1_concentration", 0.0))  # type: ignore[arg-type]
    if recalled == 0 or total_recalls == 0:
        return Verdict(
            "recall_health",
            "dormant",
            "No memory has ever been recalled; search/surface have not driven any activation.",
            recall,
        )
    if concentration >= RECALL_CONCENTRATION_WARN:
        return Verdict(
            "recall_health",
            "active",
            f"Recall is concentrated: the top memory holds {concentration:.0%} of all activations "
            f"({recalled}/{total} memories recalled).",
            recall,
        )
    return Verdict(
        "recall_health",
        "active",
        f"{recalled}/{total} memories have been recalled; activation is reasonably spread.",
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
        if user_id is None:
            linked = _count(connection, "SELECT COUNT(DISTINCT memory_id) FROM memory_space_links")
        else:
            linked = _count(
                connection,
                "SELECT COUNT(DISTINCT memory_id) FROM memory_space_links WHERE user_id = ?",
                (user_id,),
            )
        coverage["space_coverage"] = round(linked / total, 3)
        coverage["space_linked"] = linked
    else:
        coverage["space_coverage"] = 0.0
        coverage["space_linked"] = 0
    return coverage


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
        metrics["supersession_link_count"] = _count(
            connection,
            f"SELECT COUNT(*) FROM memories WHERE {scope_sql} "
            "AND (supersedes IS NOT NULL OR superseded_by IS NOT NULL)",
            scope_params,
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


def _unlink_sqlite_database(path: Path) -> None:
    for target in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            target.unlink(missing_ok=True)
        except PermissionError:
            continue


def _snapshot_readonly(source_path: Path, snapshot_path: Path) -> None:
    """用 backup API 做一致性只读快照（正确处理 WAL），不修改源库。"""
    resolved = source_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:")
    source = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    try:
        if snapshot_path.exists():
            _unlink_sqlite_database(snapshot_path)
        dest = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _read_snapshot_overview(snapshot_path: Path) -> tuple[dict[str, int], list[tuple[str, str, str]]]:
    connection = sqlite3.connect(str(snapshot_path))
    try:
        connection.row_factory = sqlite3.Row
        user_rows = connection.execute(
            "SELECT COALESCE(user_id, 'default') AS user_id, COUNT(*) AS count "
            "FROM memories WHERE COALESCE(archived, 0) = 0 GROUP BY user_id ORDER BY count DESC"
        ).fetchall()
        user_counts = {str(row["user_id"]): int(row["count"]) for row in user_rows}
        rows = connection.execute(
            "SELECT id, type, content FROM memories WHERE COALESCE(archived, 0) = 0 "
            "ORDER BY importance DESC, updated_at DESC"
        ).fetchall()
        preview = [
            (str(row["id"]), str(row["type"]), _one_line(str(row["content"] or "")))
            for row in rows
        ]
        return user_counts, preview
    finally:
        connection.close()


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
    store = MemoryStore(str(snapshot_path))
    # 与检索口径对齐：只取检索真正会纳入排名的前 N 条活跃记忆，且排除 lifecycle-archived
    # （检索默认看不到它们）。否则会让人标注/校验通过却永远召回不到，指标被悄悄拉低。
    memories = store.list_memories(
        user_id=user_id,
        limit=RECALL_CANDIDATE_POOL,
        include_lifecycle_archived=False,
    )
    payloads: list[dict[str, object]] = []
    for memory in memories:
        payload = memory.model_dump(exclude={"embedding_json"})
        payloads.append(redact_memory_payload(payload, redact_sensitive=redact_sensitive))
    return payloads


def _snapshot_memory_ids(snapshot_path: Path, *, user_id: str) -> set[str]:
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}. Run recall init first.")
    return {str(memory["id"]) for memory in _snapshot_memories(snapshot_path, user_id=user_id, redact_sensitive=False)}


def _normalize_label_entry(entry: object, *, index: int) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise ValueError(f"Label line must be a JSON object: {entry!r}")
    label_id = _one_line(str(entry.get("id") or f"q{index:03d}"), limit=80)
    query = str(entry.get("query") or "").strip()
    relevant_raw = entry.get("relevant_ids", [])
    if not isinstance(relevant_raw, list):
        raise ValueError(f"Label relevant_ids must be a list: {entry!r}")
    relevant_ids = [str(memory_id).strip() for memory_id in relevant_raw if str(memory_id).strip()]
    note_raw = entry.get("note")
    label: dict[str, object] = {
        "id": label_id or f"q{index:03d}",
        "query": query,
        "relevant_ids": list(dict.fromkeys(relevant_ids)),
    }
    if note_raw is not None:
        label["note"] = str(note_raw)
    return label


def _validate_labels(labels: list[dict[str, object]], *, valid_ids: set[str]) -> list[dict[str, object]]:
    normalized = [_normalize_label_entry(label, index=index) for index, label in enumerate(labels, start=1)]
    issues = _label_validation_issues(normalized, valid_ids=valid_ids)
    blocking = [
        issue
        for issue in issues
        if issue["code"] in {"blank_query", "unknown_memory_id", "duplicate_label_id"}
    ]
    if blocking:
        raise EvaluationError("; ".join(str(issue["message"]) for issue in blocking))
    return normalized


def _label_summary(labels: list[dict[str, object]]) -> dict[str, int]:
    graded = sum(1 for label in labels if label.get("relevant_ids"))
    return {
        "queries_total": len(labels),
        "queries_graded": graded,
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
    for label in labels:
        label_id = str(label.get("id") or "")
        if not str(label.get("query") or "").strip():
            issues.append({"code": "blank_query", "label_id": label_id, "message": "Query cannot be blank."})
        if label_id in seen:
            issues.append({"code": "duplicate_label_id", "label_id": label_id, "message": f"Duplicate label id: {label_id}"})
        seen.add(label_id)
        for memory_id in label.get("relevant_ids", []):
            memory_id_text = str(memory_id)
            if memory_id_text not in valid_ids:
                issues.append(
                    {
                        "code": "unknown_memory_id",
                        "label_id": label_id,
                        "memory_id": memory_id_text,
                        "message": f"Memory id is not in the current snapshot/user scope: {memory_id_text}",
                    }
                )
    return issues


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
    parser.add_argument("--k", type=int, default=8, help="Top-k cutoff.")
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
        result = init_eval(source_db=args.database, eval_dir=eval_dir)
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
