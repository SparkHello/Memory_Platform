from __future__ import annotations

import json
import sqlite3
from typing import AsyncIterator

import httpx
from fastapi.testclient import TestClient

from model_gateway.service import create_app
from model_gateway.config_store import load_config, read_secrets, write_config


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def test_admin_configuration_is_filtered_and_never_returns_secrets(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        response = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer local-client-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert {route["id"] for route in payload["routes"]} == {
        "memory.chat",
        "memory.embedding",
    }
    assert all("secret_ref" not in connection for connection in payload["connections"])
    assert all(
        connection["response_limit_bytes"] == 64 * 1024 * 1024
        for connection in payload["connections"]
    )
    by_deployment = {item["id"]: item for item in payload["deployments"]}
    assert by_deployment["chat-official"]["pricing"] == "official-chat-2026-08"
    assert by_deployment["chat-official"]["tool_choice_with_reasoning"] == (
        "auto_only"
    )
    assert "official-secret" not in response.text
    assert len(payload["revision"]) == 64


def test_legacy_admin_aliases_match_canonical_routes(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    cases = (
        ("POST", "/admin/channels/discover", "POST", "/admin/discover"),
        (
            "POST",
            "/admin/channel-bundles/validate",
            "POST",
            "/admin/bundles/validate",
        ),
        (
            "POST",
            "/admin/channel-bundles/apply",
            "PUT",
            "/admin/bundles",
        ),
    )

    with TestClient(app) as client:
        for canonical_method, canonical_path, alias_method, alias_path in cases:
            canonical = client.request(
                canonical_method,
                canonical_path,
                headers={"authorization": "Bearer admin-token"},
                json={},
            )
            alias = client.request(
                alias_method,
                alias_path,
                headers={"authorization": "Bearer admin-token"},
                json={},
            )

            assert alias.status_code == canonical.status_code
            assert alias.json() == canonical.json()
            assert alias.status_code != 404


def test_admin_portable_config_exports_schema_without_secret_values(
    gateway_home,
) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        denied = client.get(
            "/admin/portable-config",
            headers={"authorization": "Bearer local-client-token"},
        )
        assert denied.status_code == 403

        response = client.get(
            "/admin/portable-config",
            headers={"authorization": "Bearer admin-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] >= 1
    assert "connections" in payload
    assert "official-secret" not in response.text
    assert "admin-token" not in response.text
    assert "local-client-token" not in response.text


def test_route_changes_require_admin_validate_and_apply_atomically(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        snapshot = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer local-client-token"},
        ).json()
        body = {
            "revision": snapshot["revision"],
            "routes": [
                {
                    "id": "memory.chat",
                    "targets": ["chat-reseller", "chat-official"],
                    "enabled": True,
                }
            ],
        }
        denied = client.put(
            "/admin/routes",
            headers={"authorization": "Bearer local-client-token"},
            json=body,
        )
        assert denied.status_code == 403

        validated = client.post(
            "/admin/routes/validate",
            headers={"authorization": "Bearer admin-token"},
            json=body,
        )
        assert validated.status_code == 200
        assert validated.json()["changed_routes"] == ["memory.chat"]

        applied = client.put(
            "/admin/routes",
            headers={"authorization": "Bearer admin-token"},
            json=body,
        )
        assert applied.status_code == 200
        assert applied.json()["restart_required"] is False

        stale = client.put(
            "/admin/routes",
            headers={"authorization": "Bearer admin-token"},
            json=body,
        )
        assert stale.status_code == 409

    config = load_config(gateway_home.config)
    assert config.routes["memory.chat"].targets == ["chat-reseller", "chat-official"]


def test_admin_secret_write_is_one_way_and_connection_check_is_discovery_only(
    gateway_home,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={"data": [{"id": "author/chat-v1"}, {"id": "author/embed-v1"}]},
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    app.state.router.cooldowns.defer("official", 600)
    with TestClient(app) as client:
        updated = client.put(
            "/admin/connections/official/secret",
            headers={"authorization": "Bearer admin-token"},
            json={"value": "replacement-secret"},
        )
        assert updated.status_code == 200
        assert "replacement-secret" not in updated.text

        checked = client.post(
            "/admin/connections/official/check",
            headers={"authorization": "Bearer admin-token"},
        )
        assert checked.status_code == 200
        assert checked.json()["mode"] == "discovery"

    assert read_secrets(gateway_home.secrets)["UPSTREAM_OFFICIAL"] == "replacement-secret"
    assert app.state.router.cooldowns.remaining("official") == 0
    # Candidate validation happens before replacement, then the explicit check
    # performs a second read-only discovery. Neither request sends inference.
    assert requests == [("GET", "/v1/models"), ("GET", "/v1/models")]


def test_admin_cannot_reuse_local_client_key_as_provider_key(gateway_home) -> None:
    app = create_app(paths=gateway_home, transport=httpx.MockTransport(lambda request: None))
    with TestClient(app) as client:
        response = client.put(
            "/admin/connections/official/secret",
            headers={"authorization": "Bearer admin-token"},
            json={"value": "local-client-token"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "model_gateway_secret_domain_conflict"
    assert read_secrets(gateway_home.secrets)["UPSTREAM_OFFICIAL"] == "official-secret"


def test_service_auth_models_proxy_and_metadata_only_usage(gateway_home) -> None:
    received: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        received.append(payload)
        return httpx.Response(
            200,
            content=(
                b'{"id":"r1","model":"actual-model","choices":['
                b'{"message":{"content":"upstream-answer","reasoning_content":"r"}}],'
                b'"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'
            ),
            headers={"content-type": "application/json", "x-request-id": "r1"},
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        assert client.get("/v1/models").status_code == 401
        models = client.get(
            "/v1/models", headers={"authorization": "Bearer local-client-token"}
        )
        assert models.status_code == 200
        assert {item["id"] for item in models.json()["data"]} == {
            "memory.chat",
            "memory.embedding",
        }

        marker = "request-sensitive-marker"
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={
                "model": "memory.chat",
                "messages": [{"role": "user", "content": marker}],
                "unknown_extension": {"preserve": True},
            },
        )
        assert response.status_code == 200
        assert response.content.startswith(b'{"id":"r1"')
        assert response.headers["x-model-gateway-deployment"] == "chat-official"
        assert response.headers["x-model-gateway-pricing"] == "official-chat-2026-08"
        assert received[0]["unknown_extension"] == {"preserve": True}
        assert received[0]["model"] == "author/chat-v1"

    assert marker.encode() not in gateway_home.usage_db.read_bytes()
    assert b"upstream-answer" not in gateway_home.usage_db.read_bytes()
    with sqlite3.connect(gateway_home.usage_db) as connection:
        row = connection.execute(
            "SELECT deployment_id, channel_operator, model_author, response_model, "
            "total_tokens, estimated_cost, cost_complete "
            "FROM usage_events"
        ).fetchone()
        attempt = connection.execute(
            "SELECT attempt_index, outcome, failure_class, response_model, "
            "total_tokens, estimated_cost, cost_complete FROM attempt_events"
        ).fetchone()
    assert row == (
        "chat-official",
        "official-vendor",
        "author",
        "actual-model",
        7,
        "0.000009",
        1,
    )
    assert attempt == (1, "success", "none", "actual-model", 7, "0.000009", 1)


def test_service_rejects_wrong_route_kind(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"authorization": "Bearer local-client-token"},
            json={"model": "memory.chat", "input": "hello"},
        )
    assert response.status_code == 404


def test_service_rejects_embedding_dimensions_outside_route_space(gateway_home) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"authorization": "Bearer local-client-token"},
            json={"model": "memory.embedding", "input": "hello", "dimensions": 3},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == (
        "model_gateway_embedding_dimensions_mismatch"
    )
    assert upstream_calls == 0


def test_service_selects_a_target_that_supports_request_capabilities(
    gateway_home,
) -> None:
    config = load_config(gateway_home.config)
    config.deployments["chat-official"].capabilities.json_schema = False
    write_config(gateway_home.config, config)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json={"choices": []})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={
                "model": "memory.chat",
                "messages": [],
                "response_format": {"type": "json_schema", "json_schema": {}},
            },
        )

    assert response.status_code == 200
    assert calls == ["reseller.example"]
    assert response.headers["x-model-gateway-deployment"] == "chat-reseller"


def test_service_returns_stable_422_when_capability_is_unavailable(gateway_home) -> None:
    config = load_config(gateway_home.config)
    for deployment in config.deployments.values():
        if deployment.kind == "chat":
            deployment.capabilities.json_schema = False
    write_config(gateway_home.config, config)
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={
                "model": "memory.chat",
                "messages": [],
                "response_format": {"type": "json_schema", "json_schema": {}},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "message": "请求需要当前 route 无法提供的能力：json_schema",
        "type": "model_gateway_capability_unavailable",
        "code": "model_gateway_capability_unavailable",
        "required_capabilities": ["json_schema"],
    }
    assert upstream_calls == 0


def test_service_rejects_specific_tool_choice_with_reasoning_before_upstream(
    gateway_home,
) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"choices": []})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={
                "model": "memory.chat",
                "messages": [],
                "thinking": {"type": "enabled"},
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "lookup", "parameters": {}},
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "lookup"},
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "model_gateway_capability_unavailable"
    )
    assert response.json()["error"]["required_capabilities"] == [
        "tool_choice_with_reasoning"
    ]
    assert upstream_calls == 0


def test_service_rejects_body_while_streaming_past_limit(gateway_home) -> None:
    config = load_config(gateway_home.config)
    config.server.body_limit_bytes = 1024
    write_config(gateway_home.config, config)
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": "Bearer local-client-token",
                "content-type": "application/json",
            },
            content=json.dumps(
                {
                    "model": "memory.chat",
                    "messages": [{"role": "user", "content": "x" * 2000}],
                }
            ),
        )
    assert response.status_code == 413
    assert upstream_calls == 0


def test_service_preserves_complete_sse_bytes_and_records_usage(gateway_home) -> None:
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"r"}}]}\n\n',
        b'data: {"model":"actual","usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Stream(chunks),
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={"model": "memory.chat", "messages": [], "stream": True},
        )
    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert response.headers["x-model-gateway-deployment"] == "chat-official"
    with sqlite3.connect(gateway_home.usage_db) as connection:
        row = connection.execute(
            "SELECT complete, response_model, total_tokens FROM usage_events"
        ).fetchone()
        attempt = connection.execute(
            "SELECT response_complete, response_model, total_tokens FROM attempt_events"
        ).fetchone()
    assert row == (1, "actual", 3)
    assert attempt == (1, "actual", 3)


def test_client_disconnect_still_records_stream_usage() -> None:
    """断连（生成器被取消）时，上游关闭与用量记账仍必须恰好执行一次。"""
    import asyncio
    import time
    from types import SimpleNamespace

    from model_gateway.service import _streaming_response
    from model_gateway.usage import UsageMetadata

    recorded: list[dict] = []

    class _FakeUsageStore:
        def record(self, **kwargs):
            recorded.append(kwargs)

    class _FakeStream:
        def __init__(self):
            self.closed = 0
            self.response = SimpleNamespace(status_code=200)
            self.headers = {"content-type": "text/event-stream"}
            self.attempts = 1
            self.attempt_traces = ()
            self.target = SimpleNamespace(deployment=SimpleNamespace(id="d"))
            self.active_trace = SimpleNamespace(
                outcome="success",
                failure_class="",
                billable_unknown=False,
                response_complete=False,
                capture=None,
                latency_ms=0,
            )
            self.attempt_started_monotonic = time.monotonic()

        async def aiter_raw(self):
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            await asyncio.sleep(3600)

        async def aclose(self):
            self.closed += 1

    stream = _FakeStream()
    response = _streaming_response(
        stream,  # type: ignore[arg-type]
        usage_store=_FakeUsageStore(),  # type: ignore[arg-type]
        client=SimpleNamespace(id="local-client"),  # type: ignore[arg-type]
        kind="chat",
        route_id="memory.chat",
        started=time.monotonic(),
        pricing_id="",
        pricing=None,
        pricing_catalog={},
        metadata=UsageMetadata(),
        storage_monitor=SimpleNamespace(mark_unavailable=lambda: None),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        iterator = response.body_iterator

        async def consume() -> None:
            async for _ in iterator:
                pass

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)  # 让第一块 chunk 流出并挂起在上游
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 等待被 shield 的记账任务完成。
        for _ in range(100):
            if recorded:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert stream.closed == 1
    assert len(recorded) == 1
    assert recorded[0]["complete"] is False
    assert recorded[0]["status_code"] == 200


def test_routing_time_affinity_error_uses_stable_protocol_code(gateway_home) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": "Bearer local-client-token",
                "x-model-gateway-require-deployment": "not-in-this-route",
            },
            json={"model": "memory.chat", "messages": []},
        )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "message": "要求的 deployment 不属于当前 route 或已超出 client 权限",
        "type": "model_gateway_affinity_unavailable",
        "code": "model_gateway_affinity_unavailable",
    }
    assert upstream_calls == 0


def test_usage_storage_failure_never_changes_successful_upstream_response(
    gateway_home, monkeypatch
) -> None:
    raw = b'{"id":"ok","choices":[{"message":{"content":"answer"}}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))

    def fail_record(**kwargs) -> None:
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(app.state.usage_store, "record", fail_record)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={"model": "memory.chat", "messages": []},
        )

    assert response.status_code == 200
    assert response.content == raw


def test_chat_stream_control_must_be_boolean(gateway_home) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer local-client-token"},
            json={"model": "memory.chat", "messages": [], "stream": "true"},
        )

    assert response.status_code == 400
    assert upstream_calls == 0


def test_non_finite_json_number_is_rejected_before_proxying(gateway_home) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": "Bearer local-client-token",
                "content-type": "application/json",
            },
            content=b'{"model":"memory.chat","messages":[],"temperature":NaN}',
        )

    assert response.status_code == 400
    assert upstream_calls == 0


def test_unauthenticated_health_never_echoes_invalid_config_values(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    marker = "accidentally-pasted-sensitive-value"
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        payload = json.loads(gateway_home.config.read_text(encoding="utf-8"))
        payload["accidental_api_key"] = marker
        gateway_home.config.write_text(json.dumps(payload), encoding="utf-8")

        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "warning"
    assert response.json()["reload_error"] == (
        "configuration_reload_failed_using_last_known_good"
    )
    assert marker not in response.text


def test_admin_connection_create_dry_run_apply_and_model_discovery(gateway_home) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "newvendor/chat-1"}, {"id": "newvendor/embed-1"}]},
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        snapshot = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer admin-token"},
        ).json()
        body = {
            "revision": snapshot["revision"],
            "channel_operator": "NewVendor",
            "adapter": "generic",
            "base_url": "https://newvendor.example/v1",
        }

        denied = client.post(
            "/admin/connections",
            headers={"authorization": "Bearer local-client-token"},
            json=body,
        )
        assert denied.status_code == 403

        preview = client.post(
            "/admin/connections",
            headers={"authorization": "Bearer admin-token"},
            json={**body, "dry_run": True},
        )
        assert preview.status_code == 200
        assert preview.json() == {
            "valid": True,
            "applied": False,
            "connection_id": "newvendor-account",
            "revision": snapshot["revision"],
        }
        assert "newvendor-account" not in load_config(gateway_home.config).connections

        created = client.post(
            "/admin/connections",
            headers={"authorization": "Bearer admin-token"},
            json=body,
        )
        assert created.status_code == 200
        assert created.json()["applied"] is True

        stale = client.post(
            "/admin/connections",
            headers={"authorization": "Bearer admin-token"},
            json=body,
        )
        assert stale.status_code == 409

        invalid = client.post(
            "/admin/connections",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": created.json()["revision"],
                "channel_operator": "insecure",
                "base_url": "http://insecure.example/v1",
            },
        )
        assert invalid.status_code == 400
        assert "HTTPS" in invalid.json()["error"]["message"]

        plan_connection = client.post(
            "/admin/connections",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": created.json()["revision"],
                "channel_operator": "plan-vendor",
                "base_url": "https://plan.example/v1",
                "plan": "token_plan",
            },
        )
        assert plan_connection.status_code == 200, plan_connection.text
        assert plan_connection.json()["connection_id"] == "plan-vendor-account"
        assert plan_connection.json()["applied"] is True

        updated = client.put(
            "/admin/connections/newvendor-account/secret",
            headers={"authorization": "Bearer admin-token"},
            json={"value": "channel-secret"},
        )
        assert updated.status_code == 200

        checked = client.post(
            "/admin/connections/newvendor-account/check",
            headers={"authorization": "Bearer admin-token"},
        )
        assert checked.status_code == 200
        info = checked.json()["connections"][0]
        assert info["discovered_models"] == ["newvendor/chat-1", "newvendor/embed-1"]
        assert "channel-secret" not in checked.text

    config = load_config(gateway_home.config)
    connection = config.connections["newvendor-account"]
    assert connection.channel_operator == "newvendor"
    assert connection.auth.secret_ref == "CONNECTION_NEWVENDOR_ACCOUNT_API_KEY"
    assert read_secrets(gateway_home.secrets)[
        "CONNECTION_NEWVENDOR_ACCOUNT_API_KEY"
    ] == "channel-secret"


def test_admin_discovers_new_channel_draft_without_persisting_or_leaking(
    gateway_home,
    caplog,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer draft-provider-key"
        return httpx.Response(
            200,
            json={"data": [{"id": "draft/chat-v1"}, {"id": "draft/embed-v4"}]},
        )

    config_before = gateway_home.config.read_bytes()
    secrets_before = gateway_home.secrets.read_bytes()
    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        revision = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer admin-token"},
        ).json()["revision"]
        discovered = client.post(
            "/admin/channels/discover",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": revision,
                "channel_operator": "draft-channel",
                "base_url": "https://draft.example/v1",
                "dialect": "generic",
                "auth_type": "bearer",
                "candidate_key": "draft-provider-key",
                "allowed_private_networks": [],
                "models_endpoint": "/models",
            },
        )
        assert discovered.status_code == 200
        payload = discovered.json()
        assert payload["valid"] is True
        assert payload["persisted"] is False
        assert payload["revision"] == revision
        assert payload["candidate"] == {
            "connection_id": "",
            "channel_operator": "draft-channel",
            "base_url": "https://draft.example/v1",
            "adapter": "generic",
            "auth_type": "bearer",
            "allowed_private_networks": [],
            "models_endpoint": "/models",
        }
        assert payload["models"] == [
            {"id": "draft/chat-v1", "model_author": "unknown", "aliases": []},
            {"id": "draft/embed-v4", "model_author": "unknown", "aliases": []},
        ]
        assert payload["report"]["mode"] == "discovery"

        secret_marker = "URL-CANDIDATE-SECRET"
        rejected = client.post(
            "/admin/channels/discover",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": revision,
                "channel_operator": "draft-channel",
                "base_url": f"https://user:{secret_marker}@draft.example/v1",
                "candidate_key": "another-draft-key",
            },
        )
        assert rejected.status_code == 400
        assert secret_marker not in rejected.text
        assert secret_marker not in caplog.text

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models")
    ]
    assert gateway_home.config.read_bytes() == config_before
    assert gateway_home.secrets.read_bytes() == secrets_before
    assert b"draft-provider-key" not in gateway_home.usage_db.read_bytes()


def test_backend_usage_metadata_and_query_are_central_fact_source(gateway_home) -> None:
    marker = "prompt-and-response-body-must-never-enter-usage-db"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert marker in request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "id": "provider-request-1",
                "model": "author/chat-v1",
                "choices": [{"message": {"content": marker}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            },
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": "Bearer local-client-token",
                "x-model-gateway-correlation-id": "turn:abc-123",
                "x-model-gateway-operation": "memory.chat.answer",
                "x-model-gateway-user-tag": "user:opaque-7",
            },
            json={
                "model": "memory.chat",
                "messages": [{"role": "user", "content": marker}],
            },
        )
        assert response.status_code == 200
        event_id = response.headers["x-model-gateway-usage-event-id"]
        assert response.headers["x-model-gateway-correlation-id"] == "turn:abc-123"

        events = client.get(
            "/v1/usage/events",
            headers={"authorization": "Bearer local-client-token"},
            params={"event_id": event_id},
        )
        assert events.status_code == 200
        rows = events.json()["data"]
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "turn:abc-123"
        assert rows[0]["operation"] == "memory.chat.answer"
        assert rows[0]["user_tag"] == "user:opaque-7"
        assert rows[0]["attempt_costs"] == {"USD": "0.000009"}
        assert rows[0]["unknown_cost_attempts"] == 0

        summary = client.get(
            "/v1/usage/summary",
            headers={"authorization": "Bearer local-client-token"},
            params={
                "operation": "memory.chat.answer",
                "user_tag": "user:opaque-7",
            },
        )
        assert summary.status_code == 200
        assert summary.json()["calls"] == 1
        assert summary.json()["estimated_costs"] == {"USD": "0.000009"}

        forbidden_metadata = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": "Bearer desktop-token",
                "x-model-gateway-operation": "memory.chat.answer",
            },
            json={"model": "memory.chat", "messages": []},
        )
        assert forbidden_metadata.status_code == 403
        assert (
            forbidden_metadata.json()["error"]["type"]
            == "model_gateway_usage_metadata_forbidden"
        )
        invalid_metadata = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": "Bearer local-client-token",
                "x-model-gateway-user-tag": "raw user name",
            },
            json={"model": "memory.chat", "messages": []},
        )
        assert invalid_metadata.status_code == 400

        forbidden_query = client.get(
            "/v1/usage/summary",
            headers={"authorization": "Bearer desktop-token"},
        )
        assert forbidden_query.status_code == 403
        cross_client = client.get(
            "/v1/usage/events",
            headers={"authorization": "Bearer local-client-token"},
            params={"client_id": "desktop"},
        )
        assert cross_client.status_code == 400

    usage_files = gateway_home.home.glob("usage.db*")
    assert marker.encode("utf-8") not in b"".join(path.read_bytes() for path in usage_files)


def test_admin_deployments_create_and_repoint_routes(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        snapshot = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer admin-token"},
        ).json()
        body = {
            "revision": snapshot["revision"],
            "connection": "official",
            "deployments": [
                {
                    "upstream_model": "author/chat-v2",
                    "capabilities": {"tools": True, "reasoning": True},
                }
            ],
            "routes": [
                {"id": "memory.chat", "kind": "chat", "targets": ["$0"]},
                {"id": "knowledge.fast", "kind": "chat", "targets": ["$0"]},
            ],
        }

        denied = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer local-client-token"},
            json=body,
        )
        assert denied.status_code == 403

        preview = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer admin-token"},
            json={**body, "dry_run": True},
        )
        assert preview.status_code == 200
        assert preview.json()["applied"] is False
        assert preview.json()["deployments"] == [
            {"id": "official-author-chat-v2", "upstream_model": "author/chat-v2", "kind": "chat"}
        ]
        assert "knowledge.fast" not in load_config(gateway_home.config).routes

        applied = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer admin-token"},
            json=body,
        )
        assert applied.status_code == 200
        assert applied.json()["changed_routes"] == ["memory.chat", "knowledge.fast"]

        missing_capability = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": applied.json()["revision"],
                "connection": "official",
                "deployments": [{"upstream_model": "author/chat-plain"}],
                "routes": [{"id": "memory.chat", "kind": "chat", "targets": ["$0"]}],
            },
        )
        assert missing_capability.status_code == 400
        assert "tools" in missing_capability.json()["error"]["message"]

        bad_embedding = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": applied.json()["revision"],
                "connection": "official",
                "deployments": [{"upstream_model": "author/embed-x", "kind": "embedding"}],
            },
        )
        assert bad_embedding.status_code == 400
        assert "embedding" in bad_embedding.json()["error"]["message"]

        unknown_connection = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": applied.json()["revision"],
                "connection": "ghost",
                "deployments": [{"upstream_model": "author/chat-v2"}],
            },
        )
        assert unknown_connection.status_code == 400

        bad_kind = client.post(
            "/admin/deployments",
            headers={"authorization": "Bearer admin-token"},
            json={
                "revision": applied.json()["revision"],
                "connection": "official",
                "deployments": [
                    {
                        "upstream_model": "author/embed-2",
                        "kind": "embedding",
                        "dimensions": 4,
                        "embedding_space": "author.embed-2:4",
                    }
                ],
                "routes": [{"id": "memory.chat", "kind": "embedding", "targets": ["$0"]}],
            },
        )
        assert bad_kind.status_code == 400

    config = load_config(gateway_home.config)
    deployment = config.deployments["official-author-chat-v2"]
    assert deployment.model_author == "unknown"
    assert config.routes["memory.chat"].targets == ["official-author-chat-v2"]
    assert config.routes["knowledge.fast"].max_attempts == 1
