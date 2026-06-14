from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gateway_api_key: str = Field(default="", validation_alias="GATEWAY_API_KEY")
    upstream_base_url: str = Field(
        default="https://open.bigmodel.cn/api/paas/v4",
        validation_alias="UPSTREAM_BASE_URL",
    )
    upstream_api_key: str = Field(default="", validation_alias="UPSTREAM_API_KEY")
    upstream_model: str = Field(default="glm-5.1", validation_alias="UPSTREAM_MODEL")
    providers_config_path: str = Field(
        default="config/providers.toml",
        validation_alias="PROVIDERS_CONFIG_PATH",
    )
    embedding_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="EMBEDDING_BASE_URL",
    )
    embedding_api_key: str = Field(default="", validation_alias="EMBEDDING_API_KEY")
    embedding_model: str = Field(
        default="text-embedding-v4",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(default=1024, validation_alias="EMBEDDING_DIMENSIONS")
    database_path: str = Field(default="data/memory.db", validation_alias="DATABASE_PATH")
    request_timeout_seconds: float = Field(
        default=60.0,
        validation_alias="REQUEST_TIMEOUT_SECONDS",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
