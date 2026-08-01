"""记忆提取与解析的行为测试。

覆盖保存门槛（importance / confidence / source_quote / 假设场景）、
去重与更新逻辑，以及 memory_decision_logs 的记录行为。
"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.memory.extractor import LLMMemoryExtractor
from app.memory.ingest import MemoryIngestService
from app.memory.search import NullEmbeddingClient
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


@pytest.mark.asyncio
async def test_sensitive_ingest_is_blocked_before_remote_extraction(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
    )

    result = await service.ingest(
        user_id="default",
        text="记住，我的身份证号是 123456789012345678。",
    )

    assert result.ignored == 1
    assert result.created == 0
    assert fake_llm.extraction_messages == []
    assert memory_store.list_memories(user_id="default") == []
    audit = json.loads(memory_store.list_decision_logs(user_id="default")[0].candidate_json)
    assert audit["sensitive_egress_blocked"] is True
    assert "123456789012345678" not in json.dumps(audit, ensure_ascii=False)


@pytest.mark.asyncio
async def test_sensitive_ingest_decision_log_keeps_only_hashes(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    source_quote = f"记住，我的身份证号是 {identifier}"
    memory_text = f"用户的身份证号是 {identifier}。"
    fake_llm.extraction_content = _extraction_json(
        memory=memory_text,
        importance=8,
        confidence=0.95,
        sensitivity="normal",
        source_quote=source_quote,
    )
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
        allow_sensitive_egress=True,
    )

    result = await service.ingest(user_id="default", text=f"{source_quote}。")

    assert result.created == 1
    log = memory_store.list_decision_logs(user_id="default")[0]
    audit = json.loads(log.candidate_json)
    assert audit["redacted"] is True
    assert audit["sensitivity"] == "sensitive"
    assert audit["memory_id"] == result.items[0].memory_id
    assert audit["memory_length"] == len(memory_text)
    assert audit["source_quote_length"] == len(source_quote)
    assert len(audit["memory_sha256"]) == 64
    assert len(audit["source_quote_sha256"]) == 64
    assert identifier not in log.candidate_json
    assert memory_text not in log.candidate_json
    assert source_quote not in log.candidate_json


@pytest.mark.asyncio
async def test_malformed_sensitive_llm_output_is_hashed_in_decision_log(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    fake_llm.extraction_content = f"not-json: 身份证号是 {identifier}"
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
        allow_sensitive_egress=True,
    )

    result = await service.ingest(
        user_id="default",
        text=f"请记住，我的身份证号是 {identifier}。",
    )

    assert result.ignored == 1
    log = memory_store.list_decision_logs(user_id="default")[0]
    audit = json.loads(log.candidate_json)
    assert audit["redacted"] is True
    assert audit["raw_output_length"] > 0
    assert len(audit["raw_output_sha256"]) == 64
    assert identifier not in log.candidate_json


@pytest.mark.asyncio
async def test_upstream_extraction_failure_is_retryable_not_ignored(
    memory_store: MemoryStore,
    fake_llm,
    monkeypatch,
) -> None:
    async def fail_upstream(*args, **kwargs):
        raise RuntimeError("temporary upstream failure")

    monkeypatch.setattr(fake_llm, "create_chat_completion", fail_upstream)
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
    )

    result = await service.ingest(user_id="default", text="我喜欢黑咖啡。")

    assert result.status == "retryable_error"
    assert result.retryable is True
    assert result.ignored == 0
    assert result.items == []


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


@pytest.mark.asyncio
async def test_sensitive_memory_requires_explicit_memory_request(
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

    batch = await LLMMemoryExtractor(llm_client=fake_llm).extract_many(
        source_text="我有一项健康隐私。",
    )

    assert len(batch.outcomes) == 1
    assert batch.outcomes[0].accepted is False
    assert "敏感信息" in batch.outcomes[0].reason or "隐私" in batch.outcomes[0].reason


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


@pytest.mark.asyncio
async def test_sensitive_memory_drops_detailed_auto_entities(
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

    result = await MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
        allow_sensitive_egress=True,
    ).ingest(
        user_id="default",
        text="记住，我的身份证号是 123456。",
    )

    assert result.created == 1
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
                    "reason": "User explicitly described the current employer and start date.",
                    "source_quote": "Since 2026-01-01 I work at Company B",
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
            "text": "Since 2026-01-01 I work at Company B",
            "conversation_id": "temporal-ingest",
        },
    )

    assert response.status_code == 200
    assert response.json()["created"] == 1
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.valid_from == "2026-01-01"
    assert memory.temporal_subject == "user"
    assert memory.temporal_predicate == "current_employer"


def test_ingest_clears_temporal_date_not_grounded_in_source_quote(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="User works at Company B.",
        source_quote="I now work at Company B",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )

    response = _post_ingest(client, auth_headers, "I now work at Company B")

    assert response.status_code == 200
    assert response.json()["created"] == 1
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.valid_from is None
    assert memory.temporal_predicate == "current_employer"


@pytest.mark.parametrize(
    "overrides",
    [
        {"temporal_subject": "user", "temporal_predicate": None},
        {"valid_from": "2027-01-01", "valid_until": "2026-01-01"},
    ],
)
def test_invalid_temporal_metadata_is_rejected_as_invalid_model_output(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_llm,
    overrides: dict,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="User works at Company B.",
        source_quote="I work at Company B",
        **overrides,
    )

    response = _post_ingest(client, auth_headers, "I work at Company B")

    assert response.status_code == 200
    assert response.json()["created"] == 0
    assert response.json()["ignored"] == 1


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

    hinted = apply_extraction_hints(candidate)

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

    hinted = apply_extraction_hints(candidate)

    assert hinted.temporal_subject == "用户"
    assert hinted.temporal_predicate == "current_city"


def test_temporal_profile_hint_clears_unsupported_llm_key() -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="用户喜欢咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
        source_quote="我喜欢咖啡",
        temporal_subject="用户",
        temporal_predicate="current_city",
    )

    hinted = apply_extraction_hints(candidate, source_text="我现在住上海。我喜欢咖啡")

    assert hinted.temporal_subject is None
    assert hinted.temporal_predicate is None


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


@pytest.mark.asyncio
async def test_deterministic_sensitive_floor_overrides_normal_llm_label(
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    fake_llm.extraction_content = _extraction_json(
        memory=f"用户的身份证号是 {identifier}。",
        importance=8,
        confidence=0.95,
        sensitivity="normal",
        source_quote=f"我的身份证号是 {identifier}",
    )

    batch = await LLMMemoryExtractor(llm_client=fake_llm).extract_many(
        source_text=f"请记住，我的身份证号是 {identifier}。",
    )

    assert len(batch.outcomes) == 1
    assert batch.outcomes[0].accepted is True
    assert batch.outcomes[0].candidate is not None
    assert batch.outcomes[0].candidate.sensitivity == "sensitive"


def test_detect_text_sensitivity_is_reusable_without_llm() -> None:
    from app.memory.redaction import detect_text_sensitivity

    assert detect_text_sensitivity("我喜欢黑咖啡") == "normal"
    assert detect_text_sensitivity("我的邮箱是 user@example.com") == "private"
    assert detect_text_sensitivity("银行卡密码是 123456") == "sensitive"
    assert detect_text_sensitivity("需要持续控制血糖") == "sensitive"


@pytest.mark.asyncio
async def test_sensitive_candidate_cannot_borrow_authorization_from_another_sentence(
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    fake_llm.extraction_content = _extraction_json(
        memory=f"用户的身份证号是 {identifier}。",
        importance=8,
        confidence=0.95,
        sensitivity="normal",
        source_quote=f"我的身份证号是 {identifier}",
    )

    batch = await LLMMemoryExtractor(llm_client=fake_llm).extract_many(
        source_text=f"记住我喜欢咖啡。我的身份证号是 {identifier}。",
    )

    assert batch.outcomes[0].accepted is False
    assert "明确要求记住" in batch.outcomes[0].reason


@pytest.mark.asyncio
async def test_sensitive_candidate_cannot_use_a_multi_sentence_quote_to_bypass_scope(
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    source_text = f"记住我喜欢咖啡。我的身份证号是 {identifier}。"
    fake_llm.extraction_content = _extraction_json(
        memory=f"用户的身份证号是 {identifier}。",
        importance=8,
        confidence=0.95,
        sensitivity="normal",
        source_quote=source_text,
    )

    batch = await LLMMemoryExtractor(llm_client=fake_llm).extract_many(
        source_text=source_text,
    )

    assert batch.outcomes[0].accepted is False
    assert "明确要求记住" in batch.outcomes[0].reason


@pytest.mark.asyncio
async def test_sensitive_candidate_cannot_borrow_authorization_from_another_clause(
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    fake_llm.extraction_content = _extraction_json(
        memory=f"用户的身份证号是 {identifier}。",
        importance=8,
        confidence=0.95,
        sensitivity="normal",
        source_quote=f"我的身份证号是 {identifier}",
    )

    batch = await LLMMemoryExtractor(llm_client=fake_llm).extract_many(
        source_text=f"记住我喜欢咖啡，我的身份证号是 {identifier}。",
    )

    assert batch.outcomes[0].accepted is False
    assert "明确要求记住" in batch.outcomes[0].reason


@pytest.mark.asyncio
async def test_sensitive_candidate_cannot_use_a_wide_clause_quote_to_bypass_scope(
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    source_text = f"记住我喜欢咖啡，我的身份证号是 {identifier}。"
    fake_llm.extraction_content = _extraction_json(
        memory=f"用户的身份证号是 {identifier}。",
        importance=8,
        confidence=0.95,
        sensitivity="normal",
        source_quote=source_text,
    )

    batch = await LLMMemoryExtractor(llm_client=fake_llm).extract_many(
        source_text=source_text,
    )

    assert batch.outcomes[0].accepted is False
    assert "明确要求记住" in batch.outcomes[0].reason


def test_invented_sensitive_fact_is_not_grounded_by_unrelated_quote(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户的银行卡密码是 123456。",
        importance=9,
        confidence=0.99,
        sensitivity="normal",
        source_quote="记住我喜欢咖啡",
    )

    response = _post_ingest(client, auth_headers, "记住我喜欢咖啡。")

    assert response.status_code == 200
    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    log = memory_store.list_decision_logs()[0]
    assert log.reason == "敏感候选未保存；详细理由已脱敏"
    assert "123456" not in log.candidate_json


def test_invented_entity_is_rejected_but_ordinary_paraphrase_remains_viable(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户在 OpenAI 工作。",
        importance=8,
        confidence=0.95,
        source_quote="我目前工作很忙",
        entities=["OpenAI"],
    )
    rejected = _post_ingest(client, auth_headers, "我目前工作很忙。")

    assert rejected.json()["ignored"] == 1
    assert "candidate.entities" in memory_store.list_decision_logs()[0].reason

    fake_llm.extraction_content = _extraction_json(
        memory="用户偏好无糖黑咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
        source_quote="我喜欢不加糖的黑咖啡",
        entities=[],
    )
    accepted = _post_ingest(client, auth_headers, "我喜欢不加糖的黑咖啡。")

    assert accepted.json()["created"] == 1
    assert memory_store.list_memories(user_id="default")[0].content == "用户偏好无糖黑咖啡。"


def test_unrelated_non_sensitive_rewrite_is_not_grounded(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户喜欢绿茶。",
        type="emotional",
        importance=7,
        confidence=0.95,
        source_quote="我喜欢咖啡",
    )

    response = _post_ingest(client, auth_headers, "我喜欢咖啡。")

    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "共同事实锚点" in memory_store.list_decision_logs()[0].reason


def test_grounding_rejects_opposite_polarity(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户不喜欢咖啡。",
        type="emotional",
        importance=7,
        confidence=0.95,
        source_quote="我喜欢咖啡",
    )

    response = _post_ingest(client, auth_headers, "我喜欢咖啡。")

    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "否定含义" in memory_store.list_decision_logs()[0].reason


def test_grounding_does_not_accept_one_shared_han_character(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户住在上海。",
        importance=7,
        confidence=0.95,
        source_quote="我看了海洋纪录片",
    )

    response = _post_ingest(client, auth_headers, "我看了海洋纪录片。")

    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "共同事实锚点" in memory_store.list_decision_logs()[0].reason


def test_grounding_scopes_negation_to_best_matching_clause(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    quote = "我不喜欢咖啡，但我喜欢绿茶"
    fake_llm.extraction_content = _extraction_json(
        memory="用户不喜欢绿茶。",
        type="emotional",
        importance=7,
        confidence=0.95,
        source_quote=quote,
    )

    response = _post_ingest(client, auth_headers, f"{quote}。")

    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "否定含义" in memory_store.list_decision_logs()[0].reason


def test_sensitive_rejected_candidate_does_not_leak_entity_in_log(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    phone = "13800138000"
    fake_llm.extraction_content = _extraction_json(
        memory="用户住在上海。",
        importance=7,
        confidence=0.95,
        source_quote="我住在上海",
        entities=[phone],
    )

    response = _post_ingest(client, auth_headers, "我住在上海。")

    assert response.json()["ignored"] == 1
    log = memory_store.list_decision_logs()[0]
    assert phone not in log.candidate_json
    assert phone not in log.reason


def test_empty_candidate_model_reason_is_hashed_in_decision_log(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    identifier = "123456789012345678"
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [],
            "reason_code": "no_long_term_value",
            "reason": f"身份证号是 {identifier}",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我喜欢咖啡。")

    assert response.json()["ignored"] == 1
    log = memory_store.list_decision_logs()[0]
    audit = json.loads(log.candidate_json)
    assert audit["model_reason_redacted"] is True
    assert audit["model_reason_code"] == "no_long_term_value"
    assert len(audit["model_reason_sha256"]) == 64
    assert "原因码=no_long_term_value" in log.reason
    assert identifier not in log.candidate_json
    assert identifier not in log.reason


def test_empty_candidate_without_valid_reason_code_is_marked_unclassified(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [],
            "reason_code": "model_invented_code",
            "reason": "模型没有遵守原因码枚举",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我喜欢咖啡。")

    assert response.json()["ignored"] == 1
    log = memory_store.list_decision_logs()[0]
    audit = json.loads(log.candidate_json)
    assert audit["model_reason_code"] == "unclassified"
    assert "原因码=unclassified" in log.reason
    assert "模型没有遵守原因码枚举" not in log.candidate_json


def test_batch_extraction_prompt_requires_age_candidate_and_reason_code(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [],
            "reason_code": "other",
            "reason": "测试提示词",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我现在 18 岁。")

    assert response.status_code == 200
    system_prompt = fake_llm.extraction_messages[0]["content"]
    assert '"reason_code"' in system_prompt
    assert "不能仅因会变化而归类为 temporary_or_one_off" in system_prompt
    assert "必须输出候选" in system_prompt
    assert "memory 只写原话可支撑的“用户现在 X 岁”" in system_prompt
    assert "后端会在逐字证据校验通过后改写" in system_prompt


def test_batch_current_age_with_system_date_anchor_is_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    now = datetime.now(UTC)
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": f"截至 {now.date().isoformat()}，用户自称 18 岁。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.85,
                    "stability": "medium",
                    "source_quote": "我现在 18 岁",
                }
            ],
            "reason_code": "has_candidates",
            "reason": "用户明确陈述当前年龄",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我现在 18 岁。")

    assert response.status_code == 200
    assert response.json()["created"] == 1
    memory = memory_store.list_memories(user_id="default")[0]
    assert memory.content == f"截至 {now.strftime('%Y-%m')}，用户自称 18 岁。"
    assert memory.stability == "medium"
    assert memory.review_after is not None


def test_batch_current_age_does_not_trust_another_date_anchor(
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
                    "memory": "截至 1900-01-01，用户自称 18 岁。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.85,
                    "stability": "medium",
                    "source_quote": "我现在 18 岁",
                }
            ],
            "reason_code": "has_candidates",
            "reason": "用户明确陈述当前年龄",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我现在 18 岁。")

    assert response.status_code == 200
    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "结构化数字" in memory_store.list_decision_logs()[0].reason


def test_batch_current_age_still_rejects_another_fabricated_number(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    now = datetime.now(UTC)
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": (
                        f"截至 {now.date().isoformat()}，"
                        "用户自称 18 岁，账号为 123456。"
                    ),
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.85,
                    "stability": "medium",
                    "source_quote": "我现在 18 岁",
                }
            ],
            "reason_code": "has_candidates",
            "reason": "用户明确陈述当前年龄",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我现在 18 岁。")

    assert response.status_code == 200
    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "结构化数字" in memory_store.list_decision_logs()[0].reason


def test_batch_bare_age_answer_requires_verified_context_quote(
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
                    "memory": "用户现在 18 岁。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.85,
                    "source_quote": "18",
                    "context_quote": "",
                }
            ],
            "reason_code": "has_candidates",
            "reason": "测试缺少上下文引用",
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "18")

    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "缺少 context_quote" in memory_store.list_decision_logs()[0].reason


@pytest.mark.asyncio
async def test_batch_bare_age_answer_rejects_fabricated_context_quote(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "用户现在 18 岁。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.85,
                    "source_quote": "18",
                    "context_quote": "你今年到底多少岁",
                }
            ],
            "reason_code": "has_candidates",
            "reason": "测试伪造上下文引用",
        },
        ensure_ascii=False,
    )
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
    )

    result = await service.ingest(
        user_id="default",
        text="18",
        conversation_context="用户：你喜欢什么颜色",
        context_quote_source="用户：你喜欢什么颜色",
    )

    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "不是较早对话原文" in memory_store.list_decision_logs()[0].reason


@pytest.mark.asyncio
async def test_compressed_summary_cannot_authorize_bare_age_answer(
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "用户现在 18 岁。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.85,
                    "source_quote": "18",
                    "context_quote": "用户此前让助手猜自己的年龄",
                }
            ],
            "reason_code": "has_candidates",
            "reason": "测试压缩摘要不能作为逐字证据",
        },
        ensure_ascii=False,
    )
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
    )

    result = await service.ingest(
        user_id="default",
        text="18",
        conversation_context=(
            "<compressed_summary_non_authoritative>\n"
            "用户此前让助手猜自己的年龄\n"
            "</compressed_summary_non_authoritative>\n\n"
            "<recent_dialogue_quote_source>\n用户：最近在聊电影\n"
            "</recent_dialogue_quote_source>"
        ),
        context_quote_source="用户：最近在聊电影",
    )

    assert result.ignored == 1
    assert memory_store.list_memories(user_id="default") == []
    assert "不是较早对话原文" in memory_store.list_decision_logs()[0].reason


def test_batch_age_hint_does_not_overwrite_another_candidate(
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
                    "memory": "用户 30 岁。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.9,
                    "source_quote": "我 30 岁",
                },
                {
                    "action": "create",
                    "memory": "用户喜欢咖啡。",
                    "type": "emotional",
                    "importance": 7,
                    "confidence": 0.9,
                    "source_quote": "我喜欢咖啡",
                },
            ]
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我 30 岁。我喜欢咖啡。")

    assert response.json()["created"] == 2
    contents = [memory.content for memory in memory_store.list_memories(user_id="default")]
    assert sum("30 岁" in content for content in contents) == 1
    assert "用户喜欢咖啡。" in contents


def test_batch_temporal_hint_does_not_leak_to_preference_candidate(
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
                    "memory": "用户现在住在上海。",
                    "type": "semantic",
                    "importance": 7,
                    "confidence": 0.9,
                    "source_quote": "我现在住上海",
                },
                {
                    "action": "create",
                    "memory": "用户喜欢咖啡。",
                    "type": "emotional",
                    "importance": 7,
                    "confidence": 0.9,
                    "source_quote": "喜欢咖啡",
                },
            ]
        },
        ensure_ascii=False,
    )

    response = _post_ingest(client, auth_headers, "我现在住上海，同时喜欢咖啡。")

    assert response.json()["created"] == 2
    memories = memory_store.list_memories(user_id="default")
    city = next(memory for memory in memories if "上海" in memory.content)
    coffee = next(memory for memory in memories if "咖啡" in memory.content)
    assert city.temporal_predicate == "current_city"
    assert coffee.temporal_subject is None
    assert coffee.temporal_predicate is None


def test_invalid_quote_is_rejected_before_age_normalization(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户喜欢咖啡。",
        source_quote="我喜欢咖啡",
    )

    response = _post_ingest(client, auth_headers, "我 30 岁。")

    assert response.json()["ignored"] == 1
    log_payload = json.loads(memory_store.list_decision_logs()[0].candidate_json)
    assert log_payload["memory"] == "用户喜欢咖啡。"
    assert "source_quote" in memory_store.list_decision_logs()[0].reason


def test_grounding_rejects_candidate_with_a_second_unsupported_fact() -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="用户喜欢咖啡，并住在北京。",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote="我喜欢咖啡",
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message="我喜欢咖啡",
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "每个事实" in rejection


def test_grounding_accepts_multiple_facts_when_each_has_quote_evidence() -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    quote = "我喜欢咖啡，我住在北京"
    candidate = CandidateMemory(
        action="create",
        memory="用户喜欢咖啡，并住在北京。",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    assert (
        validate_candidate_for_save(
            candidate,
            user_message=quote,
            require_quote_in_user_message=True,
        )
        is None
    )


def test_extraction_hints_never_use_model_memory_as_its_own_evidence() -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="用户喜欢咖啡，并住在北京。",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote="我喜欢咖啡",
    )

    hinted = apply_extraction_hints(candidate)

    assert hinted.type == "emotional"
    assert hinted.temporal_subject is None
    assert hinted.temporal_predicate is None


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("User has pets.", "I have no pets."),
        ("User travels with pets.", "I never travel with pets."),
        ("User drinks coffee with sugar.", "I drink coffee without sugar."),
        ("用户有宠物。", "我没有宠物。"),
    ],
)
def test_grounding_rejects_common_english_and_chinese_negation_inversions(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message=quote,
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "否定含义" in rejection


def test_english_negation_uses_word_boundaries() -> None:
    from app.memory.utils import _has_negation

    assert _has_negation("This is notable experience.") is False
    assert _has_negation("This is not relevant.") is True


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户的银行卡密码是 123456。", "我记得银行卡密码是 123456"),
        ("User's password is secret123.", "I remember my password is secret123"),
        ("User's password is secret123.", "Remember when my password was secret123"),
        ("User's password is secret123.", "Remember whether my password is secret123"),
        ("User's password is secret123.", "Remember how my password became secret123"),
        ("User's password is secret123.", "Remember that time my password was secret123"),
    ],
)
def test_narrative_remember_is_not_sensitive_storage_authorization(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=9,
        confidence=0.95,
        sensitivity="sensitive",
        source_quote=quote,
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message=quote,
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "明确要求记住" in rejection


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户的银行卡密码是 123456。", "请记住：我的银行卡密码是 123456"),
        ("User's password is secret123.", "Please remember: my password is secret123"),
    ],
)
def test_imperative_remember_authorizes_scoped_sensitive_storage(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=9,
        confidence=0.95,
        sensitivity="sensitive",
        source_quote=quote,
    )

    assert (
        validate_candidate_for_save(
            candidate,
            user_message=quote,
            require_quote_in_user_message=True,
        )
        is None
    )


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户在 Acme 工作。", "我希望明年去 Acme 工作"),
        ("用户在 Acme 工作。", "我明年在 Acme 工作"),
        ("User works at Acme.", "I will work at Acme"),
        ("用户住在上海。", "我不想住上海"),
        ("用户住在北京。", "我下个月搬到北京"),
    ],
)
def test_future_desire_or_negative_state_is_not_a_current_profile_fact(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    hinted = apply_extraction_hints(candidate)

    assert hinted.temporal_subject is None
    assert hinted.temporal_predicate is None


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("User works at Acme.", "I applied to Acme."),
        ("User lives in Paris.", "I visited Paris."),
        ("用户购买咖啡。", "我喜欢咖啡。"),
        ("User likes coffee and resides in Beijing.", "I like coffee."),
        ("用户喜欢咖啡还住在北京。", "我喜欢咖啡。"),
        ("用户喜欢咖啡又居住北京。", "我喜欢咖啡。"),
    ],
)
def test_grounding_rejects_shared_entity_with_a_different_relation(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message=quote,
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "每个事实" in rejection


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户喜欢西瓜。", "我的猫喜欢西瓜"),
        ("用户住在北京。", "我的朋友住在北京"),
        ("User likes watermelon.", "My cat likes watermelon."),
        ("User lives in Paris.", "My friend lives in Paris."),
    ],
)
def test_grounding_rejects_relation_subject_drift(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message=quote,
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "每个事实" in rejection


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户在 Acme 工作。", "我申请了 Acme 的工作"),
        ("用户住在北京。", "我去北京旅游时住在酒店"),
        ("User works at Acme.", "I applied for an Acme job."),
        ("User lives in Paris.", "I visited Paris and lived in a hotel."),
    ],
)
def test_grounding_rejects_relation_bound_to_a_different_object(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message=quote,
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "每个事实" in rejection


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户的猫喜欢西瓜。", "我的猫喜欢西瓜"),
        ("用户的朋友住在北京。", "我的朋友住在北京"),
        ("User's cat likes watermelon.", "My cat likes watermelon."),
        ("User's friend lives in Paris.", "My friend lives in Paris."),
    ],
)
def test_grounding_accepts_an_explicitly_preserved_third_party_subject(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    assert (
        validate_candidate_for_save(
            candidate,
            user_message=quote,
            require_quote_in_user_message=True,
        )
        is None
    )


def test_normal_entity_must_be_bound_to_candidate_proposition() -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    quote = "我申请了 Acme"
    candidate = CandidateMemory(
        action="create",
        memory="用户住在北京。",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
        entities=["Acme"],
    )

    rejection = validate_candidate_for_save(
        candidate,
        user_message=quote,
        require_quote_in_user_message=True,
    )

    assert rejection is not None
    assert "未绑定" in rejection


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("User resides in Beijing.", "I live in Beijing."),
        ("用户偏好无糖黑咖啡。", "我喜欢不加糖的黑咖啡。"),
        ("用户使用 Kelivo。", "我现在主要用 Kelivo。"),
        ("User lives in Paris.", "I not only live in Paris but work there."),
        ("用户住在北京。", "我不但住在北京，而且在那里工作。"),
        ("User drinks coffee.", "Without fail I drink coffee every morning."),
    ],
)
def test_grounding_accepts_relation_paraphrase_and_additive_not_only(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extractor import validate_candidate_for_save
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    assert (
        validate_candidate_for_save(
            candidate,
            user_message=quote,
            require_quote_in_user_message=True,
        )
        is None
    )


@pytest.mark.parametrize(
    "quote",
    [
        "我住在北京，但我没有宠物",
        "我住在北京，明年想去上海旅游",
        "我不但住在北京，而且喜欢那里的公园",
    ],
)
def test_temporal_hint_scopes_negation_and_future_markers_to_matching_clause(
    quote: str,
) -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="用户住在北京。",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    hinted = apply_extraction_hints(candidate)

    assert hinted.temporal_subject == "用户"
    assert hinted.temporal_predicate == "current_city"


@pytest.mark.parametrize(
    ("memory", "quote"),
    [
        ("用户住在北京。", "我的朋友住在北京"),
        ("User lives in Paris.", "She lives in Paris"),
        ("用户主要用 Kelivo。", "我的同事主要用 Kelivo"),
        ("用户住在北京。", "我喜欢住房设计"),
        ("用户在某公司工作。", "我在远程工作"),
        ("用户在北京工作。", "我在北京工作"),
        ("User works at Python.", "I work in Python every day"),
    ],
)
def test_temporal_hint_rejects_third_party_and_non_profile_values(
    memory: str,
    quote: str,
) -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory=memory,
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    hinted = apply_extraction_hints(candidate)

    assert hinted.temporal_subject is None
    assert hinted.temporal_predicate is None


def test_temporal_hint_keeps_grounded_committed_future_interval() -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="User will work at Acme from 2027-03-01.",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote="From 2027-03-01 I will work at Acme",
        valid_from="2027-03-01",
    )

    hinted = apply_extraction_hints(candidate)

    assert hinted.temporal_subject == "用户"
    assert hinted.temporal_predicate == "current_employer"


@pytest.mark.parametrize("quote", ["不要叫我小王", "Don't call me Bob"])
def test_negative_preferred_name_is_not_current_name(quote: str) -> None:
    from app.memory.extraction_hints import apply_extraction_hints
    from app.memory.models import CandidateMemory

    candidate = CandidateMemory(
        action="create",
        memory="User is called Bob.",
        type="semantic",
        importance=8,
        confidence=0.95,
        source_quote=quote,
    )

    hinted = apply_extraction_hints(candidate)

    assert hinted.temporal_subject is None
    assert hinted.temporal_predicate is None


@pytest.mark.parametrize("status_code", [500, 501])
@pytest.mark.asyncio
async def test_all_upstream_5xx_extraction_failures_are_retryable(
    status_code: int,
    memory_store: MemoryStore,
    fake_llm,
    monkeypatch,
) -> None:
    class UpstreamHTTPError(RuntimeError):
        def __init__(self, code: int):
            super().__init__(f"upstream returned {code}")
            self.status_code = code

    async def fail_upstream(*args, **kwargs):
        raise UpstreamHTTPError(status_code)

    monkeypatch.setattr(fake_llm, "create_chat_completion", fail_upstream)
    service = MemoryIngestService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        llm_client=fake_llm,
    )

    result = await service.ingest(user_id="default", text="我喜欢黑咖啡。")

    assert result.status == "retryable_error"
    assert result.retryable is True
    assert result.ignored == 0
