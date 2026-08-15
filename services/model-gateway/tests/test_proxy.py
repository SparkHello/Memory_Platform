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

from conftest import BACKEND_CLIENT_TOKEN


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
        request_headers={"authorization": f"Bearer {BACKEND_CLIENT_TOKEN}", "accept": "application/json"},
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
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
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
    assert router.runtime_health.remaining("official") > 599
    assert len(result.attempt_traces) == 2
    assert result.attempt_traces[0].failure_class == "http_rate_limit"
    assert result.attempt_traces[0].request_sent is True
    assert result.attempt_traces[1].outcome == "success"


@pytest.mark.asyncio
async def test_connect_timeout_can_fallback_before_request_is_sent(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "official.example":
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(200, content=b'{"id":"fallback"}')

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

    assert result.status_code == 200
    assert result.attempts == 2
    assert calls == ["official.example", "reseller.example"]
    assert result.attempt_traces[0].failure_class == "connect_timeout"
    assert result.attempt_traces[0].request_sent is False


@pytest.mark.asyncio
async def test_connection_breaker_is_rechecked_before_each_attempt(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    # Both deployments share one connection here; same_channel keeps the
    # second deployment eligible so the connection breaker is what stops it.
    gateway_config.routes["memory.chat"].fallback_scope = "same_channel"
    gateway_config.deployments["chat-reseller"].connection = "official"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})

    router = Router()
    result = await RawOpenAIProxy(
        router=router,
        transport=httpx.MockTransport(handler),
    ).complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a"},
        request_headers={},
    )

    assert result.status_code == 401
    assert calls == ["author/chat-v1"]
    assert router.runtime_health.remaining_target("official", "chat-reseller") > 0


@pytest.mark.asyncio
async def test_model_not_found_breaker_is_deployment_scoped(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    # Both deployments share one connection here; same_channel keeps the
    # second deployment eligible while breakers stay deployment-scoped.
    gateway_config.routes["memory.chat"].fallback_scope = "same_channel"
    gateway_config.deployments["chat-reseller"].connection = "official"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(
            400,
            json={"error": {"code": "model_not_found", "message": "private"}},
        )

    router = Router()
    result = await RawOpenAIProxy(
        router=router,
        transport=httpx.MockTransport(handler),
    ).complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a"},
        request_headers={},
    )

    assert result.status_code == 400
    assert calls == ["author/chat-v1"]
    assert router.runtime_health.remaining_target("official", "chat-official") > 0
    assert router.runtime_health.remaining_target("official", "chat-reseller") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 402, 404])
async def test_auth_billing_and_plain_not_found_never_replay_to_next_target(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
    status_code: int,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(status_code, json={"error": {"code": "rejected"}})

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

    assert result.status_code == status_code
    assert result.attempts == 1
    assert calls == ["official.example"]


@pytest.mark.asyncio
async def test_provider_response_is_capped_without_fallback(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    gateway_config.connections["official"].response_limit_bytes = 1024
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, content=b"x" * 1025)

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

    assert result.status_code == 502
    assert calls == ["official.example"]
    assert result.attempt_traces[0].failure_class == "response_too_large"


@pytest.mark.asyncio
async def test_non_stream_response_limit_stops_reading_upstream_immediately(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.connections["official"].response_limit_bytes = 1024
    stream = ChunkStream([b"a" * 700, b"b" * 700, b"SECRET"])
    yielded = 0
    original_chunks = stream.chunks

    class CountingAsyncStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal yielded
            for chunk in original_chunks:
                yielded += 1
                yield chunk

        async def aclose(self) -> None:
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingAsyncStream())

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.complete(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": []},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )

    assert result.status_code == 502
    assert yielded == 2
    assert b"SECRET" not in result.content
    await proxy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.WriteTimeout])
async def test_ambiguous_timeout_does_not_fallback_and_risk_double_billing(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
    error_type: type[httpx.TimeoutException],
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        raise error_type("ambiguous timeout", request=request)

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

    assert result.status_code == 502
    assert result.target is not None
    assert result.target.deployment_id == "chat-official"
    assert result.headers["x-model-gateway-deployment"] == "chat-official"
    assert json.loads(result.content)["error"]["code"] == (
        "model_gateway_ambiguous_upstream_error"
    )
    assert calls == ["official.example"]


@pytest.mark.asyncio
async def test_missing_secret_does_not_consume_route_attempt(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
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
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
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
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
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
    assert calls == ["official.example"]
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
async def test_stream_connect_failure_can_fallback_before_request_is_sent(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "official.example":
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(
            200,
            stream=ChunkStream([b"data: fallback\n\n"]),
            headers={"content-type": "text/event-stream"},
        )

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.open_stream(
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
        assert calls == ["official.example", "reseller.example"]
        assert result.attempts == 2
        assert result.attempt_traces[0].failure_class == "connect_timeout"
        assert result.attempt_traces[0].request_sent is False
        assert result.headers["x-model-gateway-deployment"] == "chat-reseller"
    finally:
        await result.aclose()
        await proxy.aclose()


@pytest.mark.asyncio
async def test_stream_http_failure_can_fallback_before_successful_stream(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.routes["memory.chat"].fallback_scope = "any_channel"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "official.example":
            return httpx.Response(
                429,
                content=b'{"error":"limited"}',
                headers={"retry-after": "600"},
            )
        return httpx.Response(
            200,
            stream=ChunkStream([b"data: fallback\n\n"]),
            headers={"content-type": "text/event-stream"},
        )

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.open_stream(
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
        assert calls == ["official.example", "reseller.example"]
        assert result.attempts == 2
        assert result.attempt_traces[0].failure_class == "http_rate_limit"
        assert result.attempt_traces[0].request_sent is True
        assert router.runtime_health.remaining("official") > 599
        assert result.headers["x-model-gateway-channel-operator"] == (
            "reseller-vendor"
        )
    finally:
        await result.aclose()
        await proxy.aclose()


@pytest.mark.asyncio
async def test_stream_compressed_response_preserves_raw_bytes_and_encoding(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    compressed = gzip.compress(b"data: compressed\n\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=ChunkStream([compressed]),
            headers={
                "content-type": "text/event-stream",
                "content-encoding": "gzip",
            },
        )

    router = Router()
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.open_stream(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": [], "stream": True},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )

    assert not isinstance(result, ProxyHTTPResult)
    try:
        assert b"".join([chunk async for chunk in result.aiter_raw()]) == compressed
        assert result.headers["content-encoding"] == "gzip"
    finally:
        await result.aclose()
        await proxy.aclose()


@pytest.mark.asyncio
async def test_stream_real_transport_runs_destination_validation_before_send(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[str] = []

    async def reject_destination(
        url: str,
        *,
        allowed_private_networks: list[str],
    ) -> None:
        del allowed_private_networks
        validated.append(url)
        raise ValueError("unsafe destination")

    monkeypatch.setattr(
        "model_gateway.upstream_executor.require_safe_destination",
        reject_destination,
    )
    router = Router()
    proxy = RawOpenAIProxy(router=router)
    result = await proxy.open_stream(
        route=resolved_chat(gateway_config, backend_client, router),
        payload={"model": "memory.chat", "messages": [], "stream": True},
        secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
        request_headers={},
    )
    await proxy.aclose()

    assert isinstance(result, ProxyHTTPResult)
    assert result.attempts == 0
    assert validated == [
        "https://official.example/v1/chat/completions",
    ]


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
async def test_stream_enforces_cumulative_response_limit(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    gateway_config.connections["official"].response_limit_bytes = 1024

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=ChunkStream([b"a" * 600, b"b" * 600]),
            headers={"content-type": "text/event-stream"},
        )

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
    assert result.active_trace.failure_class == "response_too_large"
    assert result.active_trace.billable_unknown is True
    await result.aclose()
    await proxy.aclose()


@pytest.mark.asyncio
async def test_stream_read_failure_before_first_byte_does_not_risk_double_billing(
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

    assert isinstance(result, ProxyHTTPResult)
    assert result.status_code == 502
    assert json.loads(result.content)["error"]["code"] == (
        "model_gateway_ambiguous_upstream_error"
    )
    assert calls == ["official.example"]


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
        assert json.loads(request.content) == {
            "model": "author/embed-v1",
            "input": ["hello", "world"],
            "dimensions": 4,
        }
        return httpx.Response(
            200,
            content=(
                b'{"data":[{"embedding":[0,1,2,3]},'
                b'{"embedding":[4,5,6,7]}]}'
            ),
        )

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
        payload={
            "model": "memory.embedding",
            "input": ["hello", "world"],
            "dimensions": 999,
        },
        secrets={"UPSTREAM_OFFICIAL": "a"},
        request_headers={},
    )
    assert result.headers["x-model-gateway-embedding-space"] == "author.embed-v1:4"
    assert result.headers["x-model-gateway-embedding-dimensions"] == "4"


@pytest.mark.asyncio
async def test_embedding_response_rejects_any_wrong_vector_before_attribution(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    marker = "provider-private-marker"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0, 1, 2, 3]},
                    {"embedding": [0, 1, 2]},
                ],
                "private": marker,
            },
        )

    router = Router()
    route = router.resolve(
        requested_model="memory.embedding",
        kind="embedding",
        client=backend_client,
        config=gateway_config,
    )
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    result = await proxy.complete(
        route=route,
        payload={"model": "memory.embedding", "input": ["a", "b"]},
        secrets={"UPSTREAM_OFFICIAL": "a"},
        request_headers={},
    )

    assert result.status_code == 502
    assert result.attempt_traces[0].failure_class == "invalid_embedding_response"
    assert "x-model-gateway-embedding-space" not in result.headers
    assert marker.encode() not in result.content
    await proxy.aclose()


@pytest.mark.asyncio
async def test_proxy_reuses_async_client_until_service_shutdown(
    gateway_config: GatewayConfig,
    backend_client: AuthenticatedClient,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    router = Router()
    route = resolved_chat(gateway_config, backend_client, router)
    proxy = RawOpenAIProxy(router=router, transport=httpx.MockTransport(handler))
    for _ in range(2):
        result = await proxy.complete(
            route=route,
            payload={"model": "memory.chat", "messages": []},
            secrets={"UPSTREAM_OFFICIAL": "a", "UPSTREAM_RESELLER": "b"},
            request_headers={},
        )
        assert result.status_code == 200
    assert len(proxy._clients) == 1
    pooled = next(iter(proxy._clients.values()))
    assert not pooled.is_closed
    await proxy.aclose()
    assert pooled.is_closed
    assert proxy._clients == {}
