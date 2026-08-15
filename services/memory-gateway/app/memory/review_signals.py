"""Single source of truth for memory-review signal predicates and thresholds.

Both the search ranking path (surface signals) and the MemoryReviewer
(recommendations) previously re-derived the same triggers — expired,
near-expiry, review-due, sensitive, stale, emotion-uncertain, low-life — with
their own copies of the threshold constants.  Keeping them here prevents the
two from drifting apart.

Also holds the shared "time-variable fact" and "bare age answer" detectors,
formerly duplicated between review_policy / review and extractor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re

STALE_DAYS = 90.0
NEAR_EXPIRY_DAYS = 14
LOW_LIFE_THRESHOLD = 30.0

_STALE_IMPORTANCE_FLOOR = 6
_EMOTION_AROUSAL_FLOOR = 0.7
_EMOTION_CONFIDENCE_CEILING = 0.55


def is_expired(valid_until: datetime | None, *, now: datetime) -> bool:
    return valid_until is not None and valid_until < now


def is_near_expiry(valid_until: datetime | None, *, now: datetime) -> bool:
    return valid_until is not None and valid_until <= now + timedelta(days=NEAR_EXPIRY_DAYS)


def is_review_due(review_after: datetime | None, *, now: datetime) -> bool:
    return review_after is not None and review_after <= now


def is_stale(days_since_last_active: float, importance: int) -> bool:
    return days_since_last_active >= STALE_DAYS and importance >= _STALE_IMPORTANCE_FLOOR


def is_emotion_uncertain(arousal: float, confidence: float) -> bool:
    return arousal >= _EMOTION_AROUSAL_FLOOR and confidence <= _EMOTION_CONFIDENCE_CEILING


def is_low_life(life: float) -> bool:
    return life <= LOW_LIFE_THRESHOLD


_TIME_VARIABLE_PATTERNS: tuple[str, ...] = (
    r"\d{1,3}\s*岁",
    r"years?\s+old",
    r"\by/o\b",
    r"年龄",
    r"岁",
    r"自称",
    r"截至\s*\d{4}[-年]\d{1,2}",
    r"现在",
    r"目前",
    r"当前",
    r"正在",
    r"最近",
    r"近期",
    r"计划",
    r"准备",
    r"打算",
    r"临时",
    r"current(?:ly)?",
    r"\bnow\b",
    r"recently",
    r"planning",
)


def is_time_variable_memory(content: str) -> bool:
    """Union of the former review_policy and review marker sets."""
    lowered = content.lower()
    return any(re.search(pattern, lowered) for pattern in _TIME_VARIABLE_PATTERNS)


AGE_CONTEXT_PATTERN = re.compile(
    r"(?:多少\s*岁|多大(?:了)?|年龄(?:是)?多少|几岁)"
    r"|\bhow\s+old\b|\bage\b",
    re.IGNORECASE,
)

BARE_AGE_ANSWER_PATTERN = re.compile(r"^\s*(\d{1,3})\s*[。.!！]?\s*$")


def parse_bare_age_answer(source_quote: str) -> int | None:
    """Parse a bare numeric answer to an age question, or return None."""
    match = BARE_AGE_ANSWER_PATTERN.fullmatch(source_quote)
    if match is None:
        return None
    age = int(match.group(1))
    return age if 0 < age < 130 else None


def contextual_age_answer(
    *,
    source_quote: str,
    context_quote: str,
    context_text: str | None = None,
) -> int | None:
    """Interpret a bare numeric answer using a verified age-question context.

    review_policy 会额外要求 context_quote 逐字出现在 context_text 中；
    extractor 的 context_quote 由独立的 context_quote gate 校验，因此不传
    context_text。两条路径此前各有一份同名不同签名的实现，已收敛于此。
    """
    if context_text is not None and context_quote not in context_text:
        return None
    if not AGE_CONTEXT_PATTERN.search(context_quote):
        return None
    return parse_bare_age_answer(source_quote)
