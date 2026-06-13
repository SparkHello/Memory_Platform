from datetime import UTC, datetime

from app.memory.models import CoreMemorySection, MemoryRecord, RecentContextSummary
from app.memory.utils import _parse_iso_datetime


def render_memory_context(memories: list[MemoryRecord]) -> str:
    if not memories:
        return ""

    lines = [
        "以下是关于当前用户的长期记忆。请把它们当作上下文使用；如果和本轮对话冲突，以用户最新消息为准。",
    ]
    for index, memory in enumerate(memories, start=1):
        labels = []
        if memory.stability != "stable":
            labels.append(f"稳定性：{memory.stability}")
        if memory.valid_until:
            labels.append(f"有效期至：{memory.valid_until}")
        if memory.review_after:
            review_label = f"复核时间：{memory.review_after}"
            review_after = _parse_iso_datetime(memory.review_after)
            if review_after is not None and review_after <= datetime.now(UTC):
                review_label += "（待复核）"
            labels.append(review_label)
        if memory.sensitivity != "normal":
            labels.append(f"敏感级别：{memory.sensitivity}，仅在用户问题明确相关时使用")
        suffix = f"（{'；'.join(labels)}）" if labels else ""
        lines.append(f"{index}. {memory.content}{suffix}")
    return "\n".join(lines)


CORE_SECTION_TITLES = {
    "profile": "稳定背景",
    "preferences": "长期偏好与雷点",
    "relationships": "重要人物与关系",
    "routines": "生活习惯",
    "goals": "长期目标",
    "communication": "沟通偏好",
}


def render_core_memory_context(sections: list[CoreMemorySection]) -> str:
    if not sections:
        return ""

    lines = [
        "以下是关于当前用户的核心记忆。它们是从长期记忆中整理出的稳定背景，优先级高于普通召回记忆；如果和用户最新消息冲突，以用户最新消息为准。",
    ]
    for section in sections:
        title = CORE_SECTION_TITLES.get(section.section, section.section)
        lines.append(f"\n【{title}】")
        lines.append(section.content)
    return "\n".join(lines)


def render_recent_context_summary_context(summary: RecentContextSummary | None) -> str:
    if summary is None or not summary.summary.strip():
        return ""
    return "\n".join(
        [
            "以下是近期会话摘要，仅用于延续最近话题；它不是长期记忆，也不代表稳定事实。",
            summary.summary.strip(),
        ]
    )


MEMORY_EXTRACTION_SYSTEM_PROMPT = """你是 memory-gateway 的记忆提取器。分析给定的一轮对话，判断用户消息里是否包含值得长期保存的信息。

只输出一个 JSON 对象，不要包含任何其他文字、解释或 Markdown 代码块：
{
  "action": "create | update | ignore",
  "memory": "记忆内容，以「用户」开头的第三人称陈述",
  "type": "project | preference | fact | learning | style | person | relationship",
  "importance": 1 到 10 的整数,
  "confidence": 0.0 到 1.0 的小数,
  "stability": "temporary | medium | stable",
  "valid_until": "ISO 日期或时间；没有明确有效期时为 null",
  "review_after": "ISO 日期或时间；需要日后确认是否仍成立时填写，否则为 null",
  "sensitivity": "normal | private | sensitive",
  "reason": "为什么要保存或忽略",
  "source_quote": "从用户原话中逐字摘录的短引用"
}

action 含义：create 表示新信息；update 表示用户修正或补充了之前提过的信息；ignore 表示不保存。
create 和 update 都会先与已有记忆比对去重，所以拿不准时选 create 即可。

只保存同时满足这三点的信息：长期有用、用户明确表达过、未来回答时可能用到。
importance 评分参考：9-10 核心身份与长期项目；7-8 明确的偏好、事实、工作习惯；6 以下属于临时或低价值信息。
stability 选择参考：只在一段时间内成立的信息用 temporary；阶段性项目、计划、习惯用 medium；长期偏好、人物关系、沟通风格用 stable。
valid_until 只在用户明确给出截止日期、阶段或明显短期事实时填写；无法确定时用 null，不要猜日期。
review_after 用于“可能会过期但不能确定”的记忆，例如最近在准备旅行、阶段性尝试某个习惯；没有明确复核价值时用 null，不要猜日期。
sensitivity 选择参考：普通偏好和事实用 normal；家庭、财务、隐私细节用 private；健康、医疗、证件、账号、精确住址等高风险信息用 sensitive。
类型选择参考：重要的人、家人朋友、宠物等用 person；用户和某人的关系、称呼、重要日期用 relationship；用户长期喜欢/讨厌的事物用 preference；正在做的项目用 project；学习目标用 learning；回复风格偏好用 style；其他长期事实用 fact。
用户明确说「记住」「别忘了」「以后记得」时，如果内容符合长期有用且非假设，应优先保存。

以下内容一律输出 action 为 "ignore"：
- 临时状态和情绪（例如「今天有点困」）
- 玩笑和闲聊
- 一次性任务的细节
- 你自己的推测、用户没有明确表达过的内容
- 假设场景：包含「如果」「假如」「假设」「比如我用」「suppose」「if I use」「imagine」「let's say」等表达
- 敏感信息（健康、财务、隐私），除非用户明确说「记住」；即使保存也必须标为 private 或 sensitive

source_quote 必须是用户原话的逐字片段，禁止改写或自行编造。
没有值得保存的内容时：action 用 "ignore"，memory 留空，并在 reason 里说明原因。

示例 1：用户说「如果我以后用 Mac，应该怎么配置？」
这是假设场景，输出 {"action": "ignore", "memory": "", "type": "fact", "importance": 1, "confidence": 0.0, "stability": "stable", "valid_until": null, "review_after": null, "sensitivity": "normal", "reason": "假设场景，用户并未表示自己使用 Mac", "source_quote": ""}，绝不能保存成「用户使用 Mac」。

示例 2：用户说「我现在用 iPhone 和 Kelivo 做 AI 客户端。」
可以保存：{"action": "create", "memory": "用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。", "type": "fact", "importance": 7, "confidence": 0.9, "stability": "medium", "valid_until": null, "review_after": null, "sensitivity": "normal", "reason": "用户明确陈述了自己的设备与工具", "source_quote": "我现在用 iPhone 和 Kelivo 做 AI 客户端"}"""


def render_memory_extraction_messages(
    *,
    user_message: str,
    assistant_message: str,
) -> list[dict[str, str]]:
    dialogue = f"用户消息：\n{user_message}\n\n助手回复：\n{assistant_message}"
    return [
        {"role": "system", "content": MEMORY_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": dialogue},
    ]


CORE_MEMORY_CONSOLIDATION_SYSTEM_PROMPT = """你是 memory-gateway 的核心记忆整理器。你的任务是从已经保存的长期记忆中，整理出适合日常聊天长期使用的核心用户画像。

只输出一个 JSON 对象，不要包含任何其他文字、解释或 Markdown 代码块：
{
  "sections": [
    {
      "section": "profile | preferences | relationships | routines | goals | communication",
      "content": "简洁中文内容，可以是 1-5 条短句或项目符号",
      "evidence_memory_ids": ["只能使用输入中出现过的 memory id"],
      "confidence": 0.0 到 1.0 的小数
    }
  ],
  "reason": "本次整理的简短原因"
}

section 含义：
- profile：稳定生活背景，不要默认推断职业或专业身份。
- preferences：长期喜好、讨厌、饮食/娱乐/设备/生活偏好。
- relationships：家人、朋友、宠物、重要人物和称呼。
- routines：作息、习惯、常见生活安排。
- goals：长期目标、正在坚持的事、未来计划。
- communication：用户希望助手如何说话和回应。

严格规则：
- 只能基于输入的已保存记忆整理，不能凭空补充。
- 每个 section 必须带 evidence_memory_ids，且 id 必须来自输入。
- 不要保存一次性安排、当下情绪、玩笑、假设场景。
- 不要把 sensitivity 为 private 或 sensitive 的记忆写入核心记忆。
- 不要把已过 valid_until 的 temporary / medium 记忆写入核心记忆。
- 不要把普通工具使用、临时话题或一次性任务概括成核心身份。
- 不要偏向开发、职业、项目管理语境；除非输入记忆明确说明这是用户长期生活背景的一部分。
- 如果证据不足，不要输出对应 section。
- 如果当前核心记忆已经准确，不要为了改写而改写。
- 每个 section 保持短而稳定，避免堆砌细节；细节应留在普通 RAG 记忆里。"""


def render_core_memory_consolidation_messages(
    *,
    memories: list[MemoryRecord],
    current_sections: list[CoreMemorySection],
) -> list[dict[str, str]]:
    memory_lines = [
        f"- id={memory.id}; type={memory.type}; importance={memory.importance}; "
        f"stability={memory.stability}; valid_until={memory.valid_until}; "
        f"sensitivity={memory.sensitivity}; usage_count={memory.usage_count}; "
        f"updated_at={memory.updated_at}; content={memory.content}"
        for memory in memories
    ]
    current_lines = [
        f"- section={section.section}; confidence={section.confidence}; "
        f"evidence_memory_ids={section.evidence_memory_ids}; content={section.content}"
        for section in current_sections
    ]
    user_content = (
        "已保存长期记忆：\n"
        + ("\n".join(memory_lines) if memory_lines else "无")
        + "\n\n当前核心记忆：\n"
        + ("\n".join(current_lines) if current_lines else "无")
        + "\n\n请基于这些资料输出新的核心记忆 sections。"
    )
    return [
        {"role": "system", "content": CORE_MEMORY_CONSOLIDATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
