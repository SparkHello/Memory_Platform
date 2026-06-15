import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.api.deps import get_embedding_client, get_llm_client, get_memory_store
from app.config import get_settings
from app.memory.ingest import MemoryIngestService
from app.memory.models import (
    CoreMemorySection,
    MemoryRecord,
    RecentContextSummary,
)
from app.memory.search import EmbeddingClient, MemorySearchService
from app.memory.store import MemoryStore
from app.mcp_server.context import current_user_id

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """这是用户的长期记忆服务。你只有 4 个工具：

- **search_memory**：回答问题前，如果话题涉及用户的喜好、习惯、家人、健康、计划或过去聊过的事，先搜索再回答。
- **submit_memory_text**：如果用户本轮提供了值得长期记住的信息，把用户原文放入 text 提交。不要自己整理或改写，服务端会自动提取、去重和保存。
- **get_core_memory**：需要了解用户稳定背景时调用。
- **get_recent_context_summary**：需要恢复上一轮上下文时调用。

不要保存假设、玩笑、一次性安排。同一轮可以先 search 再 submit，两者不冲突。保存被拒绝时不要重试。
用户要求忘记某条信息时，请引导用户在 Web 管理台（/ui/）操作，你没有删除或遗忘的工具。"""

def create_mcp_server() -> FastMCP:
    """构建 MCP 服务器。

    SDK 的 StreamableHTTPSessionManager 每个实例只允许 run() 一次，
    所以这里必须是工厂：应用每次构建（含测试反复启动）都拿全新实例。
    """
    mcp = FastMCP(
        name="memory-gateway",
        instructions=SERVER_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        # SDK 默认的 DNS rebinding 防护只放行 localhost 的 Host 头；
        # 本服务要被 iPhone 从局域网/公网访问，鉴权已由 MCPAuthMiddleware 承担，关闭该校验
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    _register_tools(mcp)
    return mcp


def _services() -> tuple[MemoryStore, EmbeddingClient]:
    settings = get_settings()
    return get_memory_store(settings), get_embedding_client(settings)


def _memory_to_dict(memory: MemoryRecord) -> dict:
    return {
        "id": memory.id,
        "content": memory.content,
        "type": memory.type,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "usage_count": memory.usage_count,
        "last_used_at": memory.last_used_at,
        "stability": memory.stability,
        "valid_until": memory.valid_until,
        "review_after": memory.review_after,
        "sensitivity": memory.sensitivity,
        "evidence_memory_ids": memory.evidence_memory_ids,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "archived_at": memory.archived_at,
    }


def _core_memory_to_dict(section: CoreMemorySection) -> dict:
    return {
        "id": section.id,
        "section": section.section,
        "content": section.content,
        "evidence_memory_ids": section.evidence_memory_ids,
        "confidence": section.confidence,
        "version": section.version,
        "created_at": section.created_at,
        "updated_at": section.updated_at,
    }


def _recent_summary_to_dict(summary: RecentContextSummary) -> dict:
    return {
        "id": summary.id,
        "conversation_id": summary.conversation_id,
        "summary": summary.summary,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
    }


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _register_tools(mcp: FastMCP) -> None:
    mcp.tool()(search_memory)
    mcp.tool()(submit_memory_text)
    mcp.tool()(get_recent_context_summary)
    mcp.tool()(get_core_memory)


async def search_memory(query: str, limit: int = 8) -> str:
    """检索与当前话题相关的长期记忆。

    聊到用户的喜好、习惯、家人朋友、健康、计划安排、长期事项，或过去聊过的
    话题时，先调用本工具再回答，让对话自然延续。
    调用本工具后，仍要检查本轮用户消息是否包含新的长期信息；如果有，继续调用
    submit_memory_text。检索旧记忆和保存新信息可以在同一轮连续发生，不要二选一。
    query 用一句话描述要查的主题，例如「用户的饮食偏好」。
    返回 JSON 数组，按相关度排序；空数组表示没有相关记忆，此时正常回答即可。
    被返回的记忆会自动增加 usage_count 并刷新 last_used_at，用来判断哪些记忆真正常用。
    """
    store, embedding_client = _services()
    service = MemorySearchService(store=store, embedding_client=embedding_client)
    memories = await service.search(
        query=query,
        user_id=current_user_id.get(),
        limit=max(1, min(limit, 20)),
    )
    return _dump([_memory_to_dict(memory) for memory in memories])


async def submit_memory_text(text: str, conversation_id: str | None = None) -> str:
    """提交一段可能包含多条长期记忆的用户原文，由服务端自动整理保存。

    这是 iOS 客户端的优先保存入口：客户端模型只需要判断本轮用户是否提供了
    可能长期有用的信息，然后把用户原文放进 text。服务端会调用整理模型拆分为
    多条候选记忆，并逐条执行 source_quote 校验、敏感信息门槛、假设场景拦截、
    去重、更新或创建。不要在客户端手动把一大段内容拆成多次 save_memory。

    text 应尽量使用用户原话，而不是模型改写后的总结；这样服务端才能验证
    source_quote 是否真实来自用户。conversation_id 可选，用于决策日志追踪。
    返回 JSON：{"created": 0, "updated": 0, "ignored": 1, "items": [...]}。
    """
    settings = get_settings()
    store = get_memory_store(settings)
    embedding_client = get_embedding_client(settings)
    llm_client = get_llm_client(settings)
    ingester = MemoryIngestService(
        store=store,
        embedding_client=embedding_client,
        llm_client=llm_client,
    )
    result = await ingester.ingest(
        user_id=current_user_id.get(),
        text=text,
        conversation_id=conversation_id,
        source="mcp_ingest",
    )
    return _dump(result.model_dump())


async def get_recent_context_summary(conversation_id: str | None = None) -> str:
    """读取近期会话摘要。

    用于用户问「最近我们在聊什么」或需要恢复最近上下文时。摘要是短期上下文，
    不属于长期记忆，也不会进入核心记忆。
    """
    store, _ = _services()
    summary = store.get_recent_context_summary(
        user_id=current_user_id.get(),
        conversation_id=conversation_id,
    )
    if summary is None:
        return _dump({"found": False, "summary": ""})
    return _dump({"found": True, **_recent_summary_to_dict(summary)})


async def get_core_memory() -> str:
    """查看当前用户的核心记忆。

    核心记忆是服务端从长期记忆中整理出的稳定日常背景，数量少、优先级高。
    当用户询问「你对我的核心了解」「核心记忆是什么」时调用。日常回答仍应按话题
    使用 search_memory 检索细节；核心记忆不是细节检索工具。
    """
    store, _ = _services()
    sections = store.list_core_memory_sections(user_id=current_user_id.get())
    return _dump([_core_memory_to_dict(section) for section in sections])
