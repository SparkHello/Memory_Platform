from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import logging

import anyio

from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import render_conversation_context_compression_messages
from app.memory.extractor import detect_text_sensitivity
from app.memory.models import RecentContextSummary, RecentContextTurn
from app.memory.store import MemoryStore
from app.memory.utils import _parse_json_object
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.context import model_usage_scope


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecentContextDraft:
    summary: str
    compressed_summary: str
    recent_turns: list[RecentContextTurn]
    turn_count: int


def render_recent_turns(turns: list[RecentContextTurn]) -> str:
    pieces: list[str] = []
    for turn in turns:
        if turn.user.strip():
            pieces.append(f"用户：{turn.user.strip()}")
        if turn.assistant.strip():
            pieces.append(f"助手：{turn.assistant.strip()}")
    return "\n".join(pieces)


def materialize_recent_context(
    *,
    compressed_summary: str,
    recent_turns: list[RecentContextTurn],
) -> str:
    blocks: list[str] = []
    if compressed_summary.strip():
        blocks.append(f"较早对话摘要：\n{compressed_summary.strip()}")
    turns_text = render_recent_turns(recent_turns)
    if turns_text:
        blocks.append(f"最近对话原文：\n{turns_text}")
    return "\n\n".join(blocks)


def safe_extraction_context(
    *,
    state: RecentContextSummary | None,
    request_messages: list[dict[str, str]],
    allow_sensitive_egress: bool,
    recent_turn_limit: int,
    max_chars: int,
) -> str | None:
    summary = _safe_compressed_summary(
        state=state,
        allow_sensitive_egress=allow_sensitive_egress,
    )
    recent_messages = _safe_recent_messages(
        state=state,
        request_messages=request_messages,
        allow_sensitive_egress=allow_sensitive_egress,
        recent_turn_limit=recent_turn_limit,
    )
    recent_text = _render_messages(recent_messages)

    # Recent verbatim dialogue has priority because it carries the exact
    # context_quote used to disambiguate the latest user source.
    if len(recent_text) >= max_chars:
        return (
            "<recent_dialogue_quote_source>\n"
            f"{recent_text[-max_chars:]}\n"
            "</recent_dialogue_quote_source>"
        )
    available_for_summary = max_chars - len(recent_text) - (2 if recent_text else 0)
    bounded_summary = _bounded_text(
        summary,
        max_chars=max(0, available_for_summary),
    )
    blocks: list[str] = []
    if bounded_summary:
        blocks.append(
            "<compressed_summary_non_authoritative>\n"
            f"{bounded_summary}\n"
            "</compressed_summary_non_authoritative>"
        )
    if recent_text:
        blocks.append(
            "<recent_dialogue_quote_source>\n"
            f"{recent_text}\n"
            "</recent_dialogue_quote_source>"
        )
    rendered = "\n\n".join(blocks).strip()
    return rendered or None


def safe_context_quote_source(
    *,
    state: RecentContextSummary | None,
    request_messages: list[dict[str, str]],
    allow_sensitive_egress: bool,
    recent_turn_limit: int,
) -> str | None:
    rendered = _render_messages(
        _safe_recent_messages(
            state=state,
            request_messages=request_messages,
            allow_sensitive_egress=allow_sensitive_egress,
            recent_turn_limit=recent_turn_limit,
        )
    ).strip()
    return rendered or None


async def append_and_compact_recent_context(
    *,
    store: MemoryStore,
    llm_client: OpenAICompatibleClient,
    user_id: str,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    allow_sensitive_egress: bool,
    keep_recent_turns: int,
    compact_after_turns: int,
    compact_after_chars: int,
    summary_max_chars: int,
) -> RecentContextSummary | None:
    if not user_text.strip() and not assistant_text.strip():
        return None

    previous = await anyio.to_thread.run_sync(
        partial(
            store.get_recent_context_summary_for_conversation,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    )
    draft = await evolve_recent_context(
        previous=previous,
        llm_client=llm_client,
        user_id=user_id,
        user_text=user_text,
        assistant_text=assistant_text,
        allow_sensitive_egress=allow_sensitive_egress,
        keep_recent_turns=keep_recent_turns,
        compact_after_turns=compact_after_turns,
        compact_after_chars=compact_after_chars,
        summary_max_chars=summary_max_chars,
    )
    if draft is None:
        return None
    return await anyio.to_thread.run_sync(
        partial(
            store.upsert_recent_context_state,
            user_id=user_id,
            conversation_id=conversation_id,
            summary=draft.summary,
            compressed_summary=draft.compressed_summary,
            recent_turns=draft.recent_turns,
            turn_count=draft.turn_count,
        )
    )


async def evolve_recent_context(
    *,
    previous: RecentContextSummary | None,
    llm_client: OpenAICompatibleClient,
    user_text: str,
    assistant_text: str,
    allow_sensitive_egress: bool,
    keep_recent_turns: int,
    compact_after_turns: int,
    compact_after_chars: int,
    summary_max_chars: int,
    user_id: str = "default",
) -> RecentContextDraft | None:
    """Build the next rolling-context snapshot without mutating the store."""
    if not user_text.strip() and not assistant_text.strip():
        return None

    compressed_summary = ""
    turns: list[RecentContextTurn] = []
    turn_count = 0
    if previous is not None:
        compressed_summary = previous.compressed_summary.strip()
        turns = list(previous.recent_turns)
        turn_count = previous.turn_count
        if not compressed_summary and not turns and previous.summary.strip():
            compressed_summary = previous.summary.strip()

    turn_sensitivity = detect_text_sensitivity(
        "\n".join(part for part in (user_text, assistant_text) if part)
    )
    turns.append(
        RecentContextTurn(
            user=user_text.strip(),
            assistant=assistant_text.strip(),
            sensitivity=turn_sensitivity,
        )
    )
    turn_count += 1

    older_turns = turns[:-keep_recent_turns] if len(turns) > keep_recent_turns else []
    newest_turns = turns[-keep_recent_turns:]
    compactable = [
        turn
        for turn in older_turns
        if allow_sensitive_egress or turn.sensitivity == "normal"
    ]
    retained_sensitive = [turn for turn in older_turns if turn not in compactable]
    compactable_text = render_recent_turns(compactable)
    should_compact = bool(compactable) and (
        len(turns) >= compact_after_turns
        or len(compactable_text) + len(compressed_summary) >= compact_after_chars
    )
    if should_compact and (
        allow_sensitive_egress
        or detect_text_sensitivity(compressed_summary) == "normal"
    ):
        updated_summary = await _compact_context(
            llm_client=llm_client,
            user_id=user_id,
            previous_summary=compressed_summary,
            turns_text=compactable_text,
        )
        if (
            updated_summary
            and not allow_sensitive_egress
            and detect_text_sensitivity(updated_summary) != "normal"
        ):
            logger.warning(
                "会话上下文压缩输出被本地判定为敏感；保留本地原始轮次。"
            )
            updated_summary = None
        if updated_summary:
            compressed_summary = _bounded_text(
                updated_summary,
                max_chars=summary_max_chars,
            )
            turns = [*retained_sensitive, *newest_turns]

    materialized = materialize_recent_context(
        compressed_summary=compressed_summary,
        recent_turns=turns,
    )
    return RecentContextDraft(
        summary=materialized,
        compressed_summary=compressed_summary,
        recent_turns=turns,
        turn_count=turn_count,
    )


async def _compact_context(
    *,
    llm_client: OpenAICompatibleClient,
    user_id: str,
    previous_summary: str,
    turns_text: str,
) -> str | None:
    messages = render_conversation_context_compression_messages(
        previous_summary=previous_summary,
        turns_text=turns_text,
    )
    request = ChatCompletionRequest(
        model="memory-context-compactor",
        messages=messages,
        temperature=0.0,
        stream=False,
    )
    try:
        with model_usage_scope(user_id=user_id):
            response = await llm_client.create_chat_completion(
                request=request,
                messages=messages,
                thinking="disabled",
            )
        content = response["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning(
            "会话上下文压缩调用失败；保留本地原始轮次。error_type=%s",
            type(exc).__name__,
        )
        return None
    if not isinstance(content, str):
        return None
    data = _parse_json_object(content)
    if not isinstance(data, dict):
        logger.warning("会话上下文压缩输出不是合法 JSON；保留本地原始轮次。")
        return None
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        logger.warning("会话上下文压缩输出缺少 summary；保留本地原始轮次。")
        return None
    return summary.strip()


def _messages_from_turns(turns: list[RecentContextTurn]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for turn in turns:
        if turn.user.strip():
            messages.append({"role": "user", "content": turn.user.strip()})
        if turn.assistant.strip():
            messages.append({"role": "assistant", "content": turn.assistant.strip()})
    return messages


def _safe_compressed_summary(
    *,
    state: RecentContextSummary | None,
    allow_sensitive_egress: bool,
) -> str:
    if state is None:
        return ""
    summary = state.compressed_summary.strip()
    if not summary and not state.recent_turns:
        # Legacy rows stored the rolling transcript directly in summary.
        summary = state.summary.strip()
    if not allow_sensitive_egress and detect_text_sensitivity(summary) != "normal":
        return ""
    return summary


def _safe_recent_messages(
    *,
    state: RecentContextSummary | None,
    request_messages: list[dict[str, str]],
    allow_sensitive_egress: bool,
    recent_turn_limit: int,
) -> list[dict[str, str]]:
    stored_turns = state.recent_turns if state is not None else []
    safe_stored_turns = [
        turn
        for turn in stored_turns
        if allow_sensitive_egress or turn.sensitivity == "normal"
    ]
    stored_messages = _messages_from_turns(safe_stored_turns)
    safe_request_messages = [
        message
        for message in request_messages
        if allow_sensitive_egress
        or detect_text_sensitivity(message.get("content", "")) == "normal"
    ]
    return _latest_user_turns(
        _deduplicated_messages([*stored_messages, *safe_request_messages]),
        limit=recent_turn_limit,
    )


def _deduplicated_messages(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    deduplicated: list[dict[str, str]] = []
    for message in messages:
        normalized = {
            "role": str(message.get("role") or "").strip(),
            "content": str(message.get("content") or "").strip(),
        }
        if not normalized["role"] or not normalized["content"]:
            continue
        if deduplicated and normalized == deduplicated[-1]:
            continue
        deduplicated.append(normalized)
    return deduplicated


def _latest_user_turns(
    messages: list[dict[str, str]],
    *,
    limit: int,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    user_count = 0
    for message in reversed(messages):
        selected.append(message)
        if message["role"] == "user":
            user_count += 1
            if user_count >= limit:
                break
    return list(reversed(selected))


def _render_messages(messages: list[dict[str, str]]) -> str:
    labels = {"user": "用户", "assistant": "助手"}
    return "\n".join(
        f"{labels.get(message['role'], message['role'])}：{message['content']}"
        for message in messages
    )


def _bounded_text(text: str, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    half = max(1, (max_chars - 3) // 2)
    return f"{text[:half]}\n…\n{text[-half:]}"[:max_chars]
