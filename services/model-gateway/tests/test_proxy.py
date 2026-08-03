from __future__ import annotations

import gzip
import json
from typing import AsyncIterator

import httpx
import pytest

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import GatewayConfig
from model_gateway.proxy import ProxyHTTPResult, RawOpenAIProxy
from model_gateway.routing import Router


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, fail_after: int | None = None):
        self.chunks = chunks
        self.fail_after = fail_after

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if self.fail_after is not None and index >= self.fail_after:
                raise httpx.ReadError("stream broke")
            yield chunk

    async def aclose(self) -> None:
        return None


def resolved_chat(
    config: GatewayConfig, client: AuthenticatedClient, router: Router
):
    return router.resolve(
        requested_model="memory.chat",
        kind="chat",
        client=client,
        config=config,
    )


@pytest.mark.asyncio
async def test_transparent_body_and_response_preservation(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    captured: dict[str, object] = {}
    raw_response = b'{"id":"r1","unknown":{"nested":true},"reasoning_content":"secret-shape"}'

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        captured["encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            content=raw_response,
            headers={"content-type": "application/json", "x-request-id": "upstream-r1"},
        )

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    payload = {
        "model": "memory.chat",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
                "reasoning_content": "opaque reasoning",
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        ],
        "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}],
        "vendor_extension": {"keep": [1, 2, 3]},
    }
    result = await proxy.complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload=payload,
        secrets={"UPSTREAM_OFFICIAL": "official-secret", "UPSTREAM_RESELLER": "reseller-secret"},
        request_headers={"authorization": "Bearer local-client-token", "accept": "application/json"},
    )

    expected = dict(payload)
    expected["model"] = "author/chat-v1"
    assert captured["payload"] == expected
    assert captured["authorization"] == "Bearer official-secret"
    assert captured["encoding"] == "identity"
    assert result.content == raw_response
    assert result.headers["x-request-id"] == "upstream-r1"
    assert result.headers["x-model-gateway-deployment"] == "chat-official"
    assert result.headers["x-model-gateway-channel-operator"] == "official-vendor"
    assert result.headers["x-model-gateway-model-author"] == "author"
    assert result.headers["x-model-gateway-vendor"] == "official-vendor"
    assert payload["model"] == "memory.chat"


@pytest.mark.asyncio
async def test_non_stream_compressed_response_preserves_raw_bytes_and_encoding(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    raw = b'{"id":"compressed","unknown":true}'
    compressed = gzip.compress(raw)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=ChunkStream([compressed]),
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    router = Router()
    result = await RawOpenAIProxy(
        router=router,
        transport=httpx.MockTransport(handler),
    ).complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )

    assert result.content == compressed
    assert result.headers["content-encoding"] == "gzip"


@pytest.mark.asyncio
async def test_fallback_on_rate_limit_before_response(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "official.example":
            return httpx.Response(429, content=b'{"error":"limited"}', headers={"retry-after": "600"})
        return httpx.Response(200, content=b'{"id":"fallback"}')

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    assert result.status_code == 200
    assert calls == ["official.example", "reseller.example"]
    assert result.attempts == 2
    assert result.headers["x-model-gateway-attempts"] == "2"
    assert router.cooldowns.remaining("official") > 599


@pytest.mark.asyncio
async def test_missing_secret_does_not_consume_route_attempt(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].max_attempts = 1
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, content=b'{"id":"ok"}')

    router = Router()
    result = await RawOpenAIProxy(
        router=router, transport=httpx.MockTransport(handler)
    ).complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    assert result.status_code == 200
    assert calls == ["reseller.example"]
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_no_fallback_for_policy_rejection(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, content=b'{"error":"policy"}')

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    assert result.status_code == 403
    assert calls == 1


@pytest.mark.asyncio
async def test_redirect_never_forwards_credentials_or_location_to_local_client(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.example/collect"},
        )

    router = Router()
    result = await RawOpenAIProxy(
        router=router, transport=httpx.MockTransport(handler)
    ).complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    assert calls == ["official.example", "reseller.example"]
    assert result.status_code == 502
    assert "location" not in result.headers
    assert b"attacker.example" not in result.content


@pytest.mark.asyncio
async def test_sse_chunks_are_forwarded_byte_for_byte(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"r"}}]}\n\n',
        b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=ChunkStream(chunks),
            headers={"content-type": "text/event-stream"},
        )

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.open_stream(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": [], "stream": True},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={"accept": "text/event-stream"},
    )
    assert not isinstance(result, ProxyHTTPResult)
    try:
        assert b"".join([chunk async for chunk in result.aiter_raw()]) == b"".join(chunks)
    finally:
        await result.aclose()


@pytest.mark.asyncio
async def test_stream_does_not_failover_after_first_byte(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "official.example":
            return httpx.Response(
                200,
                stream=ChunkStream([b"data: first\n\n", b"data: second\n\n"], fail_after=1),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, stream=ChunkStream([b"data: fallback\n\n"]))

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.open_stream(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": [], "stream": True},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    assert not isinstance(result, ProxyHTTPResult)
    with pytest.raises(httpx.ReadError):
        _ = [chunk async for chunk in result.aiter_raw()]
    await result.aclose()
    assert calls == ["official.example"]


@pytest.mark.asyncio
async def test_stream_can_fail_over_until_first_non_empty_downstream_byte(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "official.example":
            return httpx.Response(
                200,
                stream=ChunkStream([b"", b"never"], fail_after=1),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            stream=ChunkStream([b"data: fallback\n\n"]),
            headers={"content-type": "text/event-stream"},
        )

    router = Router()
    result = await RawOpenAIProxy(
        router=router,
        transport=httpx.MockTransport(handler),
    ).open_stream(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": [], "stream": True},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )

    assert not isinstance(result, ProxyHTTPResult)
    try:
        assert b"".join([chunk async for chunk in result.aiter_raw()]) == (
            b"data: fallback\n\n"
        )
    finally:
        await result.aclose()
    assert calls == ["official.example", "reseller.example"]


@pytest.mark.asyncio
async def test_strict_affinity_returns_409_instead_of_switching_deployment(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(429, content=b'{"error":"limited"}')

    router = Router()
    route = router.resolve(
        requested_model="memory.chat",
        kind="chat",
        client=backend_client,
        config=gateway_config,
        required_deployment="chat-official",
    )
    result = await RawOpenAIProxy(
        router=router, transport=httpx.MockTransport(handler)
    ).complete(
        route=route,
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    assert calls == ["official.example"]
    assert result.status_code == 409
    assert json.loads(result.content)["error"]["code"] == "model_gateway_affinity_unavailable"


@pytest.mark.asyncio
async def test_embedding_response_declares_vector_identity(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "author/embed-v1"
        return httpx.Response(200, content=b'{"data":[{"embedding":[0,1,2,3]}]}')

    router = Router()
    route = router.resolve(
        requested_model="memory.embedding",
        kind="embedding",
        client=backend_client,
        config=gateway_config,
    )
    result = await RawOpenAIProxy(
        router=router, transport=httpx.MockTransport(handler)
    ).complete(
        route=route,
        payload={"model": "memory.embedding", "input": ["hello"]},
        secrets={"UPSTREAM_OFFICIAL": "a"},
        request_headers={},
    )
    assert result.headers["x-model-gateway-embedding-space"] == "author.embed-v1:4"
    assert result.headers["x-model-gateway-embedding-dimensions"] == "4"
