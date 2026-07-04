from __future__ import annotations

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


def test_recall_workbench_init_redacts_and_filters_user(
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
    assert workbench_response.status_code == 200
    payload = workbench_response.json()
    candidate_ids = {candidate["id"] for candidate in payload["candidates"]}
    assert public.id in candidate_ids
    assert private.id in candidate_ids
    assert other.id not in candidate_ids

    private_payload = next(candidate for candidate in payload["candidates"] if candidate["id"] == private.id)
    assert private_payload["redacted"] is True
    assert private_payload["content"] != private.content


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
