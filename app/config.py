from functools import lru_cache
from typing import Literal

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
    knowledge_database_path: str = Field(
        default="data/knowledge.db",
        validation_alias="KNOWLEDGE_DATABASE_PATH",
    )
    knowledge_max_document_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
        validation_alias="KNOWLEDGE_MAX_DOCUMENT_BYTES",
    )
    knowledge_agent_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="KNOWLEDGE_AGENT_BASE_URL",
    )
    knowledge_agent_api_key: str = Field(
        default="",
        validation_alias="KNOWLEDGE_AGENT_API_KEY",
    )
    knowledge_agent_flash_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="KNOWLEDGE_AGENT_FLASH_MODEL",
    )
    knowledge_agent_pro_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias="KNOWLEDGE_AGENT_PRO_MODEL",
    )
    knowledge_agent_egress_policy: Literal["none", "normal", "all"] = Field(
        default="none",
        validation_alias="KNOWLEDGE_AGENT_EGRESS_POLICY",
    )
    knowledge_agent_timeout_seconds: float = Field(
        default=25.0,
        ge=1.0,
        le=120.0,
        validation_alias="KNOWLEDGE_AGENT_TIMEOUT_SECONDS",
    )
    eval_dir: str = Field(default="eval", validation_alias="EVAL_DIR")
    request_timeout_seconds: float = Field(
        default=60.0,
        validation_alias="REQUEST_TIMEOUT_SECONDS",
    )
    allow_sensitive_egress: bool = Field(
        default=False,
        validation_alias="ALLOW_SENSITIVE_EGRESS",
    )

    # 衰减引擎 (Ebbinghaus)
    decay_lambda_default: float = Field(default=0.02, validation_alias="DECAY_LAMBDA_DEFAULT")
    decay_alpha_default: float = Field(default=0.3, validation_alias="DECAY_ALPHA_DEFAULT")
    decay_short_term_days: int = Field(default=3, validation_alias="DECAY_SHORT_TERM_DAYS")
    decay_short_term_time_weight: float = Field(
        default=0.7, validation_alias="DECAY_SHORT_TERM_TIME_WEIGHT"
    )
    decay_long_term_emotion_weight: float = Field(
        default=0.7, validation_alias="DECAY_LONG_TERM_EMOTION_WEIGHT"
    )
    decay_resolved_factor: float = Field(default=0.05, validation_alias="DECAY_RESOLVED_FACTOR")
    decay_digested_factor: float = Field(default=0.02, validation_alias="DECAY_DIGESTED_FACTOR")
    decay_sector_lambda_map: str = Field(
        default=(
            '{"emotional":0.01,"reflective":0.01,'
            '"semantic":0.02,"procedural":0.02,"episodic":0.03}'
        ),
        validation_alias="DECAY_SECTOR_LAMBDA_MAP",
    )
    time_ripple_delta: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        validation_alias="TIME_RIPPLE_DELTA",
    )
    time_ripple_window_hours: int = Field(
        default=48,
        ge=1,
        le=720,
        validation_alias="TIME_RIPPLE_WINDOW_HOURS",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
