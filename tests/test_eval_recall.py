from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from app.memory.search import NullEmbeddingClient
from app.memory.store import MemoryStore


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_recall.py"
SPEC = importlib.util.spec_from_file_location("eval_recall", SCRIPT_PATH)
assert SPEC is not None
eval_recall = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = eval_recall
SPEC.loader.exec_module(eval_recall)


def test_score_query_computes_ranking_metrics() -> None:
    row = eval_recall._score_query("q", ["a", "b"], ["x", "a", "y", "b"], k=4)

    assert row["hit"] == 1.0
    assert row["precision"] == 0.5      # 2 relevant in top-4
    assert row["recall"] == 1.0         # both relevant retrieved
    assert row["reciprocal_rank"] == 0.5  # first hit at rank 2
    assert row["ndcg"] == 0.6509


def test_score_query_handles_complete_miss() -> None:
    row = eval_recall._score_query("q", ["a"], ["x", "y"], k=4)

    assert row["hit"] == 0.0
    assert row["precision"] == 0.0
    assert row["recall"] == 0.0
    assert row["reciprocal_rank"] == 0.0
    assert row["ndcg"] == 0.0


def test_load_labels_skips_comments_and_blanks(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(
        "# comment line\n"
        "\n"
        '{"query": "饮食偏好", "relevant_ids": ["m1"]}\n'
        '{"query": "宠物", "relevant_ids": []}\n',
        encoding="utf-8",
    )

    labels = eval_recall.load_labels(labels_path)

    assert [entry["query"] for entry in labels] == ["饮食偏好", "宠物"]
    assert labels[0]["relevant_ids"] == ["m1"]


def test_load_labels_rejects_invalid_line(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text("not json at all\n", encoding="utf-8")

    with pytest.raises(ValueError):
        eval_recall.load_labels(labels_path)


def test_init_eval_snapshots_without_touching_source(tmp_path: Path) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢喝黑咖啡。", type="emotional", importance=5)
    source.create_memory(user_id="default", content="用户养了一只橘猫。", type="semantic", importance=5)

    eval_dir = tmp_path / "eval"
    result = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)

    assert Path(result["snapshot"]).exists()
    assert Path(result["preview"]).exists()
    assert Path(result["labels"]).exists()
    assert result["labels_created"] is True
    assert result["memory_count"] == 2
    assert result["user_counts"] == {"default": 2}

    # 二次 init 不覆盖已有标注，避免冲掉用户填好的 relevant_ids。
    again = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)
    assert again["labels_created"] is False


def test_init_eval_refreshes_when_previous_snapshot_is_open(tmp_path: Path) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢喝黑咖啡。", type="emotional", importance=5)

    eval_dir = tmp_path / "eval"
    first = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)
    connection = sqlite3.connect(str(first["snapshot"]))
    try:
        source.create_memory(user_id="default", content="用户喜欢写 TypeScript。", type="semantic", importance=6)
        second = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)
    finally:
        connection.close()

    assert Path(second["snapshot"]).exists()
    assert second["snapshot"] != first["snapshot"]
    assert second["memory_count"] == 2


def test_run_eval_reports_recall_metrics(memory_store: MemoryStore) -> None:
    coffee = memory_store.create_memory(
        user_id="default", content="用户喜欢喝黑咖啡。", type="emotional", importance=5
    )
    cat = memory_store.create_memory(
        user_id="default", content="用户养了一只橘猫。", type="semantic", importance=5
    )
    labels = [
        {"query": "咖啡", "relevant_ids": [coffee.id]},
        {"query": "橘猫", "relevant_ids": [cat.id]},
        {"query": "完全无关的主题", "relevant_ids": []},
    ]

    result = eval_recall.run_eval(
        snapshot_db=memory_store.database_path,
        labels=labels,
        user_id="default",
        k=8,
        embedding_client=NullEmbeddingClient(),
    )

    summary = result["summary"]
    assert summary["queries_total"] == 3
    assert summary["queries_graded"] == 2
    assert summary["hit_rate"] == 1.0

    by_query = {row["query"]: row for row in result["per_query"]}
    assert by_query["咖啡"]["predicted_ids"][0] == coffee.id
    assert by_query["咖啡"]["hit"] == 1.0
    assert by_query["完全无关的主题"]["relevant_count"] == 0


def test_run_eval_does_not_record_usage(memory_store: MemoryStore) -> None:
    coffee = memory_store.create_memory(
        user_id="default", content="用户喜欢喝黑咖啡。", type="emotional", importance=5
    )
    labels = [{"query": "咖啡", "relevant_ids": [coffee.id]}]

    eval_recall.run_eval(
        snapshot_db=memory_store.database_path,
        labels=labels,
        user_id="default",
        embedding_client=NullEmbeddingClient(),
    )

    # 评测必须 record_usage=False：真实库/快照的 usage_count 不能被评测污染。
    refreshed = memory_store.get_memory(memory_id=coffee.id, user_id="default")
    assert refreshed is not None
    assert refreshed.usage_count == 0
