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
    assert "official-secret" not in response.text
    assert len(payload["revision"]) == 64


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
    assert requests == [("GET", "/v1/models")]


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
    with sqlite3.connect(gateway_home.usage_db) as connection:
        row = connection.execute(
            "SELECT deployment_id, channel_operator, model_author, response_model, "
            "total_tokens, estimated_cost, cost_complete "
            "FROM usage_events"
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
    assert row == (1, "actual", 3)


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
