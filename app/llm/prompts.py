from app.memory.models import MemoryRecord


def render_memory_context(memories: list[MemoryRecord]) -> str:
    if not memories:
        return ""

    lines = [
        "以下是关于当前用户的长期记忆。请把它们当作上下文使用；如果和本轮对话冲突，以用户最新消息为准。",
    ]
    for index, memory in enumerate(memories, start=1):
        lines.append(f"{index}. {memory.content}")
    return "\n".join(lines)


MEMORY_EXTRACTION_SYSTEM_PROMPT = """你是 memory-gateway 的记忆提取器。分析给定的一轮对话，判断用户消息里是否包含值得长期保存的信息。

只输出一个 JSON 对象，不要包含任何其他文字、解释或 Markdown 代码块：
{
  "action": "create | update | ignore",
  "memory": "记忆内容，以「用户」开头的第三人称陈述",
  "type": "project | preference | fact | learning | style",
  "importance": 1 到 10 的整数,
  "confidence": 0.0 到 1.0 的小数,
  "reason": "为什么要保存或忽略",
  "source_quote": "从用户原话中逐字摘录的短引用"
}

action 含义：create 表示新信息；update 表示用户修正或补充了之前提过的信息；ignore 表示不保存。
create 和 update 都会先与已有记忆比对去重，所以拿不准时选 create 即可。

只保存同时满足这三点的信息：长期有用、用户明确表达过、未来回答时可能用到。
importance 评分参考：9-10 核心身份与长期项目；7-8 明确的偏好、事实、工作习惯；6 以下属于临时或低价值信息。

以下内容一律输出 action 为 "ignore"：
- 临时状态和情绪（例如「今天有点困」）
- 玩笑和闲聊
- 一次性任务的细节
- 你自己的推测、用户没有明确表达过的内容
- 假设场景：包含「如果」「假如」「假设」「比如我用」「suppose」「if I use」「imagine」「let's say」等表达
- 敏感信息（健康、财务、隐私），除非用户明确说「记住」

source_quote 必须是用户原话的逐字片段，禁止改写或自行编造。
没有值得保存的内容时：action 用 "ignore"，memory 留空，并在 reason 里说明原因。

示例 1：用户说「如果我以后用 Mac，应该怎么配置？」
这是假设场景，输出 {"action": "ignore", "memory": "", "type": "fact", "importance": 1, "confidence": 0.0, "reason": "假设场景，用户并未表示自己使用 Mac", "source_quote": ""}，绝不能保存成「用户使用 Mac」。

示例 2：用户说「我现在用 iPhone 和 Kelivo 做 AI 客户端。」
可以保存：{"action": "create", "memory": "用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。", "type": "fact", "importance": 7, "confidence": 0.9, "reason": "用户明确陈述了自己的设备与工具", "source_quote": "我现在用 iPhone 和 Kelivo 做 AI 客户端"}"""


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
