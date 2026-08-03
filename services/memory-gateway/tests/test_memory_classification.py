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


def test_short_latin_rules_do_not_match_inside_unrelated_words() -> None:
    candidate = CandidateMemory(
        action="create",
        memory="User prefers tea and likes apricots.",
        type="semantic",
        importance=7,
        confidence=0.9,
        source_quote="I prefer tea and like apricots",
        reason="The classifier mentioned a project and AI in its explanation.",
    )

    result = classify_memory(candidate, source_text=candidate.source_quote)

    assert "偏好" in result.topics
    assert "个人偏好" in result.space_names
    assert "项目" not in result.topics
    assert "工作与项目" not in result.space_names
    assert "工具" not in result.topics
    assert "工具与设备" not in result.space_names


def test_short_latin_rules_still_match_real_tokens() -> None:
    candidate = CandidateMemory(
        action="create",
        memory="User reviews the CI API and uses a Mac.",
        type="semantic",
        importance=7,
        confidence=0.9,
        source_quote="I review the CI API and use a Mac",
    )

    result = classify_memory(candidate, source_text=candidate.source_quote)

    assert "项目" in result.topics
    assert "工作与项目" in result.space_names
    assert "工具" in result.topics
    assert "工具与设备" in result.space_names
    assert "Mac" in result.entities
