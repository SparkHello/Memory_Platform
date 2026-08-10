from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.memory.extractor import LLMMemoryExtractor
from app.memory.models import RecentContextTurn


def test_public_search_memory_note_and_id_fields_have_explicit_bounds(
    client,
    auth_headers,
    fake_gateway,
) -> None:
    assert client.post(
        "/memories/search",
        headers=auth_headers,
        json={"query": "q" * 4097},
    ).status_code == 422
    assert client.post(
        "/knowledge/search",
        headers=auth_headers,
        json={"request": "q" * 4097},
    ).status_code == 422
    assert client.post(
        "/memories",
        headers=auth_headers,
        json={"content": "m" * 65_537},
    ).status_code == 422
    assert client.post(
        "/memories/review/revise/preview",
        headers=auth_headers,
        json={"memory_ids": ["missing"], "user_note": "n" * 20_001},
    ).status_code == 422

    long_path = client.get(f"/memories/{'i' * 201}", headers=auth_headers)
    assert long_path.status_code == 422
    assert long_path.json()["detail"]["code"] == "path_identifier_too_long"
    assert fake_gateway.payloads == []


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_public_chat_completion_token_limit_is_16k(
    client,
    auth_headers,
    fake_gateway,
    field,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={**auth_headers, "X-Memory-Mode": "off"},
        json={
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "hello"}],
            field: 16_385,
        },
    )

    assert response.status_code == 422
    assert fake_gateway.payloads == []


def test_recent_context_turn_uses_utf8_byte_limit() -> None:
    with pytest.raises(ValidationError, match="64 KiB"):
        RecentContextTurn(user="你" * 21_846, assistant="")


@pytest.mark.asyncio
async def test_batch_extraction_bounds_input_count_and_output_tokens(fake_llm) -> None:
    extractor = LLMMemoryExtractor(llm_client=fake_llm)

    oversized = await extractor.extract_many(source_text="x" * 65_537)
    assert oversized.error_code == "extraction_input_too_large"
    assert fake_llm.extraction_calls == 0

    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "ignore",
                    "memory": "",
                    "importance": 1,
                    "confidence": 0,
                    "reason": "test",
                    "source_quote": "",
                }
                for _ in range(101)
            ]
        }
    )
    too_many = await extractor.extract_many(source_text="bounded input")
    assert too_many.error_code == "too_many_extraction_candidates"
    assert fake_llm.extraction_calls == 1
    assert fake_llm.extraction_request.max_tokens == 8192
    assert fake_llm.extraction_thinking == "disabled"
