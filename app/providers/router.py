import os
import time

from app.providers.models import ProviderSelection, ProvidersConfig, RouteConfig
from app.providers.store import ProviderStore

_COOLDOWNS: dict[str, float] = {}


class ProviderRouter:
    def __init__(self, *, config: ProvidersConfig, store: ProviderStore):
        self.config = config
        self.store = store

    def candidate_selections(self, requested_model: str | None) -> list[ProviderSelection]:
        if not self.config.has_routes:
            return []

        virtual_model = self._resolve_virtual_model(requested_model)
        if not virtual_model:
            return []

        candidates = [
            route
            for route in self.config.routes
            if route.enabled and route.virtual_model == virtual_model
        ]
        candidates.sort(key=lambda route: route.priority, reverse=True)

        selections: list[ProviderSelection] = []
        for route in candidates:
            provider = self.config.providers.get(route.provider)
            if provider is None or not provider.enabled:
                continue
            if route.provider_model_id:
                provider_model = self.config.provider_models.get(route.provider_model_id)
                if (
                    provider_model is None
                    or not provider_model.enabled
                    or provider_model.provider != provider.id
                    or provider_model.api_format != "openai_compatible"
                ):
                    continue
            if self.is_in_cooldown(provider.id):
                continue
            api_key = provider.api_key or os.getenv(provider.api_key_env, "")
            if not api_key:
                continue
            balance = self.store.get_balance(provider.id).balance
            if route.min_balance > balance:
                continue
            selections.append(
                ProviderSelection(
                    virtual_model=virtual_model,
                    provider=provider,
                    route=route,
                    api_key=api_key,
                    balance=balance,
                )
            )
        return selections

    def virtual_models(self) -> list[str]:
        models = {route.virtual_model for route in self.config.routes if route.enabled}
        return sorted(models)

    @staticmethod
    def mark_cooldown(provider: str, seconds: float) -> None:
        _COOLDOWNS[provider] = time.monotonic() + seconds

    @staticmethod
    def clear_cooldowns() -> None:
        _COOLDOWNS.clear()

    @staticmethod
    def is_in_cooldown(provider: str) -> bool:
        until = _COOLDOWNS.get(provider)
        if until is None:
            return False
        if until <= time.monotonic():
            _COOLDOWNS.pop(provider, None)
            return False
        return True

    def _resolve_virtual_model(self, requested_model: str | None) -> str | None:
        requested = (requested_model or "").strip()
        if requested and self._has_route(requested):
            return requested
        default_model = (self.config.router.default_model or "").strip()
        if default_model and self._has_route(default_model):
            return default_model
        if requested:
            return requested
        return default_model or None

    def _has_route(self, virtual_model: str) -> bool:
        return any(
            route.enabled and route.virtual_model == virtual_model
            for route in self.config.routes
        )


def route_public_summary(route: RouteConfig) -> dict:
    return {
        "id": route.id,
        "virtual_model": route.virtual_model,
        "provider": route.provider,
        "upstream_model": route.upstream_model,
        "provider_model_id": route.provider_model_id,
        "priority": route.priority,
        "input_price_per_million": route.input_price_per_million,
        "output_price_per_million": route.output_price_per_million,
        "currency": route.currency,
        "min_balance": route.min_balance,
        "enabled": route.enabled,
        "created_at": route.created_at,
        "updated_at": route.updated_at,
    }
