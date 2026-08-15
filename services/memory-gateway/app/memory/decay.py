from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math

from app.config import get_settings
from app.memory.models import MemoryRecord
from app.memory.utils import _parse_iso_datetime


SECTOR_LAMBDA_MAP: dict[str, float] = {}


def _load_sector_lambda_map() -> dict[str, float]:
    """从 Settings 解析扇区 -> lambda 映射，按需 lazy-load。"""
    global SECTOR_LAMBDA_MAP
    if SECTOR_LAMBDA_MAP:
        return SECTOR_LAMBDA_MAP
    try:
        raw = get_settings().decay_sector_lambda_map
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("sector lambda map must be an object")
        SECTOR_LAMBDA_MAP = {
            str(sector): float(value)
            for sector, value in parsed.items()
            if not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and 0.0 <= float(value) <= 10.0
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        SECTOR_LAMBDA_MAP = {"semantic": 0.02}
    return SECTOR_LAMBDA_MAP


@dataclass(frozen=True)
class MemoryDecayScore:
    final_score: float
    activation_count: float
    last_active_at: str | None
    freshness_bonus: float
    days_since_last_active: float
    hours_since_created: float
    resolved_factor: float = 1.0
    emotion_factor: float = 1.0
    sector_lambda: float = 0.02


def score_memory(memory: MemoryRecord, *, now: datetime | None = None) -> MemoryDecayScore:
    """Ebbinghaus 衰减引擎。

    公式: score = importance * (activation_count + 1)^alpha
                   * weighted_factor * status_factor * digested_factor

    其中 weighted_factor = time_weight * e^(-lambda * days)
                          + emotion_weight * emotion_factor
    """
    settings = get_settings()
    current = _utc_now(now)

    activation_count = float(memory.usage_count or 0)
    days = _elapsed_days(current, _parse_iso_datetime(last_active_at(memory)))
    status = getattr(memory, "status", "dynamic") or "dynamic"

    sector_lambda_map = _load_sector_lambda_map()
    sector_lambda = sector_lambda_map.get(
        memory.type,
        settings.decay_lambda_default,
    )
    if memory.decay_lambda is not None:
        candidate_lambda = float(memory.decay_lambda)
        # MemoryRecord rejects these values on every supported write path. Keep
        # scoring defensive for legacy/corrupt rows and model_construct callers
        # so math.exp cannot become an availability failure.
        if math.isfinite(candidate_lambda) and 0.0 <= candidate_lambda <= 10.0:
            sector_lambda = candidate_lambda

    alpha = settings.decay_alpha_default

    activation_factor = (activation_count + 1.0) ** alpha
    recency_days = 0.0 if status == "pinned" else days
    recency_factor = math.exp(-sector_lambda * recency_days)

    # 短期/长期权重分离 (MemoryBank)
    if days <= settings.decay_short_term_days:
        time_weight = settings.decay_short_term_time_weight
        emotion_weight = 1.0 - time_weight
    else:
        emotion_weight = settings.decay_long_term_emotion_weight
        time_weight = 1.0 - emotion_weight

    # 情绪因子：高唤起记忆在长期阶段保留得更久
    raw_arousal = getattr(memory, "arousal", None)
    arousal = 0.3 if raw_arousal is None else float(raw_arousal)
    emotion_factor = 0.2 + 0.8 * max(0.0, min(1.0, arousal))

    weighted_factor = (
        1.0
        if status == "pinned"
        else time_weight * recency_factor + emotion_weight * emotion_factor
    )

    # 状态因子
    status_factor = 1.0
    if status == "resolved":
        status_factor = settings.decay_resolved_factor  # 0.05
    elif status == "pinned":
        status_factor = 1.0  # 永不降权

    # digested 加速衰减
    digested = bool(getattr(memory, "digested", False))
    digested_factor = (
        1.0
        if status == "pinned"
        else settings.decay_digested_factor if digested else 1.0
    )  # 0.02

    final = (
        float(memory.importance)
        * activation_factor
        * weighted_factor
        * status_factor
        * digested_factor
    )

    hours_since_created = _elapsed_hours(current, _parse_iso_datetime(memory.created_at))

    return MemoryDecayScore(
        final_score=final,
        activation_count=activation_count,
        last_active_at=last_active_at(memory),
        freshness_bonus=freshness_bonus(memory, now=current),
        days_since_last_active=days,
        hours_since_created=hours_since_created,
        resolved_factor=status_factor,
        emotion_factor=emotion_factor,
        sector_lambda=sector_lambda,
    )


def life_score(
    memory: MemoryRecord,
    *,
    now: datetime | None = None,
    decay: MemoryDecayScore | None = None,
) -> float:
    """计算记忆的生命力评分 (0-100)，用于 surface_memories 排序。"""
    current_decay = decay or score_memory(memory, now=now)
    recency = math.exp(-current_decay.sector_lambda * current_decay.days_since_last_active) * 70.0
    usage = min(20.0, math.log1p(float(current_decay.activation_count)) * 10.0)
    freshness = min(10.0, max(0.0, 1.0 - current_decay.days_since_last_active / 30.0) * 10.0)
    return max(0.0, min(100.0, recency + usage + freshness))


def last_active_at(memory: MemoryRecord) -> str | None:
    return memory.last_used_at or memory.updated_at or memory.created_at


def freshness_bonus(memory: MemoryRecord, *, now: datetime | None = None) -> float:
    """Non-persistent new-memory signal used for surfacing reasons/ranking."""
    current = _utc_now(now)
    created_at = _parse_iso_datetime(memory.created_at)
    days_since_created = _elapsed_days(current, created_at)
    bonus_window_days = 7.0
    if days_since_created >= bonus_window_days:
        return 1.0
    return 1.0 + (1.0 - days_since_created / bonus_window_days) * 0.25


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        current = datetime.now(UTC)
    else:
        current = now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    return current


def _elapsed_days(now: datetime, earlier: datetime | None) -> float:
    if earlier is None:
        return 0.0
    return max(0.0, (now - earlier).total_seconds() / 86400)


def _elapsed_hours(now: datetime, earlier: datetime | None) -> float:
    if earlier is None:
        return 0.0
    return max(0.0, (now - earlier).total_seconds() / 3600)
