import pytest

from app.api.deps import get_embedding_client
from app.config import get_settings
from app.memory.search import NullEmbeddingClient, OpenAICompatibleEmbeddingClient


def test_embedding_client_uses_embedding_config_only(monkeypatch) -> None:
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setenv("UPSTREAM_API_KEY", "zhipu-key")
    monkeypatch.setenv("UPSTREAM_MODEL", "glm-5.1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "dashscope-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1024")
    get_settings.cache_clear()

    client = get_embedding_client(get_settings())

    assert isinstance(client, OpenAICompatibleEmbeddingClient)
    assert client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert client.api_key == "dashscope-key"
    assert client.model == "text-embedding-v4"
    assert client.dimensions == 1024
    assert client.api_key != "zhipu-key"


def test_embedding_client_falls_back_without_embedding_api_key(monkeypatch) -> None:
    monkeypatch.setenv("UPSTREAM_API_KEY", "zhipu-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    get_settings.cache_clear()

    client = get_embedding_client(get_settings())

    assert isinstance(client, NullEmbeddingClient)


@pytest.mark.asyncio
async def test_embedding_request_uses_openai_compatible_payload(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("app.memory.search.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleEmbeddingClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="dashscope-key",
        model="text-embedding-v4",
        dimensions=1024,
    )

    embedding = await client.embed("用户喜欢黑咖啡")

    assert embedding == [0.1, 0.2, 0.3]
    assert calls == [
        {
            "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            "json": {
                "model": "text-embedding-v4",
                "input": "用户喜欢黑咖啡",
                "encoding_format": "float",
                "dimensions": 1024,
            },
            "headers": {"Authorization": "Bearer dashscope-key"},
        }
    ]


@pytest.mark.asyncio
async def test_sensitive_embedding_is_blocked_before_http(monkeypatch) -> None:
    calls = []

    class UnexpectedAsyncClient:
        def __init__(self, *, timeout: float):
            calls.append(timeout)

    monkeypatch.setattr("app.memory.search.httpx.AsyncClient", UnexpectedAsyncClient)
    client = OpenAICompatibleEmbeddingClient(
        base_url="https://example.invalid/v1",
        api_key="embedding-key",
        model="embedding-model",
    )

    embedding = await client.embed("我的身份证号是 123456789012345678")

    assert embedding is None
    assert calls == []


def test_sensitive_egress_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_SENSITIVE_EGRESS", raising=False)
    get_settings.cache_clear()

    assert get_settings().allow_sensitive_egress is False
