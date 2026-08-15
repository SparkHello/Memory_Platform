import json

import httpx

from app.api import usage as usage_api
from app.config import Settings
from app.config import get_settings
from app.usage.attribution import (
    MODEL_GATEWAY_CORRELATION_HEADER,
    MODEL_GATEWAY_OPERATION_HEADER,
    MODEL_GATEWAY_USER_TAG_HEADER,
    model_gateway_usage_headers,
)
from app.usage.context import model_usage_scope


def test_central_usage_headers_are_stable_opaque_and_user_isolated() -> None:
    secret = "usage-test-signing-secret-0123456789abcdef"
    with model_usage_scope(user_id="alice@example.test", operation="memory.extract"):
        first = model_gateway_usage_headers(signing_secret=secret)
        second = model_gateway_usage_headers(signing_secret=secret)
    with model_usage_scope(user_id="bob@example.test", operation="memory.extract"):
        other = model_gateway_usage_headers(signing_secret=secret)

    assert first[MODEL_GATEWAY_OPERATION_HEADER] == "memory.extract"
    assert first[MODEL_GATEWAY_USER_TAG_HEADER] == second[MODEL_GATEWAY_USER_TAG_HEADER]
    assert first[MODEL_GATEWAY_CORRELATION_HEADER] != second[MODEL_GATEWAY_CORRELATION_HEADER]
    assert first[MODEL_GATEWAY_USER_TAG_HEADER] != other[MODEL_GATEWAY_USER_TAG_HEADER]
    assert "alice" not in json.dumps(first)
    assert "example.test" not in json.dumps(first)
    assert all(len(value) <= 120 and value.isascii() for value in first.values())


def test_central_usage_invalid_operation_never_becomes_an_unsafe_header() -> None:
    with model_usage_scope(user_id="alice", operation="raw body\nforged: value"):
        headers = model_gateway_usage_headers(
            signing_secret="usage-test-signing-secret-0123456789abcdef"
        )
    assert headers[MODEL_GATEWAY_OPERATION_HEADER] == "unspecified"


def test_usage_summary_requires_authentication(client) -> None:
    response = client.get("/usage/summary")
    assert response.status_code == 401


def test_central_usage_summary_proxies_only_hmac_scoped_backend_totals(
    client,
    auth_headers,
    memory_store,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        GATEWAY_ALLOW_USER_ID_HEADER=True,
        GATEWAY_SIGNING_SECRET="summary-test-signing-secret-0123456789abcdef",
        DATABASE_PATH=memory_store.database_path,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-backend-key",
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    calls: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "days": int(request.url.params["days"]),
                "filters": {
                    "client_id": "memory-gateway",
                    "operation": "",
                    "user_tag": request.url.params["user_tag"],
                },
                "calls": 3,
                "complete_calls": 3,
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "estimated_costs": {"CNY": "0.0012"},
                "incomplete_cost_calls": 0,
                "attempts": {"recorded": 3},
                "deployments": [],
                "retention": {"raw_days": 90, "daily_days": 365},
                "unexpected_remote_field": "must-be-stripped",
            },
        )

    def client_factory(*args, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(usage_api.httpx, "AsyncClient", client_factory)
    first = client.get(
        "/usage/summary?range=all&user_tag=forged&operation=forged&client_id=admin",
        headers={**auth_headers, "X-User-Id": "alice@example.test"},
    )
    second = client.get(
        "/usage/summary?range=30&user_tag=forged",
        headers={**auth_headers, "X-User-Id": "bob@example.test"},
    )

    assert first.status_code == 200
    assert first.json()["calls"] == 3
    assert "unexpected_remote_field" not in first.json()
    assert calls[0].url.path == "/v1/usage/summary"
    assert calls[0].headers["authorization"] == "Bearer central-backend-key"
    assert calls[0].url.params["days"] == "365"
    assert calls[0].url.params["user_tag"].startswith("usr_")
    assert calls[0].url.params["user_tag"] != "forged"
    assert "client_id" not in calls[0].url.params
    assert "operation" not in calls[0].url.params
    assert "alice" not in str(calls[0].url)
    assert calls[0].url.params["user_tag"] != calls[1].url.params["user_tag"]
    assert second.status_code == 200


def test_central_usage_summary_model_unavailable_is_safe_503(
    client,
    auth_headers,
    memory_store,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        GATEWAY_SIGNING_SECRET="summary-test-signing-secret-0123456789abcdef",
        DATABASE_PATH=memory_store.database_path,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-backend-key",
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic unavailable", request=request)

    def client_factory(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(usage_api.httpx, "AsyncClient", client_factory)
    response = client.get("/usage/summary", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "model_gateway_usage_unavailable"
    assert "central-backend-key" not in response.text
