from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from model_gateway.models import GatewayConfig
from model_gateway.routing import RouteTarget
from model_gateway.storage import StorageFaultMonitor
from model_gateway.upstream_executor import (
    ProxyResponseTooLarge,
    UpstreamExecutor,
    UsageLedgerPreflightError,
)
from model_gateway.usage import UsageStore


def _target(config: GatewayConfig) -> RouteTarget:
    deployment = config.deployments["chat-official"]
    return RouteTarget(
        route_id="executor.test",
        deployment_id="chat-official",
        deployment=deployment,
        connection_id=deployment.connection,
        connection=config.connections[deployment.connection],
    )


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_accounted_exact_post_uses_safe_wire_contract_and_records_usage(
    tmp_path: Path,
    gateway_config: GatewayConfig,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "req-safe-1",
                "model": "author/chat-v1",
                "choices": [],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    async with UpstreamExecutor(transport=httpx.MockTransport(handler)) as executor:
        result = await executor.post_json_accounted(
            target=_target(gateway_config),
            payload={"model": "author/chat-v1", "messages": []},
            secret="provider-secret",
            request_headers={
                "accept": "application/json",
                "authorization": "Bearer must-not-forward",
            },
            usage_store=store,
            server=gateway_config.server,
            pricing_catalog=gateway_config.pricing,
            client_id="executor-test",
        )

    assert result.is_success is True
    assert result.usage_ledger_status == "complete"
    assert result.usage_event_id
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://official.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer provider-secret"
    assert request.headers["accept-encoding"] == "identity"
    with sqlite3.connect(store.path) as connection:
        usage = connection.execute(
            "SELECT route_id, client_id, input_tokens, output_tokens, attempts "
            "FROM usage_events"
        ).fetchone()
        attempt = connection.execute(
            "SELECT route_id, attempt_index, outcome, failure_class "
            "FROM attempt_events"
        ).fetchone()
    assert usage == ("executor.test", "executor-test", 3, 2, 1)
    assert attempt == ("executor.test", 1, "success", "none")


@pytest.mark.asyncio
async def test_ledger_preflight_failure_makes_zero_provider_calls(
    tmp_path: Path,
    gateway_config: GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    store = UsageStore(tmp_path / "usage.db")
    store.init_db()

    def unavailable() -> None:
        raise sqlite3.OperationalError("private path must not leak")

    monkeypatch.setattr(store, "probe_writable", unavailable)
    async with UpstreamExecutor(transport=httpx.MockTransport(handler)) as executor:
        with pytest.raises(UsageLedgerPreflightError):
            await executor.post_json_accounted(
                target=_target(gateway_config),
                payload={"model": "author/chat-v1", "messages": []},
                secret="provider-secret",
                usage_store=store,
                server=gateway_config.server,
                pricing_catalog=gateway_config.pricing,
                client_id="executor-test",
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_post_send_ledger_failure_preserves_provider_result_and_latches(
    tmp_path: Path,
    gateway_config: GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    monitor = StorageFaultMonitor()

    def fail_record(**_kwargs: object) -> str:
        raise sqlite3.OperationalError("database is unavailable")

    monkeypatch.setattr(store, "record", fail_record)
    async with UpstreamExecutor(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [], "usage": {"total_tokens": 1}},
            )
        )
    ) as executor:
        result = await executor.post_json_accounted(
            target=_target(gateway_config),
            payload={"model": "author/chat-v1", "messages": []},
            secret="provider-secret",
            usage_store=store,
            server=gateway_config.server,
            pricing_catalog=gateway_config.pricing,
            client_id="executor-test",
            storage_monitor=monitor,
        )

    assert result.status_code == 200
    assert result.content
    assert result.usage_ledger_status == "incomplete"
    assert result.usage_event_id == ""
    assert monitor.consume_after_successful_probe() == "disk_unavailable"


@pytest.mark.asyncio
async def test_exact_stream_returns_bounded_raw_lease_after_first_byte(
    gateway_config: GatewayConfig,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=_ChunkStream([b"ab", b"cd"]),
            headers={
                "content-type": "text/event-stream",
                "content-encoding": "vendor-raw",
            },
        )

    async with UpstreamExecutor(transport=httpx.MockTransport(handler)) as executor:
        result = await executor.open_json_stream(
            target=_target(gateway_config),
            payload={
                "model": "author/chat-v1",
                "messages": [],
                "stream": True,
            },
            secret="provider-secret",
            request_headers={"accept": "text/event-stream"},
            response_limit_bytes=3,
        )

        assert result.is_success is True
        assert result.lease is not None
        assert result.trace is not None
        assert result.trace.response_complete is False
        iterator = result.lease.aiter_raw()
        assert await anext(iterator) == b"ab"
        with pytest.raises(ProxyResponseTooLarge):
            await anext(iterator)
        assert result.trace.failure_class == "response_too_large"
        assert result.trace.billable_unknown is True
        assert result.trace.response_complete is False
        await result.lease.aclose()

    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://official.example/v1/chat/completions"
    )
    assert requests[0].headers["authorization"] == "Bearer provider-secret"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert result.headers["content-encoding"] == "vendor-raw"
