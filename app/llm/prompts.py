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
  "type": "episodic | semantic | procedural | emotional | reflective",
  "importance": 1 到 10 的整数,
  "confidence": 0.0 到 1.0 的小数,
  "valence": 0.0 到 1.0 的小数，0 表示负向，1 表示正向，无法判断时用 0.5,
  "arousal": 0.0 到 1.0 的小数，0 表示平静，1 表示高唤起，无法判断时用 0.3,
  "stability": "temporary | medium | stable",
  "valid_from": "ISO 日期或时间；事实明确从某个时间开始生效时填写，否则为 null",
  "valid_until": "ISO 日期或时间；没有明确有效期时为 null",
  "review_after": "ISO 日期或时间；需要日后确认是否仍成立时填写，否则为 null",
  "sensitivity": "normal | private | sensitive",
  "temporal_subject": "可被新事实替换的稳定主语；没有明确可替换事实键时为 null",
  "temporal_predicate": "可被新事实替换的谓词/属性；没有明确可替换事实键时为 null",
  "topics": ["最多 6 个短标签；例如 偏好、项目、沟通偏好；没有时为空数组"],
  "entities": ["最多 8 个实体名；例如产品、工具、城市、人名；没有时为空数组"],
  "reason": "为什么要保存或忽略",
  "source_quote": "从用户原话中逐字摘录的短引用"
}

action 含义：create 表示新信息；update 表示用户修正或补充了之前提过的信息；ignore 表示不保存。
create 和 update 都会先与已有记忆比对去重，所以拿不准时选 create 即可。

只保存同时满足这三点的信息：长期有用、用户明确表达过、未来回答时可能用到。
importance 评分参考：9-10 核心身份与长期项目；7-8 明确的偏好、事实、工作习惯；6 以下属于临时或低价值信息。
valence/arousal 只描述这条记忆本身的情绪色彩，不要描述当前对话语气；中性事实用 valence=0.5、arousal=0.3，积极偏好可提高 valence，压力、冲突、痛点可降低 valence 并提高 arousal。
stability 选择参考：只在一段时间内成立的信息用 temporary；阶段性项目、计划、习惯用 medium；长期偏好、人物关系、沟通风格用 stable。
valid_until 只在用户明确给出截止日期、阶段或明显短期事实时填写；无法确定时用 null，不要猜日期。
valid_from 只在用户明确给出开始时间、任职/居住/使用状态开始生效时间，或当前事实必须带时间锚点时填写；无法确定时用 null。
temporal_subject/temporal_predicate 只用于白名单 profile 槽位；不要发明其他谓词。白名单：current_employer（当前雇主/任职公司）、current_city（当前居住城市）、primary_ai_client（主要 AI 客户端）、primary_device（主力设备）、preferred_name（用户希望被称呼的名字）。
只有用户明确表达“现在/目前/主要/默认/从某时开始”的当前状态，或“叫我/称呼我/我的名字是”这类称呼事实时，才填写 temporal_subject="用户" 和白名单 temporal_predicate；拿不准、只是普通补充、偏好、经历回顾或一次性事件时都填 null，避免误触发自动失效。
topics/entities 必须是短数组：topics 用宽泛短标签，entities 只放用户原话中明确出现的实体名。不要输出 space_ids 或 memory_spaces；后端会按保守大类绑定空间。private/sensitive 记忆只允许通用低泄露 topics，entities 必须为空数组。
temporal key 正例：用户说“我从 2026 年开始在 Acme 工作”，当前雇主可用 temporal_subject="用户"、temporal_predicate="current_employer"，并在日期明确时填写 valid_from；用户说“我现在主要用 Kelivo 当 AI 客户端”，当前主要 AI 客户端可用 temporal_subject="用户"、temporal_predicate="primary_ai_client"；用户说“以后叫我阿澈”，称呼可用 temporal_predicate="preferred_name"。
temporal key 反例：用户喜欢黑咖啡、去年去过京都、总结出长文档先做提纲、一次性安排、含糊推断出的事实，都不要填写 temporal_subject/temporal_predicate。
review_after 用于“可能会过期但不能确定”的记忆，例如最近在准备旅行、阶段性尝试某个习惯；没有明确复核价值时用 null，不要猜日期。
年龄、当前状态等会随时间变化的信息必须带时间锚点：如果用户只说“我现在/今年 X 岁”但没有生日或出生年份，不要推断出生年份，记为“截至当前年月，用户自称 X 岁”，stability 用 medium，confidence 不高于 0.85，并设置 180 天后复核。
sensitivity 选择参考：普通偏好和事实用 normal；家庭、财务、隐私细节用 private；健康、医疗、证件、账号、精确住址等高风险信息用 sensitive。
类型选择必须尽量分散，不要把明显非事实类内容都塞进 semantic：
- episodic：特定时间/地点发生过的事件或经历，例如“上周在咖啡店讨论了项目”。
- semantic：稳定背景、人物关系、长期事实和一般知识，例如“用户住在上海”。
- procedural：步骤、流程、工作方法、固定操作习惯，例如“部署前先跑测试再 build”。
- emotional：用户表达的偏好、雷点、情绪、强烈态度或价值取向，例如“用户讨厌冗长解释”“用户喜欢简洁代码”。
- reflective：用户对过去经验的总结、复盘或高层推论，例如“用户发现先收口 P0 再扩展更适合该项目”。
当内容同时是“事实 + 偏好/雷点”时优先 emotional；当内容是“事实 + 流程步骤”时优先 procedural；当内容是“事实 + 复盘结论”时优先 reflective。
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
这是假设场景，输出 {"action": "ignore", "memory": "", "type": "semantic", "importance": 1, "confidence": 0.0, "stability": "stable", "valid_until": null, "review_after": null, "sensitivity": "normal", "topics": [], "entities": [], "reason": "假设场景，用户并未表示自己使用 Mac", "source_quote": ""}，绝不能保存成「用户使用 Mac」。

示例 2：用户说「我现在主要用 Kelivo 做 AI 客户端。」
可以保存：{"action": "create", "memory": "用户现在主要用 Kelivo 作为 AI 客户端。", "type": "semantic", "importance": 7, "confidence": 0.9, "valence": 0.5, "arousal": 0.3, "stability": "medium", "valid_from": null, "valid_until": null, "review_after": null, "sensitivity": "normal", "temporal_subject": "用户", "temporal_predicate": "primary_ai_client", "topics": ["工具"], "entities": ["Kelivo"], "reason": "用户明确陈述了当前主要 AI 客户端", "source_quote": "我现在主要用 Kelivo 做 AI 客户端"}

示例 3：用户说「我很讨厌代码里到处都是花哨抽象，喜欢直接清楚的实现。」
可以保存：{"action": "create", "memory": "用户讨厌花哨抽象，偏好直接清楚的代码实现。", "type": "emotional", "importance": 7, "confidence": 0.9, "valence": 0.25, "arousal": 0.55, "stability": "stable", "valid_from": null, "valid_until": null, "review_after": null, "sensitivity": "normal", "temporal_subject": null, "temporal_predicate": null, "topics": ["偏好", "项目"], "entities": [], "reason": "用户明确表达了代码风格偏好和雷点", "source_quote": "我很讨厌代码里到处都是花哨抽象，喜欢直接清楚的实现"}"""


def render_memory_extraction_messages(
    *,
    user_message: str,
    assistant_message: str,
) -> list[dict[str, str]]:
    dialogue = (
        f"当前日期：{datetime.now(UTC).date().isoformat()}\n\n"
        f"用户消息：\n{user_message}\n\n助手回复：\n{assistant_message}"
    )
    return [
        {"role": "system", "content": MEMORY_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": dialogue},
    ]


MEMORY_BATCH_EXTRACTION_SYSTEM_PROMPT = """你是 memory-gateway 的记忆提取器。你的任务是把一段用户原文拆分为多条可独立保存的长期记忆候选。

只输出一个 JSON 对象，不要包含任何其他文字、解释或 Markdown 代码块：
{
  "memories": [
    {
      "action": "create | update | ignore",
      "memory": "记忆内容，以「用户」开头的第三人称陈述",
      "type": "episodic | semantic | procedural | emotional | reflective",
      "importance": 1 到 10 的整数,
      "confidence": 0.0 到 1.0 的小数,
      "valence": 0.0 到 1.0 的小数，0 表示负向，1 表示正向，无法判断时用 0.5,
      "arousal": 0.0 到 1.0 的小数，0 表示平静，1 表示高唤起，无法判断时用 0.3,
      "stability": "temporary | medium | stable",
      "valid_from": "ISO 日期或时间；事实明确从某个时间开始生效时填写，否则为 null",
      "valid_until": "ISO 日期或时间；没有明确有效期时为 null",
      "review_after": "ISO 日期或时间；需要日后确认是否仍成立时填写，否则为 null",
      "sensitivity": "normal | private | sensitive",
      "temporal_subject": "可被新事实替换的稳定主语；没有明确可替换事实键时为 null",
      "temporal_predicate": "可被新事实替换的谓词/属性；没有明确可替换事实键时为 null",
      "topics": ["最多 6 个短标签；例如 偏好、项目、沟通偏好；没有时为空数组"],
      "entities": ["最多 8 个实体名；例如产品、工具、城市、人名；没有时为空数组"],
      "reason": "为什么要保存或忽略",
      "source_quote": "从用户原文中逐字摘录的短引用"
    }
  ],
  "reason": "本次拆分的简短说明"
}

拆分规则：
- 一条候选只表达一个长期事实、偏好、关系、人物、目标、项目或沟通风格。
- 如果同一段话包含多个独立长期信息，拆成多条 memories。
- 不要把多个无关事实塞进同一条 memory。
- type 扇区选择必须尽量分散，不要把明显非事实类内容都塞进 semantic：事件经历用 episodic；稳定事实/人物/关系/背景用 semantic；流程步骤和操作方法用 procedural；情绪、偏好、雷点和强烈态度用 emotional；经验总结、复盘和高层推论用 reflective。事实+偏好优先 emotional，事实+流程优先 procedural，事实+复盘结论优先 reflective。
- 不要保存临时状态、玩笑、一次性安排、假设场景、模型推测或助手自己说的话。
- 对敏感信息保持保守：健康、医疗、财务、证件、账号、精确住址等只有在用户明确说「记住」时才可输出保存候选，并标为 private 或 sensitive。
- valid_from 只在用户明确给出开始时间、任职/居住/使用状态开始生效时间，或当前事实必须带时间锚点时填写；无法确定时用 null。
- temporal_subject/temporal_predicate 只用于白名单 profile 槽位，不要发明其他谓词。白名单：current_employer、current_city、primary_ai_client、primary_device、preferred_name。只有用户明确表达当前状态，或“叫我/称呼我/我的名字是”这类称呼事实时才填写；拿不准、只是普通补充、偏好、经历回顾或一次性事件时都填 null。
- topics/entities 必须是短数组：topics 用宽泛短标签，entities 只放用户原文中明确出现的实体名。不要输出 space_ids 或 memory_spaces；后端会按保守大类绑定空间。private/sensitive 记忆只允许通用低泄露 topics，entities 必须为空数组。
- temporal key 正例：用户说“我从 2026 年开始在 Acme 工作”，当前雇主可用 temporal_subject="用户"、temporal_predicate="current_employer"，并在日期明确时填写 valid_from；用户说“我现在主要用 Kelivo 当 AI 客户端”，当前主要 AI 客户端可用 temporal_subject="用户"、temporal_predicate="primary_ai_client"；用户说“以后叫我阿澈”，称呼可用 temporal_predicate="preferred_name"。
- temporal key 反例：用户喜欢黑咖啡、去年去过京都、总结出长文档先做提纲、一次性安排、含糊推断出的事实，都不要填写 temporal_subject/temporal_predicate。
- source_quote 必须是用户原文里的逐字片段，禁止改写或编造；每条候选都要有自己的 source_quote。
- 没有值得保存的内容时输出 {"memories": [], "reason": "没有长期有用信息"}。
- 年龄、当前状态等会随时间变化的信息必须带时间锚点：如果用户只说“我现在/今年 X 岁”但没有生日或出生年份，不要推断出生年份，记为“截至当前年月，用户自称 X 岁”，stability 用 medium，confidence 不高于 0.85，并设置 180 天后复核。

评分规则与单条提取完全一致：importance 低于 6 或 confidence 低于 0.8 的信息不会保存；用户亲口明确表达的长期信息 confidence 通常为 0.9。valence/arousal 只描述这条记忆本身的情绪色彩，中性事实默认 valence=0.5、arousal=0.3。"""


def render_memory_batch_extraction_messages(
    *,
    source_text: str,
    assistant_message: str | None = None,
) -> list[dict[str, str]]:
    assistant_block = f"\n\n助手回复（仅作上下文，不可作为记忆来源）：\n{assistant_message}" if assistant_message else ""
    user_content = (
        f"当前日期：{datetime.now(UTC).date().isoformat()}\n\n"
        f"用户原文：\n{source_text}{assistant_block}\n\n请拆分并输出 memories。"
    )
    return [
        {"role": "system", "content": MEMORY_BATCH_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


MEMORY_REVIEW_REVISION_SYSTEM_PROMPT = """你是 memory-gateway 的记忆体检编辑器。你的任务是根据用户对体检建议的修改说明，为已选记忆生成可预览的修改操作。

只输出一个 JSON 对象，不要包含任何其他文字、解释或 Markdown 代码块：
{
  "operations": [
    {
      "operation": "update | merge | archive | no_change",
      "reason": "为什么这样处理",
      "memory_ids": ["只能使用输入中出现的 memory id"],
      "target_memory_id": "update 或 merge 保留的目标记忆 id；不需要时为 null",
      "content": "update/merge 后的完整记忆正文；archive/no_change 时为 null",
      "type": "episodic | semantic | procedural | emotional | reflective 或 null",
      "importance": 1 到 10 的整数或 null,
      "confidence": 0.0 到 1.0 的小数或 null,
      "valence": 0.0 到 1.0 的小数或 null,
      "arousal": 0.0 到 1.0 的小数或 null,
      "stability": "temporary | medium | stable 或 null",
      "valid_until": "ISO 日期或时间；没有变化时为 null",
      "sensitivity": "normal | private | sensitive 或 null"
    }
  ],
  "reason": "本次预览的简短说明"
}

严格规则：
- 只能基于用户修改说明和输入中的已选记忆，不允许凭空新增无关记忆。
- 输入中的每条已选记忆都属于本次处理范围；每个已选 memory id 必须至少出现在一个 operation.memory_ids 中。
- operation=update 用于修正一条记忆，content 必须是修改后的完整记忆正文；只有 target_memory_id 会被更新，其他已选记忆如果不修改必须另用 no_change/archive/merge 明确处理。
- operation=merge 用于把多条重复、补充或冲突后确认的记忆合并为一条，content 必须是合并后的完整记忆正文。
- operation=archive 用于把被取代或确认错误的记忆移入回收站/软删除，可恢复；不是永久删除。
- operation=no_change 用于信息不足、用户说明不明确或不应修改时；如果某条已选关联记忆参与判断但不需要改动，也必须用 no_change 明确覆盖它的 memory id。
- 多条冲突/相似记忆可以输出“update 一条 + archive 另一条”，也可以输出 merge；不要自动删除仍可能有价值的细节。
- 不要设置 review_after；后端会按记忆类型和稳定性统一计算。
- 如果用户说明包含年龄但没有生日或出生年份，不要推断出生年份；记忆正文写成“截至当前年月，用户自称 X 岁”。"""


def render_memory_review_revision_messages(
    *,
    memories: list[MemoryRecord],
    user_note: str,
    recommendation_reason: str | None = None,
    relation: str | None = None,
    suggested_content: str | None = None,
) -> list[dict[str, str]]:
    memory_lines = [
        (
            f"- id={memory.id}; type={memory.type}; importance={memory.importance}; "
            f"confidence={memory.confidence}; valence={memory.valence}; arousal={memory.arousal}; "
            f"stability={memory.stability}; sensitivity={memory.sensitivity}; "
            f"valid_until={memory.valid_until}; review_after={memory.review_after}; "
            f"updated_at={memory.updated_at}; content={memory.content}"
        )
        for memory in memories
    ]
    user_content = (
        f"当前日期：{datetime.now(UTC).date().isoformat()}\n\n"
        f"体检原因：{recommendation_reason or '未提供'}\n"
        f"关系：{relation or 'none'}\n"
        f"体检建议内容：{suggested_content or '未提供'}\n\n"
        "已选记忆（全部属于本次修改范围，必须逐条覆盖）：\n"
        + ("\n".join(memory_lines) if memory_lines else "无")
        + f"\n\n用户修改说明：\n{user_note.strip()}\n\n请输出 operations。"
    )
    return [
        {"role": "system", "content": MEMORY_REVIEW_REVISION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
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
