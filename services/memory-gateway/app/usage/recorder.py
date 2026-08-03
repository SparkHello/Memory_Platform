from __future__ import annotations

import logging
from typing import Any

from app.usage.context import current_usage_context
from app.usage.pricing import provider_slug
from app.usage.store import UsageStore


logger = logging.getLogger(__name__)


class UsageRecorder:
    """Best-effort accounting that never changes a model call's outcome."""

    def __init__(self, database_path: str):
        self.store = UsageStore(database_path)

    def record_response(
        self,
        *,
        payload: dict[str, Any],
        model: str,
        kind: str,
        provider_code: str = "",
        base_url: str = "",
        provider_override: str = "",
        use_local_pricing: bool = True,
        user_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        context = current_usage_context()
        actual_user_id = (user_id or context.user_id or "default").strip() or "default"
        actual_operation = (
            operation or context.operation or "unspecified"
        ).strip() or "unspecified"
        response_model = payload.get("model")
        actual_model = (
            response_model.strip()
            if isinstance(response_model, str) and response_model.strip()
            else model
        )
        provider = provider_override.strip().lower() or provider_slug(
            provider_code=provider_code,
            model=actual_model,
            base_url=base_url,
        )
        try:
            self.store.record_response(
                user_id=actual_user_id,
                operation=actual_operation,
                provider=provider,
                provider_code=provider_code,
                model=actual_model,
                kind=kind,
                payload=payload,
                use_local_pricing=use_local_pricing,
            )
        except Exception:
            logger.exception(
                "记录模型用量失败；不影响模型调用。provider=%s model=%s operation=%s",
                provider,
                actual_model,
                actual_operation,
            )
