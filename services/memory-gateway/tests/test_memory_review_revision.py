import json

from fastapi.testclient import TestClient
import pytest

from app.config import get_settings
from app.memory.review_revision import ReviewRevisionError
from app.memory.store import MemoryStore
from app.memory.utils import _parse_iso_datetime


def test_review_preview_signing_fails_closed_without_secret() -> None:
    import app.memory.review_revision as revision

    with pytest.raises(ReviewRevisionError) as signing_error:
        revision._sign_preview(secret="", payload={"version": 2})
    assert signing_error.value.status_code == 503

    token = revision._sign_preview(secret="configured-secret", payload={"version": 2})
    with pytest.raises(ReviewRevisionError) as verification_error:
        revision._verify_preview(secret="", token=token)
    assert verification_error.value.status_code == 503


def test_sensitive_review_is_blocked_before_remote_llm(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户有一项健康隐私。",
        sensitivity="sensitive",
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "帮我检查这条记忆"},
    )

    assert response.status_code == 422
    assert "ALLOW_SENSITIVE_EGRESS" in response.json()["detail"]
    assert fake_llm.review_revision_messages == []


def test_sensitive_recommendation_reason_is_blocked_before_remote_llm(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢深烘咖啡。",
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "user_note": "帮我检查这条记忆",
            "recommendation_reason": "身份证号是 123456789012345678",
        },
    )

    assert response.status_code == 422
    assert "ALLOW_SENSITIVE_EGRESS" in response.json()["detail"]
    assert fake_llm.review_revision_messages == []


def test_legacy_mislabeled_sensitive_memory_is_blocked_before_remote_llm(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢深烘咖啡。",
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET content = ?, sensitivity = 'normal' WHERE id = ?",
            ("用户的身份证号是 123456789012345678。", memory.id),
        )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "帮我检查这条记忆"},
    )

    assert response.status_code == 422
    assert "ALLOW_SENSITIVE_EGRESS" in response.json()["detail"]
    assert fake_llm.review_revision_messages == []


def test_review_preview_uses_thinking_structured_generation(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢深烘咖啡。",
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "no_change",
                    "memory_ids": [memory.id],
                    "reason": "当前记忆准确",
                }
            ]
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "确认这条记忆准确"},
    )

    assert response.status_code == 200
    assert fake_llm.review_revision_thinking == "enabled"
    assert fake_llm.review_revision_request.max_tokens == 4096
    assert fake_llm.review_revision_request.response_format == {"type": "json_object"}
    assert fake_llm.review_revision_structured_tool["name"] == (
        "submit_memory_review_revision"
    )
    assert (
        fake_llm.review_revision_structured_tool["parameters"]["required"]
        == ["operations"]
    )


def test_review_preview_accepts_structured_tool_arguments(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢深烘咖啡。",
    )
    fake_llm.review_revision_tool_arguments = json.dumps(
        {
            "operations": [
                {
                    "operation": "no_change",
                    "memory_ids": [memory.id],
                    "reason": "当前记忆准确",
                }
            ],
            "reason": "无需修改",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "确认这条记忆准确"},
    )

    assert response.status_code == 200
    assert response.json()["operations"][0]["operation"] == "no_change"
    assert response.json()["operations"][0]["memory_ids"] == [memory.id]
    assert response.json()["reason"] == "无需修改"


def test_review_preview_rejects_stale_memory_version(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(user_id="default", content="用户喜欢深烘咖啡。")
    fake_llm.review_revision_content = json.dumps(
        {"operations": [{"operation": "no_change", "memory_ids": [memory.id]}]}
    )
    preview = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "确认这条记忆"},
    ).json()
    updated = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"content": "用户现在喜欢浅烘咖啡。"},
    )
    assert updated.status_code == 200

    applied = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )

    assert applied.status_code == 409
    assert "预览后发生了变化" in applied.json()["detail"]


def test_review_preview_token_expires(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    import json as _json
    import app.memory.review_revision as revision

    memory = memory_store.create_memory(user_id="default", content="用户喜欢咖啡。")
    fake_llm.review_revision_content = json.dumps(
        {"operations": [{"operation": "no_change", "memory_ids": [memory.id]}]}
    )
    preview = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "确认这条记忆"},
    ).json()
    payload_part, _ = preview["preview_token"].split(".", 1)
    payload = _json.loads(revision._unb64(payload_part).decode("utf-8"))
    payload["expires_at"] = "2000-01-01T00:00:00+00:00"
    expired_token = revision._sign_preview(
        secret=get_settings().gateway_signing_secret,
        payload=payload,
    )

    applied = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "operations": preview["operations"],
            "preview_token": expired_token,
        },
    )

    assert applied.status_code == 409
    assert "已过期" in applied.json()["detail"]


def test_preview_and_apply_review_update_sets_review_after(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢深烘咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "update",
                    "memory_ids": [memory.id],
                    "target_memory_id": memory.id,
                    "content": "用户喜欢浅烘咖啡。",
                    "type": "emotional",
                    "confidence": 0.92,
                    "reason": "用户说明现在更正为浅烘。",
                }
            ],
            "reason": "生成一条更新操作。",
        },
        ensure_ascii=False,
    )

    preview_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "user_note": "我现在喜欢浅烘，不是深烘。",
            "recommendation_reason": "需要确认是否仍然成立",
            "relation": "supersede",
            "risk_tags": ["conflict"],
            "severity": "high",
        },
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    operation = preview["operations"][0]
    assert operation["operation"] == "update"
    assert operation["content"] == "用户喜欢浅烘咖啡。"
    assert operation["review_policy"]["interval_days"] == 365

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
            "risk_tags": ["conflict"],
            "severity": "high",
        },
    )

    assert apply_response.status_code == 200
    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.content == "用户喜欢浅烘咖啡。"
    assert stored.review_after is not None
    logs = memory_store.list_decision_logs(user_id="default")
    audit = json.loads(logs[0].candidate_json)
    assert audit["source"] == "review_modify"
    assert audit["risk_tags"] == ["conflict"]
    assert audit["severity"] == "high"


def test_related_revision_candidates_use_search_and_rules_without_usage(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    supplement = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
    )
    conflict = memory_store.create_memory(
        user_id="default",
        content="用户不喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    archived = memory_store.create_memory(
        user_id="default",
        content="用户喜欢过期咖啡记忆。",
        type="emotional",
        importance=7,
    )
    memory_store.archive_memory(memory_id=archived.id, user_id="default")
    memory_store.create_memory(
        user_id="other",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=10,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="用户与咖啡相关的偏好需要确认。",
        evidence_memory_ids=[conflict.id],
        confidence=0.9,
    )

    response = client.post(
        "/memories/review/revise/related",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id],
            "user_note": "咖啡这条可能有冲突，帮我找相关记忆。",
            "recommendation_reason": "同类型记忆可能冲突",
            "limit": 8,
        },
    )

    assert response.status_code == 200
    candidates = response.json()["data"]
    ids = {candidate["memory"]["id"] for candidate in candidates}
    assert selected.id not in ids
    assert supplement.id in ids
    assert conflict.id in ids
    assert archived.id not in ids
    conflict_candidate = next(candidate for candidate in candidates if candidate["memory"]["id"] == conflict.id)
    assert conflict_candidate["relation"] == "conflict"
    assert conflict_candidate["is_core_memory_evidence"] is True
    assert conflict_candidate["core_memory_sections"][0]["section"] == "preferences"
    supplement_candidate = next(candidate for candidate in candidates if candidate["memory"]["id"] == supplement.id)
    assert "rule" in supplement_candidate["channels"]
    assert any(channel.startswith("search:") for channel in supplement_candidate["channels"])
    refreshed = memory_store.get_memory(memory_id=supplement.id, user_id="default")
    assert refreshed is not None
    assert refreshed.usage_count == 0


def test_related_revision_candidates_are_limited_to_eight(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    for index in range(12):
        memory_store.create_memory(
            user_id="default",
            content=f"用户喜欢咖啡相关测试 {index}。",
            type="emotional",
            importance=7,
        )

    response = client.post(
        "/memories/review/revise/related",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id],
            "user_note": "咖啡相关记忆都找出来看看。",
            "limit": 8,
        },
    )

    assert response.status_code == 200
    assert len(response.json()["data"]) == 8


def test_review_revision_can_update_and_archive_conflict(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="用户的 AI 客户端是 Kelivo。",
        type="semantic",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="用户的 AI 客户端是 ChatWise。",
        type="semantic",
        importance=7,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "update",
                    "memory_ids": [older.id, newer.id],
                    "target_memory_id": newer.id,
                    "content": "用户目前使用 ChatWise 作为 AI 客户端。",
                    "type": "semantic",
                    "stability": "medium",
                    "reason": "用户确认 ChatWise 是当前事实。",
                },
                {
                    "operation": "archive",
                    "memory_ids": [older.id],
                    "reason": "Kelivo 记忆已被当前事实取代。",
                },
            ]
        },
        ensure_ascii=False,
    )

    preview_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [older.id, newer.id],
            "user_note": "现在用 ChatWise，Kelivo 那条过期了。",
            "recommendation_reason": "两条同类型记忆可能冲突",
            "relation": "conflict",
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert [operation["operation"] for operation in preview["operations"]] == ["update", "archive"]
    assert preview["operations"][0]["review_policy"]["code"] == "time_variable"

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [older.id, newer.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )
    assert apply_response.status_code == 200
    assert memory_store.get_memory(memory_id=older.id, user_id="default") is None
    updated = memory_store.get_memory(memory_id=newer.id, user_id="default")
    assert updated is not None
    assert updated.content == "用户目前使用 ChatWise 作为 AI 客户端。"


def test_apply_revision_returns_affected_core_sections_for_archived_evidence(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢旧工具。",
        type="semantic",
        importance=7,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="用户使用旧工具。",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "archive",
                    "memory_ids": [memory.id],
                    "reason": "用户确认这条旧工具记忆应删除。",
                }
            ]
        },
        ensure_ascii=False,
    )

    preview = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "user_note": "这条删掉。",
        },
    ).json()
    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )

    assert apply_response.status_code == 200
    payload = apply_response.json()
    assert payload["affected_core_sections"][0]["section"] == "profile"
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is None
    assert memory_store.restore_memory(memory_id=memory.id, user_id="default") is not None


def test_review_revision_merge_and_no_change_preview(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    first = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    second = memory_store.create_memory(
        user_id="default",
        content="用户喜欢浅烘咖啡。",
        type="emotional",
        importance=8,
    )
    third = memory_store.create_memory(
        user_id="default",
        content="User likes decaf coffee.",
        type="emotional",
        importance=6,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "merge",
                    "memory_ids": [first.id, second.id],
                    "target_memory_id": second.id,
                    "content": "用户喜欢浅烘咖啡。",
                    "type": "emotional",
                    "reason": "两条偏好可合并。",
                },
                {
                    "operation": "no_change",
                    "memory_ids": [third.id],
                    "reason": "The decaf preference remains useful as-is.",
                },
            ]
        },
        ensure_ascii=False,
    )

    preview_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [first.id, second.id, third.id],
            "user_note": "保留浅烘这一条就好。",
            "recommendation_reason": "重复记忆",
            "relation": "same",
        },
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert [operation["operation"] for operation in preview["operations"]] == ["merge", "no_change"]
    assert preview["operations"][1]["memory_ids"] == [third.id]

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [first.id, second.id, third.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )
    assert apply_response.status_code == 200
    assert memory_store.get_memory(memory_id=first.id, user_id="default") is None
    merged = memory_store.get_memory(memory_id=second.id, user_id="default")
    assert merged is not None
    assert merged.content == "用户喜欢浅烘咖啡。"
    unchanged = memory_store.get_memory(memory_id=third.id, user_id="default")
    assert unchanged is not None
    assert unchanged.content == "User likes decaf coffee."


def test_review_revision_rejects_preview_when_selected_related_memory_is_not_covered(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="User likes dark roast coffee.",
        type="emotional",
        importance=7,
    )
    related = memory_store.create_memory(
        user_id="default",
        content="User likes decaf coffee.",
        type="emotional",
        importance=6,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "update",
                    "memory_ids": [selected.id, related.id],
                    "target_memory_id": selected.id,
                    "content": "User likes light roast coffee.",
                    "type": "emotional",
                    "reason": "The user corrected the roast preference and the related memory was only context.",
                }
            ]
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "user_note": "Change dark roast to light roast; keep the decaf note as context.",
        },
    )

    assert response.status_code == 422
    assert related.id in response.text
    stored_selected = memory_store.get_memory(memory_id=selected.id, user_id="default")
    stored_related = memory_store.get_memory(memory_id=related.id, user_id="default")
    assert stored_selected is not None
    assert stored_selected.content == "User likes dark roast coffee."
    assert stored_related is not None
    assert stored_related.content == "User likes decaf coffee."


def test_review_revision_can_update_selected_and_mark_related_no_change(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="User likes dark roast coffee.",
        type="emotional",
        importance=7,
    )
    related = memory_store.create_memory(
        user_id="default",
        content="User likes decaf coffee.",
        type="emotional",
        importance=6,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "update",
                    "memory_ids": [selected.id],
                    "target_memory_id": selected.id,
                    "content": "User likes light roast coffee.",
                    "type": "emotional",
                    "reason": "The user corrected the roast preference.",
                },
                {
                    "operation": "no_change",
                    "memory_ids": [related.id],
                    "reason": "The decaf preference is related but still accurate.",
                },
            ]
        },
        ensure_ascii=False,
    )

    preview_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "user_note": "Change dark roast to light roast; decaf is still true.",
        },
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["operations"][1]["memory_ids"] == [related.id]

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )

    assert apply_response.status_code == 200
    results = apply_response.json()["results"]
    assert results[1]["operation"] == "no_change"
    assert results[1]["memory_ids"] == [related.id]
    updated = memory_store.get_memory(memory_id=selected.id, user_id="default")
    unchanged = memory_store.get_memory(memory_id=related.id, user_id="default")
    assert updated is not None
    assert updated.content == "User likes light roast coffee."
    assert unchanged is not None
    assert unchanged.content == "User likes decaf coffee."


def test_review_revision_can_archive_selected_related_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="User currently uses ChatWise.",
        type="semantic",
        importance=7,
    )
    related = memory_store.create_memory(
        user_id="default",
        content="User currently uses Kelivo.",
        type="semantic",
        importance=7,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "no_change",
                    "memory_ids": [selected.id],
                    "reason": "ChatWise remains the current client.",
                },
                {
                    "operation": "archive",
                    "memory_ids": [related.id],
                    "reason": "Kelivo was replaced and should move to trash.",
                },
            ]
        },
        ensure_ascii=False,
    )

    preview = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "user_note": "ChatWise is current; Kelivo is old and should go to trash.",
        },
    ).json()
    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )

    assert apply_response.status_code == 200
    assert memory_store.get_memory(memory_id=selected.id, user_id="default") is not None
    assert memory_store.get_memory(memory_id=related.id, user_id="default") is None
    archived = memory_store.list_archived_memories(user_id="default")
    assert [memory.id for memory in archived] == [related.id]
    assert memory_store.restore_memory(memory_id=related.id, user_id="default") is not None


def test_review_revision_preview_accepts_common_llm_schema_variants(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="User likes dark roast coffee.",
        type="emotional",
        importance=7,
    )
    related = memory_store.create_memory(
        user_id="default",
        content="User likes stale coffee note.",
        type="emotional",
        importance=4,
    )
    fake_llm.review_revision_content = json.dumps(
        [
            {
                "operation": "modify",
                "memory_ids": selected.id,
                "target_memory_id": selected.id,
                "content": "User likes light roast coffee.",
                "type": "emotional",
                "importance": "8",
                "confidence": "0.91",
                "reason": 123,
            },
            {
                "operation": "move_to_trash",
                "memory_ids": related.id,
                "reason": "The stale note should be moved to trash.",
            },
        ],
        ensure_ascii=False,
    )

    preview_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "user_note": "Light roast is right; stale note can go to trash.",
        },
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert [operation["operation"] for operation in preview["operations"]] == ["update", "archive"]
    assert preview["operations"][0]["memory_ids"] == [selected.id]
    assert preview["operations"][0]["importance"] == 8
    assert preview["operations"][0]["confidence"] == 0.91
    assert preview["operations"][1]["memory_ids"] == [related.id]

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id, related.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )

    assert apply_response.status_code == 200
    updated = memory_store.get_memory(memory_id=selected.id, user_id="default")
    assert updated is not None
    assert updated.content == "User likes light roast coffee."
    assert updated.importance == 8
    assert memory_store.get_memory(memory_id=related.id, user_id="default") is None
    assert memory_store.restore_memory(memory_id=related.id, user_id="default") is not None


def test_review_revision_rejects_invalid_memory_and_tampered_apply(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢茶。",
        type="emotional",
        importance=7,
    )

    missing_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": ["missing"], "user_note": "改一下"},
    )
    assert missing_response.status_code == 404

    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "update",
                    "memory_ids": [memory.id],
                    "target_memory_id": memory.id,
                    "content": "用户喜欢乌龙茶。",
                }
            ]
        },
        ensure_ascii=False,
    )
    preview = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "改成乌龙茶"},
    ).json()
    tampered_operations = [dict(preview["operations"][0], content="用户喜欢红茶。")]

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "operations": tampered_operations,
            "preview_token": preview["preview_token"],
        },
    )
    assert apply_response.status_code == 409


def test_review_revision_rejects_ai_operation_for_unselected_related_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    selected = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    unselected = memory_store.create_memory(
        user_id="default",
        content="用户不喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "archive",
                    "memory_ids": [unselected.id],
                    "reason": "AI 想删除未勾选记忆。",
                }
            ]
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={
            "memory_ids": [selected.id],
            "user_note": "只处理咖啡这条。",
        },
    )

    assert response.status_code == 422
    assert memory_store.get_memory(memory_id=unselected.id, user_id="default") is not None


def test_review_revision_rejects_invalid_ai_json(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢茶。",
        type="emotional",
        importance=7,
    )
    fake_llm.review_revision_content = "不是 JSON"

    response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "改一下"},
    )

    assert response.status_code == 502


def test_review_revision_scopes_memory_ids_to_current_user(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢茶。",
        type="emotional",
        importance=7,
    )

    response = client.post(
        "/memories/review/revise/preview",
        headers={**auth_headers, "X-User-Id": "other"},
        json={"memory_ids": [memory.id], "user_note": "改一下"},
    )

    assert response.status_code == 404


def test_unanchored_age_is_saved_with_time_anchor_and_review_after(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "action": "create",
            "memory": "用户 18 岁。",
            "type": "semantic",
            "importance": 7,
            "confidence": 0.95,
            "stability": "stable",
            "valid_until": None,
            "review_after": None,
            "sensitivity": "normal",
            "reason": "用户明确说明年龄。",
            "source_quote": "我现在18岁",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={"text": "我现在18岁。"},
    )

    assert response.status_code == 200
    [memory] = memory_store.list_memories(user_id="default")
    assert memory.content.startswith("截至 ")
    assert memory.content.endswith("用户自称 18 岁。")
    assert memory.confidence == 0.85
    assert memory.stability == "medium"
    assert memory.review_after is not None
    assert _parse_iso_datetime(memory.review_after) is not None


def test_birth_year_age_statement_is_not_rewritten(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "action": "create",
            "memory": "用户出生于 2008 年。",
            "type": "semantic",
            "importance": 7,
            "confidence": 0.9,
            "stability": "stable",
            "valid_until": None,
            "review_after": None,
            "sensitivity": "normal",
            "reason": "用户明确说明出生年份。",
            "source_quote": "我 2008 年出生",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={"text": "我 2008 年出生，现在18岁。"},
    )

    assert response.status_code == 200
    [memory] = memory_store.list_memories(user_id="default")
    assert memory.content == "用户出生于 2008 年。"
    assert memory.review_after is None


def test_apply_revision_redacts_sensitive_content_in_decision_log(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢深烘咖啡。",
    )
    fake_llm.review_revision_content = json.dumps(
        {
            "operations": [
                {
                    "operation": "update",
                    "memory_ids": [memory.id],
                    "target_memory_id": memory.id,
                    "content": "用户的邮箱密码是 TopSecret-12345。",
                    "type": "semantic",
                    "reason": "用户补充了账号信息。",
                },
            ]
        },
        ensure_ascii=False,
    )

    preview_response = client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": [memory.id], "user_note": "补充账号信息"},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()

    apply_response = client.post(
        "/memories/review/revise/apply",
        headers=auth_headers,
        json={
            "memory_ids": [memory.id],
            "operations": preview["operations"],
            "preview_token": preview["preview_token"],
        },
    )
    assert apply_response.status_code == 200

    logs = memory_store.list_decision_logs(user_id="default")
    review_logs = [
        log for log in logs if '"source": "review_modify"' in log.candidate_json
    ]
    assert review_logs
    for log in review_logs:
        assert "TopSecret-12345" not in log.candidate_json
        assert '"redacted": true' in log.candidate_json
