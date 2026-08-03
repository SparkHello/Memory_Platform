from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class ModelUsageContext:
    user_id: str = "default"
    operation: str = "unspecified"


_MODEL_USAGE_CONTEXT: ContextVar[ModelUsageContext] = ContextVar(
    "model_usage_context",
    default=ModelUsageContext(),
)


def current_usage_context() -> ModelUsageContext:
    return _MODEL_USAGE_CONTEXT.get()


@contextmanager
def model_usage_scope(
    *,
    user_id: str | None = None,
    operation: str | None = None,
) -> Iterator[ModelUsageContext]:
    current = current_usage_context()
    value = ModelUsageContext(
        user_id=(user_id or current.user_id or "default").strip() or "default",
        operation=(operation or current.operation or "unspecified").strip()
        or "unspecified",
    )
    token = _MODEL_USAGE_CONTEXT.set(value)
    try:
        yield value
    finally:
        _MODEL_USAGE_CONTEXT.reset(token)
