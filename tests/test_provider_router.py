import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.llm.client import LegacyOpenAICompatibleClient, OpenAICompatibleClient
from app.openai_compat.schemas import ChatCompletionRequest
from app.providers.client import ProviderRouterClient
from app.providers.config import (
    clear_providers_config_cache,
    load_effective_providers_config,
    load_providers_config,
)
from app.providers.router import ProviderRouter
from app.providers.store import ProviderStore


@pytest.fixture(autouse=True)
def clean_provider_state(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    clear_providers_config_cache()
    ProviderRouter.clear_cooldowns()
    yield
    clear_providers_config_cache()
    ProviderRouter.clear_cooldowns()


async def test_missing_providers_config_falls_back_to_legacy_client(monkeypatch, tmp_path):
    settings = _settings(tmp_path, tmp_path / "missing.toml")
    request = ChatCompletionRequest(
        model="ios-model",
        messages=[{"role": "user", "content": "hi"}],
    )

    async def fake_legacy(self, request, messages):
        return {"id": "legacy", "choices": [{"message": {"content": "legacy"}}]}

    monkeypatch.setattr(
        LegacyOpenAICompatibleClient,
        "create_chat_completion",
        fake_legacy,
    )

    response = await OpenAICompatibleClient(settings).create_chat_completion(
        request=request,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert response["id"] == "legacy"


def test_provider_router_selects_route_and_filters_missing_key(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path, config_path)
    store = ProviderStore(settings.database_path)
    store.init_db()
    config = load_providers_config(settings.providers_config_path)
    router = ProviderRouter(config=config, store=store)

    assert router.candidate_selections("glm-5.1") == []

    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    selections = router.candidate_selections("glm-5.1")

    assert len(selections) == 1
    assert selections[0].provider.id == "zhipu"
    assert selections[0].route.upstream_model == "glm-5.1"


def test_sqlite_config_has_priority_over_toml(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "toml-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    store.upsert_provider_config(
        provider="sqlite-provider",
        name="SQLite Provider",
        base_url="https://sqlite.test",
        api_key="sqlite-key",
    )
    store.create_route_config(
        virtual_model="sqlite-model",
        provider="sqlite-provider",
        upstream_model="sqlite-upstream",
    )

    config = load_effective_providers_config(
        database_path=settings.database_path,
        providers_config_path=settings.providers_config_path,
    )

    assert config.source == "sqlite"
    assert "sqlite-provider" in config.providers
    assert {route.virtual_model for route in config.routes} == {"sqlite-model"}


def test_sqlite_without_usable_route_falls_back_to_toml(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "toml-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    store.upsert_provider_config(
        provider="sqlite-provider",
        name="SQLite Provider",
        base_url="https://sqlite.test",
        api_key="sqlite-key",
    )

    config = load_effective_providers_config(
        database_path=settings.database_path,
        providers_config_path=settings.providers_config_path,
    )

    assert config.source == "toml"
    assert "zhipu" in config.providers


def test_provider_disabled_does_not_participate(monkeypatch, tmp_path):
    config_path = _write_config(
        tmp_path,
        provider_extra="enabled = false",
    )
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    config = load_providers_config(settings.providers_config_path)

    assert ProviderRouter(config=config, store=store).candidate_selections("glm-5.1") == []


def test_balance_filters_routes_and_min_balance_zero_allows_zero(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path, min_balance=10.0)
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    config = load_providers_config(settings.providers_config_path)
    router = ProviderRouter(config=config, store=store)

    assert store.get_balance("zhipu").balance == 0
    assert router.candidate_selections("glm-5.1") == []

    store.adjust_balance(provider="zhipu", amount_delta=20, currency="CNY", reason="test")
    assert len(router.candidate_selections("glm-5.1")) == 1

    zero_config_path = _write_config(tmp_path, filename="zero.toml")
    zero_config = load_providers_config(str(zero_config_path))
    zero_store = ProviderStore(str(tmp_path / "zero.db"))
    zero_store.init_db()
    zero_router = ProviderRouter(config=zero_config, store=zero_store)
    assert len(zero_router.candidate_selections("glm-5.1")) == 1


def test_sqlite_balance_filter_still_applies(tmp_path):
    settings = _settings(tmp_path, tmp_path / "missing.toml")
    store = ProviderStore(settings.database_path)
    store.init_db()
    store.upsert_provider_config(
        provider="sqlite-provider",
        name="SQLite Provider",
        base_url="https://sqlite.test",
        api_key="sqlite-key",
    )
    store.create_route_config(
        virtual_model="sqlite-model",
        provider="sqlite-provider",
        upstream_model="sqlite-upstream",
        min_balance=10,
    )
    config = load_effective_providers_config(
        database_path=settings.database_path,
        providers_config_path=settings.providers_config_path,
    )
    router = ProviderRouter(config=config, store=store)

    assert router.candidate_selections("sqlite-model") == []

    store.adjust_balance(
        provider="sqlite-provider",
        amount_delta=10,
        currency="CNY",
        reason="test",
    )
    assert len(router.candidate_selections("sqlite-model")) == 1


async def test_successful_provider_call_records_usage_and_deducts_balance(monkeypatch, tmp_path):
    config_path = _write_config(
        tmp_path,
        input_price=1.0,
        output_price=2.0,
    )
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    store.adjust_balance(provider="zhipu", amount_delta=100, currency="CNY", reason="top up")
    client = SequenceProviderClient(
        settings,
        responses=[
            _response(
                200,
                {
                    "id": "ok",
                    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                    "usage": {
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 500_000,
                        "total_tokens": 1_500_000,
                    },
                },
            )
        ],
        store=store,
    )

    result = await client.create_chat_completion(
        request=_request(),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert client.calls[0][1]["model"] == "glm-5.1"
    assert result["gateway"]["cost"]["total"] == 2.0
    assert store.get_balance("zhipu").balance == 98.0
    events = store.list_usage_events()
    assert len(events) == 1
    assert events[0].prompt_tokens == 1_000_000
    assert events[0].estimated is False


async def test_missing_usage_is_recorded_as_estimated(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    client = SequenceProviderClient(
        settings,
        responses=[
            _response(
                200,
                {
                    "id": "ok",
                    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                },
            )
        ],
        store=store,
    )

    result = await client.create_chat_completion(
        request=_request(),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result["gateway"]["estimated"] is True
    assert store.list_usage_events()[0].estimated is True


@pytest.mark.parametrize(
    "failure_kind",
    ["rate_limit", "server_error", "timeout"],
)
async def test_provider_failover_tries_next_candidate(monkeypatch, tmp_path, failure_kind):
    config_path = _write_two_provider_config(tmp_path)
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    first_failure = {
        "rate_limit": _response(429, {"error": "rate limited"}),
        "server_error": _response(500, {"error": "server error"}),
        "timeout": httpx.TimeoutException("timeout"),
    }[failure_kind]
    client = SequenceProviderClient(
        settings,
        responses=[
            first_failure,
            _response(
                200,
                {
                    "id": "ok",
                    "choices": [{"message": {"role": "assistant", "content": "fallback"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ),
        ],
        store=store,
    )

    result = await client.create_chat_completion(
        request=_request(),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result["choices"][0]["message"]["content"] == "fallback"
    assert [provider for provider, _ in client.calls] == ["zhipu", "deepseek"]
    assert [event.status for event in store.list_usage_events(limit=10)] == ["success", "error"]


async def test_all_provider_failures_return_502_without_api_key(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path, config_path)
    monkeypatch.setenv("ZHIPU_API_KEY", "secret-provider-key")
    store = ProviderStore(settings.database_path)
    store.init_db()
    client = SequenceProviderClient(
        settings,
        responses=[_response(500, {"error": "secret-provider-key is invalid"})],
        store=store,
    )

    with pytest.raises(HTTPException) as exc_info:
        await client.create_chat_completion(
            request=_request(),
            messages=[{"role": "user", "content": "hi"}],
        )

    assert exc_info.value.status_code == 502
    assert "secret-provider-key" not in str(exc_info.value.detail)
    assert store.list_usage_events()[0].status == "error"


async def test_sqlite_provider_uses_db_api_key_and_records_usage(tmp_path):
    settings = _settings(tmp_path, tmp_path / "missing.toml")
    store = ProviderStore(settings.database_path)
    store.init_db()
    store.upsert_provider_config(
        provider="sqlite-provider",
        name="SQLite Provider",
        base_url="https://sqlite.test",
        api_key="db-secret-key",
    )
    store.create_route_config(
        virtual_model="sqlite-model",
        provider="sqlite-provider",
        upstream_model="sqlite-upstream",
    )
    client = SequenceProviderClient(
        settings,
        responses=[
            _response(
                200,
                {
                    "id": "ok",
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        ],
        store=store,
    )

    await client.create_chat_completion(
        request=ChatCompletionRequest(
            model="sqlite-model",
            messages=[{"role": "user", "content": "hi"}],
        ),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert client.api_keys == ["db-secret-key"]
    assert store.list_usage_events()[0].provider == "sqlite-provider"


async def test_sqlite_provider_failover_tries_next_candidate(tmp_path):
    settings = _settings(tmp_path, tmp_path / "missing.toml")
    store = ProviderStore(settings.database_path)
    store.init_db()
    store.upsert_provider_config(
        provider="first",
        name="First",
        base_url="https://first.test",
        api_key="first-key",
    )
    store.upsert_provider_config(
        provider="second",
        name="Second",
        base_url="https://second.test",
        api_key="second-key",
    )
    store.create_route_config(
        virtual_model="sqlite-model",
        provider="first",
        upstream_model="first-model",
        priority=100,
    )
    store.create_route_config(
        virtual_model="sqlite-model",
        provider="second",
        upstream_model="second-model",
        priority=50,
    )
    client = SequenceProviderClient(
        settings,
        responses=[
            _response(500, {"error": "server error"}),
            _response(
                200,
                {
                    "id": "ok",
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ),
        ],
        store=store,
    )

    result = await client.create_chat_completion(
        request=ChatCompletionRequest(
            model="sqlite-model",
            messages=[{"role": "user", "content": "hi"}],
        ),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert [provider for provider, _ in client.calls] == ["first", "second"]
    assert client.api_keys == ["first-key", "second-key"]


class SequenceProviderClient(ProviderRouterClient):
    def __init__(self, settings, *, responses, store):
        super().__init__(settings, store=store)
        self.responses = list(responses)
        self.calls = []
        self.api_keys = []

    async def _post_chat_completion(self, selection, payload):
        self.calls.append((selection.provider.id, payload))
        self.api_keys.append(selection.api_key)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _settings(tmp_path, config_path) -> Settings:
    return Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        DATABASE_PATH=str(tmp_path / "memory.db"),
        PROVIDERS_CONFIG_PATH=str(config_path),
        UPSTREAM_API_KEY="legacy-key",
        UPSTREAM_MODEL="legacy-model",
    )


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="glm-5.1",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://provider.test/chat/completions"),
    )


def _write_config(
    tmp_path,
    *,
    filename: str = "providers.toml",
    provider_extra: str = "enabled = true",
    input_price: float = 0.0,
    output_price: float = 0.0,
    min_balance: float = 0.0,
):
    path = tmp_path / filename
    path.write_text(
        f"""
[router]
default_model = "glm-5.1"
fallback_enabled = true

[providers.zhipu]
name = "Zhipu"
base_url = "https://zhipu.test/v1"
api_key_env = "ZHIPU_API_KEY"
timeout_seconds = 60
{provider_extra}

[[routes]]
virtual_model = "glm-5.1"
provider = "zhipu"
upstream_model = "glm-5.1"
priority = 100
input_price_per_million = {input_price}
output_price_per_million = {output_price}
currency = "CNY"
min_balance = {min_balance}
""",
        encoding="utf-8",
    )
    clear_providers_config_cache()
    return path


def _write_two_provider_config(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text(
        """
[router]
default_model = "glm-5.1"
fallback_enabled = true

[providers.zhipu]
name = "Zhipu"
base_url = "https://zhipu.test/v1"
api_key_env = "ZHIPU_API_KEY"
enabled = true
timeout_seconds = 60

[providers.deepseek]
name = "DeepSeek"
base_url = "https://deepseek.test"
api_key_env = "DEEPSEEK_API_KEY"
enabled = true
timeout_seconds = 60

[[routes]]
virtual_model = "glm-5.1"
provider = "zhipu"
upstream_model = "glm-5.1"
priority = 100
input_price_per_million = 0.0
output_price_per_million = 0.0
currency = "CNY"
min_balance = 0.0

[[routes]]
virtual_model = "glm-5.1"
provider = "deepseek"
upstream_model = "deepseek-chat"
priority = 50
input_price_per_million = 0.0
output_price_per_million = 0.0
currency = "CNY"
min_balance = 0.0
""",
        encoding="utf-8",
    )
    clear_providers_config_cache()
    return path
