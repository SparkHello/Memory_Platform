from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from app.memory.store import MemoryStore


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_memory_health.py"
SPEC = importlib.util.spec_from_file_location("diagnose_memory_health", SCRIPT_PATH)
assert SPEC is not None
diagnose = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = diagnose
SPEC.loader.exec_module(diagnose)


def _store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    return store


def _verdict(result: dict, mechanism: str) -> dict:
    return next(item for item in result["verdicts"] if item["mechanism"] == mechanism)


def _seed(store: MemoryStore, count: int, *, type: str = "semantic") -> list[str]:
    ids = []
    for index in range(count):
        memory = store.create_memory(
            user_id="default",
            content=f"User fact number {index}.",
            type=type,
            importance=7,
            confidence=0.9,
        )
        ids.append(memory.id)
    return ids


def test_single_type_dynamic_library_is_degenerate_and_dormant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, 12, type="semantic")

    result = diagnose.run_diagnosis(store.database_path)

    assert result["memory_count"] == 12
    assert _verdict(result, "sector_typing")["state"] == "degenerate"
    assert _verdict(result, "lifecycle_status")["state"] == "dormant"
    assert _verdict(result, "temporal_kg")["state"] == "dormant"
    assert _verdict(result, "graph_structure")["state"] == "sparse"


def test_small_library_is_insufficient_data_for_distribution_verdicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, 5, type="semantic")

    result = diagnose.run_diagnosis(store.database_path)

    # 分布类判定需要足够样本，小库给出 insufficient_data 而不是误判 degenerate。
    assert _verdict(result, "sector_typing")["state"] == "insufficient_data"
    assert _verdict(result, "graph_structure")["state"] == "insufficient_data"
    # 但"零触发"类机制无论样本量都能判定。
    assert _verdict(result, "temporal_kg")["state"] == "dormant"
    assert _verdict(result, "lifecycle_status")["state"] == "dormant"


def test_diverse_library_marks_mechanisms_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = _seed(store, 6, type="semantic")
    ids += _seed(store, 3, type="episodic")
    ids += _seed(store, 3, type="emotional")

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE memories SET status = 'resolved' WHERE id = ?", (ids[0],))
        connection.execute("UPDATE memories SET status = 'pinned' WHERE id = ?", (ids[1],))
        connection.execute(
            "UPDATE memories SET temporal_subject = 'user', temporal_predicate = 'city' WHERE id = ?",
            (ids[2],),
        )
        for memory_id in ids[:4]:
            connection.execute(
                "UPDATE memories SET topics_json = '[\"life\"]' WHERE id = ?", (memory_id,)
            )

    result = diagnose.run_diagnosis(store.database_path)

    assert _verdict(result, "sector_typing")["state"] == "active"
    assert _verdict(result, "lifecycle_status")["state"] == "active"
    assert _verdict(result, "temporal_kg")["state"] == "active"
    assert _verdict(result, "graph_structure")["state"] == "active"


def test_unique_tags_without_relationships_do_not_fake_an_active_graph(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ids = _seed(store, 12)
    with sqlite3.connect(store.database_path) as connection:
        for index, memory_id in enumerate(ids):
            connection.execute(
                "UPDATE memories SET topics_json = ? WHERE id = ?",
                (f'["unique-{index}"]', memory_id),
            )

    result = diagnose.run_diagnosis(store.database_path)

    graph = result["metrics"]["graph"]
    assert graph["topic_coverage"] == 1.0
    assert graph["edge_count"] == 0
    assert _verdict(result, "graph_structure")["state"] == "sparse"


def test_temporal_health_requires_active_reciprocal_links(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.create_memory(
        user_id="default",
        content="User lived in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="city",
    )
    current = store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="city",
    )

    healthy = diagnose.run_diagnosis(store.database_path)
    temporal = healthy["metrics"]["temporal"]
    assert temporal["active_supersession_edge_count"] == 1
    assert temporal["dangling_supersession_reference_count"] == 0
    assert _verdict(healthy, "temporal_kg")["state"] == "active"

    # Simulate a legacy/corrupt soft delete that did not detach the version
    # chain. Historical columns alone must not be reported as healthy.
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE memories SET archived = 1 WHERE id = ?",
            (current.id,),
        )

    unhealthy = diagnose.run_diagnosis(store.database_path)
    temporal = unhealthy["metrics"]["temporal"]
    assert temporal["active_supersession_edge_count"] == 0
    assert temporal["trashed_supersession_link_count"] == 1
    assert temporal["dangling_supersession_reference_count"] == 1
    assert _verdict(unhealthy, "temporal_kg")["state"] == "degenerate"
    assert old.id != current.id


def test_never_recalled_count_tracks_zero_usage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = _seed(store, 3, type="semantic")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE memories SET usage_count = 4 WHERE id = ?", (ids[0],))

    result = diagnose.run_diagnosis(store.database_path)

    assert result["metrics"]["never_recalled_count"] == 2


def test_missing_database_reports_error(tmp_path: Path) -> None:
    result = diagnose.run_diagnosis(tmp_path / "nope.db")
    assert "error" in result
    assert diagnose.main(["--database", str(tmp_path / "nope.db")]) == 1


def test_emotion_affect_degenerate_when_affect_is_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # create_memory 默认 valence=0.5 / arousal=0.3，且无 emotional 扇区。
    _seed(store, 12, type="semantic")

    result = diagnose.run_diagnosis(store.database_path)

    affect = result["metrics"]["affect"]
    assert affect["available"] is True
    assert affect["emotional_sector_count"] == 0
    assert affect["default_affect_share"] == 1.0
    assert _verdict(result, "emotion_affect")["state"] == "degenerate"


def test_emotion_affect_active_when_affect_varies(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = _seed(store, 12, type="semantic")
    varied = [(0.1, 0.9), (0.8, 0.7), (0.2, 0.6), (0.9, 0.8), (0.3, 0.5)]
    with sqlite3.connect(store.database_path) as connection:
        for memory_id, (valence, arousal) in zip(ids, varied):
            connection.execute(
                "UPDATE memories SET valence = ?, arousal = ?, type = 'emotional' WHERE id = ?",
                (valence, arousal, memory_id),
            )

    result = diagnose.run_diagnosis(store.database_path)

    affect = result["metrics"]["affect"]
    assert affect["emotional_sector_count"] == 5
    assert affect["distinct_valence"] >= 5
    assert affect["default_affect_share"] < 0.7
    assert _verdict(result, "emotion_affect")["state"] == "active"


def test_recall_health_dormant_when_no_recalls(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, 12, type="semantic")

    result = diagnose.run_diagnosis(store.database_path)

    recall = result["metrics"]["recall"]
    assert recall["recalled_count"] == 0
    assert recall["total_recalls"] == 0
    assert _verdict(result, "recall_health")["state"] == "dormant"


def test_recall_health_active_with_spread_usage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = _seed(store, 12, type="semantic")
    with sqlite3.connect(store.database_path) as connection:
        for memory_id in ids[:6]:
            connection.execute("UPDATE memories SET usage_count = 3 WHERE id = ?", (memory_id,))

    result = diagnose.run_diagnosis(store.database_path)

    recall = result["metrics"]["recall"]
    assert recall["recalled_count"] == 6
    assert recall["total_recalls"] == 18
    assert recall["top1_concentration"] < 0.5
    assert _verdict(result, "recall_health")["state"] == "active"


def test_fractional_usage_count_is_preserved_as_activation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = _seed(store, 12, type="semantic")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE memories SET usage_count = 0.25 WHERE id = ?", (ids[0],))

    result = diagnose.run_diagnosis(store.database_path)

    recall = result["metrics"]["recall"]
    assert recall["activated_memory_count"] == 1
    assert recall["total_activation_count"] == 0.25
    assert recall["total_recalls"] == 0.25
    assert _verdict(result, "recall_health")["state"] == "active"


def test_space_coverage_ignores_links_to_deleted_memories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    space = store.upsert_memory_space(user_id="default", name="Work")
    active = store.create_memory(
        user_id="default",
        content="Active linked fact.",
        space_ids=[space.id],
    )
    deleted = store.create_memory(
        user_id="default",
        content="Deleted linked fact.",
        space_ids=[space.id],
    )
    assert store.archive_memory(memory_id=deleted.id, user_id="default")

    result = diagnose.run_diagnosis(store.database_path)

    coverage = result["metrics"]["tag_coverage"]
    assert result["memory_count"] == 1
    assert coverage["space_linked"] == 1
    assert coverage["space_coverage"] == 1.0
    assert active.id != deleted.id


def test_temporal_health_counts_keyless_auto_supersede_edges_as_valid(tmp_path: Path) -> None:
    from app.memory.models import AutoSupersedeDecision

    store = _store(tmp_path)
    old = store.create_memory(user_id="default", content="用户平时用 iPhone 手机。")
    store.create_memory(
        user_id="default",
        content="用户现在改用安卓手机。",
        supersede_matcher=lambda latest: AutoSupersedeDecision(
            target=old, relation="supersede", reason="test"
        ),
    )

    report = diagnose.run_diagnosis(store.database_path)
    temporal = report["metrics"]["temporal"]

    assert temporal["temporal_key_count"] == 0
    assert temporal["active_supersession_edge_count"] == 1
    assert temporal["keyless_supersession_edge_count"] == 1
    assert temporal["dangling_supersession_reference_count"] == 0
    assert _verdict(report, "temporal_kg")["state"] == "active"
