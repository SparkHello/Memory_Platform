"""记忆提取与解析的行为测试。

覆盖保存门槛（importance / confidence / source_quote / 假设场景）、
去重与更新逻辑，以及 memory_decision_logs 的记录行为。
"""

import json

from fastapi.testclient import TestClient

from app.memory.store import MemoryStore


def _extraction_json(**overrides) -> str:
    data = {
        "action": "create",
        "memory": "用户使用 iPhone。",
        "type": "semantic",
        "importance": 8,
        "confidence": 0.9,
        "reason": "用户明确表达的长期事实",
        "source_quote": "我现在用 iPhone",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _post_ingest(
    client: TestClient,
    auth_headers: dict[str, str],
    content: str,
    conversation_id: str | None = None,
):
    payload: dict = {"text": content}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return client.post("/memories/ingest", headers=auth_headers, json=payload)


def _space_names_for(memory_store: MemoryStore, space_ids: list[str]) -> list[str]:
    spaces = {
        space.id: space.name
        for space in memory_store.list_memory_spaces(user_id="default")
    }
    return [spaces[space_id] for space_id in space_ids]


def test_low_importance_memory_is_not_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户今天有点困。",
        importance=3,
        source_quote="我今天有点困",
    )

    response = _post_ingest(client, auth_headers, "我今天有点困。")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert len(logs) == 1
    assert logs[0].decision == "ignore"
    assert "importance" in logs[0].reason


def test_hypothetical_scenario_is_not_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    # 即使提取模型给出了高分，代码层也必须拦下假设场景
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 Mac。",
        importance=8,
        confidence=0.9,
        source_quote="我以后用 Mac",
    )

    response = _post_ingest(client, auth_headers, "如果我以后用 Mac，应该怎么配置？")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "假设场景" in logs[0].reason


def test_fabricated_source_quote_is_not_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(source_quote="我是 Mac 重度用户")

    response = _post_ingest(client, auth_headers, "帮我推荐一台笔记本。")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "source_quote" in logs[0].reason


def test_sensitive_memory_requires_explicit_memory_request(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户有一项健康隐私。",
        type="semantic",
        importance=8,
        confidence=0.95,
        sensitivity="sensitive",
        source_quote="我有一项健康隐私",
    )

    response = _post_ingest(client, auth_headers, "我有一项健康隐私。")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "敏感信息" in logs[0].reason or "隐私" in logs[0].reason


def test_llm_topics_entities_are_saved_and_normalized(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户现在主要用 Kelivo 做 AI 客户端。",
        source_quote="我现在主要用 Kelivo 做 AI 客户端",
        temporal_subject="用户",
        temporal_predicate="primary_ai_client",
        topics=[" AI 客户端 ", "AI 客户端", "工具"],
        entities=[" Kelivo ", "Kelivo"],
    )

    response = _post_ingest(
        client,
        auth_headers,
        "我现在主要用 Kelivo 做 AI 客户端。",
    )

    assert response.status_code == 200
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.topics.count("AI 客户端") == 1
    assert "工具" in memory.topics
    assert memory.entities.count("Kelivo") == 1
    assert "工具与设备" in _space_names_for(memory_store, memory.space_ids)


def test_rule_fallback_classifies_memory_without_llm_labels(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户喜欢黑咖啡。",
        type="emotional",
        source_quote="我喜欢黑咖啡",
    )

    response = _post_ingest(client, auth_headers, "我喜欢黑咖啡。")

    assert response.status_code == 200
    memory = memory_store.list_memories(user_id="default")[0]
    assert "偏好" in memory.topics
    assert "饮食" in memory.topics
    assert "个人偏好" in _space_names_for(memory_store, memory.space_ids)


def test_sensitive_memory_drops_detailed_auto_entities(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户有一项证件信息。",
        importance=8,
        confidence=0.95,
        sensitivity="sensitive",
        source_quote="记住，我的身份证号是 123456",
        topics=["证件"],
        entities=["123456"],
    )

    response = _post_ingest(
        client,
        auth_headers,
        "记住，我的身份证号是 123456。",
    )

    assert response.status_code == 200
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.topics == ["私密信息"]
    assert memory.entities == []
    assert _space_names_for(memory_store, memory.space_ids) == ["私密信息"]


def test_similar_memory_is_not_duplicated(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
    )
    fake_llm.extraction_content = _extraction_json(
        memory="用户喜欢黑咖啡。",
        type="emotional",
        source_quote="我喜欢黑咖啡",
    )

    response = _post_ingest(client, auth_headers, "我喜欢黑咖啡。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "相同记忆" in logs[0].reason


def test_new_detail_creates_related_memory_without_overwriting(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户使用 iPhone。",
        type="semantic",
        importance=7,
        confidence=0.9,
    )
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。",
        source_quote="我现在用 iPhone 和 Kelivo 做 AI 客户端",
    )

    response = _post_ingest(client, auth_headers, "我现在用 iPhone 和 Kelivo 做 AI 客户端。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 2
    original = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert original is not None
    assert original.content == "用户使用 iPhone。"
    assert any("Kelivo" in memory.content for memory in memories if memory.id != old.id)
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "create"
    assert "暂不自动合并" in logs[0].reason


def test_conflicting_memory_creates_related_memory_when_explicit(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户的 AI 客户端是 Kelivo。",
        type="semantic",
        importance=7,
        confidence=0.9,
    )

    # 只是猜测（confidence 低）：不允许覆盖旧记忆
    fake_llm.extraction_content = _extraction_json(
        memory="用户的 AI 客户端是 ChatWise。",
        confidence=0.5,
        source_quote="我可能会换 ChatWise",
    )
    response = _post_ingest(client, auth_headers, "我可能会换 ChatWise 吧，还没想好。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert memories[0].content == "用户的 AI 客户端是 Kelivo。"
    assert memory_store.list_decision_logs()[0].decision == "ignore"

    # 用户明确表达了新事实：保留旧时间线，新建并交给体检建议确认
    fake_llm.extraction_content = _extraction_json(
        memory="用户的 AI 客户端是 ChatWise。",
        confidence=0.95,
        source_quote="我已经换成 ChatWise 了",
    )
    response = _post_ingest(client, auth_headers, "我已经换成 ChatWise 了。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 2
    original = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert original is not None
    assert original.content == "用户的 AI 客户端是 Kelivo。"
    assert any("ChatWise" in memory.content for memory in memories if memory.id != old.id)
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "create"
    assert "暂不自动合并" in logs[0].reason


def test_invalid_extractor_json_does_not_break_chat(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = "抱歉，我没法输出你要的格式。"

    response = _post_ingest(client, auth_headers, "随便聊聊天气吧。")

    assert response.status_code == 200
    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "JSON" in logs[0].reason


def test_rest_ingest_splits_raw_text(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "用户喜欢黑咖啡。",
                    "type": "emotional",
                    "importance": 7,
                    "confidence": 0.9,
                    "stability": "stable",
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "reason": "用户明确表达长期偏好",
                    "source_quote": "我喜欢黑咖啡",
                },
                {
                    "action": "create",
                    "memory": "用户使用 iPhone。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.9,
                    "stability": "stable",
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "reason": "用户明确描述设备",
                    "source_quote": "我现在用 iPhone",
                },
            ],
            "reason": "拆分出两条长期信息",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={
            "text": "我喜欢黑咖啡，另外我现在用 iPhone。",
            "conversation_id": "rest-conv",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 2
    assert payload["ignored"] == 0
    assert len(memory_store.list_memories(user_id="default")) == 2
    logs = memory_store.list_decision_logs(conversation_id="rest-conv")
    assert len(logs) == 2
    assert {json.loads(log.candidate_json)["source"] for log in logs} == {"rest_ingest"}


def test_rest_ingest_persists_temporal_fields(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "User works at Company B.",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.9,
                    "stability": "medium",
                    "valid_from": "2026-01-01",
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "temporal_subject": " user ",
                    "temporal_predicate": " current_employer ",
                    "reason": "User explicitly described the current employer.",
                    "source_quote": "I now work at Company B",
                }
            ],
            "reason": "temporal fact",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={
            "text": "I now work at Company B",
            "conversation_id": "temporal-ingest",
        },
    )

    assert response.status_code == 200
    assert response.json()["created"] == 1
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.valid_from == "2026-01-01"
    assert memory.temporal_subject == "user"
    assert memory.temporal_predicate == "current_employer"


def test_ingest_autofills_whitelisted_temporal_profile_key(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "用户现在主要用 Kelivo 作为 AI 客户端。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.9,
                    "valence": 0.5,
                    "arousal": 0.3,
                    "stability": "medium",
                    "valid_from": None,
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "temporal_subject": None,
                    "temporal_predicate": None,
                    "reason": "User described the current AI client.",
                    "source_quote": "我现在主要用 Kelivo 做 AI 客户端",
                }
            ],
            "reason": "profile slot",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={
            "text": "我现在主要用 Kelivo 做 AI 客户端",
            "conversation_id": "temporal-profile-slot",
        },
    )

    assert response.status_code == 200
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.temporal_subject == "用户"
    assert memory.temporal_predicate == "primary_ai_client"


def test_ingest_clears_non_whitelisted_temporal_key_and_hints_emotional(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "用户喜欢黑咖啡。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.8,
                    "valence": 0.75,
                    "arousal": 0.35,
                    "stability": "stable",
                    "valid_from": None,
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "temporal_subject": "用户",
                    "temporal_predicate": "favorite_coffee",
                    "reason": "User stated a preference.",
                    "source_quote": "我喜欢黑咖啡",
                }
            ],
            "reason": "preference",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={"text": "我喜欢黑咖啡", "conversation_id": "type-hint"},
    )

    assert response.status_code == 200
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.type == "emotional"
    assert memory.temporal_subject is None
    assert memory.temporal_predicate is None


def test_decision_logs_record_create_related_create_ignore(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    # 第一轮：全新信息 -> create
    fake_llm.extraction_content = _extraction_json()
    _post_ingest(client, auth_headers, "我现在用 iPhone。", conversation_id="conv-create")

    # 第二轮：补充细节 -> create，并提示人工确认
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。",
        source_quote="我现在用 iPhone 和 Kelivo 做 AI 客户端",
    )
    _post_ingest(
        client,
        auth_headers,
        "我现在用 iPhone 和 Kelivo 做 AI 客户端。",
        conversation_id="conv-update",
    )

    # 第三轮：原样重复 -> ignore
    _post_ingest(
        client,
        auth_headers,
        "我现在用 iPhone 和 Kelivo 做 AI 客户端。",
        conversation_id="conv-ignore",
    )

    logs = memory_store.list_decision_logs()
    decisions = {log.conversation_id: log.decision for log in logs}
    assert decisions == {
        "conv-create": "create",
        "conv-update": "create",
        "conv-ignore": "ignore",
    }
    # candidate_json 必须是可回放的合法 JSON，方便调试
    for log in logs:
        parsed = json.loads(log.candidate_json)
        assert parsed["action"] in {"create", "update", "ignore"}

    # 按 conversation_id 过滤
    filtered = memory_store.list_decision_logs(conversation_id="conv-create")
    assert len(filtered) == 1
    assert filtered[0].decision == "create"


def test_type_specific_threshold_reflective_lower():
    """reflective 类型: importance=5, confidence=0.80 即可通过。"""
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    c = CandidateMemory(
        action="create",
        memory="用户认识张三",
        type="reflective",
        importance=5,
        confidence=0.80,
        reason="",
        source_quote="我认识张三",
    )
    rejection = validate_candidate_for_save(c, user_message="我认识张三", require_quote_in_user_message=True)
    assert rejection is None


def test_type_specific_threshold_reflective_rejects_below_min():
    """reflective 类型: importance=4 (<5) 应被拒绝。"""
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    c = CandidateMemory(
        action="create",
        memory="用户认识张三",
        type="reflective",
        importance=4,
        confidence=0.90,
        reason="",
        source_quote="我认识张三",
    )
    rejection = validate_candidate_for_save(c, user_message="我认识张三", require_quote_in_user_message=True)
    assert rejection is not None
    assert "importance" in rejection
    assert "4" in rejection


def test_type_specific_threshold_emotional_accepts_standard_confidence():
    """emotional 类型: importance=5, confidence=0.80 即可通过。"""
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    c = CandidateMemory(
        action="create",
        memory="用户喜欢摇滚乐",
        type="emotional",
        importance=5,
        confidence=0.80,
        reason="",
        source_quote="我喜欢摇滚乐",
    )
    rejection = validate_candidate_for_save(c, user_message="我喜欢摇滚乐", require_quote_in_user_message=True)
    assert rejection is None


def test_sector_hints_promote_obvious_reflection() -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="用户发现先收口 P0 再扩展更适合这个项目。",
        type="semantic",
        importance=7,
        confidence=0.9,
        source_quote="我发现先收口 P0 再扩展更适合这个项目",
    )

    hinted = apply_extraction_hints(
        candidate,
        source_text="我发现先收口 P0 再扩展更适合这个项目",
    )

    assert hinted.type == "reflective"


def test_temporal_profile_hint_accepts_present_state_without_now_marker() -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="用户住在上海。",
        type="semantic",
        importance=7,
        confidence=0.9,
        source_quote="我住在上海",
    )

    hinted = apply_extraction_hints(candidate, source_text="我住在上海")

    assert hinted.temporal_subject == "用户"
    assert hinted.temporal_predicate == "current_city"


def test_type_specific_threshold_semantic_default():
    """semantic 类型保持 threshold: importance=5 (<6) 应被拒。"""
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    c = CandidateMemory(
        action="create",
        memory="用户使用了 iPhone",
        type="semantic",
        importance=5,
        confidence=0.90,
        reason="",
        source_quote="我用 iPhone",
    )
    rejection = validate_candidate_for_save(c, user_message="我用 iPhone", require_quote_in_user_message=True)
    assert rejection is not None
    assert "importance" in rejection
    assert "6" in rejection


def test_type_specific_threshold_unknown_type_falls_back():
    """未注册的类型使用默认 MIN_IMPORTANCE/MIN_CONFIDENCE。"""
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    # 直接构造 import 触发 fallback
    c = CandidateMemory(
        action="create",
        memory="测试回退",
        type="semantic",
        importance=5,
        confidence=0.80,
        reason="",
        source_quote="测试回退",
    )
    rejection = validate_candidate_for_save(c, user_message="测试回退", require_quote_in_user_message=True)
    # semantic 类型 importance=5 < 6，应被拒
    assert rejection is not None

