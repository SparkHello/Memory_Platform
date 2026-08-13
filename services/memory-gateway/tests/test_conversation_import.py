from __future__ import annotations

import json

from app.memory.conversation_import import parse_conversation_import


def test_parse_openai_style_messages() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "我喜欢黑咖啡"},
            {"role": "assistant", "content": "记下了"},
            {"role": "user", "content": "我每周三打羽毛球"},
        ]
    }
    preview = parse_conversation_import(json.dumps(payload, ensure_ascii=False))
    assert preview.format == "json_messages"
    assert preview.turn_count == 2
    assert preview.turns[0].user_text == "我喜欢黑咖啡"
    assert preview.turns[0].assistant_text == "记下了"
    assert preview.turns[1].user_text == "我每周三打羽毛球"
    assert preview.turns[1].assistant_text is None


def test_parse_role_lines() -> None:
    text = """User: 我养了一只猫
Assistant: 好的
用户：周末想爬山
助手：可以
"""
    preview = parse_conversation_import(text)
    assert preview.turn_count == 2
    assert "猫" in preview.turns[0].user_text
    assert "爬山" in preview.turns[1].user_text


def test_preview_endpoint(client, auth_headers) -> None:
    response = client.post(
        "/memories/import/conversations/preview",
        headers=auth_headers,
        json={
            "content": json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "我住在上海"},
                        {"role": "assistant", "content": "了解"},
                    ]
                },
                ensure_ascii=False,
            )
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["turn_count"] == 1
    assert body["sample_turns"][0]["user_text"] == "我住在上海"
    assert body["will_not_auto_pin"] is True


def test_commit_endpoint_isolates_turns(client, auth_headers, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_ingest(self, **kwargs):  # noqa: ANN001
        from app.memory.models import MemoryIngestItemResult, MemoryIngestResult

        calls.append(kwargs["text"])
        return MemoryIngestResult(
            created=0,
            updated=0,
            ignored=1,
            items=[
                MemoryIngestItemResult(action="ignore", reason="测试忽略"),
            ],
            reason="测试忽略",
            status="ignored",
        )

    monkeypatch.setattr(
        "app.api.memories.import_conversations.MemoryIngestService.ingest",
        fake_ingest,
    )

    content = json.dumps(
        {
            "messages": [
                {"role": "user", "content": "事实甲"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "事实乙"},
            ]
        },
        ensure_ascii=False,
    )
    response = client.post(
        "/memories/import/conversations/commit",
        headers=auth_headers,
        json={"content": content},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["turn_count"] == 2
    assert len(body["turns"]) == 2
    assert calls == ["事实甲", "事实乙"]
    assert body["batch_id"].startswith("import-")
