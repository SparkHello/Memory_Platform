from fastapi.testclient import TestClient
import httpx

from app.config import get_settings
from app.main import create_app
from app.memory.store import MemoryStore
from app.providers.config import clear_providers_config_cache
from app.providers.models import UsageEvent
from app.providers.router import ProviderRouter
from app.providers.store import ProviderStore


def test_admin_usage_summary(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    store = ProviderStore(memory_store.database_path)
    store.init_db()
    store.record_usage_event(
        UsageEvent(
            virtual_model="glm-5.1",
            provider="zhipu",
            upstream_model="glm-5.1",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            total_cost=0.2,
            currency="CNY",
            status="success",
        )
    )

    usage = client.get("/admin/usage?provider=zhipu", headers=auth_headers)
    assert usage.status_code == 200
    assert usage.json()["data"][0]["virtual_model"] == "glm-5.1"

    summary = client.get("/admin/usage/summary", headers=auth_headers)
    assert summary.status_code == 200
    assert summary.json()["data"][0]["total_tokens"] == 15
    assert summary.json()["data"][0]["total_cost"] == 0.2


def test_admin_providers_missing_config_is_disabled(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/admin/providers", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["providers"] == []
    assert response.json()["routes"] == []


def test_v1_models_falls_back_to_legacy_model(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["glm-5.1"]


def test_v1_models_returns_virtual_models_from_provider_config(monkeypatch, tmp_path):
    config_path = tmp_path / "providers.toml"
    config_path.write_text(
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

[[provider_models]]
id = "model-zhipu"
provider = "zhipu"
upstream_model = "glm-5.1"
display_name = "GLM 5.1"
api_format = "openai_compatible"
pricing_mode = "flat"
input_price_per_million = 0
output_price_per_million = 0
cache_hit_price_per_million = 0
currency = "CNY"
enabled = true

[[provider_models]]
id = "model-zhipu-flash"
provider = "zhipu"
upstream_model = "glm-4-flash"
display_name = "GLM 4 Flash"
api_format = "openai_compatible"
pricing_mode = "flat"
input_price_per_million = 0
output_price_per_million = 0
cache_hit_price_per_million = 0
currency = "CNY"
enabled = true

[[routes]]
virtual_model = "glm-5.1"
provider = "zhipu"
upstream_model = "glm-5.1"
provider_model_id = "model-zhipu"
priority = 100
min_balance = 0.0

[[routes]]
virtual_model = "memory-extractor"
provider = "zhipu"
upstream_model = "glm-4-flash"
provider_model_id = "model-zhipu-flash"
priority = 100
min_balance = 0.0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("PROVIDERS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("UPSTREAM_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    get_settings.cache_clear()
    clear_providers_config_cache()
    ProviderRouter.clear_cooldowns()

    app = create_app()
    with TestClient(app) as test_client:
        response = test_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-gateway-key"},
        )

    app.dependency_overrides.clear()
    get_settings.cache_clear()
    clear_providers_config_cache()
    ProviderRouter.clear_cooldowns()

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [
        "glm-5.1",
        "memory-extractor",
    ]


def test_provider_config_api_hides_and_preserves_api_key(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    created = client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )

    assert created.status_code == 200
    assert "secret-key" not in created.text
    assert "api_key" not in created.json()["provider"]
    assert created.json()["provider"]["api_key_configured"] is True

    store = ProviderStore(memory_store.database_path)
    assert store.get_provider_config("zhipu").api_key == "secret-key"

    patched = client.patch(
        "/admin/provider-config/providers/zhipu",
        headers=auth_headers,
        json={"name": "Zhipu Updated"},
    )

    assert patched.status_code == 200
    assert "secret-key" not in patched.text
    assert "api_key" not in patched.json()["provider"]
    assert store.get_provider_config("zhipu").api_key == "secret-key"

    cleared = client.patch(
        "/admin/provider-config/providers/zhipu",
        headers=auth_headers,
        json={"api_key": ""},
    )

    assert cleared.status_code == 200
    assert cleared.json()["provider"]["api_key_configured"] is False
    assert store.get_provider_config("zhipu").api_key == ""

    config = client.get("/admin/provider-config", headers=auth_headers)
    assert config.status_code == 200
    assert "secret-key" not in config.text
    assert "api_key" not in config.json()["providers"][0]


def test_route_config_api_controls_v1_models(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    provider = client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    assert provider.status_code == 200

    route = client.post(
        "/admin/provider-config/routes",
        headers=auth_headers,
        json={
            "virtual_model": "ui-model",
            "provider": "zhipu",
            "upstream_model": "glm-5.1",
            "priority": 100,
            "input_price_per_million": 0,
            "output_price_per_million": 0,
            "currency": "CNY",
            "min_balance": 0,
            "enabled": True,
        },
    )
    assert route.status_code == 200
    route_id = route.json()["route"]["id"]

    models = client.get("/v1/models", headers=auth_headers)
    assert [item["id"] for item in models.json()["data"]] == ["ui-model"]

    disabled = client.patch(
        f"/admin/provider-config/routes/{route_id}",
        headers=auth_headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    models = client.get("/v1/models", headers=auth_headers)
    assert "ui-model" not in [item["id"] for item in models.json()["data"]]

    enabled = client.patch(
        f"/admin/provider-config/routes/{route_id}",
        headers=auth_headers,
        json={"enabled": True},
    )
    assert enabled.status_code == 200
    deleted = client.delete(f"/admin/provider-config/routes/{route_id}", headers=auth_headers)
    assert deleted.status_code == 200
    models = client.get("/v1/models", headers=auth_headers)
    assert "ui-model" not in [item["id"] for item in models.json()["data"]]


def test_route_can_select_provider_model(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    provider = client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    assert provider.status_code == 200

    provider_model = client.post(
        "/admin/provider-config/models",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "upstream_model": "glm-5-1",
            "display_name": "GLM 5.1",
            "input_price_per_million": 1.0,
            "output_price_per_million": 2.0,
            "currency": "CNY",
            "enabled": True,
        },
    )
    assert provider_model.status_code == 200
    model_id = provider_model.json()["model"]["id"]

    route = client.post(
        "/admin/provider-config/routes",
        headers=auth_headers,
        json={
            "virtual_model": "glm-5.1",
            "provider_model_id": model_id,
            "priority": 100,
            "min_balance": 0,
            "enabled": True,
        },
    )
    assert route.status_code == 200
    route_payload = route.json()["route"]
    assert route_payload["provider"] == "zhipu"
    assert route_payload["upstream_model"] == "glm-5-1"
    assert route_payload["provider_model_id"] == model_id
    assert route_payload["provider_model_id"] == model_id

    store = ProviderStore(memory_store.database_path)
    config = store.load_sqlite_providers_config()
    selection = ProviderRouter(config=config, store=store).candidate_selections("glm-5.1")
    assert len(selection) == 1
    assert selection[0].route.upstream_model == "glm-5-1"


def test_provider_model_capability_metadata_is_configurable(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    provider = client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "anthropic",
            "name": "Anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    assert provider.status_code == 200

    pricing_tiers_json = '[{"up_to_tokens":1000000,"input":3,"output":15}]'
    provider_model = client.post(
        "/admin/provider-config/models",
        headers=auth_headers,
        json={
            "provider": "anthropic",
            "upstream_model": "claude-sonnet-4",
            "display_name": "Claude Sonnet 4",
            "api_format": "claude_sdk",
            "pricing_mode": "tiered",
            "pricing_tiers_json": pricing_tiers_json,
            "input_price_per_million": 3.0,
            "output_price_per_million": 15.0,
            "currency": "USD",
            "enabled": True,
        },
    )
    assert provider_model.status_code == 200
    model_payload = provider_model.json()["model"]
    model_id = model_payload["id"]
    assert model_payload["api_format"] == "claude_sdk"
    assert model_payload["pricing_mode"] == "tiered"
    assert model_payload["pricing_tiers_json"] == pricing_tiers_json

    route = client.post(
        "/admin/provider-config/routes",
        headers=auth_headers,
        json={
            "virtual_model": "claude-sonnet",
            "provider_model_id": model_id,
            "priority": 100,
            "min_balance": 0,
            "enabled": True,
        },
    )
    assert route.status_code == 200

    config = client.get("/admin/provider-config", headers=auth_headers).json()
    assert config["provider_models"][0]["api_format"] == "claude_sdk"
    assert config["provider_models"][0]["pricing_mode"] == "tiered"

    exported = client.get("/admin/provider-config/export-toml", headers=auth_headers)
    assert exported.status_code == 200
    assert 'api_format = "claude_sdk"' in exported.text
    assert 'pricing_mode = "tiered"' in exported.text
    assert "secret-key" not in exported.text

    store = ProviderStore(memory_store.database_path)
    router = ProviderRouter(config=store.load_sqlite_providers_config(), store=store)
    assert router.candidate_selections("claude-sonnet") == []


def test_import_toml_and_export_toml_omits_api_key(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
) -> None:
    providers_toml = memory_store.database_path + ".providers.toml"
    with open(providers_toml, "w", encoding="utf-8") as handle:
        handle.write(
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

[[routes]]
virtual_model = "glm-5.1"
provider = "zhipu"
upstream_model = "glm-5.1"
priority = 100
input_price_per_million = 0.0
output_price_per_million = 0.0
currency = "CNY"
min_balance = 0.0
"""
        )

    imported = client.post("/admin/provider-config/import-toml", headers=auth_headers)
    assert imported.status_code == 200
    assert imported.json() == {"providers": 1, "provider_models": 0, "routes": 1}

    store = ProviderStore(memory_store.database_path)
    assert store.get_provider_config("zhipu").api_key == ""
    store.patch_provider_config(provider="zhipu", api_key_update="secret-key", update_api_key=True)

    exported = client.get("/admin/provider-config/export-toml", headers=auth_headers)
    assert exported.status_code == 200
    assert "secret-key" not in exported.text
    assert 'api_key_env = "ZHIPU_API_KEY"' in exported.text


def test_delete_provider_soft_disables(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )

    deleted = client.delete("/admin/provider-config/providers/zhipu", headers=auth_headers)

    assert deleted.status_code == 200
    assert deleted.json()["provider"]["enabled"] is False


def test_hard_delete_provider_removes_models_and_routes(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    provider_model = client.post(
        "/admin/provider-config/models",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "upstream_model": "glm-5-1",
            "enabled": True,
        },
    )
    model_id = provider_model.json()["model"]["id"]
    client.post(
        "/admin/provider-config/routes",
        headers=auth_headers,
        json={
            "virtual_model": "glm-5.1",
            "provider_model_id": model_id,
            "enabled": True,
        },
    )

    deleted = client.delete("/admin/provider-config/providers/zhipu?hard=true", headers=auth_headers)

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["providers"] == 1
    assert deleted.json()["provider_models"] == 1
    assert deleted.json()["routes"] == 1

    config = client.get("/admin/provider-config", headers=auth_headers)
    assert config.status_code == 200
    assert config.json()["providers"] == []
    assert config.json()["provider_models"] == []
    assert config.json()["routes"] == []


def test_hard_delete_provider_model_removes_bound_routes(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    provider_model = client.post(
        "/admin/provider-config/models",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "upstream_model": "glm-5-1",
            "enabled": True,
        },
    )
    model_id = provider_model.json()["model"]["id"]
    client.post(
        "/admin/provider-config/routes",
        headers=auth_headers,
        json={
            "virtual_model": "glm-5.1",
            "provider_model_id": model_id,
            "enabled": True,
        },
    )

    deleted = client.delete(f"/admin/provider-config/models/{model_id}?hard=true", headers=auth_headers)

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["provider_models"] == 1
    assert deleted.json()["routes"] == 1

    config = client.get("/admin/provider-config", headers=auth_headers)
    assert config.status_code == 200
    assert len(config.json()["providers"]) == 1
    assert config.json()["provider_models"] == []
    assert config.json()["routes"] == []


def test_provider_test_endpoint_does_not_require_or_return_key_when_missing(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "zhipu",
            "name": "Zhipu",
            "base_url": "https://zhipu.test/v1",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    client.post(
        "/admin/provider-config/routes",
        headers=auth_headers,
        json={
            "virtual_model": "ui-model",
            "provider": "zhipu",
            "upstream_model": "glm-5.1",
            "enabled": True,
        },
    )

    response = client.post(
        "/admin/provider-config/providers/zhipu/test",
        headers=auth_headers,
        json={},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["error_type"] == "missing_key"
    assert "api_key" not in response.text


def test_provider_test_endpoint_can_use_sqlite_model_without_route(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, json: dict, headers: dict) -> httpx.Response:
            calls.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("app.api.admin.httpx.AsyncClient", FakeAsyncClient)
    client.post(
        "/admin/provider-config/providers",
        headers=auth_headers,
        json={
            "provider": "new-provider",
            "name": "New Provider",
            "base_url": "https://provider.test/v1",
            "api_key": "secret-key",
            "enabled": True,
            "timeout_seconds": 60,
        },
    )
    client.post(
        "/admin/provider-config/models",
        headers=auth_headers,
        json={
            "provider": "new-provider",
            "upstream_model": "new-model",
            "enabled": True,
        },
    )

    response = client.post(
        "/admin/provider-config/providers/new-provider/test",
        headers=auth_headers,
        json={},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls[0]["url"] == "https://provider.test/v1/chat/completions"
    assert calls[0]["json"]["model"] == "new-model"
