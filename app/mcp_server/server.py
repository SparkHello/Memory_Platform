import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from app.api.deps import get_embedding_client, get_llm_client, get_memory_store
from app.config import get_settings
from app.memory.core import CoreMemoryConsolidator
from app.memory.extractor import validate_candidate_for_save
from app.memory.models import (
    CandidateMemory,
    CoreMemorySection,
    CoreMemorySectionHistory,
    CoreMemorySectionName,
    MemoryRecord,
    MemorySensitivity,
    MemoryStability,
    MemoryType,
    RecentContextSummary,
)
from app.memory.resolver import MemoryResolver
from app.memory.review import MemoryReviewer
from app.memory.report import build_memory_export, build_memory_report, format_memory_export
from app.memory.search import EmbeddingClient, MemorySearchService
from app.memory.store import MemoryStore
from app.mcp_server.context import current_user_id

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """这是用户的长期记忆服务。

- 每轮都独立判断两件事：是否需要 search_memory 检索旧记忆、用户本轮是否提供了应保存的新长期信息。这两件事不是二选一。
- 核心记忆是从长期记忆整理出的稳定生活背景，用于日常聊天，不要默认按开发、职业或项目管理场景理解用户。
- 当用户询问「核心记忆」「你对我的稳定了解」时，调用 get_core_memory；当用户明确要求整理核心记忆时，再调用 consolidate_core_memory。
- 聊到与用户有关的事——喜好、习惯、家人朋友、宠物、健康、计划安排、长期事项，或此前聊过的话题——先调用 search_memory 检索再回答。
- 用户在日常闲聊中自然流露的长期信息（口味与雷点、生活事实、重要的人、人物关系、目标与计划、生活背景与长期事项），即使没说「记住」也应调用 save_memory 保存。
- 如果同一轮既需要检索旧记忆，又包含新的长期信息，先调用 search_memory 获取上下文，再调用 save_memory 保存新信息；不要因为已经检索就跳过保存。
- 用户明确说「记住」时，优先调用 save_memory；人物和关系用 type=person 或 type=relationship。
- 当下情绪、玩笑、一次性安排、假设场景不要保存。保存成功后不必每次向用户汇报，除非用户明确要求。
- 用户要求回顾记忆时调用 list_memories；要求忘记某类记忆时优先调用 forget_memories，要求忘记某个明确 id 时调用 delete_memory。
- 用户要求检查、清理、复核记忆库时调用 review_memories；它只返回建议，不会自动删除。
- 用户问「你为什么记得这个」时调用 why_remember；需要把同主题碎片整理成一条时调用 merge_memories。
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


def _core_memory_history_to_dict(item: CoreMemorySectionHistory) -> dict:
    return {
        "id": item.id,
        "core_memory_section_id": item.core_memory_section_id,
        "section": item.section,
        "content": item.content,
        "evidence_memory_ids": item.evidence_memory_ids,
        "confidence": item.confidence,
        "version": item.version,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "replaced_at": item.replaced_at,
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
    mcp.tool()(save_memory)
    mcp.tool()(why_remember)
    mcp.tool()(merge_memories)
    mcp.tool()(get_recent_context_summary)
    mcp.tool()(get_core_memory)
    mcp.tool()(get_core_memory_history)
    mcp.tool()(consolidate_core_memory)
    mcp.tool()(review_memories)
    mcp.tool()(memory_report)
    mcp.tool()(export_memories)
    mcp.tool()(list_memories)
    mcp.tool()(list_deleted_memories)
    mcp.tool()(delete_memory)
    mcp.tool()(restore_memory)
    mcp.tool()(forget_memories)


async def search_memory(query: str, limit: int = 8) -> str:
    """检索与当前话题相关的长期记忆。

    聊到用户的喜好、习惯、家人朋友、健康、计划安排、长期事项，或过去聊过的
    话题时，先调用本工具再回答，让对话自然延续。
    调用本工具后，仍要检查本轮用户消息是否包含新的长期信息；如果有，继续调用
    save_memory。检索旧记忆和保存新信息可以在同一轮连续发生，不要二选一。
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


async def why_remember(memory_id: str) -> str:
    """解释服务为什么记得某条记忆。

    当用户问「你为什么记得这个」「这条记忆从哪里来」时调用。返回来源片段、
    保存时间、置信度、是否被核心记忆引用，以及合并来源 evidence ids。
    """
    store, _ = _services()
    explanation = store.explain_memory_source(
        memory_id=memory_id,
        user_id=current_user_id.get(),
    )
    if explanation is None:
        return _dump({"found": False, "reason": "记忆不存在或已删除", "memory_id": memory_id})
    return _dump({"found": True, **explanation.model_dump()})


async def merge_memories(memory_ids: list[str], content: str | None = None) -> str:
    """合并多条同主题碎片记忆。

    只在用户要求整理记忆、或 review_memories 明确建议合并时调用。第一条 id 会作为
    保留目标，其余记忆会软删除；合并后的记忆会保留所有来源 memory id 作为 evidence。
    content 可传入整理后的完整表述；不传时服务端会把原内容拼接为兜底版本。
    """
    store, _ = _services()
    result = store.merge_memories(
        user_id=current_user_id.get(),
        memory_ids=memory_ids,
        content=content,
    )
    payload = result.model_dump()
    if result.memory:
        payload["memory"] = _memory_to_dict(result.memory)
    return _dump(payload)


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


async def get_core_memory_history(
    section: CoreMemorySectionName | None = None,
    limit: int = 20,
) -> str:
    """查看核心记忆的历史版本。

    当用户想回滚、追查核心记忆为何变化，或检查上一版内容时调用。
    """
    store, _ = _services()
    history = store.list_core_memory_section_history(
        user_id=current_user_id.get(),
        section=section,
        limit=max(1, min(limit, 100)),
    )
    return _dump([_core_memory_history_to_dict(item) for item in history])


async def consolidate_core_memory() -> str:
    """整理当前用户的核心记忆。

    只在用户明确要求整理核心记忆时调用。服务端会读取已经保存的长期记忆，
    让整理模型产出少量稳定日常背景，并校验证据来源后再写入核心记忆。
    """
    settings = get_settings()
    store = get_memory_store(settings)
    llm_client = get_llm_client(settings)
    consolidator = CoreMemoryConsolidator(store=store, llm_client=llm_client)
    result = await consolidator.consolidate(user_id=current_user_id.get())
    return _dump(result.model_dump())


async def save_memory(
    memory: str,
    type: MemoryType = "fact",
    importance: int = 5,
    confidence: float = 0.0,
    stability: MemoryStability = "stable",
    valid_until: str | None = None,
    review_after: str | None = None,
    sensitivity: MemorySensitivity = "normal",
    source_quote: str = "",
    reason: str = "",
) -> str:
    """保存一条关于用户的长期信息。用户在日常闲聊中自然流露的喜好、生活事实、
    重要的人、人物关系、目标计划、生活背景与长期事项都值得保存，不需要用户明确说「记住」。
    本工具可以在同一轮中跟 search_memory 连续使用；先检索过旧记忆不代表不需要保存
    本轮新出现的长期信息。

    参数要求（不满足会被服务端拒绝，返回里会写明原因）：
    - memory：完整的陈述句，例如「用户家里养了一只猫」。
    - type：偏好用 preference，事实用 fact，重要人物用 person，人物关系用 relationship。
    - importance：1-10。对用户长期成立的偏好和事实给 6-8，重大信息（家人、
      健康、原则性偏好）给 8-10；当下情绪、玩笑、一次性安排（如「今晚吃火锅」）
      低于 6，不会被保存。
    - confidence：0-1。用户亲口说出的给 0.9；猜测和推断不会被保存。
    - stability：temporary 表示临时事实，medium 表示阶段性事实，stable 表示长期稳定信息。
    - valid_until：明确有效期，使用 ISO 日期或时间；不确定时传 null。
    - review_after：需要日后复核是否仍成立时填写 ISO 日期或时间；不需要时传 null。
    - sensitivity：normal / private / sensitive。敏感信息保存门槛更高，且不进入核心记忆。
    - source_quote：用户原话的逐字片段，不能改写、不能编造。
    - 假设场景（「如果我以后用 Mac…」）不要保存。

    服务端会自动与已有记忆比对：内容相同会忽略，同主题会更新旧记忆而不是新建。
    返回 JSON：{"action": "create|update|ignore", "reason": "...", "memory_id": "..."}
    """
    store, embedding_client = _services()
    user_id = current_user_id.get()
    try:
        candidate = CandidateMemory(
            action="create",
            memory=memory.strip(),
            type=type,
            importance=importance,
            confidence=confidence,
            stability=stability,
            valid_until=valid_until,
            review_after=review_after,
            sensitivity=sensitivity,
            reason=reason,
            source_quote=source_quote.strip(),
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        field = ".".join(str(part) for part in first_error.get("loc", ()))
        rejection = f"参数不合法（字段 {field}）"
        store.create_decision_log(
            user_id=user_id,
            conversation_id=None,
            candidate_json=_dump({"source": "mcp", "memory": memory, "error": rejection}),
            decision="ignore",
            reason=rejection,
        )
        return _dump({"action": "ignore", "reason": rejection})

    candidate_json = _dump({"source": "mcp", **candidate.model_dump()})
    rejection = validate_candidate_for_save(candidate)
    if rejection:
        store.create_decision_log(
            user_id=user_id,
            conversation_id=None,
            candidate_json=candidate_json,
            decision="ignore",
            reason=rejection,
        )
        return _dump({"action": "ignore", "reason": rejection})

    resolver = MemoryResolver(store=store, embedding_client=embedding_client)
    result = await resolver.resolve(
        user_id=user_id,
        candidate=candidate,
        source_message=candidate.source_quote,
        conversation_id=None,
    )
    store.create_decision_log(
        user_id=user_id,
        conversation_id=None,
        candidate_json=candidate_json,
        decision=result.action,
        reason=result.reason,
    )
    return _dump(
        {
            "action": result.action,
            "relation": result.relation,
            "reason": result.reason,
            "memory_id": result.memory.id if result.memory else None,
        }
    )


async def review_memories(limit: int = 200) -> str:
    """体检当前用户的记忆库，返回保留、合并、降权、删除或复核建议。

    本工具只分析并返回建议清单，不会自动修改或删除任何记忆。适用于用户要求
    「检查一下记忆」「清理重复记忆」「看看哪些记忆过期/冲突」时调用。
    """
    store, _ = _services()
    reviewer = MemoryReviewer(store=store)
    result = reviewer.review(
        user_id=current_user_id.get(),
        limit=max(1, min(limit, 500)),
    )
    return _dump(result.model_dump())


async def memory_report(format: str = "markdown") -> str:
    """Build a human-readable report of the current user's memory profile.

    Use when the user asks what the system understands about them, or wants a
    compact memory report grouped by background, preferences, relationships,
    routines, goals, and communication style. format can be "markdown" or "json".
    """
    store, _ = _services()
    report = build_memory_report(store=store, user_id=current_user_id.get())
    if format == "json":
        return _dump(report)
    return report["markdown"]


async def export_memories(format: str = "json", include_deleted: bool = True) -> str:
    """Export the current user's memory data as JSON or Markdown.

    Use for user-requested backups or portable review. Embeddings are omitted
    because they should be regenerated after migration.
    """
    store, _ = _services()
    export_data = build_memory_export(
        store=store,
        user_id=current_user_id.get(),
        include_deleted=include_deleted,
    )
    if format == "markdown":
        return format_memory_export(export_data)
    return _dump(export_data)


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


async def list_deleted_memories(limit: int = 50) -> str:
    """List soft-deleted memories that can still be restored."""
    store, _ = _services()
    memories = store.list_archived_memories(
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


async def restore_memory(memory_id: str) -> str:
    """Restore a soft-deleted memory by id."""
    store, _ = _services()
    memory = store.restore_memory(
        memory_id=memory_id,
        user_id=current_user_id.get(),
    )
    if memory is None:
        return _dump({"restored": False, "memory_id": memory_id})
    return _dump({"restored": True, "memory": _memory_to_dict(memory)})


async def forget_memories(query: str, limit: int = 5) -> str:
    """按自然语言描述批量遗忘相关记忆。

    只在用户明确要求忘记某类信息时调用，例如「忘掉关于咖啡的记忆」或
    「不要再记我以前用 Android 这件事」。本工具会先按 query 检索当前用户的
    相关记忆，再软删除最多 limit 条；如果用户只是想查看或回顾，不要调用本工具。
    返回 JSON：{"deleted_count": 1, "deleted": [...]}。
    """
    store, embedding_client = _services()
    user_id = current_user_id.get()
    normalized_query = query.strip()
    if not normalized_query:
        return _dump({"deleted_count": 0, "deleted": [], "query": query})
    service = MemorySearchService(store=store, embedding_client=embedding_client)
    matches = await service.search(
        query=normalized_query,
        user_id=user_id,
        limit=max(1, min(limit, 10)),
        record_usage=False,
    )
    deleted: list[dict] = []
    for memory in matches:
        if store.archive_memory(memory_id=memory.id, user_id=user_id):
            deleted.append(_memory_to_dict(memory))
    return _dump(
        {
            "deleted_count": len(deleted),
            "deleted": deleted,
            "query": normalized_query,
        }
    )
