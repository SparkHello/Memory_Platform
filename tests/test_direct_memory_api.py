"""测试新增的 3 个 REST 端点：POST /memories, POST /memories/forget, POST /memories/context"""

from app.memory.store import MemoryStore


class TestSaveMemoryEndpoint:
    """POST /memories — 直接保存结构化记忆"""

    def test_create_new_memory(self, client, auth_headers, memory_store: MemoryStore):
        response = client.post(
            "/memories",
            json={
                "content": "用户使用 Windows 11 + WSL2 作为开发环境",
                "type": "fact",
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

    def test_create_and_deduplicate(self, client, auth_headers, memory_store: MemoryStore):
        payload = {
            "content": "用户偏爱深色主题的编辑器",
            "type": "preference",
            "importance": 6,
            "confidence": 0.9,
            "source_quote": "我喜欢深色主题",
        }
        first = client.post("/memories", json=payload, headers=auth_headers)
        assert first.json()["action"] == "create"

        second = client.post("/memories", json=payload, headers=auth_headers)
        assert second.json()["action"] == "ignore"

    def test_reject_low_importance(self, client, auth_headers):
        response = client.post(
            "/memories",
            json={
                "content": "用户今天有点困",
                "type": "fact",
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
                "type": "fact",
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
                "type": "preference",
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
                "type": "fact",
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
                "type": "preference",
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
                "type": "preference",
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
                "type": "fact",
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
            type="preference",
            importance=7,
            confidence=0.9,
        )
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢喝茶",
            type="preference",
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
            type="preference",
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
                type="preference",
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

    def test_json_format_returns_structured_data(self, client, auth_headers, memory_store: MemoryStore):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="preference",
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
            type="preference",
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
            type="preference",
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
            type="preference",
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
