from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.routing import normalize_provider_priority


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
        default=50 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
        validation_alias="KNOWLEDGE_MAX_DOCUMENT_BYTES",
    )
    knowledge_embedding_batch_size: int = Field(
        default=20,
        ge=1,
        le=128,
        validation_alias="KNOWLEDGE_EMBEDDING_BATCH_SIZE",
    )
    knowledge_embedding_min_cosine: float = Field(
        default=0.25,
        ge=-1.0,
        le=1.0,
        validation_alias="KNOWLEDGE_EMBEDDING_MIN_COSINE",
    )
    knowledge_hybrid_vector_weight: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        validation_alias="KNOWLEDGE_HYBRID_VECTOR_WEIGHT",
    )
    llm_deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices(
            "LLM_DEEPSEEK_BASE_URL",
            "KNOWLEDGE_AGENT_BASE_URL",
        ),
    )
    llm_deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_DEEPSEEK_API_KEY",
            "KNOWLEDGE_AGENT_API_KEY",
        ),
    )
    llm_deepseek_flash_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices(
            "LLM_DEEPSEEK_FLASH_MODEL",
            "KNOWLEDGE_AGENT_FLASH_MODEL",
        ),
    )
    llm_deepseek_pro_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias=AliasChoices(
            "LLM_DEEPSEEK_PRO_MODEL",
            "KNOWLEDGE_AGENT_PRO_MODEL",
        ),
    )
    llm_provider_priority: str = Field(
        default="D",
        validation_alias=AliasChoices(
            "LLM_PROVIDER_PRIORITY",
            "KNOWLEDGE_AGENT_PROVIDER_PRIORITY",
        ),
    )
    llm_mimo_base_url: str = Field(
        default="https://api.xiaomimimo.com/v1",
        validation_alias=AliasChoices(
            "LLM_MIMO_BASE_URL",
            "KNOWLEDGE_AGENT_MIMO_BASE_URL",
        ),
    )
    llm_mimo_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_MIMO_API_KEY",
            "KNOWLEDGE_AGENT_MIMO_API_KEY",
        ),
    )
    llm_mimo_model: str = Field(
        default="mimo-v2.5-pro-ultraspeed",
        validation_alias=AliasChoices(
            "LLM_MIMO_MODEL",
            "KNOWLEDGE_AGENT_MIMO_MODEL",
        ),
    )
    llm_kimi_base_url: str = Field(
        default="https://api.moonshot.cn/v1",
        validation_alias=AliasChoices(
            "LLM_KIMI_BASE_URL",
            "KNOWLEDGE_AGENT_KIMI_BASE_URL",
        ),
    )
    llm_kimi_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_KIMI_API_KEY",
            "KNOWLEDGE_AGENT_KIMI_API_KEY",
        ),
    )
    llm_kimi_model: str = Field(
        default="kimi-k2.7-code",
        validation_alias=AliasChoices(
            "LLM_KIMI_MODEL",
            "KNOWLEDGE_AGENT_KIMI_MODEL",
        ),
    )
    llm_rate_limit_cooldown_seconds: float = Field(
        default=300.0,
        ge=1.0,
        le=3600.0,
        validation_alias=AliasChoices(
            "LLM_RATE_LIMIT_COOLDOWN_SECONDS",
            "KNOWLEDGE_AGENT_RATE_LIMIT_COOLDOWN_SECONDS",
        ),
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

    @field_validator("llm_provider_priority")
    @classmethod
    def _validate_llm_provider_priority(cls, value: str) -> str:
        return normalize_provider_priority(value)

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
