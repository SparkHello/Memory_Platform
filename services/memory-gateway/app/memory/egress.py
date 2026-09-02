"""Sentence-level egress partitioning for memory extraction.

Before this module existed, one sensitive keyword anywhere in a user message
dropped the whole turn from extraction when ``ALLOW_SENSITIVE_EGRESS`` was off.
Now the message is split into sentences; only sentences whose local
sensitivity tier exceeds the configured egress ceiling are withheld from the
remote extraction model. The kept sentences are sent verbatim and also serve
as the grounding superset for ``source_quote`` checks, so a quote can never
span into a withheld sentence.

Withheld sentences that carry an explicit, clause-adjacent 记住 / remember
directive are saved locally without any model call (see
``local_directive_candidate``); everything else is recorded in the decision
log as hashes only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Literal

from app.memory.extractor import (
    SENSITIVE_MIN_CONFIDENCE,
    SENSITIVE_MIN_IMPORTANCE,
    _has_explicit_memory_directive,
    _is_standalone_memory_directive,
    _strip_memory_directive_prefix,
)
from app.memory.models import CandidateMemory
from app.sensitivity import SENSITIVITY_RANK, detected_sensitive_categories

EgressCeiling = Literal["normal", "private"]

# 。！？；\n always end a sentence; ASCII !?;. only when followed by whitespace
# or the end of text so "3.5", "e.g" and URLs stay intact.
_SENTENCE_END_RE = re.compile(r"[。！？；\n]+|[!?;]+(?=\s|$)|\.(?=\s|$)")
_CLAUSE_SEPARATOR_RE = re.compile(r"[,，、:：]")
_CLAUSE_SPLIT_KEEP_RE = re.compile(r"([,，、:：]\s*)")
_TRAILING_TERMINATOR_RE = re.compile(r"[。！？；!?;.]+\s*$")

REASON_WITHHELD_NO_DIRECTIVE = "敏感句子未出站且未明确要求记住"
REASON_WITHHELD_LOCAL_SAVE_PREFIX = "敏感句子未出站，用户明确要求记住，已本地保存"
REASON_WITHHELD_ASSUMPTION_PREFIX = "敏感句子未出站；假设场景"


@dataclass(frozen=True)
class SentenceSpan:
    """One sentence of the user text with its local sensitivity classification."""

    text: str
    level: str
    sensitive_categories: frozenset[str]
    private_categories: frozenset[str]

    @property
    def categories(self) -> list[str]:
        return sorted(self.sensitive_categories | self.private_categories)

    @property
    def stripped(self) -> str:
        return self.text.strip()


@dataclass
class EgressPartition:
    kept: list[SentenceSpan]
    withheld: list[SentenceSpan]

    @property
    def egress_text(self) -> str:
        """Text that may leave the host: kept sentences joined verbatim."""
        return "".join(span.text for span in self.kept).strip()


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences with their terminators attached.

    Invariant: ``"".join(split_sentences(text)) == text``. Whitespace-only
    fragments are merged into the preceding sentence so nothing is lost.
    """
    spans: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        spans.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        spans.append(text[start:])
    merged: list[str] = []
    for span in spans:
        if merged and not span.strip():
            merged[-1] += span
        else:
            merged.append(span)
    return merged


def classify_sentence(sentence: str) -> SentenceSpan:
    sensitive, private = detected_sensitive_categories(sentence)
    level = "sensitive" if sensitive else "private" if private else "normal"
    return SentenceSpan(
        text=sentence,
        level=level,
        sensitive_categories=frozenset(sensitive),
        private_categories=frozenset(private),
    )


def partition_for_egress(text: str, *, ceiling: str) -> EgressPartition:
    """Split ``text`` into sentences that may leave and sentences that may not."""
    limit = SENSITIVITY_RANK.get(ceiling, 0)
    kept: list[SentenceSpan] = []
    withheld: list[SentenceSpan] = []
    for sentence in split_sentences(text):
        if not sentence.strip():
            continue
        span = classify_sentence(sentence)
        if SENSITIVITY_RANK[span.level] <= limit:
            kept.append(span)
        else:
            withheld.append(span)
    return EgressPartition(kept=kept, withheld=withheld)


def _sentence_level(text: str) -> str:
    sensitive, private = detected_sensitive_categories(text)
    return "sensitive" if sensitive else "private" if private else "normal"


def withheld_sentence_has_scoped_directive(sentence: str) -> bool:
    """Mirror the extractor's clause-scoped authorization for a whole sentence.

    A 记住 directive authorizes only the clause it starts, or a bare "记住"
    clause immediately before/after the sensitive clause. "记住我喜欢咖啡，我的
    身份证号是 X" therefore does *not* authorize the ID clause.
    """
    body = _TRAILING_TERMINATOR_RE.sub("", sentence.strip())
    clauses = [clause.strip() for clause in _CLAUSE_SEPARATOR_RE.split(body) if clause.strip()]
    if not clauses:
        return False
    if len(clauses) == 1:
        return _has_explicit_memory_directive(clauses[0])
    evidence_indices = [
        index for index, clause in enumerate(clauses) if _sentence_level(clause) != "normal"
    ]
    if not evidence_indices:
        # The pattern spans a clause boundary: only a sentence-level directive
        # can cover it, and that is exactly the single-clause rule.
        return _has_explicit_memory_directive(body)
    for index in evidence_indices:
        clause = clauses[index]
        previous_clause = clauses[index - 1] if index > 0 else ""
        next_clause = clauses[index + 1] if index + 1 < len(clauses) else ""
        if _has_explicit_memory_directive(clause):
            continue
        if _is_standalone_memory_directive(previous_clause):
            continue
        if _is_standalone_memory_directive(next_clause):
            continue
        return False
    return True


def local_directive_content(sentence: str) -> str:
    """Return the sentence without its 记住 / remember directive wording."""
    stripped = sentence.strip()
    terminator_match = _TRAILING_TERMINATOR_RE.search(stripped)
    terminator = terminator_match.group(0).strip() if terminator_match else ""
    body = stripped[: terminator_match.start()] if terminator_match else stripped

    pieces = _CLAUSE_SPLIT_KEEP_RE.split(body)
    clauses = pieces[0::2]
    separators = [*pieces[1::2], ""]
    rebuilt: list[tuple[str, str]] = []
    for clause, separator in zip(clauses, separators, strict=False):
        if _is_standalone_memory_directive(clause):
            continue
        cleaned = _strip_memory_directive_prefix(clause).strip()
        if cleaned:
            rebuilt.append((cleaned, separator))
    if not rebuilt:
        return stripped
    content = "".join(
        clause + (separator if index < len(rebuilt) - 1 else "")
        for index, (clause, separator) in enumerate(rebuilt)
    )
    return f"{content}{terminator}"


def local_directive_candidate(span: SentenceSpan) -> CandidateMemory:
    """Build a verbatim, first-person candidate for a withheld directive sentence."""
    return CandidateMemory(
        action="create",
        memory=local_directive_content(span.text),
        type="semantic",
        importance=SENSITIVE_MIN_IMPORTANCE,
        confidence=SENSITIVE_MIN_CONFIDENCE,
        stability="stable",
        sensitivity=span.level,  # type: ignore[arg-type]
        topics=[],
        entities=[],
        reason="敏感句子未出站；用户明确要求记住，本地直接保存",
        source_quote=span.stripped,
    )


def sentence_audit_fields(span: SentenceSpan) -> dict[str, object]:
    """Audit shape for a withheld sentence: hash, length, tier, categories. Never text."""
    text = span.stripped
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
        "level": span.level,
        "categories": span.categories,
    }
