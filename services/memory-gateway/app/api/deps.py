from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.auth.console_login import ConsoleLoginCodeStore
from app.auth.signing import SigningSecretNotConfigured, require_signing_secret
from app.auth.tokens import AuthPrincipal, AuthTokenStore
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
from app.llm.runtime import resolve_model_runtime
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)
from app.memory.store import MemoryStore
from app.openai_compat.gateway_client import OpenAIChatGatewayClient


def embedding_runtime_enabled(settings: Settings) -> bool:
    """Whether the active runtime has a trustworthy embedding space."""

    return resolve_model_runtime(settings).embedding.enabled


def require_api_key(
    request: Request,
) -> None:
    """Defense in depth: protected routers require an early-auth principal."""

    if not isinstance(getattr(request.state, "auth_principal", None), AuthPrincipal):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization Bearer token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_user_id(
    request: Request,
) -> str:
    return get_auth_principal(request).user_id


def get_auth_principal(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "auth_principal", None)
    if not isinstance(principal, AuthPrincipal):
        raise HTTPException(status_code=401, detail="请求缺少已认证身份")
    return principal


def get_auth_token_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthTokenStore:
    # Schema initialization/migration runs once in the application lifespan.
    return AuthTokenStore(settings.auth_database_path)


def get_console_login_code_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConsoleLoginCodeStore:
    # Schema initialization/migration runs once in the application lifespan.
    return ConsoleLoginCodeStore(settings.auth_database_path)


def get_signing_secret(
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    try:
        return require_signing_secret(settings)
    except SigningSecretNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


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
    runtime = resolve_model_runtime(settings)
    embedding = runtime.embedding
    if embedding.enabled:
        return OpenAICompatibleEmbeddingClient(
            base_url=embedding.base_url,
            api_key=embedding.api_key,
            model=embedding.model,
            dimensions=embedding.dimensions,
            expected_space_id=embedding.space_id,
            model_gateway_mode=True,
            timeout_seconds=settings.request_timeout_seconds,
            allow_sensitive_egress=settings.allow_sensitive_egress,
            usage_hmac_secret=settings.gateway_signing_secret,
        )
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
    runtime = resolve_model_runtime(settings)
    config = KnowledgeAgentConfig(
        model_runtime=runtime,
        egress_policy=settings.knowledge_agent_egress_policy,
        allow_sensitive_egress=settings.allow_sensitive_egress,
        timeout_seconds=settings.knowledge_agent_timeout_seconds,
        usage_hmac_secret=settings.gateway_signing_secret,
    )
    return KnowledgeSearchAgent(
        store=retrieval,
        config=config,
    )


def get_memory_search_service(
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MemorySearchService:
    return MemorySearchService(
        store=store,
        embedding_client=embedding_client,
    )


def get_llm_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(settings=settings)


def get_chat_gateway_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAIChatGatewayClient:
    return OpenAIChatGatewayClient(settings=settings)
