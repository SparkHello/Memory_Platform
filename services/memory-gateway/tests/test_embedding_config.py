import pytest

from app.api.deps import (
    embedding_runtime_enabled,
    get_embedding_client,
    get_knowledge_search_agent,
)
from app.config import Settings, get_settings
from app.memory.search import NullEmbeddingClient, OpenAICompatibleEmbeddingClient
from app.usage.attribution import (
    MODEL_GATEWAY_CORRELATION_HEADER,
    MODEL_GATEWAY_OPERATION_HEADER,
    MODEL_GATEWAY_USER_TAG_HEADER,
)
from app.usage.context import model_usage_scope


def test_embedding_client_requires_model_gateway(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(Exception, match="Model Gateway"):
        get_embedding_client(get_settings())


def test_embedding_client_uses_central_runtime_and_never_direct_fallback(tmp_path) -> None:
    common = {
        "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030/v1",
        "MODEL_GATEWAY_API_KEY": "central-key",
        "MODEL_GATEWAY_EMBEDDING_MODEL": "memory.embedding.custom",
        "EMBEDDING_DIMENSIONS": 3,
        "DATABASE_PATH": str(tmp_path / "memory.db"),
        "KNOWLEDGE_DATABASE_PATH": str(tmp_path / "knowledge.db"),
    }
    configured = Settings(
        _env_file=None,
        MODEL_GATEWAY_EMBEDDING_SPACE_ID="space-v1",
        **common,
    )
    missing_space = Settings(
        _env_file=None,
        MODEL_GATEWAY_EMBEDDING_SPACE_ID="",
        **common,
    )

    client = get_embedding_client(configured)

    assert isinstance(client, OpenAICompatibleEmbeddingClient)
    assert client.base_url == "http://127.0.0.1:2030/v1"
    assert client.api_key == "central-key"
    assert client.model == "memory.embedding.custom"
    assert client.expected_space_id == "space-v1"
    assert client.embedding_space_id == "space-v1"
    assert client.model_gateway_mode is True
    assert embedding_runtime_enabled(configured) is True
    assert isinstance(get_embedding_client(missing_space), NullEmbeddingClient)
    assert embedding_runtime_enabled(missing_space) is False


def test_knowledge_agent_uses_same_central_runtime_and_ignores_direct_routes(
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-key",
        MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL="knowledge.fast.custom",
        MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL="knowledge.pro.custom",
        DATABASE_PATH=str(tmp_path / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge.db"),
    )

    agent = get_knowledge_search_agent(object(), settings)

    assert agent.config.model_runtime is not None
    assert agent.config.model_runtime.is_central is True
    assert agent.config.flash_model == "knowledge.fast.custom"
    assert agent.config.pro_model == "knowledge.pro.custom"
    assert "central-key" not in repr(agent.config)


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
        def __init__(self, *, timeout: float, follow_redirects: bool, trust_env: bool):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

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
        def __init__(self, *, timeout: float, follow_redirects: bool, trust_env: bool):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

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
    captured_headers: list[dict[str, str]] = []
    local_usage_calls: list[dict] = []
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
        def __init__(self, *, timeout: float, follow_redirects: bool, trust_env: bool):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            captured_headers.append(headers)
            return FakeResponse(responses.pop(0))

    monkeypatch.setattr("app.memory.search.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleEmbeddingClient(
        base_url="http://127.0.0.1:2030/v1",
        api_key="central-key",
        model="memory.embedding",
        dimensions=3,
        expected_space_id="memory-embed-v1",
        model_gateway_mode=True,
        usage_hmac_secret="embedding-test-signing-secret-0123456789abcdef",
        usage_recorder=type(
            "Recorder",
            (),
            {"record_response": lambda _self, **kwargs: local_usage_calls.append(kwargs)},
        )(),
    )

    assert await client.embed("第一次") is None
    with model_usage_scope(user_id="alice", operation="knowledge_index"):
        assert await client.embed("第二次") == [0.1, 0.2, 0.3]
    assert captured_headers[0][MODEL_GATEWAY_OPERATION_HEADER] == "memory.embedding"
    assert captured_headers[1][MODEL_GATEWAY_OPERATION_HEADER] == "knowledge_index"
    assert all(
        headers[MODEL_GATEWAY_CORRELATION_HEADER].startswith("mgc_")
        for headers in captured_headers
    )
    assert all(
        headers[MODEL_GATEWAY_USER_TAG_HEADER].startswith("usr_")
        for headers in captured_headers
    )
    assert local_usage_calls == []


@pytest.mark.asyncio
async def test_embedding_rejects_vector_with_wrong_length(monkeypatch) -> None:
    class FakeResponse:
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool, trust_env: bool):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

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

    # This asserts the built-in default, so it must ignore the project .env
    # a developer may have switched on locally.
    assert Settings(_env_file=None).allow_sensitive_egress is False
