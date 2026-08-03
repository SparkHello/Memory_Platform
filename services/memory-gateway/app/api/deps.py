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
from app.model_catalog import providers_for_route
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

    if settings.model_gateway_enabled:
        return bool(settings.model_gateway_embedding_space_id.strip())
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
    if settings.model_gateway_enabled:
        # Vector reuse is only safe when the application knows the gateway's
        # immutable embedding space. An unset space deliberately falls back to
        # local keyword/FTS retrieval instead of guessing from model names.
        if not settings.model_gateway_embedding_space_id.strip():
            return NullEmbeddingClient()
        return OpenAICompatibleEmbeddingClient(
            base_url=settings.model_gateway_base_url,
            api_key=settings.model_gateway_api_key,
            model=settings.model_gateway_embedding_model,
            dimensions=settings.embedding_dimensions,
            expected_space_id=settings.model_gateway_embedding_space_id,
            model_gateway_mode=True,
            timeout_seconds=settings.request_timeout_seconds,
            allow_sensitive_egress=settings.allow_sensitive_egress,
            usage_recorder=UsageRecorder(settings.database_path),
        )
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
    if settings.model_gateway_enabled:
        config = KnowledgeAgentConfig(
            model_gateway_enabled=True,
            base_url=settings.model_gateway_base_url,
            api_key=settings.model_gateway_api_key,
            flash_model=settings.model_gateway_knowledge_fast_model,
            pro_model=settings.model_gateway_knowledge_pro_model,
            implicit_deepseek_fallback=False,
            egress_policy=settings.knowledge_agent_egress_policy,
            allow_sensitive_egress=settings.allow_sensitive_egress,
            timeout_seconds=settings.knowledge_agent_timeout_seconds,
        )
    else:
        fast_providers = providers_for_route(settings, "knowledge.fast")
        fast_by_code = {
            provider.code: provider for provider in reversed(fast_providers)
        }
        fast_priority = "".join(
            dict.fromkeys(provider.code for provider in fast_providers)
        )
        pro_providers = providers_for_route(settings, "knowledge.pro")
        pro_provider = next(
            (provider for provider in pro_providers if provider.code == "D"),
            None,
        )
        mimo = fast_by_code.get("M")
        kimi = fast_by_code.get("K")
        deepseek = fast_by_code.get("D")
        config = KnowledgeAgentConfig(
            base_url=(deepseek or pro_provider).base_url
            if (deepseek or pro_provider)
            else settings.llm_deepseek_base_url,
            api_key=(deepseek or pro_provider).api_key
            if (deepseek or pro_provider)
            else settings.llm_deepseek_api_key,
            flash_model=(
                deepseek.model if deepseek else settings.llm_deepseek_flash_model
            ),
            pro_model=(
                pro_provider.model if pro_provider else settings.llm_deepseek_pro_model
            ),
            provider_priority=fast_priority or settings.llm_provider_priority,
            implicit_deepseek_fallback=not bool(settings.model_routes_path),
            mimo_base_url=mimo.base_url if mimo else settings.llm_mimo_base_url,
            mimo_api_key=mimo.api_key if mimo else settings.llm_mimo_api_key,
            mimo_model=mimo.model if mimo else settings.llm_mimo_model,
            kimi_base_url=kimi.base_url if kimi else settings.llm_kimi_base_url,
            kimi_api_key=kimi.api_key if kimi else settings.llm_kimi_api_key,
            kimi_model=kimi.model if kimi else settings.llm_kimi_model,
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
