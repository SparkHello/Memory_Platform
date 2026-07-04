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

SERVER_INSTRUCTIONS = """这是用户的长期记忆服务。你只有 6 个工具：

- **search_memory**：回答问题前，如果话题涉及用户的喜好、习惯、家人、健康、计划或过去聊过的事，先搜索再回答。
- **surface_memories**：新对话开始、用户让你主动回顾或没有明确 query 但需要唤起重要长期事项时调用。
- **submit_memory_text**：如果用户本轮提供了值得长期记住的信息，把用户原文放入 text 提交。不要自己整理、改写、拆分，也不要猜 type、importance、confidence、valid_from 或 temporal key；服务端会自动提取、去重和保存。
- **get_core_memory**：需要了解用户稳定背景时调用。
- **get_recent_context_summary**：需要恢复上一轮上下文时调用。
- **digest_memories**：新对话开始时可先调用读取近期未消化记忆；完成反思后再次带 source_ids、reflection、feel、resolved_ids 提交消化结果。

不要保存假设、玩笑、一次性安排。同一轮可以先 search 再 submit，两者不冲突。保存被拒绝时不要重试。
返回结果里的 activation_count 表示活跃度，不是精确搜索次数；Time Ripple 是默认关闭的实验能力，普通客户端不需要启用。
用户要求忘记某条信息时，请引导用户在 Web 管理台（/ui/）操作，你没有删除或遗忘的工具。"""
SERVER_INSTRUCTIONS = SERVER_INSTRUCTIONS.replace(
    "\u4f60\u53ea\u6709 6 \u4e2a\u5de5\u5177",
    "\u4f60\u53ea\u6709 7 \u4e2a\u5de5\u5177",
)
SERVER_INSTRUCTIONS += (
    "\n- **update_recent_context_summary**: submit or replace a short recent "
    "conversation summary. This is short-term context only, not long-term "
    "memory, and it never enters core memory."
)


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


def _search_service(store: MemoryStore, embedding_client: EmbeddingClient) -> MemorySearchService:
    settings = get_settings()
    return MemorySearchService(
        store=store,
        embedding_client=embedding_client,
        time_ripple_delta=settings.time_ripple_delta,
        time_ripple_window_hours=settings.time_ripple_window_hours,
    )


def _memory_to_dict(memory: MemoryRecord) -> dict:
    return {
        "id": memory.id,
        "content": memory.content,
        "type": memory.type,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "valence": memory.valence,
        "arousal": memory.arousal,
        "usage_count": memory.usage_count,
        "last_used_at": memory.last_used_at,
        "stability": memory.stability,
        "valid_from": memory.valid_from,
        "valid_until": memory.valid_until,
        "review_after": memory.review_after,
        "sensitivity": memory.sensitivity,
        "evidence_memory_ids": memory.evidence_memory_ids,
        "topics": memory.topics,
        "entities": memory.entities,
        "space_ids": memory.space_ids,
        "temporal_subject": memory.temporal_subject,
        "temporal_predicate": memory.temporal_predicate,
        "status": getattr(memory, "status", "dynamic"),
        "digested": getattr(memory, "digested", False),
        "decay_lambda": getattr(memory, "decay_lambda", None),
        "supersedes": getattr(memory, "supersedes", None),
        "superseded_by": getattr(memory, "superseded_by", None),
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "archived_at": memory.archived_at,
    }


def _search_hit_to_dict(hit) -> dict:
    payload = _memory_to_dict(hit.memory)
    payload.update(
        {
            "relevance": hit.relevance,
            "channels": hit.channels,
            "final_score": hit.final_score,
            "activation_count": hit.activation_count,
            "last_active_at": hit.last_active_at,
        }
    )
    return payload


def _surface_hit_to_dict(hit) -> dict:
    payload = _memory_to_dict(hit.memory)
    payload.update(
        {
            "final_score": hit.final_score,
            "activation_count": hit.activation_count,
            "last_active_at": hit.last_active_at,
            "freshness_bonus": hit.freshness_bonus,
            "surface_reason": hit.surface_reason,
            "surface_score": hit.surface_score,
            "surface_mode": hit.surface_mode,
            "surface_reason_text": hit.surface_reason_text,
            "life_score": hit.life_score,
            "days_since_last_active": hit.days_since_last_active,
            "review_signals": hit.review_signals,
        }
    )
    return payload


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


def _register_tools(mcp: FastMCP) -> None:
    mcp.tool()(search_memory)
    mcp.tool()(surface_memories)
    mcp.tool()(submit_memory_text)
    mcp.tool()(update_recent_context_summary)
    mcp.tool()(get_recent_context_summary)
    mcp.tool()(get_core_memory)
    mcp.tool()(digest_memories)


async def search_memory(query: str, limit: int = 8) -> str:
    """检索与当前话题相关的长期记忆。

    聊到用户的喜好、习惯、家人朋友、健康、计划安排、长期事项，或过去聊过的
    话题时，先调用本工具再回答，让对话自然延续。
    调用本工具后，仍要检查本轮用户消息是否包含新的长期信息；如果有，继续调用
    submit_memory_text。检索旧记忆和保存新信息可以在同一轮连续发生，不要二选一。
    query 用一句话描述要查的主题，例如「用户的饮食偏好」。
    返回 JSON 数组，按相关度排序；空数组表示没有相关记忆，此时正常回答即可。
    被返回的记忆会自动增加底层 usage_count 并刷新 last_used_at；对外请解释为
    activation_count（活跃度），不是精确搜索次数。Time Ripple 是默认关闭的实验能力。
    """
    store, embedding_client = _services()
    service = _search_service(store, embedding_client)
    hits = await service.search_hits(
        query=query,
        user_id=current_user_id.get(),
        limit=max(1, min(limit, 20)),
    )
    return _dump([_search_hit_to_dict(hit) for hit in hits])


async def surface_memories(
    limit: int = 8,
    mode: str = "balanced",
    include_archived: bool = False,
) -> str:
    """无 query 浮现当前最值得想起的长期记忆。

    适用于新对话开场、用户让你主动回顾近况/长期事项，或当前没有明确检索词但需要
    带着长期背景进入对话时。mode 可选 balanced、important、emotional、stale、
    review_due；默认 balanced 按活跃度、新鲜度和重要度排序。
    include_archived=True 时包含已归档记忆。
    """
    store, embedding_client = _services()
    service = _search_service(store, embedding_client)
    hits = service.surface_memories(
        user_id=current_user_id.get(),
        limit=max(1, min(limit, 20)),
        mode=mode,
        include_archived=include_archived,
    )
    return _dump([_surface_hit_to_dict(hit) for hit in hits])


async def submit_memory_text(text: str, conversation_id: str = "") -> str:
    """提交一段可能包含多条长期记忆的用户原文，由服务端自动整理保存。

    这是 iOS 客户端的优先保存入口：客户端模型只需要判断本轮用户是否提供了
    可能长期有用的信息，然后把用户原文放进 text。服务端会调用整理模型拆分为
    多条候选记忆，并逐条执行 source_quote 校验、敏感信息门槛、假设场景拦截、
    去重、更新或创建。不要在客户端手动把一大段内容拆成多次 save_memory，也不要
    自行猜 type、importance、confidence、valid_from、temporal_subject 或
    temporal_predicate。

    text 应尽量使用用户原话，而不是模型改写后的总结；这样服务端才能验证
    source_quote 是否真实来自用户。conversation_id 可选，用于决策日志追踪；
    不需要追踪时可省略或传空字符串。
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
        conversation_id=conversation_id or None,
        source="mcp_ingest",
    )
    return _dump(result.model_dump())


async def get_recent_context_summary(conversation_id: str = "") -> str:
    """读取近期会话摘要。

    用于用户问「最近我们在聊什么」或需要恢复最近上下文时。摘要是短期上下文，
    不属于长期记忆，也不会进入核心记忆。
    """
    store, _ = _services()
    summary = store.get_recent_context_summary(
        user_id=current_user_id.get(),
        conversation_id=conversation_id or None,
    )
    if summary is None:
        return _dump({"found": False, "summary": ""})
    return _dump({"found": True, **_recent_summary_to_dict(summary)})


async def update_recent_context_summary(conversation_id: str = "", summary: str = "") -> str:
    """Submit or replace a short-term recent conversation summary."""
    summary_text = (summary or "").strip()
    if not summary_text:
        return _dump({"updated": False, "error": "summary is required"})
    if len(summary_text) > 12000:
        return _dump({"updated": False, "error": "summary exceeds 12000 characters"})
    store, _ = _services()
    saved = store.upsert_recent_context_summary(
        user_id=current_user_id.get(),
        conversation_id=(conversation_id or "").strip() or None,
        summary=summary_text,
    )
    return _dump({"updated": True, **_recent_summary_to_dict(saved)})


async def get_core_memory() -> str:
    """查看当前用户的核心记忆。

    核心记忆是服务端从长期记忆中整理出的稳定日常背景，数量少、优先级高。
    当用户询问「你对我的核心了解」「核心记忆是什么」时调用。日常回答仍应按话题
    使用 search_memory 检索细节；核心记忆不是细节检索工具。
    """
    store, _ = _services()
    sections = store.list_core_memory_sections(user_id=current_user_id.get())
    return _dump([_core_memory_to_dict(section) for section in sections])


async def digest_memories(
    # 默认值保持非 None：`X | None = None` 会让 FastMCP 生成带 anyOf/null 的 JSON
    # schema，iOS MCP 客户端无法解析这种联合类型，会让整台服务器的工具调用失败。
    # 入参在下方只读重绑定（不就地修改），可变默认值在此安全。
    limit: int = 10,
    source_ids: list[str] = [],
    reflection: str = "",
    feel: str = "",
    resolved_ids: list[str] = [],
) -> str:
    """Two-phase memory digestion.

    Phase 1: call with only limit to read recent undigested memories.
    Phase 2: call with source_ids plus reflection/feel/resolved_ids to persist
    reflective and emotional outputs, mark source memories digested, and resolve
    selected memories.
    """
    store, _ = _services()
    user_id = current_user_id.get()

    reflection_text = (reflection or "").strip()
    feel_text = (feel or "").strip()
    source_ids = [memory_id for memory_id in (source_ids or []) if memory_id]
    resolved_ids = [memory_id for memory_id in (resolved_ids or []) if memory_id]

    if reflection_text or feel_text or source_ids or resolved_ids:
        if not source_ids:
            return _dump({
                "error": "source_ids is required when submitting digestion output.",
                "created": [],
                "digested_ids": [],
                "resolved_ids": [],
            })
        created: list[MemoryRecord] = []
        source_memories = [
            memory
            for memory_id in source_ids
            if (memory := store.get_memory(memory_id=memory_id, user_id=user_id)) is not None
        ]
        if reflection_text:
            reflection_valence, reflection_arousal = _digest_affect(
                text=reflection_text,
                source_memories=source_memories,
            )
            created.append(
                store.create_memory(
                    user_id=user_id,
                    content=reflection_text,
                    type="reflective",
                    importance=6,
                    confidence=0.8,
                    valence=reflection_valence,
                    arousal=reflection_arousal,
                    source_message="digest_memories:reflection",
                    topics=["digestion", "reflection"],
                )
            )
        if feel_text:
            feel_valence, feel_arousal = _digest_affect(
                text=feel_text,
                source_memories=source_memories,
                default_arousal=0.4,
            )
            created.append(
                store.create_memory(
                    user_id=user_id,
                    content=feel_text,
                    type="emotional",
                    importance=5,
                    confidence=0.8,
                    valence=feel_valence,
                    arousal=feel_arousal,
                    source_message="digest_memories:feel",
                    topics=["digestion", "feel"],
                )
            )
        store.mark_digested(memory_ids=source_ids, user_id=user_id)
        resolved_count = store.update_memory_statuses(
            memory_ids=resolved_ids,
            user_id=user_id,
            status="resolved",
        )
        return _dump({
            "created": [_memory_to_dict(memory) for memory in created],
            "digested_ids": source_ids,
            "resolved_ids": resolved_ids,
            "resolved_count": resolved_count,
        })

    memories = store.list_undigested_memories(
        user_id=user_id,
        limit=max(1, min(limit, 20)),
    )
    if not memories:
        return _dump({
            "memories": [],
            "message": "没有未消化的记忆。",
            "instructions": "不需要产出 reflection/feel。",
        })
    return _dump({
        "memories": [_memory_to_dict(m) for m in memories],
        "instructions": (
            "审视以上记忆后，再次调用 digest_memories 并传入："
            '{"reflection": "高层抽象推论", "feel": "第一人称感受", '
            '"source_ids": ["已消化的源记忆ID"], '
            '"resolved_ids": ["需要标记为已解决的记忆ID"]}'
        ),
    })
