import json
import logging

from fastapi import HTTPException, status
import httpx

from app.config import Settings
from app.openai_compat.schemas import ChatCompletionRequest
from app.providers.billing import (
    build_error_usage_event,
    build_success_usage_event,
    gateway_debug_payload,
)
from app.providers.config import load_effective_providers_config
from app.providers.models import ProviderSelection, ProvidersConfig, UsageEvent
from app.providers.router import ProviderRouter
from app.providers.store import ProviderStore

logger = logging.getLogger(__name__)


class ProviderRoutingUnavailable(Exception):
    """Provider router is disabled or has no usable candidate route."""


class ProviderRouterClient:
    def __init__(
        self,
        settings: Settings,
        *,
        config: ProvidersConfig | None = None,
        store: ProviderStore | None = None,
    ):
        self.settings = settings
        self.store = store or ProviderStore(settings.database_path)
        self.store.init_db()
        self.config = config or load_effective_providers_config(
            database_path=settings.database_path,
            providers_config_path=settings.providers_config_path,
        )
        self.router = ProviderRouter(config=self.config, store=self.store)

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        messages: list[dict[str, str]],
    ) -> dict:
        selections = self.router.candidate_selections(request.model)
        if not selections:
            raise ProviderRoutingUnavailable()

        last_detail = "没有可用 provider"
        last_status = status.HTTP_502_BAD_GATEWAY
        for index, selection in enumerate(selections):
            payload = request.model_dump(exclude_none=True, exclude={"conversation_id"})
            payload["model"] = selection.route.upstream_model
            payload["messages"] = messages
            payload["stream"] = False

            try:
                response = await self._post_chat_completion(selection, payload)
            except httpx.TimeoutException as exc:
                last_detail = "provider 调用超时"
                self._record_error(selection, request, "timeout")
                self.router.mark_cooldown(selection.provider.id, 30)
                if self._can_try_next(index, selections):
                    continue
                raise _bad_gateway(last_detail) from exc
            except httpx.HTTPError as exc:
                last_detail = f"provider 网络错误：{_safe_exception_text(exc)}"
                self._record_error(selection, request, "network_error")
                self.router.mark_cooldown(selection.provider.id, 30)
                if self._can_try_next(index, selections):
                    continue
                raise _bad_gateway(last_detail) from exc

            if response.status_code >= 400:
                error_type = _classify_http_error(response)
                last_detail = f"{selection.provider.id} 返回错误：{_safe_error_detail(response, selection.api_key)}"
                last_status = response.status_code
                self._record_error(selection, request, error_type)
                if error_type in {"rate_limit", "server_error", "timeout", "network_error"}:
                    self.router.mark_cooldown(selection.provider.id, 30)
                if error_type in {"auth", "quota"}:
                    self.router.mark_cooldown(selection.provider.id, 60)
                if _should_failover(error_type) and self._can_try_next(index, selections):
                    continue
                raise _bad_gateway(last_detail)

            try:
                data = _json_from_utf8_bytes(response)
            except json.JSONDecodeError as exc:
                self._record_error(selection, request, "invalid_json")
                raise _bad_gateway("provider 返回了无法解析的 JSON") from exc

            provider_model = self.config.provider_models.get(selection.route.provider_model_id) if selection.route.provider_model_id else None
            if provider_model is None:
                logger.warning(
                    "route %s has no linked ProviderModelConfig, skipping billing",
                    selection.route.virtual_model,
                )
                data["gateway"] = {"skipped": True, "reason": "no pricing model"}
                return data
            usage_data = data.get("usage") if isinstance(data, dict) else None
            cache_hit_tokens = _coerce_non_negative_int(usage_data.get("prompt_cache_hit_tokens")) if isinstance(usage_data, dict) else 0
            event = build_success_usage_event(
                response=data,
                messages=messages,
                route=selection.route,
                model=provider_model,
                provider=selection.provider.id,
                user_id=request.user,
                conversation_id=request.conversation_id,
                cache_hit_tokens=cache_hit_tokens,
            )
            self._record_success(event)
            data["gateway"] = gateway_debug_payload(event)
            return data

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"所有 provider 调用失败，最后状态 {last_status}：{last_detail}",
        )

    async def _post_chat_completion(
        self,
        selection: ProviderSelection,
        payload: dict,
    ) -> httpx.Response:
        url = f"{selection.provider.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {selection.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=selection.provider.timeout_seconds) as client:
            return await client.post(url, json=payload, headers=headers)

    def _record_success(self, event: UsageEvent) -> None:
        try:
            self.store.record_usage_event(event)
            self.store.deduct_balance(
                provider=event.provider,
                amount=event.total_cost,
                currency=event.currency,
            )
        except Exception:
            logger.exception("provider 用量记账失败")

    def _record_error(
        self,
        selection: ProviderSelection,
        request: ChatCompletionRequest,
        error_type: str,
    ) -> None:
        try:
            provider_model = self.config.provider_models.get(selection.route.provider_model_id) if selection.route.provider_model_id else None
            if provider_model is None:
                logger.warning(
                    "route %s has no linked ProviderModelConfig, skipping error billing",
                    selection.route.virtual_model,
                )
                return
            event = build_error_usage_event(
                route=selection.route,
                model=provider_model,
                provider=selection.provider.id,
                user_id=request.user,
                conversation_id=request.conversation_id,
                error_type=error_type,
            )
            self.store.record_usage_event(event)
        except Exception:
            logger.exception("provider 错误用量记录失败")

    def _can_try_next(self, index: int, selections: list[ProviderSelection]) -> bool:
        return self.config.router.fallback_enabled and index < len(selections) - 1


def _bad_gateway(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


def _classify_http_error(response: httpx.Response) -> str:
    text = response.content.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in ("insufficient_quota", "quota", "balance")):
        return "quota"
    if response.status_code in {401, 403}:
        return "auth"
    if response.status_code == 402:
        return "quota"
    if response.status_code == 429:
        return "rate_limit"
    if response.status_code >= 500:
        return "server_error"
    return "http_error"


def _should_failover(error_type: str) -> bool:
    return error_type in {"auth", "quota", "rate_limit", "server_error", "timeout", "network_error"}


def _safe_error_detail(response: httpx.Response, api_key: str) -> str:
    try:
        data = _json_from_utf8_bytes(response)
        detail = str(data)
    except (UnicodeError, json.JSONDecodeError):
        detail = response.content.decode("utf-8", errors="replace")
    return _redact(detail, api_key)[:500]


def _safe_exception_text(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:300]


def _json_from_utf8_bytes(response: httpx.Response) -> dict:
    try:
        raw_text = response.content.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("provider 响应不是合法 UTF-8，已使用替换字符解码。")
        raw_text = response.content.decode("utf-8", errors="replace")
    return json.loads(raw_text)


def _redact(text: str, secret: str) -> str:
    if secret:
        return text.replace(secret, "[redacted]")
    return text


def _coerce_non_negative_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)
