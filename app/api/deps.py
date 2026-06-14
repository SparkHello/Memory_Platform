from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.llm.client import OpenAICompatibleClient
from app.memory.search import (
    EmbeddingClient,
    MemorySearchService,
    NullEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)
from app.memory.store import MemoryStore
from app.providers.config import load_effective_providers_config
from app.providers.models import ProvidersConfig
from app.providers.store import ProviderStore

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
    store = MemoryStore(settings.database_path)
    store.init_db()
    return store


def get_provider_store(settings: Annotated[Settings, Depends(get_settings)]) -> ProviderStore:
    store = ProviderStore(settings.database_path)
    store.init_db()
    return store


def get_providers_config(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProvidersConfig:
    return load_effective_providers_config(
        database_path=settings.database_path,
        providers_config_path=settings.providers_config_path,
    )


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
        )
    return NullEmbeddingClient()


def get_memory_search_service(
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
) -> MemorySearchService:
    return MemorySearchService(store=store, embedding_client=embedding_client)


def get_llm_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(settings=settings)
