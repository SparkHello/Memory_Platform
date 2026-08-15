import json

from fastapi.testclient import TestClient

from app.memory.store import MemoryStore


def test_memory_network_returns_core_evidence_and_similarity_edges(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    coffee = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=8,
        confidence=0.9,
        valence=0.75,
        arousal=0.35,
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )
    pour_over = memory_store.create_memory(
        user_id="default",
        content="用户喜欢手冲黑咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
        valence=0.78,
        arousal=0.4,
        embedding_json=json.dumps([0.98, 0.02]),
        embedding_space_id="test-space",
        evidence_memory_ids=[coffee.id],
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="用户偏好黑咖啡。",
        evidence_memory_ids=[coffee.id],
        confidence=0.9,
    )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={"limit": 20, "similarity_threshold": 0.8, "max_similarity_edges": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    nodes = {node["id"]: node for node in payload["nodes"]}
    edge_kinds = {(edge["source"], edge["target"], edge["kind"]) for edge in payload["edges"]}

    assert "core:preferences" in nodes
    assert nodes[coffee.id]["valence"] == 0.75
    assert nodes[pour_over.id]["arousal"] == 0.4
    assert ("core:preferences", coffee.id, "core_evidence") in edge_kinds
    assert (pour_over.id, coffee.id, "memory_evidence") in edge_kinds
    assert any(edge["kind"] == "similarity" for edge in payload["edges"])


def test_memory_network_falls_back_to_text_similarity_without_embeddings(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    left = memory_store.create_memory(
        user_id="default",
        content="用户喜欢在早晨散步。",
        type="emotional",
        importance=6,
    )
    right = memory_store.create_memory(
        user_id="default",
        content="用户喜欢早晨去公园散步。",
        type="emotional",
        importance=6,
    )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={"limit": 20, "similarity_threshold": 0.2, "max_similarity_edges": 10},
    )

    assert response.status_code == 200
    edges = response.json()["edges"]
    assert any(
        edge["kind"] == "similarity"
        and {edge["source"], edge["target"]} == {left.id, right.id}
        for edge in edges
    )


def test_memory_network_falls_back_to_text_for_mixed_embedding_dimensions(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    left = memory_store.create_memory(
        user_id="default",
        content="用户喜欢在早晨散步。",
        type="emotional",
        importance=6,
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )
    right = memory_store.create_memory(
        user_id="default",
        content="用户喜欢早晨散步。",
        type="emotional",
        importance=6,
        embedding_json=json.dumps([1.0, 0.0, 0.0]),
        embedding_space_id="test-space",
    )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={"limit": 20, "similarity_threshold": 0.2, "max_similarity_edges": 10},
    )

    assert response.status_code == 200
    assert any(
        edge["kind"] == "similarity"
        and {edge["source"], edge["target"]} == {left.id, right.id}
        for edge in response.json()["edges"]
    )


def test_memory_network_does_not_compare_vectors_from_different_spaces(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    left = memory_store.create_memory(
        user_id="default",
        content="用户正在准备越野跑。",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="space-a",
    )
    right = memory_store.create_memory(
        user_id="default",
        content="用户收藏了一台老式相机。",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="space-b",
    )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={"limit": 20, "similarity_threshold": 0.95, "max_similarity_edges": 10},
    )

    assert response.status_code == 200
    assert not any(
        edge["kind"] == "similarity"
        and {edge["source"], edge["target"]} == {left.id, right.id}
        for edge in response.json()["edges"]
    )


def test_memory_network_filters_by_space_type_sensitivity_and_emotion(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    work = memory_store.upsert_memory_space(user_id="default", name="Work")
    target = memory_store.create_memory(
        user_id="default",
        content="用户在推进记忆空间工作台。",
        type="semantic",
        importance=8,
        sensitivity="private",
        valence=0.7,
        arousal=0.4,
        space_ids=[work.id],
    )
    other = memory_store.create_memory(
        user_id="default",
        content="用户喜欢晚饭后散步。",
        type="emotional",
        importance=8,
        sensitivity="normal",
        valence=0.7,
        arousal=0.4,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="goals",
        content="用户在推进记忆系统。",
        evidence_memory_ids=[target.id, other.id],
        confidence=0.9,
    )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={
            "limit": 20,
            "space_id": work.id,
            "type": "semantic",
            "sensitivity": "private",
            "valence_min": 0.6,
            "valence_max": 0.8,
            "arousal_min": 0.2,
            "arousal_max": 0.6,
            "similarity_threshold": 0.0,
            "max_similarity_edges": 20,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    nodes = {node["id"]: node for node in payload["nodes"]}
    node_ids = set(nodes)
    edge_targets = {edge["target"] for edge in payload["edges"]}
    assert target.id in node_ids
    assert other.id not in node_ids
    assert nodes[target.id]["space_ids"] == [work.id]
    assert target.id in edge_targets
    assert other.id not in edge_targets


def test_memory_network_redacts_sensitive_node_content_only_in_response(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    private = memory_store.create_memory(
        user_id="default",
        content="用户的私人证件号码是 PA-12345。",
        source_message="我的证件号码是 PA-12345。",
        type="semantic",
        importance=9,
        sensitivity="sensitive",
    )
    normal = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=5,
    )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={
            "limit": 20,
            "similarity_threshold": 0.0,
            "max_similarity_edges": 20,
            "redact_sensitive": True,
        },
    )

    assert response.status_code == 200
    nodes = {node["id"]: node for node in response.json()["nodes"]}
    assert nodes[private.id]["redacted"] is True
    assert nodes[private.id]["redaction_reason"] == "sensitive"
    assert nodes[private.id]["content"] != private.content
    assert nodes[private.id]["source_message"] != private.source_message
    assert nodes[private.id]["importance"] == private.importance
    assert nodes[normal.id]["content"] == normal.content

    stored = memory_store.get_memory(memory_id=private.id, user_id="default")
    assert stored is not None
    assert stored.content == private.content


def test_memory_network_omits_stale_core_after_evidence_becomes_sensitive(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    evidence = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        importance=8,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="用户喜欢黑咖啡。",
        evidence_memory_ids=[evidence.id],
        confidence=0.9,
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET sensitivity = 'sensitive' WHERE id = ?",
            (evidence.id,),
        )

    response = client.post(
        "/memories/network",
        headers=auth_headers,
        json={"redact_sensitive": True},
    )

    assert response.status_code == 200
    assert "core:preferences" not in {
        node["id"] for node in response.json()["nodes"]
    }


def test_memory_network_traverse_returns_multihop_paths_with_depth_limit(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="用户正在设计本地记忆网关。",
        type="semantic",
        importance=8,
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )
    bridge = memory_store.create_memory(
        user_id="default",
        content="记忆网关会把相关主题串成网络图。",
        type="semantic",
        importance=7,
        embedding_json=json.dumps([0.8, 0.6]),
        embedding_space_id="test-space",
    )
    target = memory_store.create_memory(
        user_id="default",
        content="网络图遍历可以找到多跳相关记忆。",
        type="semantic",
        importance=7,
        embedding_json=json.dumps([0.28, 0.96]),
        embedding_space_id="test-space",
    )
    unrelated = memory_store.create_memory(
        user_id="default",
        content="用户喜欢周末整理照片。",
        type="emotional",
        importance=7,
        embedding_json=json.dumps([-1.0, 0.0]),
        embedding_space_id="test-space",
    )

    response = client.post(
        "/memories/network/traverse",
        headers=auth_headers,
        json={
            "seed_id": seed.id,
            "depth": 2,
            "limit": 5,
            "similarity_threshold": 0.75,
            "max_candidates": 20,
            "max_edges": 20,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    result_ids = [item["memory"]["id"] for item in payload["results"]]
    assert bridge.id in result_ids
    assert target.id in result_ids
    assert unrelated.id not in result_ids
    assert payload["meta"]["depth"] == 2
    assert payload["meta"]["reachable_count"] >= 2

    target_item = next(item for item in payload["results"] if item["memory"]["id"] == target.id)
    assert target_item["depth"] == 2
    assert target_item["score"] > 0
    assert [
        {edge["source"], edge["target"]}
        for edge in target_item["path"]
    ] == [
        {seed.id, bridge.id},
        {bridge.id, target.id},
    ]

    shallow_response = client.post(
        "/memories/network/traverse",
        headers=auth_headers,
        json={
            "seed_id": seed.id,
            "depth": 1,
            "limit": 5,
            "similarity_threshold": 0.75,
            "max_candidates": 20,
            "max_edges": 20,
        },
    )

    assert shallow_response.status_code == 200
    shallow_ids = [item["memory"]["id"] for item in shallow_response.json()["results"]]
    assert bridge.id in shallow_ids
    assert target.id not in shallow_ids


def test_memory_network_traverse_spends_edge_budget_from_seed_frontier(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="seed",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )
    reachable = memory_store.create_memory(
        user_id="default",
        content="reachable",
        embedding_json=json.dumps([0.8, 0.6]),
        embedding_space_id="test-space",
    )
    for index, vector in enumerate(
        ([0.0, 1.0], [0.01, 0.99995], [-0.01, 0.99995]),
        start=1,
    ):
        memory_store.create_memory(
            user_id="default",
            content=f"unrelated-{index}",
            embedding_json=json.dumps(vector),
            embedding_space_id="test-space",
        )

    response = client.post(
        "/memories/network/traverse",
        headers=auth_headers,
        json={
            "seed_id": seed.id,
            "depth": 2,
            "limit": 5,
            "similarity_threshold": 0.75,
            "max_candidates": 20,
            "max_edges": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert reachable.id in {
        item["memory"]["id"] for item in payload["results"]
    }
    assert payload["meta"]["edge_count"] <= 2


def test_memory_network_traverse_bounds_induced_candidate_graph(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="bounded traversal seed",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )
    for index in range(70):
        memory_store.create_memory(
            user_id="default",
            content=f"candidate-{index}",
            embedding_json=json.dumps([1.0, index / 1000]),
            embedding_space_id="test-space",
        )

    response = client.post(
        "/memories/network/traverse",
        headers=auth_headers,
        json={"seed_id": seed.id, "max_candidates": 500, "max_edges": 1500},
    )

    assert response.status_code == 200
    assert response.json()["meta"]["candidate_count"] == 50


def test_memory_network_traverse_uses_explicit_evidence_edge(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="Evidence seed.",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="Lexically unrelated reflection.",
        origin="agent_derived",
        evidence_memory_ids=[seed.id],
        embedding_json=json.dumps([0.0, 1.0]),
        embedding_space_id="test-space",
    )

    response = client.post(
        "/memories/network/traverse",
        headers=auth_headers,
        json={
            "seed_id": seed.id,
            "depth": 1,
            "limit": 5,
            "similarity_threshold": 0.99,
            "max_candidates": 20,
            "max_edges": 10,
        },
    )

    assert response.status_code == 200
    item = next(
        result
        for result in response.json()["results"]
        if result["memory"]["id"] == derived.id
    )
    assert item["path"][0]["kind"] == "evidence"


def test_memory_network_traverse_respects_user_boundary(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    other_user_memory = memory_store.create_memory(
        user_id="other",
        content="其他用户的记忆不应被遍历。",
        type="semantic",
        importance=8,
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )

    response = client.post(
        "/memories/network/traverse",
        headers=auth_headers,
        json={"seed_id": other_user_memory.id},
    )

    assert response.status_code == 404
