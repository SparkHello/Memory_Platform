"""Affect (valence/arousal) heuristics for digested memories.

`_digest_affect` derives the emotional charge of a digestion artifact
(reflection/feel text) from its source memories plus bilingual keyword
markers.  This is memory-domain logic rather than MCP transport, so it lives
here; the MCP server only imports it.
"""

from __future__ import annotations

from app.memory.models import MemoryRecord


def _digest_affect(
    *,
    text: str,
    source_memories: list[MemoryRecord],
    default_valence: float = 0.5,
    default_arousal: float = 0.3,
) -> tuple[float, float]:
    if source_memories:
        valence = sum(memory.valence for memory in source_memories) / len(source_memories)
        arousal = sum(memory.arousal for memory in source_memories) / len(source_memories)
    else:
        valence = default_valence
        arousal = default_arousal

    lowered = text.lower()
    positive_markers = (
        "安心",
        "稳定",
        "期待",
        "满意",
        "顺畅",
        "有信心",
        "踏实",
        "喜欢",
        "relief",
        "confident",
        "good",
    )
    negative_markers = (
        "焦虑",
        "压力",
        "担心",
        "讨厌",
        "难受",
        "挫败",
        "烦",
        "害怕",
        "anxious",
        "pressure",
        "frustrated",
        "worried",
    )
    high_arousal_markers = (
        "强烈",
        "压力",
        "焦虑",
        "兴奋",
        "紧张",
        "冲突",
        "痛点",
        "urgent",
        "intense",
    )
    calm_markers = ("稳定", "平静", "安心", "踏实", "settled", "calm")

    if any(marker in lowered for marker in positive_markers):
        valence += 0.15
    if any(marker in lowered for marker in negative_markers):
        valence -= 0.20
        arousal += 0.15
    if any(marker in lowered for marker in high_arousal_markers):
        arousal += 0.15
    if any(marker in lowered for marker in calm_markers):
        arousal -= 0.05

    return _clamp01(valence), _clamp01(arousal)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, round(float(value), 3)))
