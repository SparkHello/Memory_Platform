from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from conftest import config_payload
from model_gateway.health import check_health
from model_gateway.models import GatewayConfig
from model_gateway.storage import StorageFaultMonitor
from model_gateway.usage import UsageStore


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


@pytest.mark.asyncio
async def test_live_health_records_one_exact_operation_per_provider_post(
    tmp_path: Path,
    gateway_config: GatewayConfig,
) -> None:
    gateway_config.deployments = {
        "chat-official": gateway_config.deployments["chat-official"]
    }
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        live=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
        ),
        usage_store=store,
    )

    deployment = report.connections[0].deployments[0]
    assert deployment.status == "live_ok"
    assert deployment.usage_ledger_status == "complete"
    with sqlite3.connect(store.path) as connection:
        usage = connection.execute(
            "SELECT route_id, operation, client_id, attempts FROM usage_events"
        ).fetchall()
        attempts = connection.execute(
            "SELECT route_id, client_id FROM attempt_events"
        ).fetchall()
    assert usage == [
        ("health.live", "health.live", "modelgw-health-check", 1)
    ]
    assert attempts == [("health.live", "modelgw-health-check")]


@pytest.mark.asyncio
async def test_live_health_keeps_success_as_warning_when_ledger_record_fails(
    tmp_path: Path,
    gateway_config: GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_config.deployments = {
        "chat-official": gateway_config.deployments["chat-official"]
    }
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    monitor = StorageFaultMonitor()

    def fail_record(**_kwargs: object) -> str:
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(store, "record", fail_record)
    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        live=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        ),
        usage_store=store,
        storage_monitor=monitor,
    )

    connection = report.connections[0]
    deployment = connection.deployments[0]
    assert deployment.status == "live_ok"
    assert deployment.level == "warning"
    assert deployment.usage_ledger_status == "incomplete"
    assert "结果仍保留" in deployment.detail
    assert connection.level == "warning"
    assert monitor.consume_after_successful_probe() == "disk_unavailable"


def _deployment_health(deployment_id: str, level: str) -> "DeploymentHealth":
    from model_gateway.health import DeploymentHealth

    status_by_level = {
        "ok": "available",
        "warning": "connected_unlisted",
        "error": "invalid_response",
        "skipped": "disabled",
    }
    return DeploymentHealth(
        deployment_id=deployment_id,
        kind="chat",
        upstream_model="author/model",
        status=status_by_level[level],
        level=level,
        detail="detail",
    )


def test_health_report_summary_counts_each_level_exactly() -> None:
    import dataclasses

    from model_gateway.health import ConnectionHealth, HealthReport

    healthy = ConnectionHealth(
        connection_id="c1",
        channel_operator="op",
        status="connected",
        level="ok",
        detail="",
        deployments=(
            _deployment_health("d1", "ok"),
            _deployment_health("d2", "warning"),
        ),
    )
    failing = ConnectionHealth(
        connection_id="c2",
        channel_operator="op",
        status="invalid_response",
        level="error",
        detail="",
        deployments=(_deployment_health("d3", "error"),),
    )
    skipped = ConnectionHealth(
        connection_id="c3",
        channel_operator="op",
        status="disabled",
        level="skipped",
        detail="",
        deployments=(_deployment_health("d4", "skipped"),),
    )
    report = HealthReport(mode="discovery", connections=(healthy, failing, skipped))

    assert report.summary == {
        "connections": 3,
        "deployments": 4,
        "ok": 1,
        "warnings": 1,
        "errors": 1,
        "skipped": 1,
    }
    assert report.has_errors is True

    clean = HealthReport(
        mode="discovery",
        connections=(
            ConnectionHealth(
                connection_id="c1",
                channel_operator="op",
                status="connected",
                level="ok",
                detail="",
                deployments=(_deployment_health("d1", "ok"),),
            ),
        ),
    )
    assert clean.has_errors is False
    assert clean.summary["errors"] == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        clean.connections[0].deployments[0].level = "error"


@pytest.mark.parametrize(
    ("status_code", "discovery", "expected_status", "expected_level"),
    [
        (401, True, "auth_failed", "error"),
        (403, False, "auth_failed", "error"),
        (429, True, "rate_limited", "warning"),
        (404, True, "check_unsupported", "warning"),
        (405, True, "check_unsupported", "warning"),
        (404, False, "model_not_found", "error"),
        (500, True, "provider_error", "error"),
    ],
)
def test_http_failure_maps_status_codes_to_levels(
    status_code: int, discovery: bool, expected_status: str, expected_level: str
) -> None:
    from model_gateway.health import _http_failure

    status, level, detail = _http_failure(status_code, discovery=discovery)
    assert status == expected_status
    assert level == expected_level
    assert str(status_code) in detail
    assert detail


@pytest.mark.asyncio
async def test_timeout_is_clamped_to_a_minimum_of_100ms(
    gateway_config: GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import model_gateway.discovery as discovery

    captured: dict[str, float] = {}
    real_probe_client = discovery.probe_client

    def spy(
        connection, timeout_seconds: float, transport
    ):
        captured["timeout"] = timeout_seconds
        return real_probe_client(connection, timeout_seconds, transport)

    monkeypatch.setattr(discovery, "probe_client", spy)

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        timeout_seconds=0,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"object": "list", "data": []}
            )
        ),
    )
    assert report.connections[0].status == "connected"
    assert captured["timeout"] == 0.1


@pytest.mark.asyncio
async def test_discovered_model_listing_caps_at_five_hundred_entries(
    gateway_config: GatewayConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": f"author/model-{index}"} for index in range(600)],
            },
        )

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(handler),
    )
    connection = report.connections[0]
    assert connection.discovered_model_count == 600
    assert len(connection.discovered_models) == 500
    assert connection.discovered_models[0] == "author/model-0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disable_kind", "expected_detail"),
    [
        ("enabled", "connection 已禁用"),
        ("usage_scope", "connection 已禁用"),
    ],
)
async def test_disabled_connections_are_skipped_without_any_request(
    gateway_config: GatewayConfig,
    disable_kind: str,
    expected_detail: str,
) -> None:
    if disable_kind == "enabled":
        gateway_config.connections["official"].enabled = False
    else:
        gateway_config.connections["official"].usage_scope = "disabled"

    report = await check_health(
        config=gateway_config,
        secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
        connection_id="official",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("disabled connection must not be probed")
        ),
    )
    connection = report.connections[0]
    assert connection.status == "disabled"
    assert connection.level == "skipped"
    assert connection.detail == expected_detail
    assert all(item.level == "skipped" for item in connection.deployments)


@pytest.mark.asyncio
async def test_unknown_connection_id_is_rejected(gateway_config: GatewayConfig) -> None:
    from model_gateway.health import HealthCheckError

    with pytest.raises(HealthCheckError, match="未知 connection"):
        await check_health(
            config=gateway_config,
            secrets={"UPSTREAM_OFFICIAL": "provider-secret"},
            connection_id="does-not-exist",
        )
