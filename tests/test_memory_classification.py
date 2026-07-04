from app.memory.classification import classify_memory
from app.memory.models import CandidateMemory


def test_classification_cleans_duplicate_empty_and_long_labels() -> None:
    candidate = CandidateMemory(
        action="create",
        memory="用户现在主要用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=7,
        confidence=0.9,
        source_quote="我现在主要用 Kelivo 做 AI 客户端",
        topics=[" 工具 ", "", "工具", "x" * 41],
        entities=[" Kelivo ", "Kelivo", "y" * 41],
    )

    result = classify_memory(candidate, source_text=candidate.source_quote)

    assert result.topics.count("工具") == 1
    assert "x" * 41 not in result.topics
    assert result.entities == ["Kelivo"]
    assert "工具与设备" in result.space_names
