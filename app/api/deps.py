from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.knowledge.agent import KnowledgeAgentConfig, KnowledgeSearchAgent
from app.knowledge.store import KnowledgeStore
from app.llm.client import OpenAICompatibleClient
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)
from app.memory.store import MemoryStore

security = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.gateway_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GATEWAY_API_KEY 未配置",
        )
    if credentials is None or credentials.credentials != settings.gateway_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization Bearer token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    return x_user_id or "default"


def get_memory_store(settings: Annotated[Settings, Depends(get_settings)]) -> MemoryStore:
    # Schema initialization/migration runs once in the application lifespan.
    return MemoryStore(settings.database_path)


def get_knowledge_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> KnowledgeStore:
    # Schema initialization/migration runs once in the application lifespan.
    return KnowledgeStore(
        settings.knowledge_database_path,
        max_document_bytes=settings.knowledge_max_document_bytes,
    )


def get_knowledge_search_agent(
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KnowledgeSearchAgent:
    config = KnowledgeAgentConfig(
        base_url=settings.knowledge_agent_base_url,
        api_key=settings.knowledge_agent_api_key,
        flash_model=settings.knowledge_agent_flash_model,
        pro_model=settings.knowledge_agent_pro_model,
        egress_policy=settings.knowledge_agent_egress_policy,
        allow_sensitive_egress=settings.allow_sensitive_egress,
        timeout_seconds=settings.knowledge_agent_timeout_seconds,
    )
    return KnowledgeSearchAgent(store=store, config=config)


def get_embedding_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmbeddingClient:
    if settings.embedding_api_key and settings.embedding_model:
        return OpenAICompatibleEmbeddingClient(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.request_timeout_seconds,
            allow_sensitive_egress=settings.allow_sensitive_egress,
        )
    return NullEmbeddingClient()


def get_memory_search_service(
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MemorySearchService:
    return MemorySearchService(
        store=store,
        embedding_client=embedding_client,
        time_ripple_delta=settings.time_ripple_delta,
        time_ripple_window_hours=settings.time_ripple_window_hours,
    )


def get_llm_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(settings=settings)
