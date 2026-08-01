import json

import httpx
import pytest

from app.config import Settings
from app.llm.routing import ProviderCooldowns
from app.openai_compat.gateway_client import (
    GatewayUpstreamHTTPError,
    OpenAIChatGatewayClient,
)


def _settings(**overrides) -> Settings:
    values = {
        "GATEWAY_API_KEY": "gateway-key",
        "LLM_PROVIDER_PRIORITY": "MD",
        "LLM_MIMO_BASE_URL": "https://mimo.invalid/v1",
        "LLM_MIMO_API_KEY": "mimo-key",
        "LLM_MIMO_MODEL": "mimo-test",
        "LLM_DEEPSEEK_BASE_URL": "https://deepseek.invalid/v1",
        "LLM_DEEPSEEK_API_KEY": "deepseek-key",
        "LLM_DEEPSEEK_FLASH_MODEL": "deepseek-test",
        "UPSTREAM_API_KEY": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_gateway_client_preserves_payload_and_fails_over() -> None:
    calls: list[tuple[httpx.Request, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request, payload))
        if request.url.host == "mimo.invalid":
            return httpx.Response(
                429,
                json={"error": {"message": "slow down"}},
                headers={"Retry-After": "120"},
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-real-client",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "reasoning_content": "kept",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
            headers={"X-Request-Id": "req-1"},
        )

    transport = httpx.MockTransport(handler)
    client = OpenAIChatGatewayClient(
        _settings(),
        transport=transport,
        cooldowns=ProviderCooldowns(),
    )
    user_content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]

    result = await client.complete(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": user_content}],
            "tools": tools,
            "stream_options": {"include_usage": True},
            "vendor_extension": {"keep": True},
            "conversation_id": "local-only",
        }
    )

    assert [call[0].url.host for call in calls] == [
        "mimo.invalid",
        "deepseek.invalid",
    ]
    assert calls[0][0].headers["authorization"] == "Bearer mimo-key"
    assert calls[1][0].headers["authorization"] == "Bearer deepseek-key"
    assert all("gateway-key" not in call[0].headers["authorization"] for call in calls)
    assert calls[0][1]["model"] == "mimo-test"
    assert calls[1][1]["model"] == "deepseek-test"
    assert calls[1][1]["messages"][0]["content"] == user_content
    assert calls[1][1]["tools"] == tools
    assert calls[1][1]["stream_options"] == {"include_usage": True}
    assert calls[1][1]["vendor_extension"] == {"keep": True}
    assert calls[1][1]["stream"] is False
    assert "conversation_id" not in calls[1][1]
    assert result.status_code == 200
    assert json.loads(result.content)["choices"][0]["message"]["reasoning_content"] == (
        "kept"
    )
    assert result.headers["x-request-id"] == "req-1"


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_gateway_client_opens_and_closes_stream_without_buffering() -> None:
    chunks = [
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    source = _ChunkStream(chunks)
    seen_payload: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=source,
        )

    client = OpenAIChatGatewayClient(
        _settings(LLM_PROVIDER_PRIORITY="D"),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )

    stream = await client.open_stream(
        {
            "model": "deepseek-test",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    iterator = stream.aiter_bytes()
    first = await anext(iterator)
    remainder = [chunk async for chunk in iterator]
    await stream.aclose()

    assert first == chunks[0]
    assert remainder == chunks[1:]
    assert seen_payload["stream"] is True
    assert seen_payload["stream_options"] == {"include_usage": True}
    assert stream.headers["content-type"] == "text/event-stream"
    assert source.closed is True


def test_gateway_client_model_list_uses_only_configured_upstreams() -> None:
    configured = OpenAIChatGatewayClient(_settings())
    empty = OpenAIChatGatewayClient(
        _settings(
            LLM_PROVIDER_PRIORITY="D",
            LLM_MIMO_API_KEY="",
            LLM_DEEPSEEK_API_KEY="",
            UPSTREAM_API_KEY="",
        )
    )

    assert configured.list_models() == ["memory-auto", "mimo-test", "deepseek-test"]
    assert empty.list_models() == []


@pytest.mark.asyncio
async def test_gateway_stream_uses_a_longer_read_timeout() -> None:
    client = OpenAIChatGatewayClient(
        _settings(
            REQUEST_TIMEOUT_SECONDS=60,
            CHAT_GATEWAY_STREAM_READ_TIMEOUT_SECONDS=600,
            CHAT_GATEWAY_STREAM_WRITE_TIMEOUT_SECONDS=120,
        )
    )
    http_client = client._new_client(stream=True)
    try:
        assert http_client.timeout.connect == 60
        assert http_client.timeout.read == 600
        assert http_client.timeout.write == 120
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_gateway_client_translates_flit_reasoning_without_conflicts() -> None:
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    cases = [
        (
            _settings(LLM_PROVIDER_PRIORITY="D"),
            "deepseek-test",
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "has_tool_choice": False,
                "temperature": None,
            },
        ),
        (
            _settings(LLM_PROVIDER_PRIORITY="M"),
            "mimo-test",
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": None,
                "has_tool_choice": True,
                "temperature": None,
            },
        ),
        (
            _settings(
                LLM_PROVIDER_PRIORITY="K",
                LLM_KIMI_BASE_URL="https://kimi-proxy.invalid/v1",
                LLM_KIMI_API_KEY="kimi-key",
                LLM_KIMI_MODEL="kimi-k2.7-code",
            ),
            "kimi-k2.7-code",
            {
                "thinking": {"type": "enabled", "keep": "all"},
                "reasoning_effort": None,
                "has_tool_choice": True,
                "temperature": 1.0,
            },
        ),
        (
            _settings(
                LLM_PROVIDER_PRIORITY="K",
                LLM_KIMI_BASE_URL="https://kimi-proxy.invalid/v1",
                LLM_KIMI_API_KEY="kimi-key",
                LLM_KIMI_MODEL="kimi-k3-test",
            ),
            "kimi-k3-test",
            {
                "thinking": None,
                "reasoning_effort": "low",
                "has_tool_choice": True,
                "temperature": None,
            },
        ),
    ]

    for settings, model, expected in cases:
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        client = OpenAIChatGatewayClient(
            settings,
            transport=httpx.MockTransport(handler),
            cooldowns=ProviderCooldowns(),
        )
        await client.complete(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": "low",
                "tools": tools,
                "tool_choice": "auto",
            }
        )

        assert captured.get("thinking") == expected["thinking"]
        assert captured.get("reasoning_effort") == expected["reasoning_effort"]
        assert ("tool_choice" in captured) is expected["has_tool_choice"]
        assert captured.get("temperature") == expected["temperature"]


@pytest.mark.asyncio
async def test_deepseek_preserves_flit_max_reasoning_strength() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(LLM_PROVIDER_PRIORITY="D"),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    await client.complete(
        {
            "model": "deepseek-test",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "max",
        }
    )

    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_streaming_rejects_successful_json_before_sending_sse_headers() -> None:
    source = _ChunkStream([b'{"choices":[]}'])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=source,
        )

    client = OpenAIChatGatewayClient(
        _settings(LLM_PROVIDER_PRIORITY="D"),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )

    with pytest.raises(GatewayUpstreamHTTPError) as error:
        await client.open_stream(
            {
                "model": "deepseek-test",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )

    assert error.value.status_code == 502
    assert b"upstream_stream_protocol_error" in error.value.content
    assert source.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash"),
        ("https://api.mistral.ai/v1", "mistral-large-latest"),
        ("https://private-proxy.invalid/v1", "zai-org/GLM-4.7-flash"),
        ("https://private-proxy.invalid/v1", "vendor/codestral-latest"),
    ],
)
async def test_gateway_strips_flit_stream_options_for_incompatible_upstreams(
    base_url: str,
    model: str,
) -> None:
    seen_payload: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_ChunkStream([b"data: [DONE]\n\n"]),
        )

    client = OpenAIChatGatewayClient(
        _settings(
            LLM_PROVIDER_PRIORITY="D",
            LLM_DEEPSEEK_BASE_URL="",
            LLM_DEEPSEEK_API_KEY="",
            LLM_DEEPSEEK_FLASH_MODEL="",
            UPSTREAM_BASE_URL=base_url,
            UPSTREAM_API_KEY="upstream-key",
            UPSTREAM_MODEL=model,
        ),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )

    stream = await client.open_stream(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    await stream.aclose()

    assert "stream_options" not in seen_payload


@pytest.mark.asyncio
async def test_gateway_strips_flit_reasoning_controls_for_mistral() -> None:
    seen_payload: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(
            LLM_PROVIDER_PRIORITY="D",
            LLM_DEEPSEEK_BASE_URL="",
            LLM_DEEPSEEK_API_KEY="",
            LLM_DEEPSEEK_FLASH_MODEL="",
            UPSTREAM_BASE_URL="https://private-proxy.invalid/v1",
            UPSTREAM_API_KEY="upstream-key",
            UPSTREAM_MODEL="vendor/codestral-latest",
        ),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    await client.complete(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
            "thinking": {"type": "enabled"},
        }
    )

    assert "reasoning_effort" not in seen_payload
    assert "thinking" not in seen_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "zai-org/GLM-5.1",
        "private/deepseek-v3.2",
    ],
)
async def test_private_proxy_models_still_receive_native_auto_thinking(
    model: str,
) -> None:
    seen_payload: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(
            LLM_PROVIDER_PRIORITY="D",
            LLM_DEEPSEEK_BASE_URL="https://private-proxy.invalid/v1",
            LLM_DEEPSEEK_API_KEY="private-key",
            LLM_DEEPSEEK_FLASH_MODEL=model,
        ),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    await client.complete(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert seen_payload["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_memory_auto_enables_native_thinking_and_respects_explicit_off() -> None:
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(
            LLM_PROVIDER_PRIORITY="K",
            LLM_KIMI_BASE_URL="https://kimi-proxy.invalid/v1",
            LLM_KIMI_API_KEY="kimi-key",
            LLM_KIMI_MODEL="kimi-k2.7-code",
        ),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )

    await client.complete(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "auto"}],
        }
    )
    await client.complete(
        {
            "model": "kimi-k2.7-code",
            "messages": [{"role": "user", "content": "exact auto"}],
        }
    )
    await client.complete(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "off"}],
            "reasoning_effort": "none",
        }
    )

    assert payloads[0]["thinking"] == {"type": "enabled", "keep": "all"}
    assert payloads[0]["temperature"] == 1.0
    assert payloads[1]["thinking"] == {"type": "enabled", "keep": "all"}
    assert payloads[2]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payloads[2]


@pytest.mark.asyncio
async def test_gateway_completes_reasoning_content_for_alias_tool_history() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(LLM_PROVIDER_PRIORITY="D"),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    await client.complete(
        {
            "model": "memory-auto",
            "messages": [
                {"role": "user", "content": "first"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function"}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
                {"role": "assistant", "content": "finished"},
                {"role": "user", "content": "second"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call_2", "type": "function"}],
                },
            ],
        }
    )

    assistant_messages = [
        message
        for message in captured["messages"]
        if message.get("role") == "assistant"
    ]
    assert [message["reasoning_content"] for message in assistant_messages] == [
        "",
        "",
        "",
    ]


@pytest.mark.asyncio
async def test_memory_auto_prefers_cached_tool_provider_before_normal_priority() -> None:
    hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    await client.complete(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "hello"}],
        },
        preferred_provider_code="D",
    )

    assert hosts == ["deepseek.invalid"]


@pytest.mark.asyncio
async def test_failover_does_not_replay_one_providers_reasoning_to_another() -> None:
    payloads: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads[request.url.host or ""] = json.loads(request.content)
        if request.url.host == "deepseek.invalid":
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAIChatGatewayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    await client.complete(
        {
            "model": "memory-auto",
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "deepseek-private-state",
                    "tool_calls": [{"id": "call_1", "type": "function"}],
                },
            ],
        },
        preferred_provider_code="D",
    )

    assert (
        payloads["deepseek.invalid"]["messages"][1]["reasoning_content"]
        == "deepseek-private-state"
    )
    assert payloads["mimo.invalid"]["messages"][1]["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_stream_failover_also_strips_the_previous_providers_reasoning() -> None:
    payloads: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads[request.url.host or ""] = json.loads(request.content)
        if request.url.host == "deepseek.invalid":
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_ChunkStream([b"data: [DONE]\n\n"]),
        )

    client = OpenAIChatGatewayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
    )
    stream = await client.open_stream(
        {
            "model": "memory-auto",
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "deepseek-private-state",
                    "tool_calls": [{"id": "call_1", "type": "function"}],
                },
            ],
        },
        preferred_provider_code="D",
    )
    await stream.aclose()

    assert (
        payloads["deepseek.invalid"]["messages"][1]["reasoning_content"]
        == "deepseek-private-state"
    )
    assert payloads["mimo.invalid"]["messages"][1]["reasoning_content"] == ""
