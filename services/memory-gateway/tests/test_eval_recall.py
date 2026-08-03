from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from app.memory.evaluation import (
    EvaluationError,
    _label_validation_issues,
    _validate_labels,
    delete_user_eval_workspace,
)
from app.memory.search import EmbeddingClient, NullEmbeddingClient
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


def test_score_query_distinguishes_precision_at_k_from_returned_precision() -> None:
    row = eval_recall._score_query("q", ["a"], ["a"], k=4)

    assert row["precision"] == 0.25
    assert row["returned_precision"] == 1.0


def test_identical_duplicate_queries_are_reported_and_collapsed(
    memory_store: MemoryStore,
) -> None:
    coffee = memory_store.create_memory(user_id="default", content="用户喜欢咖啡。")
    labels = [
        {"id": "q1", "query": "咖啡", "judgment": "relevant", "relevant_ids": [coffee.id]},
        {"id": "q2", "query": " 咖啡 ", "judgment": "relevant", "relevant_ids": [coffee.id]},
    ]

    issues = _label_validation_issues(labels, valid_ids={coffee.id})
    result = eval_recall.run_eval(
        snapshot_db=memory_store.database_path,
        labels=labels,
        user_id="default",
        k=4,
        embedding_client=NullEmbeddingClient(),
    )

    assert [issue["code"] for issue in issues] == ["duplicate_query"]
    assert result["summary"]["queries_input"] == 2
    assert result["summary"]["queries_total"] == 1
    assert result["summary"]["duplicate_queries_collapsed"] == 1


def test_conflicting_duplicate_query_is_blocking() -> None:
    labels = [
        {"id": "q1", "query": "咖啡", "judgment": "relevant", "relevant_ids": ["m1"]},
        {"id": "q2", "query": "咖啡", "judgment": "no_answer", "relevant_ids": []},
    ]

    with pytest.raises(EvaluationError, match="query 相同但标注冲突"):
        _validate_labels(labels, valid_ids={"m1"})


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
    assert labels[0]["judgment"] == "relevant"
    # 旧标注文件兼容：缺少 judgment 且 relevant_ids 为空时仍是未标注，
    # 不会被悄悄算成 no-answer 样本。
    assert labels[1]["judgment"] == "unlabeled"


def test_load_labels_rejects_invalid_line(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text("not json at all\n", encoding="utf-8")

    with pytest.raises(EvaluationError, match="Invalid label JSON on line 1"):
        eval_recall.load_labels(labels_path)


def test_load_labels_rejects_non_utf8_file_as_evaluation_error(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(EvaluationError, match="not valid UTF-8"):
        eval_recall.load_labels(labels_path)


def test_recall_cli_reports_invalid_labels_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    eval_dir = tmp_path / "eval"
    initialized = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)
    Path(initialized["labels"]).write_text("not json\n", encoding="utf-8")

    exit_code = eval_recall.main(["--run", "--eval-dir", str(eval_dir)])

    assert exit_code == 1
    assert "Invalid label JSON on line 1" in capsys.readouterr().out


def test_recall_cli_rejects_k_above_search_limit_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    eval_dir = tmp_path / "eval"
    eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)

    exit_code = eval_recall.main(
        ["--run", "--eval-dir", str(eval_dir), "--k", "21"]
    )

    assert exit_code == 1
    assert "between 1 and 20" in capsys.readouterr().out


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


def test_init_eval_invalidates_results_from_previous_snapshot(tmp_path: Path) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢喝黑咖啡。")
    eval_dir = tmp_path / "eval"
    first = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)
    workspace = Path(first["snapshot"]).parent
    keyword_result = workspace / "last_keyword_result.json"
    embedding_result = workspace / "last_embedding_result.json"
    keyword_result.write_text('{"snapshot": "stale"}', encoding="utf-8")
    embedding_result.write_text('{"snapshot": "stale"}', encoding="utf-8")

    second = eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)

    assert second["snapshot"] != first["snapshot"]
    assert not keyword_result.exists()
    assert not embedding_result.exists()


def test_init_eval_physically_filters_and_separates_user_workspaces(tmp_path: Path) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    default = source.create_memory(user_id="default", content="DEFAULT_ONLY_SECRET")
    other = source.create_memory(user_id="other", content="OTHER_ONLY_SECRET")
    eval_dir = tmp_path / "eval"

    default_result = eval_recall.init_eval(
        source_db=source.database_path,
        eval_dir=eval_dir,
        user_id="default",
    )
    other_result = eval_recall.init_eval(
        source_db=source.database_path,
        eval_dir=eval_dir,
        user_id="other",
    )

    default_snapshot = Path(default_result["snapshot"])
    other_snapshot = Path(other_result["snapshot"])
    assert default_snapshot.parent != other_snapshot.parent
    assert Path(default_result["labels"]).parent == default_snapshot.parent
    assert Path(other_result["labels"]).parent == other_snapshot.parent
    assert default_result["user_counts"] == {"default": 1}
    assert other_result["user_counts"] == {"other": 1}

    with sqlite3.connect(str(default_snapshot)) as connection:
        rows = connection.execute("SELECT id, user_id, content FROM memories").fetchall()
        assert rows == [(default.id, "default", "DEFAULT_ONLY_SECRET")]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'core_memory_sections'"
        ).fetchone() == (1,)
    with sqlite3.connect(str(other_snapshot)) as connection:
        rows = connection.execute("SELECT id, user_id, content FROM memories").fetchall()
        assert rows == [(other.id, "other", "OTHER_ONLY_SECRET")]

    assert b"OTHER_ONLY_SECRET" not in default_snapshot.read_bytes()
    assert b"DEFAULT_ONLY_SECRET" not in other_snapshot.read_bytes()
    assert source.get_memory(memory_id=default.id, user_id="default") is not None
    assert source.get_memory(memory_id=other.id, user_id="other") is not None


def test_init_eval_does_not_publish_or_retain_failed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.memory.evaluation as evaluation_module

    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="DEFAULT_ONLY_SECRET")
    source.create_memory(user_id="other", content="OTHER_ONLY_SECRET")
    eval_dir = tmp_path / "eval"

    def fail_filter(connection: sqlite3.Connection, *, user_id: str) -> None:
        raise RuntimeError("injected filter failure")

    monkeypatch.setattr(evaluation_module, "_filter_snapshot_to_user", fail_filter)

    with pytest.raises(RuntimeError, match="injected filter failure"):
        eval_recall.init_eval(source_db=source.database_path, eval_dir=eval_dir)

    assert [path for path in eval_dir.rglob("*") if path.is_file()] == []


def test_snapshot_failure_preserves_published_file_and_cleans_temp_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.memory.evaluation as evaluation_module

    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    published = tmp_path / "published.db"
    published.write_bytes(b"previous snapshot")

    def fail_filter(connection: sqlite3.Connection, *, user_id: str) -> None:
        temp_database = Path(
            connection.execute("PRAGMA database_list").fetchone()[2]
        )
        Path(str(temp_database) + "-wal").write_bytes(b"temporary wal")
        Path(str(temp_database) + "-shm").write_bytes(b"temporary shm")
        raise RuntimeError("injected vacuum failure")

    monkeypatch.setattr(evaluation_module, "_filter_snapshot_to_user", fail_filter)

    with pytest.raises(RuntimeError, match="injected vacuum failure"):
        evaluation_module._snapshot_readonly(
            Path(source.database_path),
            published,
            user_id="default",
        )

    assert published.read_bytes() == b"previous snapshot"
    temp_path = published.with_name(f".{published.name}.tmp")
    assert not temp_path.exists()
    assert not Path(str(temp_path) + "-wal").exists()
    assert not Path(str(temp_path) + "-shm").exists()


def test_delete_user_eval_workspace_removes_legacy_sqlite_sidecars(
    tmp_path: Path,
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    legacy_files = [
        eval_dir / "eval_snapshot.db",
        eval_dir / "eval_snapshot.db-wal",
        eval_dir / "eval_snapshot.db-shm",
        eval_dir / "eval_snapshot_20260101000000000000.db-wal",
        eval_dir / "eval_snapshot_20260101000000000000.db-shm",
    ]
    for path in legacy_files:
        path.write_bytes(b"legacy")

    result = delete_user_eval_workspace(eval_dir, user_id="default")

    assert result["legacy_artifacts_removed"] == len(legacy_files)
    assert all(not path.exists() for path in legacy_files)


def test_initialized_workspace_can_be_deleted_immediately_on_windows(
    tmp_path: Path,
) -> None:
    source = MemoryStore(str(tmp_path / "real.db"))
    source.init_db()
    source.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    eval_dir = tmp_path / "eval"
    initialized = eval_recall.init_eval(
        source_db=source.database_path,
        eval_dir=eval_dir,
        user_id="default",
    )
    snapshot = Path(initialized["snapshot"])
    eval_recall.run_eval(
        snapshot_db=snapshot,
        labels=[
            {
                "query": "咖啡",
                "judgment": "no_answer",
                "relevant_ids": [],
            }
        ],
        user_id="default",
        embedding_client=NullEmbeddingClient(),
    )
    assert not Path(str(snapshot) + "-wal").exists()
    assert not Path(str(snapshot) + "-shm").exists()

    result = delete_user_eval_workspace(eval_dir, user_id="default")

    assert result["workspace_removed"] is True
    assert not snapshot.exists()
    assert not snapshot.parent.exists()


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
        {"query": "咖啡", "judgment": "no_answer", "relevant_ids": []},
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
    assert summary["queries_graded"] == 3
    assert summary["queries_relevant"] == 2
    assert summary["queries_no_answer"] == 1
    assert summary["effective_k"] == 8
    assert summary["hit_rate"] == 1.0
    assert summary["no_answer_false_positive_rate"] == 1.0
    assert summary["no_answer_abstention_rate"] == 0.0

    relevant_coffee = next(row for row in result["per_query"] if row["judgment"] == "relevant" and row["query"] == "咖啡")
    no_answer = next(row for row in result["per_query"] if row["judgment"] == "no_answer")
    assert relevant_coffee["predicted_ids"][0] == coffee.id
    assert relevant_coffee["hit"] == 1.0
    assert no_answer["false_positive"] is True
    assert no_answer["retrieved"] > 0


def test_run_eval_rejects_k_above_search_limit(memory_store: MemoryStore) -> None:
    with pytest.raises(EvaluationError, match="between 1 and 20"):
        eval_recall.run_eval(
            snapshot_db=memory_store.database_path,
            labels=[],
            user_id="default",
            k=21,
            embedding_client=NullEmbeddingClient(),
        )


class _UnavailableEmbeddingClient(EmbeddingClient):
    async def embed(self, text: str) -> list[float] | None:
        return None


def test_run_eval_reports_actual_embedding_fallback(memory_store: MemoryStore) -> None:
    coffee = memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")

    result = eval_recall.run_eval(
        snapshot_db=memory_store.database_path,
        labels=[{"query": "咖啡", "relevant_ids": [coffee.id]}],
        user_id="default",
        embedding_client=_UnavailableEmbeddingClient(),
        requested_mode="embedding",
    )

    row = result["per_query"][0]
    assert row["requested_mode"] == "embedding"
    assert row["retrieval_mode"] == "keyword_fallback"
    assert row["fallback_reason"] == "embedding_unavailable"
    assert row["embedding_available"] is False
    assert result["summary"]["retrieval_mode_counts"] == {"keyword_fallback": 1}
    assert result["summary"]["fallback_queries"] == 1


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
