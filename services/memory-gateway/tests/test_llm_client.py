import asyncio
import json

import pytest
import httpx
from fastapi import HTTPException

from app.config import Settings
from app.llm.client import OpenAICompatibleClient
from app.llm.model_gateway import (
    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER,
    MODEL_GATEWAY_CONNECTION_HEADER,
    MODEL_GATEWAY_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_ROUTE_HEADER,
    MODEL_GATEWAY_MODEL_AUTHOR_HEADER,
    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER,
    MODEL_GATEWAY_VENDOR_HEADER,
)
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.attribution import (
    MODEL_GATEWAY_CORRELATION_HEADER,
    MODEL_GATEWAY_OPERATION_HEADER,
    MODEL_GATEWAY_USER_TAG_HEADER,
)


def _central_settings(**overrides) -> Settings:
    payload = {
        "_env_file": None,
        "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030/v1",
        "MODEL_GATEWAY_API_KEY": "central-key",
        "GATEWAY_SIGNING_SECRET": "llm-test-signing-secret-0123456789abcdef",
        "MODEL_GATEWAY_MEMORY_EXTRACT_MODEL": "route.extract",
        "MODEL_GATEWAY_MEMORY_COMPACT_MODEL": "route.compact",
        "MODEL_GATEWAY_MEMORY_CORE_MODEL": "route.core",
        "MODEL_GATEWAY_MEMORY_REVIEW_MODEL": "route.review",
    }
    payload.update(overrides)
    return Settings(**payload)


@pytest.mark.asyncio
async def test_model_gateway_chat_response_uses_json_bytes_without_mojibake(
    monkeypatch,
) -> None:
    calls = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool, trust_env: bool):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers})
            body = {
                "id": "chatcmpl-gateway-test",
                "object": "chat.completion",
                "created": 0,
                "model": json["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "好的，我已经记住你喜欢黑咖啡。",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
            return httpx.Response(
                200,
                content=json_module_dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json; charset=iso-8859-1",
                    MODEL_GATEWAY_ROUTE_HEADER: json["model"],
                    MODEL_GATEWAY_DEPLOYMENT_HEADER: "deployment-primary",
                    MODEL_GATEWAY_CONNECTION_HEADER: "connection-primary",
                    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "zhipu",
                    MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "zhipu",
                    MODEL_GATEWAY_VENDOR_HEADER: "zhipu",
                    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "glm-5.1",
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.llm.client.httpx.AsyncClient", FakeAsyncClient)
    settings = _central_settings()
    client = OpenAICompatibleClient(settings=settings)
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "我喜欢黑咖啡，请记住。"}],
    )

    response = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "我喜欢黑咖啡，请记住。"}],
    )

    assert response["choices"][0]["message"]["content"] == "好的，我已经记住你喜欢黑咖啡。"
    assert calls[0]["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert calls[0]["json"]["model"] == "route.review"
    assert calls[0]["json"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_model_gateway_enforces_total_timeout(monkeypatch) -> None:
    class SlowAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool, trust_env: bool):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            await asyncio.sleep(0.05)
            raise AssertionError("总超时应先中止请求")

    monkeypatch.setattr("app.llm.client.httpx.AsyncClient", SlowAsyncClient)
    settings = _central_settings(REQUEST_TIMEOUT_SECONDS=0.01)
    client = OpenAICompatibleClient(settings=settings)
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )

    with pytest.raises(HTTPException) as exc_info:
        await client.create_chat_completion(
            request=request,
            messages=[{"role": "user", "content": "只输出 JSON"}],
        )

    assert exc_info.value.status_code == 504
    assert "0.01 秒" in exc_info.value.detail


@pytest.mark.asyncio
async def test_model_gateway_network_failure_returns_502() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    settings = _central_settings()
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "test"}],
    )

    with pytest.raises(HTTPException) as exc_info:
        await client.create_chat_completion(
            request=request,
            messages=[{"role": "user", "content": "test"}],
        )

    assert exc_info.value.status_code == 502
    assert "simulated connection failure" in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_model"),
    [
        ("memory-extractor", "route.extract"),
        ("memory-ingester", "route.extract"),
        ("memory-context-compactor", "route.compact"),
        ("core-memory-consolidator", "route.core"),
        ("memory-review-editor", "route.review"),
    ],
)
async def test_model_gateway_routes_internal_operation_once_without_vendor_rewrite(
    operation: str,
    expected_model: str,
) -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("Authorization"),
                "headers": dict(request.headers),
                "payload": payload,
            }
        )
        return httpx.Response(
            200,
            headers={
                MODEL_GATEWAY_ROUTE_HEADER: expected_model,
                MODEL_GATEWAY_DEPLOYMENT_HEADER: "deployment-primary",
                MODEL_GATEWAY_CONNECTION_HEADER: "connection-primary",
                MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "moonshot",
                MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "moonshot",
                MODEL_GATEWAY_VENDOR_HEADER: "kimi",
                MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "kimi-k2.7-code",
            },
            json={
                "choices": [{"message": {"content": '{"operations":[]}'}}],
                "model": expected_model,
            },
        )

    settings = _central_settings()
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model=operation,
        messages=[{"role": "user", "content": "只输出 JSON"}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
        structured_tool={
            "name": "submit_result",
            "parameters": {"type": "object"},
        },
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "http://127.0.0.1:2030/v1/chat/completions"
    assert calls[0]["authorization"] == "Bearer central-key"
    assert calls[0]["headers"][MODEL_GATEWAY_OPERATION_HEADER.lower()] == expected_model
    assert calls[0]["headers"][MODEL_GATEWAY_CORRELATION_HEADER.lower()].startswith("mgc_")
    assert calls[0]["headers"][MODEL_GATEWAY_USER_TAG_HEADER.lower()].startswith("usr_")
    assert calls[0]["payload"]["model"] == expected_model
    assert calls[0]["payload"]["temperature"] == 0.0
    assert calls[0]["payload"]["stream"] is False
    assert calls[0]["payload"]["reasoning_effort"] == "high"
    assert calls[0]["payload"]["tools"][0]["function"]["name"] == "submit_result"
    assert calls[0]["payload"]["tool_choice"] == "auto"
    assert "response_format" not in calls[0]["payload"]


@pytest.mark.asyncio
async def test_model_gateway_does_not_retry_failed_central_route() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={"error": {"message": "central route unavailable"}},
        )

    settings = _central_settings()
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "test"}],
    )

    with pytest.raises(HTTPException) as exc_info:
        await client.create_chat_completion(
            request=request,
            messages=[{"role": "user", "content": "test"}],
        )

    assert calls == 1
    assert exc_info.value.status_code == 502
    assert "central route unavailable" in exc_info.value.detail


@pytest.mark.asyncio
async def test_model_gateway_usage_is_not_duplicated_in_local_ledger() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={
                MODEL_GATEWAY_ROUTE_HEADER: payload["model"],
                MODEL_GATEWAY_DEPLOYMENT_HEADER: "deployment-deepseek",
                MODEL_GATEWAY_CONNECTION_HEADER: "connection-official",
                MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "deepseek",
                MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "deepseek",
                MODEL_GATEWAY_VENDOR_HEADER: "deepseek",
                MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "deepseek-v4-flash",
            },
            json={
                "choices": [{"message": {"content": "{}"}}],
                "model": payload["model"],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    settings = _central_settings()
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "test"}],
    )

    response = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "test"}],
    )

    assert response["model"] == "route.review"


def json_module_dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)
