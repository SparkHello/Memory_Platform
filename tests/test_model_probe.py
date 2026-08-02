from __future__ import annotations

import json

import httpx

from app.config import Settings
from app.model_catalog import ModelSpec
from app.model_probe import check_model_catalog


def _chat_model(*, provider: str = "mimo", model: str = "mimo-test") -> ModelSpec:
    return ModelSpec(
        id=f"{provider}/{model}",
        provider=provider,
        model=model,
        kind="chat",
    )


def test_catalog_probe_checks_models_endpoint_once_per_provider() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer hidden-key"
        return httpx.Response(200, json={"data": [{"id": "mimo-test"}]})

    settings = Settings(
        _env_file=None,
        LLM_MIMO_BASE_URL="https://provider.example/v1",
        LLM_MIMO_API_KEY="hidden-key",
    )
    models = [_chat_model(), _chat_model(model="mimo-other")]

    results = check_model_catalog(
        settings,
        models,
        transport=httpx.MockTransport(handler),
    )

    assert [result.status for result in results] == ["available", "connected_unlisted"]
    assert len(requests) == 1


def test_catalog_probe_reports_auth_failure_without_exposing_key() -> None:
    settings = Settings(
        _env_file=None,
        LLM_KIMI_BASE_URL="https://provider.example/v1",
        LLM_KIMI_API_KEY="never-print-this",
    )

    results = check_model_catalog(
        settings,
        [_chat_model(provider="kimi", model="kimi-test")],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "unauthorized"})
        ),
    )

    assert results[0].status == "auth_failed"
    assert results[0].failed is True
    assert "never-print-this" not in results[0].detail


def test_live_probe_sends_minimal_chat_request() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": []})

    settings = Settings(
        _env_file=None,
        LLM_KIMI_BASE_URL="https://provider.example/v1",
        LLM_KIMI_API_KEY="hidden-key",
    )
    results = check_model_catalog(
        settings,
        [_chat_model(provider="kimi", model="kimi-k2.7-code")],
        live=True,
        transport=httpx.MockTransport(handler),
    )

    assert results[0].status == "live_ok"
    assert payloads[0]["model"] == "kimi-k2.7-code"
    assert payloads[0]["max_tokens"] == 1
    assert payloads[0]["temperature"] == 1.0
    assert payloads[0]["stream"] is False


def test_live_probe_uses_embeddings_endpoint_for_embedding_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(200, json={"data": [{"embedding": [0.0]}]})

    settings = Settings(
        _env_file=None,
        EMBEDDING_BASE_URL="https://provider.example/v1",
        EMBEDDING_API_KEY="hidden-key",
    )
    model = ModelSpec(
        id="embedding/text-embedding-v4",
        provider="embedding",
        model="text-embedding-v4",
        kind="embedding",
    )

    results = check_model_catalog(
        settings,
        [model],
        live=True,
        transport=httpx.MockTransport(handler),
    )

    assert results[0].status == "live_ok"
