import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from app.api.deps import (
    get_embedding_client,
    get_llm_client,
    get_memory_search_service,
    get_memory_store,
    get_user_id,
    require_api_key,
)
from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import render_memory_context
from app.memory.extractor import LLMMemoryExtractor
from app.memory.resolver import MemoryResolver
from app.memory.search import EmbeddingClient, MemorySearchService
from app.memory.store import MemoryStore
from app.openai_compat.schemas import ChatCompletionRequest, ChatMessage
from app.openai_compat.streaming import raise_streaming_not_implemented

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1",
    tags=["openai-compatible"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    user_id: Annotated[str, Depends(get_user_id)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
) -> dict:
    if request.stream:
        raise_streaming_not_implemented()

    latest_user_message = _latest_user_message(request.messages)
    memories = await search_service.search(
        query=latest_user_message.content if latest_user_message else "",
        user_id=user_id,
        limit=8,
    )
    upstream_messages = _inject_memories(request.messages, memories)
    upstream_response = await llm_client.create_chat_completion(
        request=request,
        messages=upstream_messages,
    )
    client_response = _sanitize_chat_completion_response(upstream_response)

    assistant_message = _assistant_message_from_response(client_response)
    if latest_user_message and assistant_message:
        background_tasks.add_task(
            _extract_and_resolve_memories,
            store=store,
            embedding_client=embedding_client,
            llm_client=llm_client,
            user_id=user_id,
            user_message=latest_user_message.content,
            assistant_message=assistant_message,
            source_conversation_id=request.conversation_id,
        )

    return client_response


def _latest_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "user":
            return message
    return None


def _inject_memories(messages: list[ChatMessage], memories: list) -> list[dict[str, str]]:
    memory_prompt = render_memory_context(memories)
    upstream_messages = [message.model_dump(exclude_none=True) for message in messages]
    if memory_prompt:
        return [{"role": "system", "content": memory_prompt}, *upstream_messages]
    return upstream_messages


def _assistant_message_from_response(response: dict) -> str | None:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) and content else None


def _sanitize_chat_completion_response(response: dict) -> dict:
    sanitized = dict(response)
    choices = sanitized.get("choices")
    if not isinstance(choices, list):
        return sanitized

    sanitized_choices = []
    for choice in choices:
        if not isinstance(choice, dict):
            sanitized_choices.append(choice)
            continue

        sanitized_choice = dict(choice)
        message = sanitized_choice.get("message")
        if isinstance(message, dict):
            sanitized_choice["message"] = {
                "role": message.get("role", "assistant"),
                "content": message.get("content", ""),
            }
        sanitized_choices.append(sanitized_choice)

    sanitized["choices"] = sanitized_choices
    return sanitized


async def _extract_and_resolve_memories(
    *,
    store: MemoryStore,
    embedding_client: EmbeddingClient,
    llm_client: OpenAICompatibleClient,
    user_id: str,
    user_message: str,
    assistant_message: str,
    source_conversation_id: str | None,
) -> None:
    # 后台任务：无论提取、解析还是落库出错，都不能影响聊天接口本身
    try:
        extractor = LLMMemoryExtractor(llm_client=llm_client)
        outcome = await extractor.extract(
            user_message=user_message,
            assistant_message=assistant_message,
        )
        if not outcome.accepted or outcome.candidate is None:
            store.create_decision_log(
                conversation_id=source_conversation_id,
                candidate_json=outcome.candidate_json,
                decision="ignore",
                reason=outcome.reason,
            )
            return

        resolver = MemoryResolver(store=store, embedding_client=embedding_client)
        result = await resolver.resolve(
            user_id=user_id,
            candidate=outcome.candidate,
            source_message=user_message,
            conversation_id=source_conversation_id,
        )
        store.create_decision_log(
            conversation_id=source_conversation_id,
            candidate_json=outcome.candidate_json,
            decision=result.action,
            reason=result.reason,
        )
    except Exception:
        logger.exception("记忆提取后台任务失败")
