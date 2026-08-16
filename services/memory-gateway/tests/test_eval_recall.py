from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

import app.memory.evaluation_workspace as evaluation_workspace
from app.memory.evaluation import (
    EvaluationError,
    _label_validation_issues,
    _validate_labels,
    delete_user_eval_workspace,
)
from app.memory.evaluation_workspace import (
    TRASH_ROOT_MARKER_NAME,
    cleanup_abandoned_eval_trash,
    evaluation_workspace_lock,
    mark_staged_eval_workspace_committed,
    restore_staged_eval_workspace,
    stage_user_eval_workspace,
    user_eval_dir,
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


def _assert_owned_trash_is_empty(eval_dir: Path) -> None:
    trash_root = eval_dir / ".trash"
    assert trash_root.is_dir()
    assert {path.name for path in trash_root.iterdir()} == {
        TRASH_ROOT_MARKER_NAME
    }


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

    files = [path for path in eval_dir.rglob("*") if path.is_file()]
    assert {path.name for path in files} == {
        ".workspace.lock",
        TRASH_ROOT_MARKER_NAME,
    }


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


def test_eval_workspace_stage_is_reversible_before_database_commit(
    tmp_path: Path,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    snapshot = workspace / "eval_snapshot_20260101000000000000.db"
    snapshot.write_bytes(b"alice-evaluation-copy")
    legacy = eval_dir / "labels.jsonl"
    legacy.write_text("legacy", encoding="utf-8")

    staged = stage_user_eval_workspace(eval_dir, user_id="alice")

    assert not workspace.exists()
    assert not legacy.exists()
    assert staged.trash_dir is not None and staged.trash_dir.exists()
    assert all(
        staged_path.parent == staged.trash_dir
        for _, staged_path in staged.moved
    )

    restore_staged_eval_workspace(staged)

    assert snapshot.read_bytes() == b"alice-evaluation-copy"
    assert legacy.read_text(encoding="utf-8") == "legacy"
    _assert_owned_trash_is_empty(eval_dir)


def test_regular_stage_failure_restores_every_completed_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    snapshot = workspace / "snapshot.db"
    snapshot.write_bytes(b"workspace copy")
    legacy = eval_dir / "labels.jsonl"
    legacy.write_text("legacy labels", encoding="utf-8")
    original_replace = Path.replace
    moves = 0

    def fail_second_move(path: Path, target: Path) -> Path:
        nonlocal moves
        if path in {workspace, legacy}:
            moves += 1
            if moves == 2:
                raise OSError("simulated regular stage failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_move)
    with pytest.raises(OSError, match="regular stage failure"):
        stage_user_eval_workspace(eval_dir, user_id="alice")

    assert snapshot.read_bytes() == b"workspace copy"
    assert legacy.read_text(encoding="utf-8") == "legacy labels"
    _assert_owned_trash_is_empty(eval_dir)


def test_discard_reports_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("labels", encoding="utf-8")
    staged = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        committed_intent=True,
    )

    def fail_cleanup(_path: Path) -> None:
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(
        evaluation_workspace,
        "_remove_managed_transaction",
        fail_cleanup,
    )

    result = evaluation_workspace.discard_staged_eval_workspace(staged)

    assert result["cleanup_failed"] is True


@pytest.mark.parametrize(
    "corruption",
    (
        "identity",
        "fields",
        "mapping",
    ),
)
def test_cleanup_rejects_each_independently_corrupt_manifest_field(
    tmp_path: Path,
    corruption: str,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("labels", encoding="utf-8")
    staged = stage_user_eval_workspace(eval_dir, user_id="alice")
    assert staged.trash_dir is not None
    manifest_path = staged.trash_dir / evaluation_workspace.TRASH_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if corruption == "identity":
        manifest["schema_version"] = 999
    elif corruption == "fields":
        manifest["user_id"] = []
    else:
        manifest["mappings"][0]["original"] = "../escape"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(OSError, match="unowned or invalid"):
        cleanup_abandoned_eval_trash(eval_dir)


def test_transaction_directory_validator_rejects_one_bad_attribute(
    tmp_path: Path,
) -> None:
    transaction_dir = tmp_path / "not-a-transaction-id"
    transaction_dir.mkdir()

    with pytest.raises(OSError, match="directory is unsafe"):
        evaluation_workspace._validate_transaction_directory_name(transaction_dir)


def test_tombstone_cleanup_preserves_invalid_empty_directory(tmp_path: Path) -> None:
    transaction_dir = tmp_path / "not-a-transaction-id"
    transaction_dir.mkdir()

    assert (
        evaluation_workspace._remove_empty_transaction_tombstone(transaction_dir)
        is False
    )
    assert transaction_dir.is_dir()


def test_transaction_target_probe_requires_a_database_path() -> None:
    assert (
        evaluation_workspace._transaction_targets_exist(
            None,
            user_id="alice",
            target_memory_ids=["memory-id"],
        )
        is None
    )


def test_startup_cleanup_removes_abandoned_eval_trash(tmp_path: Path) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("sensitive copy", encoding="utf-8")
    staged = stage_user_eval_workspace(eval_dir, user_id="alice")
    assert staged.trash_dir is not None and staged.trash_dir.exists()
    mark_staged_eval_workspace_committed(staged)

    assert cleanup_abandoned_eval_trash(eval_dir) == 1
    _assert_owned_trash_is_empty(eval_dir)
    assert not workspace.exists()


def test_unconditional_delete_intent_is_recoverable_without_database(
    tmp_path: Path,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("manual labels", encoding="utf-8")
    staged = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        committed_intent=True,
    )
    assert staged.trash_dir is not None and staged.trash_dir.exists()

    assert cleanup_abandoned_eval_trash(eval_dir) == 1
    assert not workspace.exists()
    _assert_owned_trash_is_empty(eval_dir)


def test_unconditional_delete_recovers_after_partial_stage_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("manual labels", encoding="utf-8")
    legacy = eval_dir / "labels.jsonl"
    legacy.write_text("legacy labels", encoding="utf-8")
    original_replace = Path.replace
    moves = 0

    def interrupt_second_move(path: Path, target: Path) -> Path:
        nonlocal moves
        if path in {workspace, legacy}:
            moves += 1
            if moves == 2:
                raise KeyboardInterrupt("simulated partial committed stage")
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", interrupt_second_move)
        with pytest.raises(KeyboardInterrupt, match="partial committed stage"):
            stage_user_eval_workspace(
                eval_dir,
                user_id="alice",
                committed_intent=True,
            )

    assert not workspace.exists()
    assert legacy.exists()
    assert cleanup_abandoned_eval_trash(eval_dir) == 1
    assert not workspace.exists()
    assert not legacy.exists()
    _assert_owned_trash_is_empty(eval_dir)


def test_cleanup_retries_empty_transaction_after_final_rmdir_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("sensitive copy", encoding="utf-8")
    staged = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        committed_intent=True,
    )
    assert staged.trash_dir is not None
    transaction_dir = staged.trash_dir
    original_rmdir = Path.rmdir
    failed_once = False

    def fail_transaction_rmdir_once(path: Path) -> None:
        nonlocal failed_once
        if path == transaction_dir and not failed_once:
            failed_once = True
            raise OSError("simulated transient directory handle")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_transaction_rmdir_once)
    with pytest.raises(OSError, match="transient directory handle"):
        cleanup_abandoned_eval_trash(eval_dir)

    assert transaction_dir.is_dir()
    assert not list(transaction_dir.iterdir())
    assert cleanup_abandoned_eval_trash(eval_dir) == 1
    assert not transaction_dir.exists()
    _assert_owned_trash_is_empty(eval_dir)


def test_startup_cleanup_restores_staged_labels_when_database_rolled_back(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    memory = store.create_memory(user_id="alice", content="keep database row")
    assert store.archive_memory(memory_id=memory.id, user_id="alice")
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    labels = workspace / "labels.jsonl"
    labels.write_text("manual labels", encoding="utf-8")
    staged = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        target_memory_ids=[memory.id],
    )

    assert cleanup_abandoned_eval_trash(
        eval_dir,
        database_path=store.database_path,
    ) == 1
    assert labels.read_text(encoding="utf-8") == "manual labels"
    assert staged.trash_dir is not None and not staged.trash_dir.exists()


def test_startup_cleanup_discards_staged_copy_after_database_commit(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    memory = store.create_memory(user_id="alice", content="purged database row")
    assert store.archive_memory(memory_id=memory.id, user_id="alice")
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("manual labels", encoding="utf-8")
    stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        target_memory_ids=[memory.id],
    )
    assert store.purge_archived_memory(memory_id=memory.id, user_id="alice")

    assert cleanup_abandoned_eval_trash(
        eval_dir,
        database_path=store.database_path,
    ) == 1
    assert not workspace.exists()


def test_new_purge_resolves_prior_committed_cleanup_failure_first(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    first = store.create_memory(user_id="alice", content="first purge target")
    second = store.create_memory(user_id="alice", content="second purge target")
    assert store.archive_memory(memory_id=first.id, user_id="alice")
    assert store.archive_memory(memory_id=second.id, user_id="alice")
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "snapshot.db").write_text(
        "first purge target; second purge target",
        encoding="utf-8",
    )
    staged_first = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        target_memory_ids=[first.id],
        database_path=store.database_path,
    )
    assert store.purge_archived_memory(memory_id=first.id, user_id="alice")
    mark_staged_eval_workspace_committed(staged_first)
    assert staged_first.trash_dir is not None and staged_first.trash_dir.exists()

    staged_second = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        target_memory_ids=[second.id],
        database_path=store.database_path,
    )

    assert staged_second.trash_dir is None
    assert not staged_first.trash_dir.exists()
    _assert_owned_trash_is_empty(eval_dir)


def test_new_purge_fails_closed_on_invalid_prior_transaction(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    first = store.create_memory(user_id="alice", content="first target")
    second = store.create_memory(user_id="alice", content="second target")
    assert store.archive_memory(memory_id=first.id, user_id="alice")
    assert store.archive_memory(memory_id=second.id, user_id="alice")
    eval_dir = tmp_path / "eval"
    workspace = user_eval_dir(eval_dir, user_id="alice")
    workspace.mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("manual labels", encoding="utf-8")
    staged = stage_user_eval_workspace(
        eval_dir,
        user_id="alice",
        target_memory_ids=[first.id],
        database_path=store.database_path,
    )
    assert staged.trash_dir is not None
    (staged.trash_dir / "foreign-entry").write_text("unknown", encoding="utf-8")

    with pytest.raises(OSError, match="unowned or invalid"):
        stage_user_eval_workspace(
            eval_dir,
            user_id="alice",
            target_memory_ids=[second.id],
            database_path=store.database_path,
        )

    assert second.id in {
        item.id for item in store.list_archived_memories(user_id="alice")
    }
    assert (staged.trash_dir / "foreign-entry").exists()


def test_cleanup_preserves_unmanaged_dot_trash(tmp_path: Path) -> None:
    eval_dir = tmp_path / "configured-home"
    foreign_trash = eval_dir / ".trash"
    foreign_trash.mkdir(parents=True)
    foreign = foreign_trash / "unrelated-user-file"
    foreign.write_text("do not delete", encoding="utf-8")

    with pytest.raises(OSError, match="not owned"):
        cleanup_abandoned_eval_trash(eval_dir)
    with pytest.raises(OSError, match="not owned"):
        stage_user_eval_workspace(eval_dir, user_id="alice")

    assert foreign.read_text(encoding="utf-8") == "do not delete"


def test_unfiltered_snapshot_build_is_managed_and_recovered_after_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.memory.evaluation as evaluation_module

    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    store.create_memory(user_id="alice", content="ALICE_SECRET")
    store.create_memory(user_id="bob", content="BOB_SECRET")
    eval_dir = tmp_path / "eval"

    def hard_stop(connection: sqlite3.Connection, *, user_id: str) -> None:
        del connection, user_id
        raise KeyboardInterrupt("simulated hard stop")

    monkeypatch.setattr(evaluation_module, "_filter_snapshot_to_user", hard_stop)
    with pytest.raises(KeyboardInterrupt, match="simulated hard stop"):
        eval_recall.init_eval(
            source_db=store.database_path,
            eval_dir=eval_dir,
            user_id="alice",
        )

    assert not list((eval_dir / "users").glob("**/*.db.tmp"))
    assert (eval_dir / ".trash").exists()
    assert cleanup_abandoned_eval_trash(
        eval_dir,
        database_path=store.database_path,
    ) == 1
    _assert_owned_trash_is_empty(eval_dir)


def test_global_workspace_lock_blocks_concurrent_snapshot_publish(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    store.create_memory(user_id="alice", content="serialized snapshot")
    eval_dir = tmp_path / "eval"
    started = Event()
    finished = Event()

    def initialize() -> None:
        started.set()
        eval_recall.init_eval(
            source_db=store.database_path,
            eval_dir=eval_dir,
            user_id="alice",
        )
        finished.set()

    with evaluation_workspace_lock(eval_dir):
        worker = Thread(target=initialize)
        worker.start()
        assert started.wait(timeout=2)
        assert not finished.wait(timeout=0.2)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert finished.is_set()


def test_workspace_ancestor_link_cannot_escape_eval_dir(tmp_path: Path) -> None:
    eval_dir = tmp_path / "eval"
    external = tmp_path / "external-users"
    eval_dir.mkdir()
    external.mkdir()
    users_link = eval_dir / "users"
    try:
        users_link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlink unavailable: {exc}")
        completed = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(users_link.resolve()),
                str(external.resolve()),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"directory junction unavailable: {completed.stderr}")
    external_workspace = user_eval_dir(eval_dir, user_id="alice").resolve()
    external_workspace.mkdir(parents=True)
    sensitive = external_workspace / "labels.jsonl"
    sensitive.write_text("external labels", encoding="utf-8")

    with pytest.raises(OSError, match="root must not be a link or junction"):
        with evaluation_workspace_lock(eval_dir):
            pass
    with pytest.raises(OSError, match="escapes EVAL_DIR"):
        stage_user_eval_workspace(eval_dir, user_id="alice")

    assert sensitive.read_text(encoding="utf-8") == "external labels"


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
