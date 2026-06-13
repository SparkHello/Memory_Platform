import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from app.api.deps import get_embedding_client, get_memory_store
from app.config import get_settings
from app.memory.extractor import MIN_CONFIDENCE, MIN_IMPORTANCE, find_assumption_marker
from app.memory.models import CandidateMemory, MemoryRecord, MemoryType
from app.memory.resolver import MemoryResolver
from app.memory.search import EmbeddingClient, MemorySearchService
from app.memory.store import MemoryStore
from app.mcp_server.context import current_user_id

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """这是用户的长期记忆服务。

- 聊到与用户有关的事——喜好、习惯、家人朋友、宠物、健康、计划安排、工作项目，或此前聊过的话题——先调用 search_memory 检索再回答。
- 用户在日常闲聊中自然流露的长期信息（口味与雷点、生活事实、重要的人、目标与计划、职业与项目），即使没说「记住」也应调用 save_memory 保存；当下情绪、玩笑、一次性安排、假设场景不要保存。
- 用户要求忘记某件事时，先用 search_memory 或 list_memories 找到对应记忆的 id，再调用 delete_memory。
- 保存被服务端拒绝时会返回原因，不要换个说法反复重试。"""

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
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
    }


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _register_tools(mcp: FastMCP) -> None:
    mcp.tool()(search_memory)
    mcp.tool()(save_memory)
    mcp.tool()(list_memories)
    mcp.tool()(delete_memory)


async def search_memory(query: str, limit: int = 8) -> str:
    """检索与当前话题相关的长期记忆。

    聊到用户的喜好、习惯、家人朋友、健康、计划安排、工作项目，或过去聊过的
    话题时，先调用本工具再回答，让对话自然延续。
    query 用一句话描述要查的主题，例如「用户的饮食偏好」。
    返回 JSON 数组，按相关度排序；空数组表示没有相关记忆，此时正常回答即可。
    """
    store, embedding_client = _services()
    service = MemorySearchService(store=store, embedding_client=embedding_client)
    memories = await service.search(
        query=query,
        user_id=current_user_id.get(),
        limit=max(1, min(limit, 20)),
    )
    return _dump([_memory_to_dict(memory) for memory in memories])


async def save_memory(
    memory: str,
    type: MemoryType = "fact",
    importance: int = 5,
    confidence: float = 0.0,
    source_quote: str = "",
    reason: str = "",
) -> str:
    """保存一条关于用户的长期信息。用户在日常闲聊中自然流露的喜好、生活事实、
    重要的人、目标计划、职业项目都值得保存，不需要用户明确说「记住」。

    参数要求（不满足会被服务端拒绝，返回里会写明原因）：
    - memory：完整的陈述句，例如「用户家里养了一只猫」。
    - importance：1-10。对用户长期成立的偏好和事实给 6-8，重大信息（家人、
      健康、原则性偏好）给 8-10；当下情绪、玩笑、一次性安排（如「今晚吃火锅」）
      低于 6，不会被保存。
    - confidence：0-1。用户亲口说出的给 0.9；猜测和推断不会被保存。
    - source_quote：用户原话的逐字片段，不能改写、不能编造。
    - 假设场景（「如果我以后用 Mac…」）不要保存。

    服务端会自动与已有记忆比对：内容相同会忽略，同主题会更新旧记忆而不是新建。
    返回 JSON：{"action": "create|update|ignore", "reason": "...", "memory_id": "..."}
    """
    store, embedding_client = _services()
    try:
        candidate = CandidateMemory(
            action="create",
            memory=memory.strip(),
            type=type,
            importance=importance,
            confidence=confidence,
            reason=reason,
            source_quote=source_quote.strip(),
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        field = ".".join(str(part) for part in first_error.get("loc", ()))
        rejection = f"参数不合法（字段 {field}）"
        store.create_decision_log(
            conversation_id=None,
            candidate_json=_dump({"source": "mcp", "memory": memory, "error": rejection}),
            decision="ignore",
            reason=rejection,
        )
        return _dump({"action": "ignore", "reason": rejection})

    candidate_json = _dump({"source": "mcp", **candidate.model_dump()})
    rejection = _save_gate_reason(candidate)
    if rejection:
        store.create_decision_log(
            conversation_id=None,
            candidate_json=candidate_json,
            decision="ignore",
            reason=rejection,
        )
        return _dump({"action": "ignore", "reason": rejection})

    resolver = MemoryResolver(store=store, embedding_client=embedding_client)
    result = await resolver.resolve(
        user_id=current_user_id.get(),
        candidate=candidate,
        source_message=candidate.source_quote,
        conversation_id=None,
    )
    store.create_decision_log(
        conversation_id=None,
        candidate_json=candidate_json,
        decision=result.action,
        reason=result.reason,
    )
    return _dump(
        {
            "action": result.action,
            "reason": result.reason,
            "memory_id": result.memory.id if result.memory else None,
        }
    )


async def list_memories(limit: int = 50) -> str:
    """列出当前用户的全部长期记忆，按重要性和更新时间排序。

    用于用户问「你记住了我哪些事」，或需要整体回顾、清理记忆时。
    日常对话中按主题检索请用 search_memory，不要每轮都全量列出。
    """
    store, _ = _services()
    memories = store.list_memories(
        user_id=current_user_id.get(),
        limit=max(1, min(limit, 200)),
    )
    return _dump([_memory_to_dict(memory) for memory in memories])


async def delete_memory(memory_id: str) -> str:
    """删除一条记忆（软删除，可由管理员恢复）。

    只在用户明确要求忘记某件事时调用。先用 search_memory 或 list_memories
    找到对应记忆的 id，确认内容匹配后再删除。
    返回 JSON：{"deleted": true|false}，false 表示 id 不存在或已删除。
    """
    store, _ = _services()
    deleted = store.archive_memory(
        memory_id=memory_id,
        user_id=current_user_id.get(),
    )
    return _dump({"deleted": deleted})


def _save_gate_reason(candidate: CandidateMemory) -> str | None:
    """MCP 版保存门槛：与 extractor 的规则一致，但服务端看不到完整对话，
    source_quote 的逐字校验降级为非空 + 假设表达检测。"""
    if not candidate.memory:
        return "memory 内容为空"
    if candidate.importance < MIN_IMPORTANCE:
        return f"importance {candidate.importance} 低于保存阈值 {MIN_IMPORTANCE}"
    if candidate.confidence < MIN_CONFIDENCE:
        return f"confidence {candidate.confidence} 低于保存阈值 {MIN_CONFIDENCE}"
    if not candidate.source_quote:
        return "缺少 source_quote（必须提供用户原话的逐字片段）"
    marker = find_assumption_marker(candidate.source_quote)
    if marker:
        return f"假设场景（命中「{marker}」），不保存"
    return None
