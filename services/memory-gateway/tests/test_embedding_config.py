import pytest

from app.api.deps import get_embedding_client
from app.config import Settings, get_settings
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


def test_model_gateway_embedding_requires_explicit_immutable_space() -> None:
    without_space = get_embedding_client(
        Settings(
            _env_file=None,
            MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
            MODEL_GATEWAY_API_KEY="central-key",
            EMBEDDING_API_KEY="legacy-key-must-not-be-used",
        )
    )
    configured = get_embedding_client(
        Settings(
            _env_file=None,
            MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
            MODEL_GATEWAY_API_KEY="central-key",
            MODEL_GATEWAY_EMBEDDING_MODEL="memory.embedding",
            MODEL_GATEWAY_EMBEDDING_SPACE_ID="qwen3.7/1024/v1",
            EMBEDDING_DIMENSIONS=1024,
            EMBEDDING_API_KEY="legacy-key-must-not-be-used",
        )
    )

    assert isinstance(without_space, NullEmbeddingClient)
    assert isinstance(configured, OpenAICompatibleEmbeddingClient)
    assert configured.base_url == "http://127.0.0.1:2030/v1"
    assert configured.api_key == "central-key"
    assert configured.model == "memory.embedding"
    assert configured.expected_space_id == "qwen3.7/1024/v1"
    assert configured.embedding_space_id == "qwen3.7/1024/v1"
    assert configured.model_gateway_mode is True


def test_direct_embedding_uses_stable_local_space_without_key_material(tmp_path) -> None:
    common = {
        "MODEL_GATEWAY_BASE_URL": "",
        "MODEL_GATEWAY_API_KEY": "",
        "EMBEDDING_BASE_URL": "https://embedding.example.invalid/v1/",
        "EMBEDDING_MODEL": "embedding-v1",
        "EMBEDDING_DIMENSIONS": 768,
        "DATABASE_PATH": str(tmp_path / "memory.db"),
    }
    original = get_embedding_client(
        Settings(_env_file=None, EMBEDDING_API_KEY="first-key", **common)
    )
    rotated_key = get_embedding_client(
        Settings(_env_file=None, EMBEDDING_API_KEY="second-key", **common)
    )
    changed_model = get_embedding_client(
        Settings(
            _env_file=None,
            EMBEDDING_API_KEY="first-key",
            **{**common, "EMBEDDING_MODEL": "embedding-v2"},
        )
    )
    changed_dimensions = get_embedding_client(
        Settings(
            _env_file=None,
            EMBEDDING_API_KEY="first-key",
            **{**common, "EMBEDDING_DIMENSIONS": 1024},
        )
    )

    assert isinstance(original, OpenAICompatibleEmbeddingClient)
    assert isinstance(rotated_key, OpenAICompatibleEmbeddingClient)
    assert original.expected_space_id == ""
    assert original.embedding_space_id.startswith(
        "direct-openai-compatible-v1:"
    )
    assert original.embedding_space_id == rotated_key.embedding_space_id
    assert original.embedding_space_id != changed_model.embedding_space_id
    assert original.embedding_space_id != changed_dimensions.embedding_space_id
    assert "first-key" not in original.embedding_space_id


@pytest.mark.asyncio
async def test_embedding_request_uses_openai_compatible_payload(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        headers = {"X-Model-Gateway-Embedding-Space": "memory-embed-v1"}

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
        dimensions=3,
        expected_space_id="memory-embed-v1",
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
                "dimensions": 3,
            },
            "headers": {"Authorization": "Bearer dashscope-key"},
        }
    ]


@pytest.mark.asyncio
async def test_embedding_rejects_unexpected_model_gateway_space(monkeypatch) -> None:
    class FakeResponse:
        headers = {"X-Model-Gateway-Embedding-Space": "other-space"}

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
            return FakeResponse()

    monkeypatch.setattr("app.memory.search.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleEmbeddingClient(
        base_url="http://127.0.0.1:2030/v1",
        api_key="local-key",
        model="memory.embedding",
        dimensions=3,
        expected_space_id="memory-embed-v1",
    )

    assert await client.embed("普通文本") is None


@pytest.mark.asyncio
async def test_embedding_model_gateway_requires_origin_metadata(monkeypatch) -> None:
    responses = [
        {"X-Model-Gateway-Embedding-Space": "memory-embed-v1"},
        {
            "X-Model-Gateway-Route": "memory.embedding",
            "X-Model-Gateway-Deployment": "embedding-primary",
            "X-Model-Gateway-Connection": "dashscope-official",
            "X-Model-Gateway-Channel-Operator": "alibaba",
            "X-Model-Gateway-Model-Author": "alibaba",
            "X-Model-Gateway-Vendor": "alibaba",
            "X-Model-Gateway-Upstream-Model": "qwen3.7-text-embedding",
            "X-Model-Gateway-Embedding-Space": "memory-embed-v1",
            "X-Model-Gateway-Embedding-Dimensions": "3",
        },
    ]

    class FakeResponse:
        def __init__(self, headers: dict[str, str]):
            self.headers = headers

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
            return FakeResponse(responses.pop(0))

    monkeypatch.setattr("app.memory.search.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleEmbeddingClient(
        base_url="http://127.0.0.1:2030/v1",
        api_key="central-key",
        model="memory.embedding",
        dimensions=3,
        expected_space_id="memory-embed-v1",
        model_gateway_mode=True,
    )

    assert await client.embed("第一次") is None
    assert await client.embed("第二次") == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embedding_rejects_vector_with_wrong_length(monkeypatch) -> None:
    class FakeResponse:
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            return FakeResponse()

    monkeypatch.setattr("app.memory.search.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleEmbeddingClient(
        base_url="https://example.invalid/v1",
        api_key="embedding-key",
        model="embedding-model",
        dimensions=3,
        expected_space_id="direct-space",
    )

    assert await client.embed("普通文本") is None


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
