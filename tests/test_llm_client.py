import json

import pytest
import httpx

from app.config import Settings
from app.llm.client import OpenAICompatibleClient
from app.openai_compat.schemas import ChatCompletionRequest


@pytest.mark.asyncio
async def test_upstream_chat_response_uses_json_bytes_without_mojibake(monkeypatch) -> None:
    calls = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers})
            body = {
                "id": "chatcmpl-zhipu-test",
                "object": "chat.completion",
                "created": 0,
                "model": "glm-5.1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "好的，我已经记住你喜欢黑咖啡。"},
                        "finish_reason": "stop",
                    }
                ],
            }
            return httpx.Response(
                200,
                content=json_module_dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=iso-8859-1"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.llm.client.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        UPSTREAM_BASE_URL="https://open.bigmodel.cn/api/paas/v4",
        UPSTREAM_API_KEY="zhipu-key",
        UPSTREAM_MODEL="glm-5.1",
    )
    client = OpenAICompatibleClient(settings=settings)
    request = ChatCompletionRequest(
        model="ios-model",
        messages=[{"role": "user", "content": "我喜欢黑咖啡，请记住。"}],
    )

    response = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "我喜欢黑咖啡，请记住。"}],
    )

    assert response["choices"][0]["message"]["content"] == "好的，我已经记住你喜欢黑咖啡。"
    assert calls[0]["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert calls[0]["json"]["model"] == "glm-5.1"


def json_module_dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)
