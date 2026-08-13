"""测试新增的 REST 端点：记忆保存、遗忘、上下文和召回解释。"""

import json

from app.config import Settings, get_settings
from app.llm.embedding_contract import resolve_embedding_contract
from app.memory.store import MemoryStore


class TestSaveMemoryEndpoint:
    """POST /memories — 直接保存结构化记忆"""

    def test_create_new_memory(self, client, auth_headers, memory_store: MemoryStore):
        response = client.post(
            "/memories",
            json={
                "content": "用户使用 Windows 11 + WSL2 作为开发环境",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.95,
                "stability": "stable",
                "sensitivity": "normal",
                "source_quote": "我平时用 Windows 11 + WSL2 开发",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "create"
        assert data["memory_id"] is not None

        memory = memory_store.get_memory(memory_id=data["memory_id"], user_id="default")
        assert memory is not None
        assert "Windows 11" in memory.content
        assert memory.importance == 7
        assert memory.confidence == 0.95

    def test_explicit_classification_is_preserved(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        response = client.post(
            "/memories",
            json={
                "content": "用户使用 Kelivo 和 Windows 作为测试环境",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.95,
                "source_quote": "我用 Kelivo 和 Windows 测试",
                "topics": [" 自定义标签 ", "自定义标签"],
                "entities": [" CustomEntity ", "CustomEntity"],
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "create"
        memory = memory_store.get_memory(memory_id=data["memory_id"], user_id="default")
        assert memory is not None
        assert memory.topics == ["自定义标签"]
        assert memory.entities == ["CustomEntity"]
        assert memory.space_ids == []

    def test_create_and_deduplicate(self, client, auth_headers, memory_store: MemoryStore):
        payload = {
            "content": "用户偏爱深色主题的编辑器",
            "type": "emotional",
            "importance": 6,
            "confidence": 0.9,
            "source_quote": "我喜欢深色主题",
        }
        first = client.post("/memories", json=payload, headers=auth_headers)
        assert first.json()["action"] == "create"

        second = client.post("/memories", json=payload, headers=auth_headers)
        assert second.json()["action"] == "ignore"

    def test_local_sensitivity_floor_applies_before_direct_save_gate(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        identifier = "123456789012345678"
        base_payload = {
            "content": f"用户的身份证号是 {identifier}。",
            "type": "semantic",
            "importance": 8,
            "confidence": 0.95,
            "sensitivity": "normal",
        }

        rejected = client.post(
            "/memories",
            json={
                **base_payload,
                "source_quote": f"我的身份证号是 {identifier}",
            },
            headers=auth_headers,
        )

        assert rejected.status_code == 200
        assert rejected.json()["action"] == "ignore"
        assert "明确要求记住" in rejected.json()["reason"]

        accepted = client.post(
            "/memories",
            json={
                **base_payload,
                "source_quote": f"请记住，我的身份证号是 {identifier}",
            },
            headers=auth_headers,
        )

        assert accepted.json()["action"] == "create"
        memory = memory_store.get_memory(
            memory_id=accepted.json()["memory_id"],
            user_id="default",
        )
        assert memory is not None
        assert memory.sensitivity == "sensitive"

    def test_temporal_fields_timeline_and_restore(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        old = client.post(
            "/memories",
            json={
                "content": "User uses Tool A as the primary notes app.",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.95,
                "stability": "medium",
                "source_quote": "I use Tool A",
                "valid_from": "2025-01-01",
                "temporal_subject": "user",
                "temporal_predicate": "primary_notes_app",
            },
            headers=auth_headers,
        )
        assert old.status_code == 200
        old_id = old.json()["memory_id"]

        new = client.post(
            "/memories",
            json={
                "content": "User uses Tool B as the primary notes app.",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.95,
                "stability": "medium",
                "source_quote": "I use Tool B",
                "valid_from": "2026-01-01",
                "temporal_subject": " user ",
                "temporal_predicate": " primary_notes_app ",
            },
            headers=auth_headers,
        )
        assert new.status_code == 200
        new_id = new.json()["memory_id"]

        old_record = memory_store.get_memory(memory_id=old_id, user_id="default")
        assert old_record is not None
        assert old_record.valid_until == "2026-01-01"
        assert old_record.superseded_by == new_id

        timeline = client.get(
            "/memories/timeline",
            params={"subject": "user", "predicate": "primary_notes_app"},
            headers=auth_headers,
        )
        assert timeline.status_code == 200
        assert [memory["id"] for memory in timeline.json()["data"]] == [old_id, new_id]

        restored = client.post(
            f"/memories/{old_id}/temporal/restore",
            headers=auth_headers,
        )
        assert restored.status_code == 200
        restored_memory = restored.json()["memory"]
        restored_id = restored_memory["id"]
        assert restored_id not in {old_id, new_id}
        assert restored_memory["valid_until"] is None
        assert restored_memory["status"] == "dynamic"
        assert restored_memory["supersedes"] == new_id

        old_after = memory_store.get_memory(memory_id=old_id, user_id="default")
        new_after = memory_store.get_memory(memory_id=new_id, user_id="default")
        assert old_after is not None
        assert new_after is not None
        assert old_after.valid_until == "2026-01-01"
        assert old_after.superseded_by == new_id
        assert new_after.supersedes == old_id
        assert new_after.valid_until == restored_memory["valid_from"]
        assert new_after.superseded_by == restored_id

    def test_patch_temporal_key_omits_old_synthesized_boundary(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        old = memory_store.create_memory(
            user_id="default",
            content="User lives in City A.",
            valid_from="2025-01-01",
            temporal_subject="user",
            temporal_predicate="current_city",
        )
        latest = memory_store.create_memory(
            user_id="default",
            content="User lives in City B.",
            valid_from="2026-01-01",
            temporal_subject="user",
            temporal_predicate="current_city",
        )
        assert memory_store.get_memory(
            memory_id=old.id,
            user_id="default",
        ).valid_until == latest.valid_from

        response = client.patch(
            f"/memories/{old.id}",
            headers=auth_headers,
            json={"temporal_subject": "former_user"},
        )

        assert response.status_code == 200
        moved = memory_store.get_memory(memory_id=old.id, user_id="default")
        latest_after = memory_store.get_memory(
            memory_id=latest.id,
            user_id="default",
        )
        assert moved is not None
        assert latest_after is not None
        assert moved.temporal_subject == "former_user"
        assert moved.valid_until is None
        assert moved.status == "dynamic"
        assert moved.supersedes is None
        assert moved.superseded_by is None
        assert latest_after.supersedes is None
        assert latest_after.valid_until is None
        assert latest_after.status == "dynamic"

    def test_reject_low_importance(self, client, auth_headers):
        response = client.post(
            "/memories",
            json={
                "content": "用户今天有点困",
                "type": "semantic",
                "importance": 3,
                "confidence": 0.95,
                "source_quote": "今天有点困",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["action"] == "ignore"
        assert "importance" in response.json()["reason"].lower()

    def test_reject_low_confidence(self, client, auth_headers):
        response = client.post(
            "/memories",
            json={
                "content": "用户可能喜欢 Rust",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.5,
                "source_quote": "也许我该学 Rust",
            },
            headers=auth_headers,
        )
        assert response.json()["action"] == "ignore"
        assert "confidence" in response.json()["reason"].lower()

    def test_reject_empty_source_quote(self, client, auth_headers):
        response = client.post(
            "/memories",
            json={
                "content": "用户喜欢咖啡",
                "type": "emotional",
                "importance": 7,
                "confidence": 0.9,
                "source_quote": "",
            },
            headers=auth_headers,
        )
        assert response.json()["action"] == "ignore"
        assert "source_quote" in response.json()["reason"].lower()

    def test_reject_assumption_scenario(self, client, auth_headers):
        response = client.post(
            "/memories",
            json={
                "content": "用户使用 Mac",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.9,
                "source_quote": "如果我以后用 Mac 的话",
            },
            headers=auth_headers,
        )
        assert response.json()["action"] == "ignore"
        assert "假设" in response.json()["reason"]

    def test_update_existing_memory_same_topic(self, client, auth_headers, memory_store: MemoryStore):
        # 先创建一条偏好记忆
        client.post(
            "/memories",
            json={
                "content": "用户喜欢黑咖啡",
                "type": "emotional",
                "importance": 7,
                "confidence": 0.9,
                "source_quote": "我喜欢黑咖啡",
            },
            headers=auth_headers,
        )
        # 提交更完整的同主题信息
        response = client.post(
            "/memories",
            json={
                "content": "用户喜欢黑咖啡，不加糖不加奶，每天早上喝一杯",
                "type": "emotional",
                "importance": 8,
                "confidence": 0.95,
                "source_quote": "我每天早上喝一杯黑咖啡，不加糖不加奶",
            },
            headers=auth_headers,
        )
        data = response.json()
        assert data["action"] in ("update", "create")

    def test_requires_auth(self, client):
        response = client.post(
            "/memories",
            json={
                "content": "test",
                "type": "semantic",
                "importance": 7,
                "confidence": 0.9,
                "source_quote": "test",
            },
        )
        assert response.status_code == 401


class TestForgetMemoriesEndpoint:
    """POST /memories/forget — 按自然语言批量软删除"""

    def test_forget_matching_memories(self, client, auth_headers, memory_store: MemoryStore):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=7,
            confidence=0.9,
        )
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢喝茶",
            type="emotional",
            importance=7,
            confidence=0.9,
        )
        response = client.post(
            "/memories/forget",
            json={"query": "咖啡", "limit": 5},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] >= 1
        assert any("咖啡" in item["content"] for item in data["deleted"])

        # 确认咖啡记忆已归档
        coffee_deleted = memory_store.list_archived_memories(user_id="default")
        assert any("咖啡" in m.content for m in coffee_deleted)

    def test_forget_empty_query(self, client, auth_headers):
        response = client.post(
            "/memories/forget",
            json={"query": "", "limit": 5},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0

    def test_forget_no_match(self, client, auth_headers, memory_store: MemoryStore):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=7,
            confidence=0.9,
        )
        response = client.post(
            "/memories/forget",
            json={"query": "潜水艇", "limit": 5},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 0

    def test_forget_respects_limit(self, client, auth_headers, memory_store: MemoryStore):
        for i in range(5):
            memory_store.create_memory(
                user_id="default",
                content=f"用户喜欢咖啡品牌{i}",
                type="emotional",
                importance=7,
                confidence=0.9,
            )
        response = client.post(
            "/memories/forget",
            json={"query": "咖啡", "limit": 2},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["deleted_count"] <= 2


class TestMemoryContextEndpoint:
    """POST /memories/context — 一站式上下文检索"""

    def test_recent_context_upsert_endpoint_and_limit_validation(self, client, auth_headers):
        created = client.post(
            "/memories/recent-context",
            json={
                "conversation_id": "ctx-rest",
                "summary": "  用户：聊咖啡\n助手：继续整理  ",
            },
            headers=auth_headers,
        )
        assert created.status_code == 200
        assert created.json()["data"]["summary"] == "用户：聊咖啡\n助手：继续整理"

        updated = client.post(
            "/memories/recent-context",
            json={"conversation_id": "ctx-rest", "summary": "用户：聊早餐"},
            headers=auth_headers,
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["id"] == created.json()["data"]["id"]
        assert updated.json()["data"]["summary"] == "用户：聊早餐"

        listed = client.get("/memories/recent-context?limit=1", headers=auth_headers)
        assert listed.status_code == 200
        assert len(listed.json()["data"]) == 1

        invalid_limit = client.get("/memories/recent-context?limit=0", headers=auth_headers)
        assert invalid_limit.status_code == 422

    def test_json_format_returns_structured_data(self, client, auth_headers, memory_store: MemoryStore):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=7,
            confidence=0.9,
        )
        response = client.post(
            "/memories/context",
            json={
                "query": "咖啡",
                "include_core_memory": True,
                "include_recent_context": True,
                "format": "json",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "core_memory" in data
        assert "search_results" in data
        assert "recent_context" in data
        assert isinstance(data["core_memory"], list)
        assert isinstance(data["search_results"], list)
        assert isinstance(data["recent_context"], dict)

    def test_markdown_format_returns_text(self, client, auth_headers, memory_store: MemoryStore):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=7,
            confidence=0.9,
        )
        response = client.post(
            "/memories/context",
            json={
                "query": "咖啡",
                "include_core_memory": True,
                "include_recent_context": True,
                "format": "markdown",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert "text/markdown" in response.headers["content-type"]
        assert "咖啡" in response.text

    def test_empty_query_returns_only_core_and_recent(self, client, auth_headers):
        response = client.post(
            "/memories/context",
            json={
                "query": "",
                "include_core_memory": True,
                "include_recent_context": True,
                "format": "json",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["search_results"] == []

    def test_skip_recent_context(self, client, auth_headers, memory_store: MemoryStore):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=7,
            confidence=0.9,
        )
        response = client.post(
            "/memories/context",
            json={
                "query": "咖啡",
                "include_core_memory": True,
                "include_recent_context": False,
                "format": "json",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["recent_context"]["found"] is False

    def test_context_with_core_memory_populated(self, client, auth_headers, memory_store: MemoryStore):
        # 先创建记忆
        mem = memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
        )
        # 手动创建核心记忆分区
        memory_store.upsert_core_memory_section(
            user_id="default",
            section="preferences",
            content="- 喜欢黑咖啡",
            evidence_memory_ids=[mem.id],
            confidence=0.9,
        )
        response = client.post(
            "/memories/context",
            json={
                "query": "咖啡",
                "include_core_memory": True,
                "include_recent_context": False,
                "format": "json",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["core_memory"]) >= 1
        assert any(s["section"] == "preferences" for s in data["core_memory"])

    def test_recent_context_from_conversation(self, client, auth_headers, memory_store: MemoryStore):
        memory = memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
        )
        memory_store.upsert_recent_context_summary(
            user_id="default",
            conversation_id="test-conv",
            summary="用户：我喜欢黑咖啡\n助手：好的，记住了",
        )
        response = client.post(
            "/memories/context",
            json={
                "query": "",
                "include_core_memory": True,
                "include_recent_context": True,
                "conversation_id": "test-conv",
                "format": "json",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["recent_context"]["found"] is True
        assert "黑咖啡" in data["recent_context"]["summary"]
        assert [item["id"] for item in data["search_results"]] == [memory.id]

    def test_empty_query_uses_latest_recent_context_when_conversation_is_omitted(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        memory = memory_store.create_memory(
            user_id="default",
            content="用户喜欢浅烘咖啡豆",
            type="emotional",
            importance=8,
            confidence=0.95,
        )
        memory_store.upsert_recent_context_summary(
            user_id="default",
            conversation_id="latest-conv",
            summary="用户：继续聊浅烘咖啡豆\n助手：可以继续",
        )

        response = client.post(
            "/memories/context",
            json={
                "query": "",
                "include_core_memory": True,
                "include_recent_context": True,
                "format": "json",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["recent_context"]["found"] is True
        assert data["search_results"][0]["id"] == memory.id


class TestMemoryContextExplainEndpoint:
    """POST /memories/context/explain — 调试一次上下文召回"""

    def test_explain_returns_context_package_and_does_not_record_usage(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        memory = memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
        )
        memory_store.upsert_core_memory_section(
            user_id="default",
            section="preferences",
            content="- 喜欢黑咖啡",
            evidence_memory_ids=[memory.id],
            confidence=0.9,
        )
        memory_store.upsert_recent_context_summary(
            user_id="default",
            conversation_id="coffee-conv",
            summary="用户：最近在比较不同咖啡豆",
        )

        response = client.post(
            "/memories/context/explain",
            json={
                "query": "咖啡",
                "include_core_memory": True,
                "include_recent_context": True,
                "limit": 1,
                "conversation_id": "coffee-conv",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert set(data) >= {
            "context_package",
            "core_memory",
            "search_results",
            "recent_context",
            "candidate_pool",
            "excluded_candidates",
        }
        assert data["context_package"]["query"] == "咖啡"
        assert data["core_memory"][0]["section"] == "preferences"
        assert data["recent_context"]["found"] is True
        assert data["search_results"][0]["id"] == memory.id
        assert data["search_results"][0]["score_breakdown"]["keyword_score"] > 0

        refreshed = memory_store.get_memory(memory_id=memory.id, user_id="default")
        assert refreshed is not None
        assert refreshed.usage_count == 0
        assert refreshed.last_used_at is None

    def test_context_excludes_legacy_sensitive_core_section(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        source = memory_store.create_memory(
            user_id="default",
            content="用户长期喜欢黑咖啡。",
            importance=8,
        )
        secret = "123456789012345678"
        memory_store.upsert_core_memory_section(
            user_id="default",
            section="profile",
            content=f"- 用户的身份证号是 {secret}。",
            evidence_memory_ids=[source.id],
            confidence=0.95,
        )

        response = client.post(
            "/memories/context",
            json={"query": "咖啡", "include_core_memory": True},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["core_memory"] == []
        assert secret not in response.text

    def test_explain_reports_candidates_excluded_by_limit(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        for index in range(3):
            memory_store.create_memory(
                user_id="default",
                content=f"用户喜欢咖啡口味 {index}",
                type="emotional",
                importance=5 + index,
            )

        response = client.post(
            "/memories/context/explain",
            json={"query": "咖啡", "limit": 1},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["search_results"]) == 1
        assert len(data["candidate_pool"]) >= 2
        assert data["excluded_candidates"]
        assert data["excluded_candidates"][0]["excluded_reason"] == "rank_below_limit"

    def test_explain_redacts_sensitive_search_results(
        self,
        client,
        auth_headers,
        memory_store: MemoryStore,
    ):
        private = memory_store.create_memory(
            user_id="default",
            content="User passport number is PA-12345.",
            source_message="My passport number is PA-12345.",
            type="semantic",
            importance=9,
            sensitivity="sensitive",
        )

        response = client.post(
            "/memories/context/explain",
            json={
                "query": "passport",
                "limit": 1,
                "include_sensitive": True,
                "redact_sensitive": True,
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        search_result = data["search_results"][0]
        context_result = data["context_package"]["search_results"][0]
        candidate = data["candidate_pool"][0]
        assert search_result["id"] == private.id
        assert search_result["redacted"] is True
        assert search_result["content"] != private.content
        assert search_result["source_message"] != private.source_message
        assert context_result["redacted"] is True
        assert candidate["redacted"] is True

        stored = memory_store.get_memory(memory_id=private.id, user_id="default")
        assert stored is not None
        assert stored.content == private.content


class TestSearchFeedbackEndpoint:
    """POST /memories/search-feedback — 记录召回反馈到审计日志"""

    def test_feedback_writes_decision_log(self, client, auth_headers, memory_store: MemoryStore):
        memory = memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=7,
        )

        response = client.post(
            "/memories/search-feedback",
            json={
                "query": "咖啡",
                "memory_id": memory.id,
                "feedback": "useful",
                "note": "命中准确",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["recorded"] is True
        logs = memory_store.list_decision_logs(user_id="default", limit=1)
        assert len(logs) == 1
        payload = json.loads(logs[0].candidate_json)
        assert payload["source"] == "search_feedback"
        assert payload["query"] == "咖啡"
        assert payload["memory_id"] == memory.id
        assert payload["feedback"] == "useful"
        assert logs[0].decision == "ignore"
        assert "useful" in logs[0].reason

    def test_missing_feedback_allows_empty_memory_id(self, client, auth_headers):
        response = client.post(
            "/memories/search-feedback",
            json={"query": "咖啡", "feedback": "missing"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["recorded"] is True

    def test_non_missing_feedback_requires_memory_id(self, client, auth_headers):
        response = client.post(
            "/memories/search-feedback",
            json={"query": "咖啡", "feedback": "wrong"},
            headers=auth_headers,
        )

        assert response.status_code == 422

    def test_feedback_rejects_missing_memory(self, client, auth_headers):
        response = client.post(
            "/memories/search-feedback",
            json={
                "query": "咖啡",
                "memory_id": "does-not-exist",
                "feedback": "wrong",
            },
            headers=auth_headers,
        )

        assert response.status_code == 404

    def test_feedback_rejects_unknown_feedback_value(self, client, auth_headers):
        response = client.post(
            "/memories/search-feedback",
            json={"query": "咖啡", "feedback": "maybe"},
            headers=auth_headers,
        )

        assert response.status_code == 422


class TestReEmbedEndpoint:
    """POST /memories/re-embed"""

    def test_re_embed_rejects_without_embedding_config(self, client, auth_headers):
        """未配置 embedding 服务时应返回 400。"""
        response = client.post(
            "/memories/re-embed",
            json={"memory_ids": ["mem-test"]},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "未配置 embedding" in response.json()["detail"]

    def test_re_embed_requires_auth(self, client):
        """未认证时返回 401。"""
        response = client.post(
            "/memories/re-embed",
            json={"memory_ids": ["mem-test"]},
        )
        assert response.status_code == 401

    def test_re_embed_rejects_empty_memory_ids(self, client, auth_headers):
        """空 memory_ids 列表时先被 NullEmbeddingClient 拦截返回 400。"""
        response = client.post(
            "/memories/re-embed",
            json={"memory_ids": []},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_re_embed_rejects_neither_mode(self, client, auth_headers):
        """未指定模式时先被 NullEmbeddingClient 拦截返回 400。"""
        response = client.post(
            "/memories/re-embed",
            json={},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_re_embed_scan_no_embedding(self, client, auth_headers):
        """scan 模式无 embedding 配置时返回 400。"""
        response = client.post(
            "/memories/re-embed",
            json={"scan": True},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_re_embed_scan_with_fake_embedding(self, client, auth_headers, memory_store):
        """scan 模式重建维度正确但包含非有限数的损坏向量。"""
        import app.api.deps as deps

        damaged = memory_store.create_memory(
            user_id="default",
            content="需要重新生成向量的记忆",
            embedding_json="[NaN, 0.0, 0.0, 0.0]",
        )
        healthy = memory_store.create_memory(
            user_id="default",
            content="有效向量不应被扫描重建",
            embedding_json="[0.1, 0.2, 0.3, 0.4]",
            embedding_space_id="test-space",
        )

        class FakeEmbeddingClient:
            def __init__(self) -> None:
                self.dimensions = 4
                self.embedding_space_id = "test-space"

            async def embed(self, text: str) -> list[float]:
                return [0.1, 0.2, 0.3, 0.4]

        client.app.dependency_overrides[deps.get_embedding_client] = (
            lambda: FakeEmbeddingClient()
        )
        response = client.post(
            "/memories/re-embed",
            json={"scan": True},
            headers=auth_headers,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["re_embedded"] == 1
        assert payload["memory_ids"] == [damaged.id]
        assert healthy.id not in payload["memory_ids"]
        refreshed = memory_store.get_memory(
            memory_id=payload["memory_ids"][0],
            user_id="default",
        )
        assert refreshed is not None
        assert refreshed.embedding_space_id == "test-space"

    def test_re_embed_scan_selects_memories_without_space(self, client, auth_headers, memory_store):
        import app.api.deps as deps

        orphan = memory_store.create_memory(
            user_id="default",
            content="开启语义搜索前写下的记忆",
        )
        current = memory_store.create_memory(
            user_id="default",
            content="已在当前空间的记忆",
            embedding_json="[0.1, 0.2, 0.3, 0.4]",
            embedding_space_id="test-space",
        )

        class FakeEmbeddingClient:
            def __init__(self) -> None:
                self.dimensions = 4
                self.embedding_space_id = "test-space"

            async def embed(self, text: str) -> list[float]:
                return [0.5, 0.4, 0.3, 0.2]

        client.app.dependency_overrides[deps.get_embedding_client] = (
            lambda: FakeEmbeddingClient()
        )
        response = client.post(
            "/memories/re-embed",
            json={"scan": True},
            headers=auth_headers,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["re_embedded"] == 1
        assert payload["memory_ids"] == [orphan.id]
        assert current.id not in payload["memory_ids"]
        refreshed = memory_store.get_memory(memory_id=orphan.id, user_id="default")
        assert refreshed is not None
        assert refreshed.embedding_space_id == "test-space"


def test_memory_health_uses_authoritative_embedding_dimensions(
    client,
    auth_headers,
    memory_store,
) -> None:
    settings = Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        DATABASE_PATH=memory_store.database_path,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="backend-key",
        MODEL_GATEWAY_EMBEDDING_SPACE_ID="",
        EMBEDDING_DIMENSIONS=1024,
    )
    resolve_embedding_contract(
        settings,
        {
            "connections": [
                {"id": "embedding-channel", "enabled": True, "configured": True}
            ],
            "deployments": [
                {
                    "id": "embedding-primary",
                    "connection": "embedding-channel",
                    "kind": "embedding",
                    "enabled": True,
                    "dimensions": 4,
                    "embedding_space": "route-space",
                }
            ],
            "routes": [
                {
                    "id": "memory.embedding",
                    "kind": "embedding",
                    "enabled": True,
                    "targets": ["embedding-primary"],
                }
            ],
        },
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    memory = memory_store.create_memory(
        user_id="default",
        content="route 契约维度为四维",
        embedding_json="[0.1, 0.2, 0.3, 0.4]",
        embedding_space_id="route-space",
    )

    response = client.get("/memories/health", headers=auth_headers)

    assert response.status_code == 200
    mismatched_ids = {
        issue.get("related_id")
        for issue in response.json()["issues"]
        if issue["type"] == "embedding_dimension_mismatch"
    }
    assert memory.id not in mismatched_ids


class TestArchiveExpiredEndpoint:
    """POST /memories/archive-expired"""

    def test_empty_store_returns_zero(self, client, auth_headers, memory_store):
        response = client.post(
            "/memories/archive-expired",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["archived"] == 0

    def test_archives_expired_keeps_active(self, client, auth_headers, memory_store):
        from datetime import UTC, datetime, timedelta

        past = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()

        m1 = memory_store.create_memory(
            user_id="default",
            content="expired",
            type="semantic",
            valid_until=past,
        )
        m2 = memory_store.create_memory(
            user_id="default",
            content="future",
            type="semantic",
            valid_until=future,
        )
        m3 = memory_store.create_memory(
            user_id="default",
            content="no expiry",
            type="semantic",
        )

        response = client.post(
            "/memories/archive-expired",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["archived"] == 1

        # 验证 m1 已归档
        res = client.get(f"/memories/{m1.id}", headers=auth_headers)
        assert res.status_code == 404
        # m2,m3 仍可访问
        assert client.get(f"/memories/{m2.id}", headers=auth_headers).status_code == 200
        assert client.get(f"/memories/{m3.id}", headers=auth_headers).status_code == 200

    def test_idempotent(self, client, auth_headers, memory_store):
        from datetime import UTC, datetime, timedelta

        past = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        memory_store.create_memory(
            user_id="default", content="expired", type="semantic", valid_until=past,
        )

        r1 = client.post("/memories/archive-expired", headers=auth_headers)
        assert r1.json()["archived"] == 1

        r2 = client.post("/memories/archive-expired", headers=auth_headers)
        assert r2.json()["archived"] == 0

    def test_requires_auth(self, client):
        response = client.post("/memories/archive-expired")
        assert response.status_code == 401
