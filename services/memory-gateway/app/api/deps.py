from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

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
from app.llm.runtime import (
    direct_embedding_space_id as build_direct_embedding_space_id,
    resolve_model_runtime,
)
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

def direct_embedding_space_id(settings: Settings) -> str:
    """Return a stable local identity for a direct-provider vector space.

    Direct OpenAI-compatible providers do not return the Model Gateway space
    header.  The configured endpoint, model and vector dimensions therefore
    form the local contract for vectors created in compatibility mode.  The
    API key is deliberately excluded so key rotation does not invalidate a
    vector space.
    """

    return build_direct_embedding_space_id(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )


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
        client = OpenAICompatibleEmbeddingClient(
            base_url=embedding.base_url,
            api_key=embedding.api_key,
            model=embedding.model,
            dimensions=embedding.dimensions,
            expected_space_id=(embedding.space_id if embedding.model_gateway_mode else ""),
            model_gateway_mode=embedding.model_gateway_mode,
            timeout_seconds=settings.request_timeout_seconds,
            allow_sensitive_egress=settings.allow_sensitive_egress,
            usage_recorder=(
                None
                if embedding.model_gateway_mode
                else UsageRecorder(settings.database_path)
            ),
            usage_hmac_secret=settings.gateway_signing_secret,
        )
        # Direct providers normally cannot attest an immutable space in their
        # response headers.  Keep response validation independent while still
        # tagging every newly generated vector with a deterministic local
        # space.  Existing rows remain NULL/unknown until explicitly re-embedded.
        if not embedding.model_gateway_mode:
            client.embedding_space_id = embedding.space_id
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
    runtime = resolve_model_runtime(settings)
    fast_providers = (
        []
        if runtime.is_central
        else providers_for_route(settings, "knowledge.fast")
    )
    pro_providers = (
        []
        if runtime.is_central
        else providers_for_route(settings, "knowledge.pro")
    )
    config = KnowledgeAgentConfig(
        fast_providers=fast_providers,
        pro_provider=pro_providers[0] if pro_providers else None,
        model_runtime=runtime if runtime.is_central else None,
        rate_limit_cooldown_seconds=settings.llm_rate_limit_cooldown_seconds,
        egress_policy=settings.knowledge_agent_egress_policy,
        allow_sensitive_egress=settings.allow_sensitive_egress,
        timeout_seconds=settings.knowledge_agent_timeout_seconds,
        usage_hmac_secret=settings.gateway_signing_secret,
    )
    return KnowledgeSearchAgent(
        store=retrieval,
        config=config,
        usage_recorder=(
            None if runtime.is_central else UsageRecorder(settings.database_path)
        ),
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
    runtime = resolve_model_runtime(settings)
    return OpenAICompatibleClient(
        settings=settings,
        usage_recorder=(
            None if runtime.is_central else UsageRecorder(settings.database_path)
        ),
    )


def get_chat_gateway_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAIChatGatewayClient:
    return OpenAIChatGatewayClient(settings=settings)


def get_usage_recorder(
    settings: Annotated[Settings, Depends(get_settings)],
) -> UsageRecorder | None:
    return (
        None
        if resolve_model_runtime(settings).is_central
        else UsageRecorder(settings.database_path)
    )
