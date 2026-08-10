import asyncio
import json

import pytest
import httpx
from fastapi import HTTPException

from app.config import Settings
from app.llm.routing import LLMProvider
from app.knowledge.agent import (
    KnowledgeAgentConfig,
    OpenAICompatibleKnowledgeAgentClient,
)
from app.llm.client import OpenAICompatibleClient, _thinking_payload
from app.llm.model_gateway import (
    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER,
    MODEL_GATEWAY_CONNECTION_HEADER,
    MODEL_GATEWAY_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_ROUTE_HEADER,
    MODEL_GATEWAY_MODEL_AUTHOR_HEADER,
    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER,
    MODEL_GATEWAY_VENDOR_HEADER,
)
from app.llm.routing import ProviderCooldowns
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.attribution import (
    MODEL_GATEWAY_CORRELATION_HEADER,
    MODEL_GATEWAY_OPERATION_HEADER,
    MODEL_GATEWAY_USER_TAG_HEADER,
)


@pytest.mark.asyncio
async def test_upstream_chat_response_uses_json_bytes_without_mojibake(monkeypatch) -> None:
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
        _env_file=None,
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
    assert calls[0]["json"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_deepseek_chat_enables_thinking_for_structured_tasks(
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
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"operations":[]}',
                        }
                    }
                ]
            }
            return httpx.Response(
                200,
                content=json_module_dumps(body).encode("utf-8"),
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.llm.client.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        _env_file=None,
        UPSTREAM_BASE_URL="https://api.deepseek.com",
        UPSTREAM_API_KEY="deepseek-key",
        UPSTREAM_MODEL="deepseek-v4-flash",
    )
    client = OpenAICompatibleClient(settings=settings)
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "只输出 JSON"}],
        max_tokens=2048,
        response_format={"type": "json_object"},
    )

    await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )

    assert calls[0]["json"]["thinking"] == {"type": "enabled"}
    assert calls[0]["json"]["max_tokens"] == 2048
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_mimo_ultraspeed_uses_forced_tool_for_structured_output() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "submit_memory_review_revision",
                                        "arguments": '{"operations":[]}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "model": payload["model"],
            },
        )

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="M",
        LLM_MIMO_API_KEY="mimo-key",
        UPSTREAM_API_KEY="",
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "只输出 JSON"}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    structured_tool = {
        "name": "submit_memory_review_revision",
        "description": "Return review operations",
        "parameters": {
            "type": "object",
            "properties": {"operations": {"type": "array"}},
            "required": ["operations"],
        },
    }

    await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
        structured_tool=structured_tool,
    )

    assert "response_format" not in calls[0]
    assert calls[0]["tools"] == [{"type": "function", "function": structured_tool}]
    assert calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_memory_review_revision"},
    }


@pytest.mark.asyncio
async def test_kimi_k27_uses_cn_endpoint_and_temperature_one() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append({"url": str(request.url), "payload": payload})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"operations":[]}'}}],
                "model": payload["model"],
            },
        )

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="K",
        LLM_KIMI_API_KEY="kimi-key",
        LLM_KIMI_MODEL="kimi-k2.7-code-highspeed",
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "只输出 JSON"}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )

    assert calls[0]["url"] == "https://api.moonshot.cn/v1/chat/completions"
    assert calls[0]["payload"]["temperature"] == 1.0
    assert calls[0]["payload"]["thinking"] == {"type": "enabled", "keep": "all"}
    assert calls[0]["payload"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_upstream_chat_enforces_total_timeout(monkeypatch) -> None:
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
    settings = Settings(
        _env_file=None,
        UPSTREAM_BASE_URL="https://api.deepseek.com",
        UPSTREAM_API_KEY="deepseek-key",
        UPSTREAM_MODEL="deepseek-v4-flash",
        REQUEST_TIMEOUT_SECONDS=0.01,
    )
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
async def test_upstream_network_failure_returns_original_502() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    settings = Settings(
        _env_file=None,
        UPSTREAM_API_KEY="legacy-key",
    )
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
async def test_shared_llm_priority_routes_memory_tasks_and_cools_down_429_provider() -> None:
    now = {"value": 100.0}
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode("utf-8"))["model"]
        calls.append(model)
        if model == "mimo-v2.5-pro-ultraspeed":
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"operations":[]}',
                        }
                    }
                ],
                "model": model,
            },
        )

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="MKD",
        LLM_MIMO_API_KEY="mimo-key",
        LLM_KIMI_API_KEY="kimi-key",
        LLM_DEEPSEEK_API_KEY="deepseek-key",
        LLM_RATE_LIMIT_COOLDOWN_SECONDS=300,
        REQUEST_TIMEOUT_SECONDS=5,
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(clock=lambda: now["value"]),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "只输出 JSON"}],
        response_format={"type": "json_object"},
    )

    first = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )
    second = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )

    assert calls == [
        "mimo-v2.5-pro-ultraspeed",
        "kimi-k2.7-code",
        "kimi-k2.7-code",
    ]
    assert first["model"] == "kimi-k2.7-code"
    assert second["model"] == "kimi-k2.7-code"

    now["value"] += 300
    await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )
    assert calls[-2:] == [
        "mimo-v2.5-pro-ultraspeed",
        "kimi-k2.7-code",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 402, 404])
async def test_provider_configuration_and_auth_errors_do_not_fail_over(
    status_code: int,
) -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        if payload["model"] == "mimo-v2.5-pro-ultraspeed":
            return httpx.Response(
                status_code,
                json={"error": {"message": "Not supported model"}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"operations":[]}'}}],
                "model": payload["model"],
            },
        )

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="MKD",
        LLM_MIMO_API_KEY="mimo-key",
        LLM_KIMI_API_KEY="kimi-key",
        LLM_KIMI_MODEL="kimi-k2.7-code-highspeed",
        LLM_DEEPSEEK_API_KEY="deepseek-key",
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "test"}],
        temperature=0.0,
    )

    with pytest.raises(HTTPException) as exc_info:
        await client.create_chat_completion(
            request=request,
            messages=[{"role": "user", "content": "test"}],
        )

    assert exc_info.value.status_code == 502
    assert [call["model"] for call in calls] == ["mimo-v2.5-pro-ultraspeed"]


@pytest.mark.asyncio
async def test_read_timeout_does_not_resend_to_next_provider() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode("utf-8"))["model"]
        calls.append(model)
        if len(calls) == 1:
            raise httpx.ReadTimeout("possibly billed", request=request)
        return httpx.Response(200, json={"choices": [], "model": model})

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="MK",
        LLM_MIMO_API_KEY="mimo-key",
        LLM_KIMI_API_KEY="kimi-key",
    )
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

    assert exc_info.value.status_code == 504
    assert calls == ["mimo-v2.5-pro-ultraspeed"]


@pytest.mark.asyncio
async def test_m_only_priority_uses_legacy_upstream_as_implicit_d_fallback() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode("utf-8"))["model"]
        calls.append(model)
        if model == "mimo-v2.5-pro-ultraspeed":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}], "model": model},
        )

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="M",
        LLM_MIMO_API_KEY="mimo-key",
        LLM_DEEPSEEK_API_KEY="",
        UPSTREAM_BASE_URL="https://api.deepseek.com",
        UPSTREAM_API_KEY="legacy-deepseek-key",
        UPSTREAM_MODEL="deepseek-v4-flash",
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "test"}],
    )

    response = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "test"}],
    )

    assert calls == ["mimo-v2.5-pro-ultraspeed", "deepseek-v4-flash"]
    assert response["model"] == "deepseek-v4-flash"


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

    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-key",
        GATEWAY_SIGNING_SECRET="llm-test-signing-secret-0123456789abcdef",
        MODEL_GATEWAY_MEMORY_EXTRACT_MODEL="route.extract",
        MODEL_GATEWAY_MEMORY_COMPACT_MODEL="route.compact",
        MODEL_GATEWAY_MEMORY_CORE_MODEL="route.core",
        MODEL_GATEWAY_MEMORY_REVIEW_MODEL="route.review",
        LLM_PROVIDER_PRIORITY="MKD",
        LLM_MIMO_API_KEY="must-not-be-used",
        LLM_KIMI_API_KEY="must-not-be-used",
        LLM_DEEPSEEK_API_KEY="must-not-be-used",
    )
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
async def test_model_gateway_does_not_fall_back_to_legacy_provider() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={"error": {"message": "central route unavailable"}},
        )

    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-key",
        LLM_PROVIDER_PRIORITY="MKD",
        LLM_MIMO_API_KEY="must-not-be-used",
        LLM_KIMI_API_KEY="must-not-be-used",
        LLM_DEEPSEEK_API_KEY="must-not-be-used",
    )
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
    class CapturingUsageRecorder:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def record_response(self, **kwargs) -> None:
            self.calls.append(kwargs)

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

    recorder = CapturingUsageRecorder()
    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-key",
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
        usage_recorder=recorder,  # type: ignore[arg-type]
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "test"}],
    )

    response = await client.create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "test"}],
    )

    assert response["model"] == "memory.review"
    assert recorder.calls == []


def test_memory_and_knowledge_clients_share_process_cooldown_registry() -> None:
    settings = Settings(
        _env_file=None,
        LLM_MIMO_API_KEY="mimo-key",
    )
    memory_client = OpenAICompatibleClient(settings=settings)
    knowledge_client = OpenAICompatibleKnowledgeAgentClient(
        KnowledgeAgentConfig(
            fast_providers=[
                LLMProvider(
                    code="mimo",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5-pro-ultraspeed",
                )
            ]
        )
    )

    assert memory_client.cooldowns is knowledge_client.cooldowns


def json_module_dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        (
            "https://api.deepseek.com",
            "deepseek-v4-flash",
            {"thinking": {"type": "enabled"}},
        ),
        (
            "https://api.xiaomimimo.com/v1",
            "mimo-v2.5-pro-ultraspeed",
            {"thinking": {"type": "enabled"}},
        ),
        (
            "https://api.moonshot.cn/v1",
            "kimi-k2.7-code",
            {"thinking": {"type": "enabled", "keep": "all"}},
        ),
        (
            "https://api.moonshot.cn/v1",
            "kimi-k3",
            {"reasoning_effort": "max"},
        ),
        (
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-5.1",
            {"thinking": {"type": "enabled"}},
        ),
        ("https://compatible.invalid/v1", "unknown-model", {}),
    ],
)
def test_thinking_payload_matches_supported_provider_contracts(
    base_url: str,
    model: str,
    expected: dict,
) -> None:
    assert _thinking_payload(base_url=base_url, model=model) == expected
