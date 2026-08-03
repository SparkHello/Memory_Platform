from datetime import UTC, datetime, timedelta
import json

from fastapi.testclient import TestClient

from app.memory.review import MemoryReviewer
from app.memory.store import MemoryStore


def test_review_duplicate_recommends_same_with_newer_content(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee",
        type="emotional",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee!",
        type="emotional",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "merge"
    assert recommendation.relation == "same"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content
    assert recommendation.risk_tags == ["duplicate"]
    assert recommendation.severity == "medium"
    assert "merge" in recommendation.next_action_options


def test_review_contained_memory_recommends_supplement(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes coffee",
        type="emotional",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User likes coffee with oat milk",
        type="emotional",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "merge"
    assert recommendation.relation == "supplement"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content


def test_review_high_similarity_recommends_supersede(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee",
        type="emotional",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User likes dark coffee",
        type="emotional",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "review"
    assert recommendation.relation == "supersede"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content


def test_review_negation_difference_recommends_conflict(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee",
        type="emotional",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User does not like black coffee",
        type="emotional",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "review"
    assert recommendation.relation == "conflict"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content
    assert recommendation.risk_tags == ["conflict"]
    assert recommendation.severity == "high"
    assert "ai_modify" in recommendation.next_action_options


def test_review_marks_due_expired_low_value_and_time_uncertain(memory_store: MemoryStore) -> None:
    now = datetime.now(UTC)
    due = memory_store.create_memory(
        user_id="default",
        content="用户目前使用 ChatWise 作为 AI 客户端。",
        type="semantic",
        importance=7,
        review_after=(now - timedelta(days=1)).isoformat(),
    )
    expired_low_value = memory_store.create_memory(
        user_id="default",
        content="用户临时想试用一个旧工具。",
        type="semantic",
        importance=2,
        stability="temporary",
        valid_until=(now - timedelta(days=1)).isoformat(),
    )
    unanchored = memory_store.create_memory(
        user_id="default",
        content="用户现在 18 岁。",
        type="semantic",
        importance=6,
    )

    result = MemoryReviewer(store=memory_store).review(user_id="default")
    by_id = {recommendation.memory_ids[0]: recommendation for recommendation in result.recommendations}

    assert "time_uncertain" in by_id[due.id].risk_tags
    assert by_id[due.id].action == "review"
    assert {"ai_modify", "confirm_valid", "snooze"} <= set(by_id[due.id].next_action_options)
    assert by_id[expired_low_value.id].action == "delete"
    assert by_id[expired_low_value.id].risk_tags == ["expired", "low_value"]
    assert "move_to_trash" in by_id[expired_low_value.id].next_action_options
    assert by_id[unanchored.id].risk_tags == ["time_uncertain"]
    assert by_id[unanchored.id].severity == "medium"


def test_review_marks_sensitive_and_core_evidence_risks(memory_store: MemoryStore) -> None:
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户有一条敏感偏好。",
        type="emotional",
        sensitivity="sensitive",
        importance=7,
    )
    weak_core_evidence = memory_store.create_memory(
        user_id="default",
        content="用户可能正在使用旧设备。",
        type="semantic",
        confidence=0.4,
        importance=8,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="用户设备信息。",
        evidence_memory_ids=[weak_core_evidence.id],
        confidence=0.8,
    )

    result = MemoryReviewer(store=memory_store).review(user_id="default")
    sensitive_rec = next(
        recommendation for recommendation in result.recommendations
        if recommendation.memory_ids == [sensitive.id]
    )
    core_rec = next(
        recommendation for recommendation in result.recommendations
        if recommendation.memory_ids == [weak_core_evidence.id]
        and "core_evidence" in recommendation.risk_tags
    )

    assert sensitive_rec.risk_tags == ["sensitive"]
    assert sensitive_rec.severity == "medium"
    assert core_rec.severity == "high"
    assert core_rec.core_memory_sections == ["profile"]
    assert "review_core_memory" in core_rec.next_action_options


def test_review_marks_emotion_uncertain(memory_store: MemoryStore) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户对演示复盘感到强烈不安。",
        type="semantic",
        importance=5,
        confidence=0.45,
        arousal=0.85,
    )

    recommendation = _only_recommendation(memory_store)

    assert recommendation.risk_tags == ["emotion_uncertain"]
    assert recommendation.severity == "medium"
    assert {"ai_modify", "confirm_valid", "snooze"} <= set(recommendation.next_action_options)


def test_review_marks_stale_and_low_life(memory_store: MemoryStore) -> None:
    stale = memory_store.create_memory(
        user_id="default",
        content="用户关注一个旧研究方向。",
        type="procedural",
        importance=8,
    )
    low_life = memory_store.create_memory(
        user_id="default",
        content="用户收藏了一个旧链接。",
        type="procedural",
        importance=2,
    )
    old_time = (datetime.now(UTC) - timedelta(days=140)).isoformat()
    _set_updated_at(memory_store, stale.id, old_time)
    _set_updated_at(memory_store, low_life.id, old_time)

    result = MemoryReviewer(store=memory_store).review(user_id="default")
    by_id = {recommendation.memory_ids[0]: recommendation for recommendation in result.recommendations}

    assert by_id[stale.id].risk_tags == ["stale"]
    assert by_id[stale.id].severity == "medium"
    assert by_id[low_life.id].risk_tags == ["low_value"]
    assert by_id[low_life.id].action == "lower"
    assert "lower_importance" in by_id[low_life.id].next_action_options


def test_review_action_confirm_valid_writes_audit_log(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户最近在试用 ChatWise。",
        type="semantic",
        importance=7,
    )

    response = client.post(
        "/memories/review/actions",
        headers=auth_headers,
        json={
            "action": "confirm_valid",
            "memory_ids": [memory.id],
            "risk_tags": ["time_uncertain"],
            "severity": "medium",
            "reason": "用户确认仍有效",
        },
    )

    assert response.status_code == 200
    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.review_after is not None
    [log] = memory_store.list_decision_logs(user_id="default")
    audit = json.loads(log.candidate_json)
    assert audit["source"] == "review_action"
    assert audit["action"] == "confirm_valid"
    assert audit["risk_tags"] == ["time_uncertain"]
    assert audit["severity"] == "medium"
    assert audit["before"][0]["id"] == memory.id
    assert audit["after"][0]["id"] == memory.id


def test_review_action_audit_hashes_sensitive_memory_text(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    secret = "123456789012345678"
    memory = memory_store.create_memory(
        user_id="default",
        content=f"用户的身份证号是 {secret}。",
        source_message=f"请记住，我的身份证号是 {secret}",
        importance=8,
        confidence=0.95,
    )

    response = client.post(
        "/memories/review/actions",
        headers=auth_headers,
        json={"action": "confirm_valid", "memory_ids": [memory.id]},
    )

    assert response.status_code == 200
    [log] = memory_store.list_decision_logs(user_id="default")
    audit = json.loads(log.candidate_json)
    assert audit["before"][0]["redacted"] is True
    assert audit["after"][0]["redacted"] is True
    assert len(audit["before"][0]["content_sha256"]) == 64
    assert len(audit["before"][0]["source_message_sha256"]) == 64
    assert secret not in log.candidate_json


def test_review_action_rejects_cross_user_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=7,
    )

    response = client.post(
        "/memories/review/actions",
        headers={**auth_headers, "X-User-Id": "other"},
        json={
            "action": "snooze",
            "memory_ids": [memory.id],
        },
    )

    assert response.status_code == 404


def _only_recommendation(memory_store: MemoryStore):
    result = MemoryReviewer(store=memory_store).review(user_id="default")

    assert len(result.recommendations) == 1
    return result.recommendations[0]


def _set_updated_at(memory_store: MemoryStore, memory_id: str, updated_at: str) -> None:
    with memory_store._connect() as connection:
        connection.execute(
            """
            UPDATE memories
            SET updated_at = ?
            WHERE id = ?
            """,
            (updated_at, memory_id),
        )

