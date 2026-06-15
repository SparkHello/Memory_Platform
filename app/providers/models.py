from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.memory.models import utc_now_iso


class ProviderConfig(BaseModel):
    id: str
    name: str
    base_url: str
    api_key_env: str = ""
    api_key: str = Field(default="", exclude=True)
    enabled: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0)
    created_at: str | None = None
    updated_at: str | None = None


class ProviderModelConfig(BaseModel):
    id: str
    provider: str
    upstream_model: str
    display_name: str = ""
    api_format: Literal["openai_compatible", "claude_sdk"] = "openai_compatible"
    pricing_mode: Literal["flat", "tiered"] = "flat"
    pricing_tiers_json: str = ""
    input_price_per_million: float = Field(default=0.0, ge=0.0)
    output_price_per_million: float = Field(default=0.0, ge=0.0)
    cache_hit_price_per_million: float = Field(default=0.0, ge=0.0)
    currency: str = "CNY"
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class RouteConfig(BaseModel):
    id: str | None = None
    virtual_model: str
    provider: str
    upstream_model: str
    provider_model_id: str | None = None
    priority: int = 0
    min_balance: float = Field(default=0.0, ge=0.0)
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class RouterConfig(BaseModel):
    default_model: str | None = None
    fallback_enabled: bool = True


class ProvidersConfig(BaseModel):
    enabled: bool
    path: str
    source: str = "legacy"
    router: RouterConfig = Field(default_factory=RouterConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    provider_models: dict[str, ProviderModelConfig] = Field(default_factory=dict)
    routes: list[RouteConfig] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def has_routes(self) -> bool:
        if not self.enabled or not self.providers or not self.routes:
            return False
        return any(
            route.enabled
            and (provider := self.providers.get(route.provider)) is not None
            and provider.enabled
            and (
                not route.provider_model_id
                or (
                    (model := self.provider_models.get(route.provider_model_id)) is not None
                    and model.enabled
                    and model.provider == provider.id
                    and model.api_format == "openai_compatible"
                )
            )
            for route in self.routes
        )


class ProviderSelection(BaseModel):
    virtual_model: str
    provider: ProviderConfig
    route: RouteConfig
    api_key: str
    balance: float


class BalanceRecord(BaseModel):
    provider: str
    currency: str = "CNY"
    balance: float = 0.0
    updated_at: str | None = None


class UsageEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str | None = None
    conversation_id: str | None = None
    virtual_model: str
    provider: str
    upstream_model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    currency: str = "CNY"
    estimated: bool = False
    status: str = "success"
    error_type: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
