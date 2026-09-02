import json
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException
import httpx
import pytest

from app.config import Settings
from app.llm.model_gateway import (
    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER,
    MODEL_GATEWAY_CONNECTION_HEADER,
    MODEL_GATEWAY_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_MODEL_AUTHOR_HEADER,
    MODEL_GATEWAY_PREFERRED_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_REASONING_ORIGIN_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_REQUIRE_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_ROUTE_HEADER,
    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER,
)
from app.openai_compat.gateway_client import (
    PUBLIC_MODEL_IDS,
    GatewayUpstreamHTTPError,
    OpenAIChatGatewayClient,
    is_auto_model_id,
    memory_mode_for_model,
    resolve_public_model,
)
from app.usage.attribution import (
    MODEL_GATEWAY_CORRELATION_HEADER,
    MODEL_GATEWAY_OPERATION_HEADER,
    MODEL_GATEWAY_USER_TAG_HEADER,
)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


DEEPSEEK_QUIRKS = {
    "thinking_style": "type_object",
    "reasoning_effort_max": "max",
    "keeps_reasoning_effort": True,
    "tool_choice_with_thinking": "none",
    "requires_reasoning_replay": True,
}
MIMO_QUIRKS = {"thinking_style": "type_object", "requires_reasoning_replay": True}
KIMI_QUIRKS = {
    "thinking_style": "type_object_keep_all",
    "tool_choice_with_thinking": "auto_only",
    "forces_temperature_one": True,
    "requires_reasoning_replay": True,
}


def _provider_settings(
    providers: dict[str, dict],
    chat_targets: list[str],
    **overrides,
) -> Settings:
    """Settings whose `chat` route resolves to the given ad-hoc providers.

    Behaviour that used to be inferred from the hostname is now declared per
    provider, so a private proxy with no vendor marker in its URL is expressed
    the same way as an official endpoint.
    """
    directory = Path(tempfile.mkdtemp(prefix="gateway-client-"))
    presets = {}
    for provider_id, spec in providers.items():
        presets[provider_id] = {
            "name": provider_id,
            "protocol": "openai",
            "api_host": spec["api_host"],
            "quirks": spec.get("quirks", {}),
            "models": [{"id": model, "kind": "chat"} for model in spec["models"]],
        }
        os.environ[f"PROVIDER_{provider_id.upper()}_API_KEY"] = spec.get("api_key", "test-key")
    (directory / "providers.json").write_text(
        json.dumps({"version": 1, "presets": presets}), encoding="utf-8"
    )
    (directory / "routes.json").write_text(
        json.dumps({"version": 1, "routes": {"chat": chat_targets}}), encoding="utf-8"
    )
    values = {
        "GATEWAY_API_KEY": "gateway-key",
        "GATEWAY_SIGNING_SECRET": "gateway-test-signing-secret-0123456789abcdef",
        "PROVIDERS_PATH": str(directory / "providers.json"),
        "ROUTES_PATH": str(directory / "routes.json"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _settings(**overrides) -> Settings:
    """The default two-provider chain: MiMo first, DeepSeek as the fallback."""
    return _provider_settings(
        {
            "mimo": {
                "api_host": "https://mimo.invalid/v1",
                "api_key": "mimo-key",
                "models": ["mimo-test"],
                "quirks": MIMO_QUIRKS,
            },
            "deepseek": {
                "api_host": "https://deepseek.invalid/v1",
                "api_key": "deepseek-key",
                "models": ["deepseek-test"],
                "quirks": DEEPSEEK_QUIRKS,
            },
        },
        ["mimo/mimo-test", "deepseek/deepseek-test"],
        **overrides,
    )


def _central_settings(**overrides) -> Settings:
    values = {
        "GATEWAY_API_KEY": "gateway-key",
        "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030/v1",
        "MODEL_GATEWAY_API_KEY": "central-key",
        "MODEL_GATEWAY_CHAT_MODEL": "memory.chat.custom",
        # Deliberately configure a direct provider too: central mode must never
        # send a transparent chat request to it.
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _central_headers(
    *,
    route: str = "memory.chat.custom",
    deployment: str = "chat-primary",
    content_type: str = "application/json; charset=utf-8",
) -> dict[str, str]:
    return {
        "Content-Type": content_type,
        MODEL_GATEWAY_ROUTE_HEADER: route,
        MODEL_GATEWAY_DEPLOYMENT_HEADER: deployment,
        MODEL_GATEWAY_CONNECTION_HEADER: "official-connection",
        MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "official-channel",
        MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "model-author",
        MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "upstream-chat-model",
        "X-Model-Gateway-Attempts": "1",
        "X-Model-Gateway-Pricing": "synthetic-price",
        "X-Model-Gateway-Usage-Event-Id": "usage-synthetic-1",
        "X-Model-Gateway-Correlation-Id": "mgc_synthetic",
        "X-Model-Gateway-Usage-Ledger-Status": "recorded",
    }


@pytest.mark.asyncio
async def test_central_gateway_preserves_unknown_chat_json_and_validates_attribution() -> None:
    calls: list[tuple[httpx.Request, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-central",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "vendor_response_extension": {"kept": True},
            },
            headers=_central_headers(),
        )

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )
    user_content = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]

    result = await client.complete(
        {
            "model": "memory-auto",
            "messages": [{"role": "user", "content": user_content}],
            "tools": tools,
            "request_extension": {"keep": [1, 2, 3]},
            "conversation_id": "local-only",
        }
    )

    assert client.list_models() == [
        "memory-auto",
        "memory-read",
        "memory-off",
        "memory.chat.custom",
    ]
    assert len(calls) == 1
    request, payload = calls[0]
    assert str(request.url) == "http://127.0.0.1:2030/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer central-key"
    assert request.headers[MODEL_GATEWAY_OPERATION_HEADER] == "chat_completion"
    assert request.headers[MODEL_GATEWAY_CORRELATION_HEADER].startswith("mgc_")
    assert request.headers[MODEL_GATEWAY_USER_TAG_HEADER].startswith("usr_")
    assert "look" not in json.dumps(dict(request.headers))
    assert payload["model"] == "memory.chat.custom"
    assert payload["messages"][0]["content"] == user_content
    assert payload["tools"] == tools
    assert payload["request_extension"] == {"keep": [1, 2, 3]}
    assert payload["stream"] is False
    assert "conversation_id" not in payload
    assert result.provider.deployment_id == "chat-primary"
    assert result.provider.connection_id == "official-connection"
    assert result.provider.model == "upstream-chat-model"
    assert result.provider.vendor == "official-channel"
    assert result.headers[MODEL_GATEWAY_ROUTE_HEADER.lower()] == "memory.chat.custom"
    assert result.headers[MODEL_GATEWAY_DEPLOYMENT_HEADER.lower()] == "chat-primary"
    assert result.headers[MODEL_GATEWAY_CONNECTION_HEADER.lower()] == "official-connection"
    assert result.headers[MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER.lower()] == "official-channel"
    assert result.headers[MODEL_GATEWAY_MODEL_AUTHOR_HEADER.lower()] == "model-author"
    assert result.headers[MODEL_GATEWAY_UPSTREAM_MODEL_HEADER.lower()] == "upstream-chat-model"
    assert result.headers["x-model-gateway-attempts"] == "1"
    assert result.headers["x-model-gateway-usage-event-id"] == "usage-synthetic-1"
    assert result.headers["x-model-gateway-correlation-id"] == "mgc_synthetic"
    assert result.headers["x-model-gateway-usage-ledger-status"] == "recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested",
    [
        "memory-read",
        "memory-off",
        "Memory-Auto",
        " auto ",
        "default",
        "memory-gateway",
        "memory.chat.custom",
    ],
)
async def test_public_model_aliases_resolve_to_chat_route(requested: str) -> None:
    """Every public alias is normalized to the configured chat route upstream."""
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-alias",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
            headers=_central_headers(),
        )

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )

    await client.complete(
        {"model": requested, "messages": [{"role": "user", "content": "hi"}]}
    )

    assert payloads[-1]["model"] == "memory.chat.custom"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested",
    ["memory.extract", "memory.embedding", "knowledge.pro", "gpt-4o", "MEMORY.CHAT.CUSTOM"],
)
async def test_unknown_or_internal_models_are_rejected_before_upstream(requested: str) -> None:
    """Internal routes must never be reachable through the public chat proxy."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected models must not reach the Model Gateway")

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as excinfo:
        await client.complete(
            {"model": requested, "messages": [{"role": "user", "content": "hi"}]}
        )

    assert excinfo.value.status_code == 404
    assert requested in str(excinfo.value.detail)


def test_resolve_public_model_and_mode_helpers() -> None:
    assert PUBLIC_MODEL_IDS == ("memory-auto", "memory-read", "memory-off")

    assert memory_mode_for_model("memory-auto") is None
    assert memory_mode_for_model("MEMORY-READ") == "read"
    assert memory_mode_for_model(" memory-off ") == "off"
    assert memory_mode_for_model("memory.chat.custom") is None
    assert memory_mode_for_model("gpt-4o") is None

    for alias in (*PUBLIC_MODEL_IDS, "auto", "default", "memory-gateway"):
        assert is_auto_model_id(alias) is True
    assert is_auto_model_id("memory.chat.custom") is False

    explicit = resolve_public_model("memory.chat.custom", route="memory.chat.custom")
    assert explicit is not None
    assert explicit.is_alias is False
    assert explicit.memory_mode is None
    # The explicit route is exact-match only; aliases are case-insensitive.
    assert resolve_public_model("MEMORY.CHAT.CUSTOM", route="memory.chat.custom") is None
    assert resolve_public_model("gpt-4o", route="memory.chat.custom") is None


@pytest.mark.asyncio
async def test_central_gateway_stream_is_byte_preserving_and_requires_affinity() -> None:
    chunks = [
        b'data: {"choices":[{"delta":{"vendor_field":true,"content":"a"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    source = _ChunkStream(chunks)
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers=_central_headers(
                deployment="chat-affinity",
                content_type="text/event-stream",
            ),
            stream=source,
        )

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )
    stream = await client.open_stream(
        {
            "model": "memory.chat.custom",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "unknown_stream_extension": "kept",
        },
        preferred_provider_code="chat-affinity",
    )
    received = [chunk async for chunk in stream.aiter_bytes()]
    await stream.aclose()

    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-model-gateway-preferred-deployment"] == "chat-affinity"
    assert headers["x-model-gateway-require-deployment"] == "chat-affinity"
    assert headers["x-model-gateway-reasoning-origin-deployment"] == "chat-affinity"
    assert captured["payload"]["unknown_stream_extension"] == "kept"
    assert received == chunks
    assert stream.provider.deployment_id == "chat-affinity"
    assert stream.headers[MODEL_GATEWAY_ROUTE_HEADER.lower()] == "memory.chat.custom"
    assert stream.headers[MODEL_GATEWAY_DEPLOYMENT_HEADER.lower()] == "chat-affinity"
    assert stream.headers["x-model-gateway-attempts"] == "1"
    assert source.closed is True


@pytest.mark.asyncio
async def test_central_gateway_preserves_explicit_affinity_rejection() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "headers": dict(request.headers),
                "payload": json.loads(request.content),
            }
        )
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "model_gateway_affinity_unavailable",
                    "message": "old deployment unavailable",
                }
            },
        )

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GatewayUpstreamHTTPError) as exc_info:
        await client.complete(
            {
                "model": "memory-auto",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "tool history",
                        "reasoning": "private-a",
                        "reasoning_content": "private-b",
                    },
                    {"role": "user", "content": "continue"},
                ],
            },
            preferred_provider_code="chat-old",
        )

    assert exc_info.value.status_code == 409
    assert b"model_gateway_affinity_unavailable" in exc_info.value.content
    assert len(calls) == 1
    assert calls[0]["headers"][
        MODEL_GATEWAY_PREFERRED_DEPLOYMENT_HEADER.lower()
    ] == "chat-old"
    assert calls[0]["headers"][
        MODEL_GATEWAY_REQUIRE_DEPLOYMENT_HEADER.lower()
    ] == "chat-old"
    assert calls[0]["headers"][
        MODEL_GATEWAY_REASONING_ORIGIN_DEPLOYMENT_HEADER.lower()
    ] == "chat-old"
    assistant = calls[0]["payload"]["messages"][0]
    assert assistant["reasoning"] == "private-a"
    assert assistant["reasoning_content"] == "private-b"


@pytest.mark.asyncio
async def test_central_stream_preserves_explicit_affinity_rejection() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "headers": dict(request.headers),
                "payload": json.loads(request.content),
            }
        )
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "model_gateway_affinity_unavailable",
                    "message": "old deployment unavailable",
                }
            },
        )

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GatewayUpstreamHTTPError) as exc_info:
        await client.open_stream(
            {
                "model": "memory-auto",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "tool history",
                        "reasoning_content": "private-stream-reasoning",
                    },
                    {"role": "user", "content": "continue"},
                ],
                "stream": True,
            },
            preferred_provider_code="chat-old",
        )

    assert exc_info.value.status_code == 409
    assert b"model_gateway_affinity_unavailable" in exc_info.value.content
    assert len(calls) == 1
    assert calls[0]["headers"][
        MODEL_GATEWAY_REQUIRE_DEPLOYMENT_HEADER.lower()
    ] == "chat-old"
    assert (
        calls[0]["payload"]["messages"][0]["reasoning_content"]
        == "private-stream-reasoning"
    )


@pytest.mark.asyncio
async def test_central_gateway_rejects_missing_or_wrong_attribution() -> None:
    responses = [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(
            200,
            json={"choices": []},
            headers=_central_headers(route="memory.extract"),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = OpenAIChatGatewayClient(
        _central_settings(),
        transport=httpx.MockTransport(handler),
    )

    for _ in range(2):
        with pytest.raises(GatewayUpstreamHTTPError) as error:
            await client.complete(
                {
                    "model": "memory-auto",
                    "messages": [{"role": "user", "content": "hello"}],
                }
            )
        assert error.value.status_code == 502
        assert b"model_gateway_protocol_error" in error.value.content
