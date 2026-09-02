"""Local pre-filter that skips the ``memory.extract`` LLM call for trivial turns.

Every completed ``/v1`` turn used to trigger one extraction request, so a bare
"你好" or "谢谢" cost as much as a real statement. This module makes a cheap,
deterministic decision *before* the finalize outbox is written. It is
deliberately conservative: it only recognises closed classes of text
(greetings/acknowledgements, question-only turns, code-only turns) and never
uses a bare length threshold, because short Chinese statements such as
"不吃辣" or "我姓王" carry long-term facts.

Rules that always force extraction, evaluated first:

* the turn contains an explicit memory directive (记住 / remember …);
* the previous assistant message asked a question, so a short reply may be an
  elided answer ("18" after "你多大了？");
* with prior context, a very short reply that is *not* a lexicon acknowledgement
  is treated as a possible elided answer as well.

Any exception inside the caller must fall open to extraction; skipping is an
optimisation, losing a memory is a defect.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from app.memory.extractor import (
    _has_explicit_memory_directive,
    _is_standalone_memory_directive,
)
from app.memory.search import _QUERY_QUESTION_PHRASES

PrefilterRule = Literal["greeting", "question_only", "code_only"]

REASON_PREFIX = "本地预过滤："
_RULE_REASONS: dict[PrefilterRule, str] = {
    "greeting": f"{REASON_PREFIX}本轮仅为寒暄或确认，未调用提取模型",
    "question_only": f"{REASON_PREFIX}本轮仅为提问，未调用提取模型",
    "code_only": f"{REASON_PREFIX}本轮仅包含代码，未调用提取模型",
}

# Replies this short (after compaction) are treated as possible elided answers
# when the conversation already has context, unless they are lexicon hits.
_SHORT_ANSWER_MAX_COMPACT_CHARS = 8


@dataclass(frozen=True, slots=True)
class PrefilterDecision:
    skip: bool
    rule: PrefilterRule | None = None
    reason: str = ""


_NO_SKIP = PrefilterDecision(skip=False)


def _skip(rule: PrefilterRule) -> PrefilterDecision:
    return PrefilterDecision(skip=True, rule=rule, reason=_RULE_REASONS[rule])


_NON_WORD_RE = re.compile(r"[\W_]+")


def compact_text(text: str) -> str:
    """Lower-case the text and drop whitespace, punctuation and emoji.

    ``\\W`` is Unicode-aware for ``str`` patterns: CJK characters, Latin letters
    and digits survive, while ASCII/fullwidth punctuation and emoji are removed.
    """
    return _NON_WORD_RE.sub("", text).lower()


# Closed set of greetings and acknowledgements. Polar answers (是的 / 对 / 不是 /
# yes / no) are intentionally absent: they may answer an assistant question and
# are protected by the assistant-question guard instead.
_ACKNOWLEDGEMENT_WORDS: tuple[str, ...] = (
    "你好",
    "您好",
    "哈喽",
    "嗨",
    "早",
    "早上好",
    "中午好",
    "下午好",
    "晚上好",
    "晚安",
    "谢谢",
    "谢谢你",
    "谢谢啦",
    "多谢",
    "感谢",
    "非常感谢",
    "好的",
    "好",
    "好吧",
    "嗯",
    "嗯嗯",
    "哦",
    "噢",
    "喔",
    "ok",
    "okay",
    "okk",
    "收到",
    "明白",
    "明白了",
    "了解",
    "知道了",
    "可以",
    "行",
    "没问题",
    "不用了",
    "没了",
    "没有了",
    "继续",
    "再见",
    "拜拜",
    "辛苦了",
    "麻烦了",
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "bye",
    "goodbye",
    "sure",
    "fine",
    "great",
    "cool",
    "nice",
    "got it",
    "ok thanks",
    "okay thanks",
    "good morning",
    "good night",
)
ACKNOWLEDGEMENT_LEXICON: frozenset[str] = frozenset(
    compact_text(word) for word in _ACKNOWLEDGEMENT_WORDS
)

# Same delimiter family as extraction_hints._temporal_fact_clauses, but with a
# capturing group so the terminator survives and "ends with ？" can be read.
_CLAUSE_SPLIT_RE = re.compile(
    r"([。！？!?;；\n,，]+|(?<!不)但(?:是)?|不过|然而|\b(?:but|however|while)\b)",
    flags=re.IGNORECASE,
)
_QUESTION_TERMINATOR_RE = re.compile(r"[？?]")
_QUESTION_SUFFIX_RE = re.compile(
    r"(?:吗|呢|么|嘛|什么|多少|哪里|哪儿|几点|哪天|谁|如何|怎么样|怎样|为什么)$"
)
_SUBJECT_PREFIX_RE = re.compile(r"^(?:那么|那|所以|请问|你们|你|您|我们|我的|我|咱们|咱)+")
_QUESTION_STARTERS: tuple[str, ...] = tuple(
    sorted(
        {
            *_QUERY_QUESTION_PHRASES,
            "请问",
            "帮我看看",
            "能不能",
            "可以吗",
            "哪天",
            "几点",
            "多少",
            "谁",
            "是不是",
            "能否",
            "怎样",
            "咋",
            "啥",
            "何时",
            "什么时候",
        },
        key=len,
        reverse=True,
    )
)
_ENGLISH_QUESTION_RE = re.compile(
    r"^(?:what|why|how|when|where|which|who|whom|whose|can|could|would|will|"
    r"should|shall|do|does|did|is|are|was|were|am|have|has|may|might)\b"
    r"|^please\s+(?:explain|check|help|tell|show|remind)\b",
    flags=re.IGNORECASE,
)
_FENCED_CODE_RE = re.compile(r"```.*?```", flags=re.DOTALL)
_UNTERMINATED_FENCE_RE = re.compile(r"```.*\Z", flags=re.DOTALL)


def _split_clauses(text: str) -> list[tuple[str, str]]:
    """Return ``(clause, terminator)`` pairs; empty clauses are dropped."""
    parts = _CLAUSE_SPLIT_RE.split(text)
    clauses: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        terminator = parts[index + 1] if index + 1 < len(parts) else ""
        if clause:
            clauses.append((clause, terminator))
    return clauses


def _has_memory_directive(text: str) -> bool:
    """Any clause carrying an explicit 记住 / remember request forces extraction."""
    for clause, _terminator in _split_clauses(text):
        if _has_explicit_memory_directive(clause) or _is_standalone_memory_directive(clause):
            return True
    return False


def _is_interrogative_clause(clause: str, terminator: str) -> bool:
    if _QUESTION_TERMINATOR_RE.search(terminator):
        return True
    compact = compact_text(clause)
    if not compact:
        return False
    if _QUESTION_SUFFIX_RE.search(compact):
        return True
    # "请问…" is interrogative on its own; check before stripping subject
    # prefixes so "请问我上次去哪里旅游" is still recognised.
    if compact.startswith(_QUESTION_STARTERS):
        return True
    stripped = _SUBJECT_PREFIX_RE.sub("", compact)
    if stripped.startswith(_QUESTION_STARTERS):
        return True
    return bool(_ENGLISH_QUESTION_RE.match(clause.strip()))


def _strip_code_fences(text: str) -> tuple[str, bool]:
    prose, fenced = _FENCED_CODE_RE.subn("", text)
    prose, unterminated = _UNTERMINATED_FENCE_RE.subn("", prose)
    return prose, (fenced + unterminated) > 0


def prefilter_extraction_turn(
    *,
    user_text: str,
    last_assistant_text: str | None,
    has_context: bool,
) -> PrefilterDecision:
    """Decide whether the extraction model can be skipped for this turn."""
    text = user_text.strip()
    if not text:
        return _NO_SKIP
    if _has_memory_directive(text):
        return _NO_SKIP
    if last_assistant_text and _QUESTION_TERMINATOR_RE.search(last_assistant_text):
        # The user may be answering the assistant's question with a bare value.
        return _NO_SKIP

    compact = compact_text(text)
    is_acknowledgement = not compact or compact in ACKNOWLEDGEMENT_LEXICON
    if (
        has_context
        and len(compact) <= _SHORT_ANSWER_MAX_COMPACT_CHARS
        and not is_acknowledgement
    ):
        return _NO_SKIP
    if is_acknowledgement:
        return _skip("greeting")

    prose, had_code = _strip_code_fences(text)
    prose_compact = compact_text(prose)
    if had_code and not prose_compact:
        return _skip("code_only")

    clauses = _split_clauses(prose)
    if clauses and all(
        _is_interrogative_clause(clause, terminator) for clause, terminator in clauses
    ):
        return _skip("code_only" if had_code else "question_only")
    if had_code and prose_compact in ACKNOWLEDGEMENT_LEXICON:
        return _skip("code_only")
    return _NO_SKIP
