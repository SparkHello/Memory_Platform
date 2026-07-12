from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from app.memory.store import MemoryStore


def test_evaluation_diagnosis_requires_auth_and_respects_user_scope(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    for index in range(12):
        memory_store.create_memory(
            user_id="default",
            content=f"Default semantic fact {index}.",
            type="semantic",
        )
    memory_store.create_memory(user_id="other", content="Other user fact.", type="emotional")

    unauthorized = client.get("/memories/evaluation/diagnosis")
    scoped = client.get("/memories/evaluation/diagnosis", headers=auth_headers)
    other = client.get(
        "/memories/evaluation/diagnosis",
        headers={**auth_headers, "X-User-Id": "other"},
    )

    assert unauthorized.status_code == 401
    assert scoped.status_code == 200
    assert scoped.json()["memory_count"] == 12
    assert scoped.json()["metrics"]["type_distribution"] == {"semantic": 12}
    assert other.status_code == 200
    assert other.json()["memory_count"] == 1
    assert other.json()["metrics"]["type_distribution"] == {"emotional": 1}


def test_recall_workbench_candidates_match_default_search_eligibility(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    public = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        sensitivity="normal",
    )
    private = memory_store.create_memory(
        user_id="default",
        content="用户的私人计划。",
        sensitivity="private",
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="模型形成的派生反思。",
        type="reflective",
        origin="agent_derived",
        evidence_memory_ids=[public.id],
    )
    legacy_mislabeled = memory_store.create_memory(
        user_id="default",
        content="用户的银行卡号是 6222021234567890。",
        sensitivity="normal",
    )
    other = memory_store.create_memory(
        user_id="other",
        content="Other user memory.",
        sensitivity="normal",
    )

    init_response = client.post("/memories/evaluation/recall/init", headers=auth_headers)
    workbench_response = client.get(
        "/memories/evaluation/recall/workbench",
        headers=auth_headers,
    )

    assert init_response.status_code == 200
    assert init_response.json()["labels_created"] is True
    assert init_response.json()["memory_count"] == 1
    assert workbench_response.status_code == 200
    payload = workbench_response.json()
    candidate_ids = {candidate["id"] for candidate in payload["candidates"]}
    assert candidate_ids == {public.id}
    assert candidate_ids.isdisjoint(
        {private.id, derived.id, legacy_mislabeled.id, other.id}
    )


def test_recall_artifacts_are_isolated_per_user(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    default = memory_store.create_memory(user_id="default", content="DEFAULT_USER_ONLY")
    other = memory_store.create_memory(user_id="other", content="OTHER_USER_ONLY")
    other_headers = {**auth_headers, "X-User-Id": "other"}

    default_init = client.post("/memories/evaluation/recall/init", headers=auth_headers)
    other_init = client.post("/memories/evaluation/recall/init", headers=other_headers)

    assert default_init.status_code == 200
    assert other_init.status_code == 200
    default_payload = default_init.json()
    other_payload = other_init.json()
    assert default_payload["user_id"] == "default"
    assert other_payload["user_id"] == "other"
    assert default_payload["snapshot"] != other_payload["snapshot"]
    assert default_payload["labels"] != other_payload["labels"]
    assert default_payload["user_counts"] == {"default": 1}
    assert other_payload["user_counts"] == {"other": 1}

    with sqlite3.connect(str(Path(default_payload["snapshot"]))) as connection:
        assert connection.execute("SELECT id, user_id FROM memories").fetchall() == [
            (default.id, "default")
        ]
    with sqlite3.connect(str(Path(other_payload["snapshot"]))) as connection:
        assert connection.execute("SELECT id, user_id FROM memories").fetchall() == [
            (other.id, "other")
        ]

    default_labels = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={
            "labels": [
                {
                    "id": "default-q",
                    "query": "default",
                    "judgment": "relevant",
                    "relevant_ids": [default.id],
                }
            ]
        },
    )
    other_labels = client.put(
        "/memories/evaluation/recall/labels",
        headers=other_headers,
        json={
            "labels": [
                {
                    "id": "other-q",
                    "query": "other",
                    "judgment": "relevant",
                    "relevant_ids": [other.id],
                }
            ]
        },
    )
    assert default_labels.status_code == 200
    assert other_labels.status_code == 200
    assert client.get(
        "/memories/evaluation/recall/workbench", headers=auth_headers
    ).json()["labels"][0]["id"] == "default-q"
    assert client.get(
        "/memories/evaluation/recall/workbench", headers=other_headers
    ).json()["labels"][0]["id"] == "other-q"

    assert client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 8},
    ).status_code == 200
    assert client.post(
        "/memories/evaluation/recall/run",
        headers=other_headers,
        json={"mode": "keyword", "k": 8},
    ).status_code == 200
    default_result_path = Path(default_payload["snapshot"]).parent / "last_keyword_result.json"
    other_result_path = Path(other_payload["snapshot"]).parent / "last_keyword_result.json"
    assert default_result_path != other_result_path
    assert json.loads(default_result_path.read_text(encoding="utf-8"))["user_id"] == "default"
    assert json.loads(other_result_path.read_text(encoding="utf-8"))["user_id"] == "other"


def test_recall_labels_reject_cross_user_memory(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    default = memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    other = memory_store.create_memory(user_id="other", content="Other user memory.")

    assert client.post("/memories/evaluation/recall/init", headers=auth_headers).status_code == 200

    invalid = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={
            "labels": [
                {
                    "id": "q001",
                    "query": "咖啡偏好",
                    "relevant_ids": [default.id, other.id],
                }
            ]
        },
    )
    blank = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={"labels": [{"id": "q001", "query": "   ", "relevant_ids": [default.id]}]},
    )

    assert invalid.status_code == 422
    assert other.id in invalid.json()["detail"]
    assert blank.status_code == 422


def test_recall_labels_empty_query_returns_friendly_message(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    assert client.post("/memories/evaluation/recall/init", headers=auth_headers).status_code == 200

    # 空字符串 query 应走领域校验（带 label id 的友好提示），而非原始 Pydantic 422。
    empty = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={"labels": [{"id": "q001", "query": "", "relevant_ids": []}]},
    )

    assert empty.status_code == 422
    detail = empty.json()["detail"]
    assert isinstance(detail, str)
    assert "blank" in detail.lower()


def test_recall_workbench_candidates_match_retriever_pool(
    client,
    auth_headers,
    memory_store: MemoryStore,
    monkeypatch,
):
    import app.memory.evaluation as evaluation_module

    # 缩小检索池便于断言：候选/校验都应只覆盖按重要度排序的前 N 条。
    monkeypatch.setattr(evaluation_module, "RECALL_CANDIDATE_POOL", 2)

    high = memory_store.create_memory(user_id="default", content="高重要度记忆", importance=9)
    mid = memory_store.create_memory(user_id="default", content="中重要度记忆", importance=8)
    low = memory_store.create_memory(user_id="default", content="低重要度记忆", importance=7)

    assert client.post("/memories/evaluation/recall/init", headers=auth_headers).status_code == 200
    workbench = client.get("/memories/evaluation/recall/workbench", headers=auth_headers)

    assert workbench.status_code == 200
    candidate_ids = [candidate["id"] for candidate in workbench.json()["candidates"]]
    assert len(candidate_ids) == 2
    assert high.id in candidate_ids
    assert mid.id in candidate_ids
    # 池外记忆检索永远够不到，不应作为候选出现
    assert low.id not in candidate_ids

    # 也不能把池外记忆标成相关：校验按同一口径拒绝
    rejected = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={"labels": [{"id": "q001", "query": "重要度", "relevant_ids": [low.id]}]},
    )
    assert rejected.status_code == 422
    assert low.id in rejected.json()["detail"]


def test_recall_run_reports_missing_snapshot_and_embedding_config(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    missing = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 8},
    )
    assert missing.status_code == 404

    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    assert client.post("/memories/evaluation/recall/init", headers=auth_headers).status_code == 200

    embedding = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "embedding", "k": 8},
    )
    assert embedding.status_code == 400
    assert "embedding" in embedding.json()["detail"]


def test_recall_endpoints_return_422_for_malformed_labels_file(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    initialized = client.post(
        "/memories/evaluation/recall/init",
        headers=auth_headers,
    )
    assert initialized.status_code == 200
    Path(initialized.json()["labels"]).write_text("not json\n", encoding="utf-8")

    workbench = client.get(
        "/memories/evaluation/recall/workbench",
        headers=auth_headers,
    )
    run = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 8},
    )

    assert workbench.status_code == 422
    assert run.status_code == 422
    assert "Invalid label JSON on line 1" in workbench.json()["detail"]
    assert "Invalid label JSON on line 1" in run.json()["detail"]


def test_recall_run_enforces_and_reports_effective_k(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    initialized = client.post(
        "/memories/evaluation/recall/init",
        headers=auth_headers,
    )
    assert initialized.status_code == 200
    saved = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={
            "labels": [
                {
                    "id": "q001",
                    "query": "咖啡",
                    "judgment": "no_answer",
                    "relevant_ids": [],
                }
            ]
        },
    )
    assert saved.status_code == 200

    accepted = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 20},
    )
    rejected = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 21},
    )

    assert accepted.status_code == 200
    assert accepted.json()["summary"]["requested_k"] == 20
    assert accepted.json()["summary"]["effective_k"] == 20
    assert accepted.json()["summary"]["k"] == 20
    assert rejected.status_code == 422


def test_recall_run_keyword_does_not_touch_real_usage(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    coffee = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        importance=7,
    )
    memory_store.create_memory(user_id="default", content="用户喜欢写 TypeScript。")

    assert client.post("/memories/evaluation/recall/init", headers=auth_headers).status_code == 200
    labels = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={
            "labels": [
                {
                    "id": "q001",
                    "query": "咖啡偏好",
                    "relevant_ids": [coffee.id],
                    "note": "关键词基线应命中咖啡记忆",
                }
            ]
        },
    )
    assert labels.status_code == 200

    run = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 8},
    )

    assert run.status_code == 200
    payload = run.json()
    assert payload["mode"] == "keyword"
    assert payload["summary"]["queries_graded"] == 1
    refreshed = memory_store.get_memory(memory_id=coffee.id, user_id="default")
    assert refreshed is not None
    assert refreshed.usage_count == 0


def test_recall_run_grades_explicit_no_answer_labels(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
    assert client.post("/memories/evaluation/recall/init", headers=auth_headers).status_code == 200
    labels = client.put(
        "/memories/evaluation/recall/labels",
        headers=auth_headers,
        json={
            "labels": [
                {
                    "id": "q-no-answer",
                    "query": "咖啡",
                    "judgment": "no_answer",
                    "relevant_ids": [],
                }
            ]
        },
    )

    assert labels.status_code == 200
    assert labels.json()["summary"]["queries_graded"] == 1
    assert labels.json()["summary"]["queries_no_answer"] == 1

    run = client.post(
        "/memories/evaluation/recall/run",
        headers=auth_headers,
        json={"mode": "keyword", "k": 8},
    )
    assert run.status_code == 200
    summary = run.json()["summary"]
    assert summary["queries_graded"] == 1
    assert summary["queries_no_answer"] == 1
    assert summary["no_answer_false_positive_rate"] == 1.0
    assert summary["no_answer_abstention_rate"] == 0.0
    assert run.json()["per_query"][0]["false_positive"] is True
