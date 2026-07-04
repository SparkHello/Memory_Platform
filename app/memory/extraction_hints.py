from __future__ import annotations

from dataclasses import dataclass
import re

from app.memory.models import CandidateMemory


TEMPORAL_PROFILE_PREDICATES: set[str] = {
    "current_employer",
    "current_city",
    "primary_ai_client",
    "primary_device",
    "preferred_name",
}


@dataclass(frozen=True)
class TemporalProfileSlot:
    predicate: str
    patterns: tuple[str, ...]
    needs_current_marker: bool = True


_CURRENT_MARKERS = (
    "现在",
    "目前",
    "当前",
    "正在",
    "主要",
    "主力",
    "默认",
    "常用",
    "从",
    "开始",
    "now",
    "currently",
    "primary",
    "main",
    "default",
    "since",
)

_PAST_MARKERS = (
    "以前",
    "曾经",
    "曾在",
    "曾用",
    "过去",
    "去年",
    "上家公司",
    "用过",
    "去过",
    "previously",
    "formerly",
    "used to",
    "last year",
)

_TEMPORAL_PROFILE_SLOTS: tuple[TemporalProfileSlot, ...] = (
    TemporalProfileSlot(
        "preferred_name",
        (
            r"叫我",
            r"称呼我",
            r"我叫",
            r"我的名字是",
            r"\bcall me\b",
            r"\bmy name is\b",
        ),
        needs_current_marker=False,
    ),
    TemporalProfileSlot(
        "current_employer",
        (
            r"工作",
            r"任职",
            r"就职",
            r"雇主",
            r"公司",
            r"\bwork(?:ing|s)?\s+(?:at|for|in)\b",
            r"\bemployer\b",
        ),
        needs_current_marker=False,
    ),
    TemporalProfileSlot(
        "current_city",
        (
            r"住在",
            r"居住",
            r"常住",
            r"定居",
            r"搬到",
            r"\bliv(?:e|es|ing)\s+in\b",
            r"\bbased in\b",
        ),
        needs_current_marker=False,
    ),
    TemporalProfileSlot(
        "primary_ai_client",
        (
            r"AI\s*客户端",
            r"ai\s*client",
            r"Kelivo",
            r"ChatGPT",
            r"Claude",
        ),
    ),
    TemporalProfileSlot(
        "primary_device",
        (
            r"主力设备",
            r"主力手机",
            r"主力电脑",
            r"手机",
            r"电脑",
            r"iPhone",
            r"Mac",
            r"Windows",
            r"\bdevice\b",
            r"\bphone\b",
            r"\blaptop\b",
            r"\bcomputer\b",
        ),
    ),
)

_SECTOR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "emotional",
        (
            r"喜欢",
            r"偏好",
            r"讨厌",
            r"不喜欢",
            r"雷点",
            r"希望",
            r"担心",
            r"焦虑",
            r"压力",
            r"开心",
            r"难受",
            r"\bprefer(?:s|red)?\b",
            r"\blike(?:s|d)?\b",
            r"\bhate(?:s|d)?\b",
            r"\banxious\b",
            r"\bfrustrat(?:ed|ing)\b",
        ),
    ),
    (
        "reflective",
        (
            r"发现",
            r"意识到",
            r"总结",
            r"复盘",
            r"反思",
            r"经验是",
            r"更适合",
            r"\breali[sz]ed\b",
            r"\bconcluded\b",
            r"\blesson\b",
            r"\breflection\b",
        ),
    ),
    (
        "procedural",
        (
            r"步骤",
            r"流程",
            r"方法",
            r"先.+再",
            r"先.+然后",
            r"部署",
            r"检查清单",
            r"\bsteps?\b",
            r"\bworkflow\b",
            r"\bprocess\b",
            r"\bprocedure\b",
        ),
    ),
    (
        "episodic",
        (
            r"昨天",
            r"上周",
            r"上个月",
            r"去年",
            r"今天在",
            r"那次",
            r"\b\d{4}-\d{1,2}-\d{1,2}\b",
            r"\byesterday\b",
            r"\blast week\b",
            r"\blast month\b",
        ),
    ),
)


def apply_extraction_hints(candidate: CandidateMemory, *, source_text: str) -> CandidateMemory:
    """Apply conservative post-extraction hints that are safer than broad guessing.

    The LLM still does the main extraction. This layer only nudges obvious sector
    collapses and fills temporal keys for a tiny whitelist of replaceable profile
    slots.
    """
    if candidate.action == "ignore":
        return candidate

    text = _hint_text(candidate, source_text)
    _apply_sector_hint(candidate, text)
    _apply_temporal_profile_hint(candidate, text)
    return candidate


def _apply_sector_hint(candidate: CandidateMemory, text: str) -> None:
    if candidate.type != "semantic":
        return
    for memory_type, patterns in _SECTOR_HINTS:
        if _matches_any(text, patterns):
            candidate.type = memory_type  # type: ignore[assignment]
            _append_reason(candidate, f"根据用户原文的明显信号，将类型校正为 {memory_type}。")
            return


def _apply_temporal_profile_hint(candidate: CandidateMemory, text: str) -> None:
    predicate = (candidate.temporal_predicate or "").strip()
    if predicate:
        if predicate in TEMPORAL_PROFILE_PREDICATES:
            candidate.temporal_subject = candidate.temporal_subject or "用户"
            return
        candidate.temporal_subject = None
        candidate.temporal_predicate = None
        _append_reason(candidate, "非白名单 temporal key 已清空，避免误触发自动失效。")

    if _matches_any(text, _PAST_MARKERS):
        return

    for slot in _TEMPORAL_PROFILE_SLOTS:
        if slot.needs_current_marker and not _matches_any(text, _CURRENT_MARKERS):
            continue
        if not _matches_any(text, slot.patterns):
            continue
        candidate.temporal_subject = "用户"
        candidate.temporal_predicate = slot.predicate
        _append_reason(candidate, f"命中白名单 profile 槽位 {slot.predicate}，补充 temporal key。")
        return


def _hint_text(candidate: CandidateMemory, source_text: str) -> str:
    return "\n".join(
        part
        for part in (source_text, candidate.source_quote, candidate.memory, candidate.reason)
        if part
    )


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def _append_reason(candidate: CandidateMemory, suffix: str) -> None:
    candidate.reason = f"{candidate.reason}；{suffix}" if candidate.reason else suffix
