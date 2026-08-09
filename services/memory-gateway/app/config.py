from functools import lru_cache
import json
import math
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.routing import normalize_provider_priority


def describe_settings_error(exc: Exception) -> str:
    """Render a Settings validation error without embedding submitted values."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "Settings"
            parts.append(f"{location}: {error.get('msg', '配置无效')}")
        return "；".join(parts) or "配置无效"
    return str(exc)


class Settings(BaseSettings):
    gateway_api_key: str = Field(default="", validation_alias="GATEWAY_API_KEY")
    gateway_user_id: str = Field(default="default", validation_alias="GATEWAY_USER_ID")
    gateway_allow_user_id_header: bool = Field(
        default=False,
        validation_alias="GATEWAY_ALLOW_USER_ID_HEADER",
    )
    chat_gateway_enabled: bool = Field(
        default=True,
        validation_alias="CHAT_GATEWAY_ENABLED",
    )
    chat_gateway_default_memory_mode: Literal["off", "read", "read-write"] = Field(
        default="read-write",
        validation_alias="CHAT_GATEWAY_DEFAULT_MEMORY_MODE",
    )
    chat_gateway_search_limit: int = Field(
        default=8,
        ge=1,
        le=20,
        validation_alias="CHAT_GATEWAY_SEARCH_LIMIT",
    )
    chat_gateway_context_max_chars: int = Field(
        default=12000,
        ge=1000,
        le=100000,
        validation_alias="CHAT_GATEWAY_CONTEXT_MAX_CHARS",
    )
    chat_gateway_recall_timeout_seconds: float = Field(
        default=4.0,
        ge=0.25,
        le=30.0,
        validation_alias="CHAT_GATEWAY_RECALL_TIMEOUT_SECONDS",
    )
    chat_gateway_stream_read_timeout_seconds: float = Field(
        default=600.0,
        ge=30.0,
        le=3600.0,
        validation_alias="CHAT_GATEWAY_STREAM_READ_TIMEOUT_SECONDS",
    )
    chat_gateway_stream_write_timeout_seconds: float = Field(
        default=120.0,
        ge=30.0,
        le=3600.0,
        validation_alias="CHAT_GATEWAY_STREAM_WRITE_TIMEOUT_SECONDS",
    )
    chat_gateway_max_request_body_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
        validation_alias="CHAT_GATEWAY_MAX_REQUEST_BODY_BYTES",
    )
    chat_gateway_turn_ttl_seconds: float = Field(
        default=3600.0,
        ge=30.0,
        le=86400.0,
        validation_alias="CHAT_GATEWAY_TURN_TTL_SECONDS",
    )
    chat_gateway_extraction_context_turns: int = Field(
        default=2,
        ge=1,
        le=6,
        validation_alias="CHAT_GATEWAY_EXTRACTION_CONTEXT_TURNS",
    )
    chat_gateway_extraction_context_max_chars: int = Field(
        default=8000,
        ge=1000,
        le=50000,
        validation_alias="CHAT_GATEWAY_EXTRACTION_CONTEXT_MAX_CHARS",
    )
    chat_gateway_context_compact_after_turns: int = Field(
        default=8,
        ge=3,
        le=50,
        validation_alias="CHAT_GATEWAY_CONTEXT_COMPACT_AFTER_TURNS",
    )
    chat_gateway_context_compact_after_chars: int = Field(
        default=6000,
        ge=1000,
        le=100000,
        validation_alias="CHAT_GATEWAY_CONTEXT_COMPACT_AFTER_CHARS",
    )
    chat_gateway_compacted_summary_max_chars: int = Field(
        default=4000,
        ge=500,
        le=20000,
        validation_alias="CHAT_GATEWAY_COMPACTED_SUMMARY_MAX_CHARS",
    )
    upstream_base_url: str = Field(
        default="https://open.bigmodel.cn/api/paas/v4",
        validation_alias="UPSTREAM_BASE_URL",
    )
    upstream_api_key: str = Field(default="", validation_alias="UPSTREAM_API_KEY")
    upstream_model: str = Field(default="glm-5.1", validation_alias="UPSTREAM_MODEL")
    model_gateway_base_url: str = Field(
        default="",
        validation_alias="MODEL_GATEWAY_BASE_URL",
    )
    model_gateway_api_key: str = Field(
        default="",
        validation_alias="MODEL_GATEWAY_API_KEY",
    )
    model_gateway_chat_model: str = Field(
        default="memory.chat",
        validation_alias="MODEL_GATEWAY_CHAT_MODEL",
    )
    model_gateway_memory_extract_model: str = Field(
        default="memory.extract",
        validation_alias="MODEL_GATEWAY_MEMORY_EXTRACT_MODEL",
    )
    model_gateway_memory_compact_model: str = Field(
        default="memory.compact",
        validation_alias="MODEL_GATEWAY_MEMORY_COMPACT_MODEL",
    )
    model_gateway_memory_core_model: str = Field(
        default="memory.core",
        validation_alias="MODEL_GATEWAY_MEMORY_CORE_MODEL",
    )
    model_gateway_memory_review_model: str = Field(
        default="memory.review",
        validation_alias="MODEL_GATEWAY_MEMORY_REVIEW_MODEL",
    )
    model_gateway_knowledge_fast_model: str = Field(
        default="knowledge.fast",
        validation_alias="MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL",
    )
    model_gateway_knowledge_pro_model: str = Field(
        default="knowledge.pro",
        validation_alias="MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL",
    )
    model_gateway_embedding_model: str = Field(
        default="memory.embedding",
        validation_alias="MODEL_GATEWAY_EMBEDDING_MODEL",
    )
    model_gateway_embedding_space_id: str = Field(
        default="",
        validation_alias="MODEL_GATEWAY_EMBEDDING_SPACE_ID",
    )
    embedding_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="EMBEDDING_BASE_URL",
    )
    embedding_api_key: str = Field(default="", validation_alias="EMBEDDING_API_KEY")
    embedding_model: str = Field(
        default="qwen3.7-text-embedding",
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
    model_catalog_path: str = Field(
        default="",
        validation_alias="MODEL_CATALOG_PATH",
    )
    model_routes_path: str = Field(
        default="",
        validation_alias="MODEL_ROUTES_PATH",
    )
    # New two-level provider configuration; consumers are still being migrated.
    providers_path: str = Field(
        default="",
        validation_alias="PROVIDERS_PATH",
    )
    routes_path: str = Field(
        default="",
        validation_alias="ROUTES_PATH",
    )
    pricing_catalog_path: str = Field(
        default="",
        validation_alias="PRICING_CATALOG_PATH",
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
    ui_dist_dir: str = Field(default="", validation_alias="UI_DIST_DIR")
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

    @field_validator("model_gateway_base_url")
    @classmethod
    def _validate_model_gateway_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MODEL_GATEWAY_BASE_URL 不是安全的服务 URL")
        loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError(
                "MODEL_GATEWAY_BASE_URL 仅允许 HTTPS，或本机回环地址上的 HTTP"
            )
        return normalized

    @field_validator(
        "model_gateway_chat_model",
        "model_gateway_memory_extract_model",
        "model_gateway_memory_compact_model",
        "model_gateway_memory_core_model",
        "model_gateway_memory_review_model",
        "model_gateway_knowledge_fast_model",
        "model_gateway_knowledge_pro_model",
        "model_gateway_embedding_model",
    )
    @classmethod
    def _validate_model_gateway_route_alias(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", normalized):
            raise ValueError("Model Gateway route 必须是安全的稳定 ID")
        return normalized

    @field_validator("model_gateway_embedding_space_id")
    @classmethod
    def _validate_model_gateway_embedding_space_id(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 300 or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        ):
            raise ValueError("MODEL_GATEWAY_EMBEDDING_SPACE_ID 格式无效")
        return normalized

    @field_validator("model_gateway_api_key")
    @classmethod
    def _validate_model_gateway_api_key(cls, value: str) -> str:
        if len(value) > 4096 or any(character in value for character in "\r\n\x00"):
            raise ValueError("MODEL_GATEWAY_API_KEY 格式无效")
        return value

    @model_validator(mode="after")
    def _validate_model_gateway_credentials(self) -> Self:
        has_base_url = bool(self.model_gateway_base_url.strip())
        has_api_key = bool(self.model_gateway_api_key.strip())
        if has_base_url != has_api_key:
            raise ValueError(
                "MODEL_GATEWAY_BASE_URL 和 MODEL_GATEWAY_API_KEY 必须同时配置"
            )
        return self

    @property
    def model_gateway_enabled(self) -> bool:
        return bool(
            self.model_gateway_base_url.strip()
            and self.model_gateway_api_key.strip()
        )

    # 衰减引擎 (Ebbinghaus)
    decay_lambda_default: float = Field(
        default=0.02, ge=0.0, le=10.0, validation_alias="DECAY_LAMBDA_DEFAULT"
    )
    decay_alpha_default: float = Field(
        default=0.3, ge=0.0, le=2.0, validation_alias="DECAY_ALPHA_DEFAULT"
    )
    decay_short_term_days: int = Field(
        default=3, ge=0, le=36500, validation_alias="DECAY_SHORT_TERM_DAYS"
    )
    decay_short_term_time_weight: float = Field(
        default=0.7, ge=0.0, le=1.0, validation_alias="DECAY_SHORT_TERM_TIME_WEIGHT"
    )
    decay_long_term_emotion_weight: float = Field(
        default=0.7, ge=0.0, le=1.0, validation_alias="DECAY_LONG_TERM_EMOTION_WEIGHT"
    )
    decay_resolved_factor: float = Field(
        default=0.05, ge=0.0, le=1.0, validation_alias="DECAY_RESOLVED_FACTOR"
    )
    decay_digested_factor: float = Field(
        default=0.02, ge=0.0, le=1.0, validation_alias="DECAY_DIGESTED_FACTOR"
    )
    decay_sector_lambda_map: str = Field(
        default=(
            '{"emotional":0.01,"reflective":0.01,'
            '"semantic":0.02,"procedural":0.02,"episodic":0.03}'
        ),
        validation_alias="DECAY_SECTOR_LAMBDA_MAP",
    )

    @field_validator("decay_sector_lambda_map")
    @classmethod
    def _validate_decay_sector_lambda_map(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("DECAY_SECTOR_LAMBDA_MAP must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("DECAY_SECTOR_LAMBDA_MAP must be a JSON object")
        for sector, raw_lambda in parsed.items():
            if not isinstance(sector, str) or not sector.strip():
                raise ValueError("decay sector names must be non-empty strings")
            if (
                isinstance(raw_lambda, bool)
                or not isinstance(raw_lambda, (int, float))
                or not math.isfinite(float(raw_lambda))
                or not 0.0 <= float(raw_lambda) <= 10.0
            ):
                raise ValueError(
                    "decay sector lambda values must be finite numbers between 0 and 10"
                )
        return value
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
