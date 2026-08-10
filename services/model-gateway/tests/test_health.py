from __future__ import annotations

import json

import httpx
import pytest

from conftest import config_payload
from model_gateway.health import check_health
from model_gateway.models import GatewayConfig


@pytest.mark.asyncio
async def test_discovery_is_grouped_by_connection_and_unlisted_is_only_warning(
    gateway_config: GatewayConfig,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer provider-secret"
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "author/chat-v1"}]},
        )

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(handler),
    )

    assert len(requests) == 1
    connection = report.connections[0]
    assert connection.status == "connected"
    assert connection.discovered_model_count == 1
    by_id = {item.deployment_id: item for item in connection.deployments}
    assert by_id["chat-official"].status == "available"
    assert by_id["embed-official"].status == "connected_unlisted"
    assert by_id["embed-official"].level == "warning"
    assert "不表示模型已废弃" in by_id["embed-official"].detail
    assert "provider-secret" not in json.dumps(report.as_dict(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_live_check_sends_minimum_chat_and_embedding_requests(
    gateway_config: GatewayConfig,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.method == "POST"
        if request.url.path == "/v1/chat/completions":
            assert payload == {
                "model": "author/chat-v1",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        assert request.url.path == "/v1/embeddings"
        assert payload == {
            "model": "author/embed-v1",
            "input": ["ping"],
            "dimensions": 4,
        }
        return httpx.Response(200, json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]})

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        live=True,
        transport=httpx.MockTransport(handler),
    )

    assert len(requests) == 2
    assert report.mode == "live"
    assert report.connections[0].status == "live_ok"
    assert {item.status for item in report.connections[0].deployments} == {"live_ok"}


@pytest.mark.asyncio
async def test_live_embedding_check_validates_every_returned_vector(
    gateway_config: GatewayConfig,
) -> None:
    gateway_config.deployments = {
        "embed-official": gateway_config.deployments["embed-official"]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["dimensions"] == 4
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0, 0.0, 0.0, 0.0]},
                    {"embedding": [0.0, 0.0, 0.0]},
                ]
            },
        )

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        live=True,
        transport=httpx.MockTransport(handler),
    )

    deployment = report.connections[0].deployments[0]
    assert deployment.status == "dimension_mismatch"
    assert deployment.level == "error"


@pytest.mark.asyncio
async def test_backend_check_never_touches_restricted_connection() -> None:
    payload = config_payload()
    payload["connections"]["official"]["billing_plan"] = {
        "type": "token_plan",
        "name": "interactive coding only",
    }
    payload["connections"]["official"]["usage_scope"] = "interactive_only"
    config = GatewayConfig.model_validate(payload)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    report = await check_health(
        config=config,
        secrets={"UPSTREAM_OFFICIAL": "must-not-be-used"},
        connection_id="official",
        live=True,
        client_kind="backend",
        transport=httpx.MockTransport(handler),
    )

    assert calls == 0
    assert report.connections[0].status == "policy_blocked"
    assert all(
        item.status == "policy_blocked"
        for item in report.connections[0].deployments
    )


@pytest.mark.asyncio
async def test_discovery_auth_failure_does_not_include_provider_body(
    gateway_config: GatewayConfig,
) -> None:
    sensitive = "provider echoed a secret value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": sensitive}})

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(handler),
    )

    serialized = json.dumps(report.as_dict(), ensure_ascii=False)
    assert report.connections[0].status == "auth_failed"
    assert sensitive not in serialized
    assert "provider-secret" not in serialized


@pytest.mark.asyncio
async def test_discovery_response_limit_stops_before_later_chunks(
    gateway_config: GatewayConfig,
) -> None:
    yielded: list[int] = []

    class CountingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunks = [
                b"a" * (1024 * 1024),
                b"b" * (1024 * 1024 + 1),
                b"SECRET",
            ]
            for index, chunk in enumerate(chunks):
                yielded.append(index)
                yield chunk

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream())

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(handler),
    )

    assert report.connections[0].status == "invalid_response"
    assert yielded == [0, 1]


@pytest.mark.asyncio
async def test_health_probe_does_not_follow_redirect_with_credential(
    gateway_config: GatewayConfig,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://attacker.example/models"})

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(handler),
    )

    assert len(requests) == 1
    assert requests[0].url.host == "official.example"
    assert report.connections[0].status == "provider_error"


@pytest.mark.asyncio
async def test_live_chat_check_rejects_unstructured_http_200(
    gateway_config: GatewayConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, text="login page")
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]},
        )

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        live=True,
        transport=httpx.MockTransport(handler),
    )

    by_id = {
        item.deployment_id: item for item in report.connections[0].deployments
    }
    assert by_id["chat-official"].status == "invalid_response"
    assert by_id["chat-official"].level == "error"
    assert by_id["embed-official"].status == "live_ok"
    assert report.connections[0].status == "degraded"


@pytest.mark.asyncio
async def test_discovery_response_size_is_bounded(
    gateway_config: GatewayConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(handler),
    )

    connection = report.connections[0]
    assert connection.status == "invalid_response"
    assert connection.level == "error"
    assert "安全上限" in connection.detail
