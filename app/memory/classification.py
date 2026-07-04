import re

from pydantic import BaseModel, Field

from app.memory.models import CandidateMemory
from app.memory.store import normalize_classification_names


SPACE_WORK = "工作与项目"
SPACE_TOOLS = "工具与设备"
SPACE_PREFERENCE = "个人偏好"
SPACE_RELATIONSHIP = "人际关系"
SPACE_LIFE = "生活与地点"
SPACE_COMMUNICATION = "沟通方式"
SPACE_PRIVATE = "私密信息"

_MAX_TOPIC_COUNT = 6
_MAX_ENTITY_COUNT = 8
_MAX_SPACE_COUNT = 3


class ClassificationResult(BaseModel):
    topics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    space_names: list[str] = Field(default_factory=list)
    reason: str = ""


def classify_memory(
    candidate: CandidateMemory,
    source_text: str,
    existing_topics: list[str] | None = None,
) -> ClassificationResult:
    text = _combined_text(candidate, source_text)
    if candidate.sensitivity in {"private", "sensitive"}:
        return ClassificationResult(
            topics=[SPACE_PRIVATE],
            entities=[],
            space_names=[SPACE_PRIVATE],
            reason="private_or_sensitive_memory",
        )

    rule_topics = _rule_topics(candidate, text)
    topics = normalize_classification_values(
        [
            *candidate.topics,
            *(existing_topics or []),
            *rule_topics,
        ],
        max_items=_MAX_TOPIC_COUNT,
        field_name="topics",
    )
    if not topics:
        topics = ["信息"]

    entities = normalize_classification_values(
        [
            *candidate.entities,
            *_extract_entities(text),
        ],
        max_items=_MAX_ENTITY_COUNT,
        field_name="entities",
    )
    space_names = normalize_classification_values(
        _space_names(candidate, text, topics, entities),
        max_items=_MAX_SPACE_COUNT,
        field_name="space",
    )
    return ClassificationResult(
        topics=topics,
        entities=entities,
        space_names=space_names,
        reason="llm_labels_with_rule_fallback" if candidate.topics or candidate.entities else "rule_fallback",
    )


def normalize_classification_values(
    values: list[str] | tuple[str, ...],
    *,
    max_items: int,
    field_name: str,
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        raw = " ".join(str(value).strip().split())
        if not raw or len(raw) > 40:
            continue
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(raw)
        if len(cleaned) >= max_items:
            break
    return normalize_classification_names(
        cleaned,
        max_items=max_items,
        field_name=field_name,
    )


def _combined_text(candidate: CandidateMemory, source_text: str) -> str:
    parts = [
        source_text,
        candidate.memory,
        candidate.source_quote,
        candidate.reason,
        candidate.temporal_subject or "",
        candidate.temporal_predicate or "",
    ]
    return "\n".join(part for part in parts if part)


def _rule_topics(candidate: CandidateMemory, text: str) -> list[str]:
    topics: list[str] = []
    lowered = text.casefold()

    if candidate.type == "procedural":
        topics.append("流程方法")
    elif candidate.type == "emotional":
        topics.append("偏好")
    elif candidate.type == "reflective":
        topics.append("复盘")
    elif candidate.type == "episodic":
        topics.append("经历")

    if _contains_any(lowered, ["喜欢", "偏好", "讨厌", "不喜欢", "希望", "习惯", "口味", "雷点"]):
        topics.append("偏好")
    if _contains_any(lowered, ["咖啡", "茶", "早餐", "午餐", "晚餐", "饮食", "吃", "喝"]):
        topics.append("饮食")
    if _contains_any(lowered, ["沟通", "回复", "语气", "口吻", "称呼", "叫我", "简洁", "详细", "直接"]):
        topics.append("沟通偏好")
    if _contains_any(
        lowered,
        ["项目", "代码", "测试", "部署", "需求", "客户", "产品", "脚本", "pytest", "github", "pr", "api", "ci"],
    ):
        topics.append("项目")
    if _contains_any(
        lowered,
        [
            "工具",
            "设备",
            "手机",
            "电脑",
            "iphone",
            "mac",
            "windows",
            "wsl",
            "chatgpt",
            "claude",
            "codex",
            "openai",
            "kelivo",
            "模型",
            "ai",
        ],
    ):
        topics.append("工具")
    if _contains_any(lowered, ["朋友", "同事", "家人", "父母", "妈妈", "爸爸", "伴侣", "孩子", "老师", "同学"]):
        topics.append("人际关系")
    if _contains_any(lowered, ["住在", "居住", "城市", "地点", "生活", "通勤", "旅行", "上海", "北京", "深圳", "广州", "杭州"]):
        topics.append("地点")
    if candidate.temporal_subject and candidate.temporal_predicate:
        topics.append("时间事实")
    return topics


def _space_names(
    candidate: CandidateMemory,
    text: str,
    topics: list[str],
    entities: list[str],
) -> list[str]:
    lowered = "\n".join([text, " ".join(topics), " ".join(entities)]).casefold()
    spaces: list[str] = []

    if _contains_any(lowered, ["沟通", "回复", "语气", "口吻", "称呼", "叫我", "简洁", "详细", "直接"]):
        spaces.append(SPACE_COMMUNICATION)
    if _contains_any(lowered, ["朋友", "同事", "家人", "父母", "妈妈", "爸爸", "伴侣", "孩子", "老师", "同学"]):
        spaces.append(SPACE_RELATIONSHIP)
    if _contains_any(
        lowered,
        ["项目", "代码", "测试", "部署", "需求", "客户", "产品", "脚本", "pytest", "github", "pr", "api", "ci"],
    ):
        spaces.append(SPACE_WORK)
    if _contains_any(
        lowered,
        [
            "工具",
            "设备",
            "手机",
            "电脑",
            "iphone",
            "mac",
            "windows",
            "wsl",
            "chatgpt",
            "claude",
            "codex",
            "openai",
            "kelivo",
            "模型",
            "ai",
        ],
    ):
        spaces.append(SPACE_TOOLS)
    if candidate.type == "emotional" or _contains_any(lowered, ["喜欢", "偏好", "讨厌", "不喜欢", "希望", "习惯", "口味", "雷点"]):
        spaces.append(SPACE_PREFERENCE)
    if _contains_any(
        lowered,
        ["咖啡", "茶", "早餐", "午餐", "晚餐", "饮食", "吃", "喝", "住在", "居住", "城市", "地点", "生活", "通勤", "旅行"],
    ):
        spaces.append(SPACE_LIFE)

    if not spaces and topics:
        spaces.append(SPACE_PREFERENCE if candidate.type == "emotional" else SPACE_LIFE)
    return spaces


def _extract_entities(text: str) -> list[str]:
    entities: list[str] = []
    known_entities = {
        "memory-gateway": "memory-gateway",
        "memory gateway": "Memory Gateway",
        "kelivo": "Kelivo",
        "chatgpt": "ChatGPT",
        "claude": "Claude",
        "codex": "Codex",
        "openai": "OpenAI",
        "iphone": "iPhone",
        "ipad": "iPad",
        "mac": "Mac",
        "windows": "Windows",
        "wsl": "WSL",
        "wsl2": "WSL2",
        "sqlite": "SQLite",
        "fastapi": "FastAPI",
        "react": "React",
        "vite": "Vite",
        "python": "Python",
        "github": "GitHub",
        "obsidian": "Obsidian",
        "chatwise": "ChatWise",
        "glm": "GLM",
    }
    lowered = text.casefold()
    for key, display_name in known_entities.items():
        if key in lowered:
            entities.append(display_name)

    for city in ["上海", "北京", "深圳", "广州", "杭州", "成都", "重庆", "南京", "苏州", "东京", "纽约"]:
        if city in text:
            entities.append(city)

    for match in re.finditer(r"[“\"「]([^”\"」]{2,40})[”\"」]", text):
        entities.append(match.group(1))

    stop_words = {
        "AI",
        "API",
        "CI",
        "PR",
        "JSON",
        "REST",
        "MCP",
        "LLM",
        "HTTP",
        "SQL",
    }
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9.+#_-]{1,39}\b", text):
        token = match.group(0)
        if token in stop_words:
            continue
        entities.append(token)
    return entities


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle.casefold() in text for needle in needles)
