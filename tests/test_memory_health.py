import json

from app.memory.health import MemoryHealthChecker
from app.memory.search import SEARCH_CACHE
from app.memory.store import MemoryStore


def test_memory_health_rest_returns_ok_for_empty_database(client, auth_headers):
    response = client.get("/memories/health", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["summary"] == {"errors": 0, "warnings": 0, "info": 0}
    assert payload["issues"] == []


def test_memory_health_reports_archived_core_evidence(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes old project notes.",
        embedding_json=_vector_json(1024),
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="User has an old project note.",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )
    memory_store.archive_memory(memory_id=memory.id, user_id="default")

    response = client.get("/memories/health", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "warning"
    assert _issue_types(payload) == {"archived_core_evidence"}
    assert payload["issues"][0]["object_id"] == "core:profile"
    assert payload["issues"][0]["related_id"] == memory.id


def test_memory_health_has_no_orphan_core_evidence_after_purge(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User private target can be purged.",
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="User has a purge target.",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )
    memory_store.archive_memory(memory_id=memory.id, user_id="default")
    assert memory_store.purge_archived_memory(
        memory_id=memory.id,
        user_id="default",
        affected_core_sections=[],
        call_source="test",
    )

    response = client.get("/memories/health", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert not any(issue["type"] == "orphan_core_evidence" for issue in payload["issues"])
    assert memory_store.list_core_memory_sections(user_id="default") == []


def test_memory_health_reports_orphan_space_links_and_export_reference(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User has a space link target.",
        embedding_json=_vector_json(1024),
    )
    with memory_store._connect() as connection:
        connection.execute(
            """
            INSERT INTO memory_space_links (user_id, memory_id, space_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("default", "missing-memory", "missing-space-a", "2026-06-16T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO memory_space_links (user_id, memory_id, space_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("default", memory.id, "missing-space-b", "2026-06-16T00:01:00+00:00"),
        )

    response = client.get("/memories/health", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["summary"]["errors"] == 3
    assert "orphan_space_link_memory" in _issue_types(payload)
    assert "orphan_space_link_space" in _issue_types(payload)
    assert "export_space_reference_missing" in _issue_types(payload)
    with memory_store._connect() as connection:
        [count] = connection.execute(
            "SELECT COUNT(*) FROM memory_space_links WHERE user_id = ?",
            ("default",),
        ).fetchone()
    assert count == 2


def test_memory_health_embedding_missing_severity_follows_embedding_config(
    memory_store: MemoryStore,
):
    memory_store.create_memory(
        user_id="default",
        content="User has a memory without embedding.",
    )

    disabled_result = MemoryHealthChecker(
        store=memory_store,
        expected_embedding_dimensions=1024,
        embedding_enabled=False,
    ).check(user_id="default")
    enabled_result = MemoryHealthChecker(
        store=memory_store,
        expected_embedding_dimensions=1024,
        embedding_enabled=True,
    ).check(user_id="default")

    assert disabled_result.status == "ok"
    assert disabled_result.issues[0].type == "embedding_missing"
    assert disabled_result.issues[0].severity == "info"
    assert enabled_result.status == "warning"
    assert enabled_result.issues[0].type == "embedding_missing"
    assert enabled_result.issues[0].severity == "warning"


def test_memory_health_reports_invalid_and_mismatched_embeddings(
    memory_store: MemoryStore,
):
    invalid = memory_store.create_memory(
        user_id="default",
        content="User has invalid embedding.",
        embedding_json="not-json",
    )
    mismatch = memory_store.create_memory(
        user_id="default",
        content="User has short embedding.",
        embedding_json="[0.1, 0.2]",
    )

    result = MemoryHealthChecker(
        store=memory_store,
        expected_embedding_dimensions=3,
        embedding_enabled=True,
    ).check(user_id="default")

    assert result.status == "warning"
    issues_by_related_id = {issue.related_id: issue for issue in result.issues}
    assert issues_by_related_id[invalid.id].type == "embedding_invalid"
    assert issues_by_related_id[mismatch.id].type == "embedding_dimension_mismatch"


def test_memory_health_turns_export_failure_into_issue(monkeypatch, memory_store: MemoryStore):
    def broken_export(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.memory.health.build_memory_export", broken_export)

    result = MemoryHealthChecker(
        store=memory_store,
        expected_embedding_dimensions=1024,
        embedding_enabled=False,
    ).check(user_id="default")

    assert result.status == "error"
    assert result.issues[0].type == "export_consistency_error"
    assert "boom" in result.issues[0].message


def test_memory_health_reports_search_cache_and_decision_log_info(
    memory_store: MemoryStore,
):
    SEARCH_CACHE[("default", "coffee", 5)] = (
        9999999999.0,
        "2026-06-16T00:00:00+00:00",
        1,
        [{"id": "missing-cache-memory"}],
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id=None,
        candidate_json=json.dumps({"memory_id": "missing-log-memory"}),
        decision="ignore",
        reason="test log",
    )
    with memory_store._connect() as connection:
        connection.execute(
            """
            INSERT INTO memory_decision_logs (
                id, user_id, conversation_id, candidate_json, decision, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid-json-log",
                "default",
                None,
                "{not-json",
                "ignore",
                "invalid json",
                "2026-06-16T00:00:00+00:00",
            ),
        )

    result = MemoryHealthChecker(
        store=memory_store,
        expected_embedding_dimensions=1024,
        embedding_enabled=False,
    ).check(user_id="default")

    assert result.status == "ok"
    assert {
        "stale_search_cache_reference",
        "orphan_decision_log_reference",
        "invalid_decision_log_json",
    }.issubset({issue.type for issue in result.issues})


def test_memory_health_rest_requires_auth_and_respects_user_isolation(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="Default user deleted evidence.",
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="goals",
        content="Default user goal.",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )
    memory_store.archive_memory(memory_id=memory.id, user_id="default")

    unauthorized = client.get("/memories/health")
    other_user = client.get(
        "/memories/health",
        headers={**auth_headers, "X-User-Id": "other"},
    )

    assert unauthorized.status_code == 401
    assert other_user.status_code == 200
    assert other_user.json()["status"] == "ok"
    assert other_user.json()["issues"] == []


def _issue_types(payload: dict) -> set[str]:
    return {issue["type"] for issue in payload["issues"]}


def _vector_json(dimensions: int) -> str:
    return json.dumps([0.01] * dimensions)

