from datetime import datetime, timedelta
import re

from app.memory.models import (
    CandidateMemory,
    MemoryReviewPolicy,
    MemorySensitivity,
    MemoryStability,
    MemoryType,
)
from app.memory.review_signals import (
    contextual_age_answer,
    is_time_variable_memory,
)
from app.memory.utils import _utc_now


def review_after_for_days(days: int, *, now: datetime | None = None) -> str:
    base = _utc_now(now)
    return (base + timedelta(days=days)).isoformat()


def build_review_policy(
    *,
    content: str,
    type: MemoryType,
    stability: MemoryStability,
    sensitivity: MemorySensitivity,
    now: datetime | None = None,
) -> MemoryReviewPolicy:
    code, days, reason = _review_policy_parts(
        content=content,
        type=type,
        stability=stability,
        sensitivity=sensitivity,
    )
    return MemoryReviewPolicy(
        code=code,
        interval_days=days,
        review_after=review_after_for_days(days, now=now),
        reason=reason,
    )


def normalize_time_uncertain_candidate(
    candidate: CandidateMemory,
    *,
    source_text: str | None = None,
    now: datetime | None = None,
) -> CandidateMemory:
    if candidate.action == "ignore":
        return candidate

    # Age is rewritten only when the candidate's exact source quote carries
    # the age, either explicitly or as a bare answer to a verified age question.
    age = _unanchored_age(candidate.source_quote)
    if age is None and source_text and candidate.context_quote:
        age = contextual_age_answer(
            source_quote=candidate.source_quote,
            context_quote=candidate.context_quote,
            context_text=source_text,
        )
    if age is None:
        return candidate

    base = _utc_now(now)
    candidate.memory = f"截至 {base.strftime('%Y-%m')}，用户自称 {age} 岁。"
    candidate.type = "semantic"
    candidate.stability = "medium"
    candidate.confidence = min(candidate.confidence, 0.85)
    candidate.review_after = review_after_for_days(180, now=base)
    suffix = "年龄信息缺少生日或出生年份，已保存为带时间锚点的自称事实。"
    candidate.reason = f"{candidate.reason}；{suffix}" if candidate.reason else suffix
    return candidate


def _review_policy_parts(
    *,
    content: str,
    type: MemoryType,
    stability: MemoryStability,
    sensitivity: MemorySensitivity,
) -> tuple[str, int, str]:
    if stability == "temporary":
        return "temporary", 15, "临时记忆按 15 天复核。"
    if sensitivity != "normal":
        return "sensitive", 90, "私密或敏感记忆按 90 天复核。"
    if is_time_variable_memory(content):
        return "time_variable", 180, "年龄、当前状态或时间锚点不足的事实按 180 天复核。"
    if stability == "medium" or type in {"episodic", "procedural"}:
        return "stage", 30, "阶段性事件、流程或中期记忆按 30 天复核。"
    return "stable", 365, "稳定语义、情绪或反思记忆按 365 天复核。"


def _unanchored_age(text: str) -> int | None:
    if _has_time_or_birth_anchor(text):
        return None
    patterns = (
        r"(?:我|本人)?\s*(?:现在|目前|今年)\s*(\d{1,3})\s*岁",
        r"(?:我|本人)?\s*(\d{1,3})\s*岁",
        r"(?:i am|i'm)\s*(\d{1,3})(?:\s*years?\s+old)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        age = int(match.group(1))
        if 0 < age < 130:
            return age
    return None


def _has_time_or_birth_anchor(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"(?:19|20)\d{2}\s*[年/-]", lowered):
        return True
    markers = (
        "出生",
        "生日",
        "生于",
        "出生于",
        "born",
        "birthday",
    )
    return any(marker in lowered for marker in markers)
