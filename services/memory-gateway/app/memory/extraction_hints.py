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

_FUTURE_OR_INTENT_MARKERS = (
    "未来",
    "将来",
    "明年",
    "明天",
    "下周",
    "下个月",
    "下月",
    "下季度",
    "以后",
    "计划",
    "打算",
    "希望",
    "想要",
    "准备",
    "考虑",
    "即将",
    "可能",
    "也许",
    "如果",
    "假如",
    "假设",
    r"(?:^|[\s,，。])想(?:去|到|在|为|搬|换|用|买|住|工作)",
    r"(?:^|[\s,，。])(?:将|会)(?:去|在|为|搬|换|用|成为|开始|住|工作)",
    "next week",
    "next month",
    "next year",
    "in the future",
    "plan to",
    "planning to",
    "intend to",
    "hope to",
    "want to",
    "would like to",
    "going to",
    "might",
    "may",
    r"\bwill\b",
    r"\bshall\b",
    "considering",
    "if ",
)

_UNCOMMITTED_INTENT_MARKERS = (
    "计划",
    "打算",
    "希望",
    "想要",
    "准备",
    "考虑",
    "可能",
    "也许",
    "如果",
    "假如",
    "假设",
    r"(?:^|[\s,，。])想(?:去|到|在|为|搬|换|用|买|住|工作)",
    "plan to",
    "planning to",
    "intend to",
    "hope to",
    "want to",
    "would like to",
    "might",
    "may",
    "considering",
    "if ",
)

_THIRD_PARTY_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:我的?|我)\s*)?(?:朋友|同事|妻子|丈夫|伴侣|父亲|母亲|"
    r"父母|孩子|儿子|女儿|老师|客户|他|她|他们|她们)"
    r"|^\s*(?:my\s+)?(?:friend|colleague|coworker|wife|husband|partner|"
    r"parent|parents|child|children|teacher|client)\b|^\s*(?:he|she|they)\b",
    flags=re.IGNORECASE,
)
_JOINT_FIRST_PERSON_RE = re.compile(
    r"^\s*(?:我和|我与|我跟)|^\s*(?:i\s+and\b|my\s+.+?\s+and\s+i\b)",
    flags=re.IGNORECASE,
)
_EMPLOYER_VALUE_RE = re.compile(
    r"(?:在|为)\s*(?P<zh_value>.{1,40}?)\s*(?:工作|任职|就职)"
    r"|(?:任职|就职)(?:于|在)\s*(?P<zh_after>.{1,40})"
    r"|\bwork(?:ing|s)?\s+(?:at|for)\s+(?P<en_value>[^,.;!?]{1,60})",
    flags=re.IGNORECASE,
)
_EMPLOYER_GENERIC_VALUES = {
    "远程",
    "线上",
    "线下",
    "本地",
    "家里",
    "家中",
    "办公室",
    "学校",
    "北京",
    "上海",
    "深圳",
    "广州",
    "杭州",
    "成都",
    "重庆",
    "南京",
    "苏州",
    "python",
    "javascript",
    "java",
    "software",
    "tech",
    "technology",
    "home",
    "the office",
    "an office",
    "school",
    "remote",
    "remotely",
    "night",
}
_CITY_GENERIC_VALUES = {
    "医院",
    "酒店",
    "宿舍",
    "家里",
    "家中",
    "当下",
    "python",
    "software",
    "technology",
    "the moment",
    "a simulation",
}
_CITY_REJECT_PREFIXES = (
    "住房",
    "房屋",
    "房产",
    "宿舍",
    "住宿",
    "医院",
    "酒店",
    "python",
    "software",
    "technology",
)

_TEMPORAL_PROFILE_SLOTS: tuple[TemporalProfileSlot, ...] = (
    TemporalProfileSlot(
        "preferred_name",
        (
            r"^\s*(?:(?:以后|请|请你|麻烦你)\s*)*(?:叫我|称呼我)",
            r"^\s*我\s*(?:叫|的名字是)",
            r"^\s*(?:please\s+)?call me\b",
            r"^\s*my name is\b",
        ),
        needs_current_marker=False,
    ),
    TemporalProfileSlot(
        "current_employer",
        (
            r"^\s*(?:(?:我|本人)\s*)?(?:(?:现在|目前|当前|正在)\s*)?"
            r"(?:在|为).{1,40}(?:工作|任职|就职)",
            r"^\s*(?:(?:我|本人)\s*)?(?:任职|就职)(?:于|在).{1,40}",
            r"^\s*(?:我的)?(?:雇主|所在公司|就职公司)\s*(?:是|为)",
            r"^\s*(?:since\s+\S+\s+|currently\s+)?i\s+(?:(?:now|currently)\s+)?"
            r"work(?:ing)?\s+(?:at|for)\b",
            r"^\s*from\s+\S+\s+i\s+will\s+work\s+(?:at|for)\b",
            r"^\s*my\s+employer\s+is\b",
        ),
        needs_current_marker=False,
    ),
    TemporalProfileSlot(
        "current_city",
        (
            r"^\s*(?:(?:现在|目前|当前)\s*)?(?:我|本人)\s*"
            r"(?:(?:现在|目前|当前)\s*)?(?:住(?:在)?|居住(?:在)?|常住(?:在)?|定居(?:在)?)\s*(?=\S)",
            r"^\s*(?:(?:现在|目前|当前)\s*)?(?:住在|居住(?:在)?|常住(?:在)?|定居(?:在)?)\s*(?=\S)",
            r"^\s*(?:(?:我|本人)\s*)?(?:已经|刚刚|刚|现在).{0,8}搬到\s*(?=\S)",
            r"^\s*(?:currently\s+)?i\s+(?:currently\s+)?(?:live|am living)\s+in\b",
            r"^\s*i(?:'m|\s+am)\s+based in\b",
            r"^\s*i\s+(?:have\s+|just\s+)?moved\s+to\b",
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


def apply_extraction_hints(
    candidate: CandidateMemory,
    *,
    source_text: str | None = None,
) -> CandidateMemory:
    """Apply conservative post-extraction hints that are safer than broad guessing.

    The LLM still does the main extraction. This layer only nudges obvious sector
    collapses and fills temporal keys for a tiny whitelist of replaceable profile
    slots.
    """
    # Kept as an ignored compatibility argument for older callers. Whole-batch
    # source text must never participate in per-candidate inference.
    del source_text
    if candidate.action == "ignore":
        return candidate

    text = _hint_text(candidate)
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
            slot = next(slot for slot in _TEMPORAL_PROFILE_SLOTS if slot.predicate == predicate)
            if _matches_temporal_slot(text, slot, candidate=candidate):
                candidate.temporal_subject = candidate.temporal_subject or "用户"
                return
            candidate.temporal_subject = None
            candidate.temporal_predicate = None
            _append_reason(candidate, "temporal key 缺少候选引用支撑，已清空。")
        else:
            candidate.temporal_subject = None
            candidate.temporal_predicate = None
            _append_reason(candidate, "非白名单 temporal key 已清空，避免误触发自动失效。")

    if _matches_any(text, _PAST_MARKERS):
        return

    for slot in _TEMPORAL_PROFILE_SLOTS:
        if not _matches_temporal_slot(text, slot, candidate=candidate):
            continue
        candidate.temporal_subject = "用户"
        candidate.temporal_predicate = slot.predicate
        _append_reason(candidate, f"命中白名单 profile 槽位 {slot.predicate}，补充 temporal key。")
        return


def _matches_temporal_slot(
    text: str,
    slot: TemporalProfileSlot,
    *,
    candidate: CandidateMemory,
) -> bool:
    # Scope polarity and time intent to the clause that actually asserts the
    # slot.  A later "但我没有宠物" or "明年去旅游" must not erase the
    # independently asserted current city in the first clause.
    for clause in _temporal_fact_clauses(text):
        if _clause_is_third_party_assertion(clause):
            continue
        fact_clause = _normalize_additive_clause(clause)
        if not _matches_any(fact_clause, slot.patterns):
            continue
        if _matches_any(clause, _PAST_MARKERS):
            continue
        if slot.predicate == "preferred_name":
            if _is_negative_preferred_name(clause):
                continue
        elif _has_negative_polarity(clause):
            continue

        if slot.predicate != "preferred_name" and _matches_any(
            clause,
            _FUTURE_OR_INTENT_MARKERS,
        ):
            # A committed, explicitly dated future state is a valid scheduled
            # temporal fact.  Desires, plans, hypotheticals and relative dates
            # without a grounded valid_from remain outside the current slot.
            if (
                not candidate.valid_from
                or _matches_any(clause, _UNCOMMITTED_INTENT_MARKERS)
            ):
                continue
        if slot.needs_current_marker and not _matches_any(clause, _CURRENT_MARKERS):
            continue
        if not _slot_value_is_supported(slot, fact_clause):
            continue
        return True
    return False


def _hint_text(candidate: CandidateMemory) -> str:
    # The model-authored normalized memory must never serve as evidence for its
    # own type or temporal metadata. Only the verbatim, already-verified quote
    # may drive deterministic hints.
    return candidate.source_quote


def _has_negative_polarity(text: str) -> bool:
    from app.memory.utils import _has_negation

    normalized = re.sub(
        r"不但|不仅|不只是|不只|\bnot\s+only\b|\bwithout\s+fail\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return _has_negation(normalized)


def _temporal_fact_clauses(text: str) -> list[str]:
    clauses = [
        clause.strip()
        for clause in re.split(
            r"[。！？!?;；\n,，]+|(?<!不)但(?:是)?|不过|然而|"
            r"\b(?:but|however|while)\b",
            text,
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    return clauses or [text]


def _clause_is_third_party_assertion(clause: str) -> bool:
    if _JOINT_FIRST_PERSON_RE.search(clause):
        return False
    return _THIRD_PARTY_SUBJECT_RE.search(clause) is not None


def _normalize_additive_clause(clause: str) -> str:
    return re.sub(
        r"^(\s*(?:我|本人|i)\s*)(?:不但|不仅|不只|not\s+only)\s*",
        r"\1",
        clause,
        flags=re.IGNORECASE,
    )


def _is_negative_preferred_name(clause: str) -> bool:
    return re.search(
        r"(?:不要|别)(?:再)?\s*(?:叫我|称呼我)"
        r"|\b(?:do\s+not|don't|never)\s+call\s+me\b",
        clause,
        flags=re.IGNORECASE,
    ) is not None


def _slot_value_is_supported(slot: TemporalProfileSlot, clause: str) -> bool:
    if slot.predicate == "current_employer":
        return _has_employer_value(clause)
    if slot.predicate == "current_city":
        return _has_city_value(clause)
    return True


def _has_employer_value(clause: str) -> bool:
    explicit = re.search(
        r"(?:雇主|所在公司|就职公司)\s*(?:是|为)\s*(?P<value>.+)$"
        r"|\bmy\s+employer\s+is\s+(?P<en_explicit>.+)$",
        clause,
        flags=re.IGNORECASE,
    )
    match = explicit or _EMPLOYER_VALUE_RE.search(clause)
    if match is None:
        return False
    value = next((value for value in match.groupdict().values() if value), "")
    normalized = re.sub(
        r"^(?:现在|目前|当前|正在|currently)\s*",
        "",
        value.strip().casefold(),
    ).strip()
    normalized = re.sub(r"^(?:the|a|an|一家)\s+", "", normalized)
    if not normalized:
        return False
    return normalized not in _EMPLOYER_GENERIC_VALUES


def _has_city_value(clause: str) -> bool:
    match = re.search(
        r"(?:住在|居住(?:在)?|常住(?:在)?|定居(?:在)?|搬到)\s*(?P<zh>.+)$"
        r"|(?:我|本人)\s*(?:现在|目前|当前)?\s*住\s*(?P<zh_bare>.+)$"
        r"|\b(?:live|living)\s+in\s+(?P<en>.+)$"
        r"|\bbased\s+in\s+(?P<en_based>.+)$"
        r"|\bmoved\s+to\s+(?P<en_moved>.+)$",
        clause,
        flags=re.IGNORECASE,
    )
    if match is None:
        return False
    value = next((value for value in match.groupdict().values() if value), "")
    normalized = value.strip().casefold().strip()
    normalized = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    if not normalized:
        return False
    return (
        normalized not in _CITY_GENERIC_VALUES
        and not any(normalized.startswith(prefix) for prefix in _CITY_REJECT_PREFIXES)
    )


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def _append_reason(candidate: CandidateMemory, suffix: str) -> None:
    candidate.reason = f"{candidate.reason}；{suffix}" if candidate.reason else suffix
