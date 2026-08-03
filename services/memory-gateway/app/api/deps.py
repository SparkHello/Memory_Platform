import hashlib
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.knowledge.agent import (
    KnowledgeAgentConfig,
    KnowledgeSearchAgent,
)
from app.knowledge.retrieval import (
    KnowledgeEmbeddingIndexer,
    KnowledgeRetrievalService,
)
from app.knowledge.store import KnowledgeStore
from app.llm.client import OpenAICompatibleClient
from app.providers.catalog import providers_for_route
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)
from app.memory.store import MemoryStore
from app.openai_compat.gateway_client import OpenAIChatGatewayClient
from app.usage.recorder import UsageRecorder

security = HTTPBearer(auto_error=False)


def direct_embedding_space_id(settings: Settings) -> str:
    """Return a stable local identity for a direct-provider vector space.

    Direct OpenAI-compatible providers do not return the Model Gateway space
    header.  The configured endpoint, model and vector dimensions therefore
    form the local contract for vectors created in compatibility mode.  The
    API key is deliberately excluded so key rotation does not invalidate a
    vector space.
    """

    identity = "\0".join(
        (
            "direct-openai-compatible-v1",
            settings.embedding_base_url.strip().rstrip("/"),
            settings.embedding_model.strip(),
            str(settings.embedding_dimensions),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"direct-openai-compatible-v1:{digest}"


def embedding_runtime_enabled(settings: Settings) -> bool:
    """Whether the active runtime has a trustworthy embedding space."""

    return bool(
        settings.embedding_api_key.strip() and settings.embedding_model.strip()
    )


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


def get_embedding_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmbeddingClient:
    if settings.embedding_api_key and settings.embedding_model:
        client = OpenAICompatibleEmbeddingClient(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.request_timeout_seconds,
            allow_sensitive_egress=settings.allow_sensitive_egress,
            usage_recorder=UsageRecorder(settings.database_path),
        )
        # Direct providers normally cannot attest an immutable space in their
        # response headers.  Keep response validation independent while still
        # tagging every newly generated vector with a deterministic local
        # space.  Existing rows remain NULL/unknown until explicitly re-embedded.
        client.embedding_space_id = direct_embedding_space_id(settings)
        return client
    return NullEmbeddingClient()


def get_knowledge_retrieval_service(
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KnowledgeRetrievalService:
    return KnowledgeRetrievalService(
        store=store,
        embedding_client=embedding_client,
        vector_weight=settings.knowledge_hybrid_vector_weight,
        min_cosine=settings.knowledge_embedding_min_cosine,
    )


def get_knowledge_embedding_indexer(
    store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KnowledgeEmbeddingIndexer:
    return KnowledgeEmbeddingIndexer(
        store=store,
        embedding_client=embedding_client,
        batch_size=settings.knowledge_embedding_batch_size,
    )


def get_knowledge_search_agent(
    retrieval: Annotated[
        KnowledgeRetrievalService, Depends(get_knowledge_retrieval_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KnowledgeSearchAgent:
    fast_providers = providers_for_route(settings, "knowledge.fast")
    pro_providers = providers_for_route(settings, "knowledge.pro")
    config = KnowledgeAgentConfig(
        fast_providers=fast_providers,
        pro_provider=pro_providers[0] if pro_providers else None,
        rate_limit_cooldown_seconds=settings.llm_rate_limit_cooldown_seconds,
        egress_policy=settings.knowledge_agent_egress_policy,
        allow_sensitive_egress=settings.allow_sensitive_egress,
        timeout_seconds=settings.knowledge_agent_timeout_seconds,
    )
    return KnowledgeSearchAgent(
        store=retrieval,
        config=config,
        usage_recorder=UsageRecorder(settings.database_path),
    )


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
    return OpenAICompatibleClient(
        settings=settings,
        usage_recorder=UsageRecorder(settings.database_path),
    )


def get_chat_gateway_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAIChatGatewayClient:
    return OpenAIChatGatewayClient(settings=settings)


def get_usage_recorder(
    settings: Annotated[Settings, Depends(get_settings)],
) -> UsageRecorder:
    return UsageRecorder(settings.database_path)
