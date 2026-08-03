import json
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.llm.client import OpenAICompatibleClient
from app.llm.routing import ProviderCooldowns
from app.memory.search import OpenAICompatibleEmbeddingClient
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.context import model_usage_scope
from app.usage.pricing import price_for
from app.usage.recorder import UsageRecorder
from app.usage.store import UsageStore, parse_usage


def test_parse_usage_supports_openai_and_deepseek_cache_fields() -> None:
    assert parse_usage(
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 250,
            "total_tokens": 1_250,
            "prompt_tokens_details": {"cached_tokens": 400},
        }
    ) == {
        "available": True,
        "input_tokens": 1_000,
        "cached_input_tokens": 400,
        "output_tokens": 250,
        "total_tokens": 1_250,
    }
    assert parse_usage(
        {
            "prompt_cache_hit_tokens": 200,
            "prompt_cache_miss_tokens": 800,
            "completion_tokens": 100,
        }
    ) == {
        "available": True,
        "input_tokens": 1_000,
        "cached_input_tokens": 200,
        "output_tokens": 100,
        "total_tokens": 1_100,
    }
    assert parse_usage(None)["available"] is False


def test_usage_store_preserves_full_user_isolation_key(tmp_path) -> None:
    store = UsageStore(str(tmp_path / "usage.db"))
    store.init_db()
    shared_prefix = "u" * 300
    long_user_id = f"{shared_prefix}-alice"

    store.record_response(
        user_id=long_user_id,
        operation="chat_completion",
        provider="deepseek",
        provider_code="D",
        model="deepseek-v4-flash",
        kind="chat",
        payload={
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            }
        },
    )

    assert store.summary(user_id=long_user_id, days=None)["totals"]["calls"] == 1
    assert store.summary(user_id=shared_prefix, days=None)["totals"]["calls"] == 0


def test_usage_recorder_accepts_authoritative_gateway_vendor(tmp_path) -> None:
    database = str(tmp_path / "usage.db")
    store = UsageStore(database)
    store.init_db()
    recorder = UsageRecorder(database)
    recorder.record_response(
        payload={
            "model": "deepseek-v4-flash",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
        model="deepseek-v4-flash",
        kind="chat",
        base_url="http://127.0.0.1:2030/v1",
        provider_override="siliconflow",
        user_id="alice",
    )

    event = store.summary(user_id="alice", days=None)["recent"][0]
    assert event["provider"] == "siliconflow"
    assert event["model"] == "deepseek-v4-flash"
    assert event["price_available"] is False


def test_central_gateway_usage_never_reuses_local_provider_price(tmp_path) -> None:
    database = str(tmp_path / "usage.db")
    store = UsageStore(database)
    store.init_db()
    UsageRecorder(database).record_response(
        payload={
            "model": "deepseek-v4-flash",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        },
        model="deepseek-v4-flash",
        kind="chat",
        provider_override="deepseek",
        use_local_pricing=False,
        user_id="alice",
    )

    event = store.summary(user_id="alice", days=None)["recent"][0]
    assert event["provider"] == "deepseek"
    assert event["usage_available"] is True
    assert event["price_available"] is False
    assert event["cost_cny"] is None


def test_usage_store_prices_known_models_and_keeps_unknowns_visible(
    tmp_path,
) -> None:
    store = UsageStore(str(tmp_path / "usage.db"))
    store.init_db()

    store.record_response(
        user_id="alice",
        operation="chat_completion",
        provider="deepseek",
        provider_code="D",
        model="deepseek-v4-flash",
        kind="chat",
        payload={
            "id": "chat-deepseek",
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 200_000,
                "prompt_cache_miss_tokens": 800_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            },
        },
    )
    store.record_response(
        user_id="alice",
        operation="knowledge_index",
        provider="alibaba",
        provider_code="",
        model="text-embedding-v4",
        kind="embedding",
        payload={
            "usage": {
                "prompt_tokens": 2_000_000,
                "total_tokens": 2_000_000,
            }
        },
    )
    store.record_response(
        user_id="alice",
        operation="chat_completion",
        provider="custom",
        provider_code="",
        model="private-model",
        kind="chat",
        payload={
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 100,
                "total_tokens": 1_100,
            }
        },
    )
    store.record_response(
        user_id="alice",
        operation="chat_completion",
        provider="deepseek",
        provider_code="D",
        model="deepseek-v4-pro",
        kind="chat",
        payload={"id": "missing-usage"},
    )
    store.record_response(
        user_id="bob",
        operation="chat_completion",
        provider="deepseek",
        provider_code="D",
        model="deepseek-v4-flash",
        kind="chat",
        payload={
            "usage": {
                "prompt_tokens": 9_999,
                "completion_tokens": 1,
                "total_tokens": 10_000,
            }
        },
    )

    summary = store.summary(user_id="alice", days=None)

    assert summary["totals"] == {
        "calls": 4,
        "measured_calls": 3,
        "priced_calls": 2,
        "unmeasured_calls": 1,
        "unpriced_calls": 1,
        "input_tokens": 3_001_000,
        "cached_input_tokens": 200_000,
        "output_tokens": 100_100,
        "total_tokens": 3_101_100,
        "cost_cny": 2.004,
        "cache_hit_rate": 0.0666,
    }
    assert {item["model"] for item in summary["by_model"]} == {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "private-model",
        "text-embedding-v4",
    }
    assert summary["recent"][0]["provider_label"]
    assert all("user_id" not in event for event in summary["recent"])
    assert store.summary(user_id="bob", days=None)["totals"]["calls"] == 1


def test_pricing_requires_an_exact_official_model_id() -> None:
    kimi_code = price_for(
        provider="kimi",
        model="kimi-k2.7-code",
        kind="chat",
    )
    assert kimi_code is not None

    kimi_highspeed = price_for(
        provider="kimi",
        model="kimi-k2.7-code-highspeed",
        kind="chat",
    )
    assert kimi_highspeed is not None
    assert kimi_highspeed.input_cache_hit_per_million == Decimal("2.60")
    assert kimi_highspeed.input_cache_miss_per_million == Decimal("13.00")
    assert kimi_highspeed.output_per_million == Decimal("54.00")

    assert (
        price_for(
            provider="kimi",
            model="kimi-k2.7-highspeed",
            kind="chat",
        )
        is None
    )
    assert (
        price_for(
            provider="zhipu",
            model="glm-5.1",
            kind="chat",
            input_tokens=31_999,
        ).key
        == "zhipu:glm-5.1:input-lt-32k"
    )
    assert (
        price_for(
            provider="zhipu",
            model="glm-5.1",
            kind="chat",
            input_tokens=32_000,
        ).key
        == "zhipu:glm-5.1:input-gte-32k"
    )


def test_glm_51_uses_the_actual_input_length_price_tier(tmp_path) -> None:
    store = UsageStore(str(tmp_path / "usage.db"))
    store.init_db()
    for prompt_tokens, cached_tokens in ((31_999, 0), (32_000, 10_000)):
        store.record_response(
            user_id="alice",
            operation="chat_completion",
            provider="zhipu",
            provider_code="D",
            model="glm-5.1",
            kind="chat",
            payload={
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 1_000,
                    "total_tokens": prompt_tokens + 1_000,
                    "prompt_tokens_details": {
                        "cached_tokens": cached_tokens,
                    },
                }
            },
        )

    summary = store.summary(user_id="alice", days=None)

    assert summary["totals"]["cost_cny"] == pytest.approx(0.439994)
    assert {
        event["price_key"]
        for event in summary["recent"]
    } == {
        "zhipu:glm-5.1:input-lt-32k",
        "zhipu:glm-5.1:input-gte-32k",
    }


def test_recorder_prefers_the_actual_response_model(tmp_path) -> None:
    database_path = str(tmp_path / "usage.db")
    UsageStore(database_path).init_db()
    recorder = UsageRecorder(database_path)

    recorder.record_response(
        user_id="alice",
        operation="chat_completion",
        provider_code="D",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-5.1",
        kind="chat",
        payload={
            "model": "glm-5.2",
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 100,
                "total_tokens": 1_100,
            },
        },
    )

    summary = UsageStore(database_path).summary(user_id="alice", days=None)
    assert summary["by_model"][0]["model"] == "glm-5.2"
    assert summary["recent"][0]["price_key"] == "zhipu:glm-5.2"
    assert summary["totals"]["cost_cny"] == pytest.approx(0.0108)


@pytest.mark.asyncio
async def test_failover_is_billed_to_the_actual_successful_provider(
    tmp_path,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload["model"])
        if payload["model"] == "mimo-v2.5-pro-ultraspeed":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "id": "chat-kimi",
                "model": payload["model"],
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"operations":[]}',
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 100,
                    "total_tokens": 1_100,
                    "prompt_tokens_details": {"cached_tokens": 400},
                },
            },
        )

    database_path = str(tmp_path / "usage.db")
    UsageStore(database_path).init_db()
    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY="MKD",
        LLM_MIMO_API_KEY="mimo-key",
        LLM_KIMI_API_KEY="kimi-key",
        LLM_DEEPSEEK_API_KEY="deepseek-key",
        REQUEST_TIMEOUT_SECONDS=5,
    )
    client = OpenAICompatibleClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
        cooldowns=ProviderCooldowns(),
        usage_recorder=UsageRecorder(database_path),
    )
    request = ChatCompletionRequest(
        model="memory-review-editor",
        messages=[{"role": "user", "content": "只输出 JSON"}],
        response_format={"type": "json_object"},
    )

    with model_usage_scope(user_id="alice"):
        await client.create_chat_completion(
            request=request,
            messages=[{"role": "user", "content": "只输出 JSON"}],
        )

    summary = UsageStore(database_path).summary(user_id="alice", days=None)
    assert calls == ["mimo-v2.5-pro-ultraspeed", "kimi-k2.7-code"]
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["cost_cny"] == pytest.approx(0.00712)
    assert summary["by_model"][0]["provider"] == "kimi"
    assert summary["by_model"][0]["model"] == "kimi-k2.7-code"
    assert summary["by_operation"][0]["operation"] == "memory-review-editor"


def test_usage_summary_api_is_authenticated_and_user_isolated(
    client,
    auth_headers,
    memory_store,
) -> None:
    store = UsageStore(memory_store.database_path)
    store.record_response(
        user_id="alice",
        operation="chat_completion",
        provider="deepseek",
        provider_code="D",
        model="deepseek-v4-flash",
        kind="chat",
        payload={
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            }
        },
    )

    unauthenticated = client.get("/usage/summary")
    response = client.get(
        "/usage/summary?range=all",
        headers={**auth_headers, "X-User-Id": "alice"},
    )
    other_user = client.get(
        "/usage/summary",
        headers={**auth_headers, "X-User-Id": "bob"},
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    payload = response.json()
    assert payload["totals"]["calls"] == 1
    assert any(
        item["model"] == "kimi-k2.7-code-highspeed"
        and item["input_cache_hit_per_million"] == "2.60"
        and item["input_cache_miss_per_million"] == "13.00"
        and item["output_per_million"] == "54.00"
        for item in payload["pricing"]["models"]
    )
    assert other_user.status_code == 200
    assert other_user.json()["totals"]["calls"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_chat_gateway_records_non_stream_and_stream_usage(
    stream,
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": stream,
        },
    )

    summary = client.get(
        "/usage/summary?range=all",
        headers=auth_headers,
    ).json()

    assert response.status_code == 200
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["input_tokens"] == 1
    assert summary["totals"]["output_tokens"] == 1
    assert summary["by_operation"][0]["operation"] == "chat_completion"
    assert summary["by_model"][0]["model"] == "test-upstream"


@pytest.mark.asyncio
async def test_embedding_response_is_recorded_in_the_same_ledger(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "model": "text-embedding-v4",
                    "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                    "usage": {
                        "prompt_tokens": 40,
                        "total_tokens": 40,
                    },
                },
            )

    monkeypatch.setattr(
        "app.memory.search.httpx.AsyncClient",
        FakeAsyncClient,
    )
    database_path = str(tmp_path / "usage.db")
    UsageStore(database_path).init_db()
    embedding_client = OpenAICompatibleEmbeddingClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="embedding-key",
        model="text-embedding-v4",
        dimensions=2,
        allow_sensitive_egress=True,
        usage_recorder=UsageRecorder(database_path),
    )

    with model_usage_scope(user_id="alice", operation="memory_search"):
        vector = await embedding_client.embed("普通测试文本")

    summary = UsageStore(database_path).summary(user_id="alice", days=None)
    assert vector == [0.1, 0.2]
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["input_tokens"] == 40
    assert summary["totals"]["cost_cny"] == pytest.approx(0.00002)
    assert summary["by_model"][0]["kind"] == "embedding"
    assert summary["by_operation"][0]["operation"] == "memory_search"
