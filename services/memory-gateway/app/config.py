from functools import lru_cache
import hmac
from io import StringIO
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Literal, Self
from urllib.parse import urlsplit

from dotenv import dotenv_values
from model_gateway_contracts import (
    KNOWLEDGE_FAST_ROUTE,
    KNOWLEDGE_PRO_ROUTE,
    MEMORY_CHAT_ROUTE,
    MEMORY_COMPACT_ROUTE,
    MEMORY_CORE_ROUTE,
    MEMORY_EMBEDDING_ROUTE,
    MEMORY_EXTRACT_ROUTE,
    MEMORY_REVIEW_ROUTE,
)
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PRIVATE_MODEL_GATEWAY_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)
_SETTINGS_FILE_MAX_BYTES = 1024 * 1024
_SETTINGS_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 一次性 console 登录 code 交换允许的来源网段：回环 + 既有 RFC1918/ULA 私网。
# Docker 发布端口（如 -p 127.0.0.1:2026:2026）下，宿主机浏览器的连接经
# docker 代理进入容器后 source 变为网桥地址（如 172.17.0.1）而非回环，且
# 无法与同一网桥上的其他容器区分；因此调用方还必须要求请求目标 Host 为
# localhost/回环（见 app/auth/middleware.py 的本机判定与依据注释）。
_LOCAL_CONSOLE_LOGIN_SOURCE_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    *_PRIVATE_MODEL_GATEWAY_NETWORKS,
)

def is_local_console_login_source(source_host: str) -> bool:
    """Whether a client source address may attempt a console login exchange."""
    try:
        address = ipaddress.ip_address(source_host)
    except ValueError:
        return False
    return any(
        address in network for network in _LOCAL_CONSOLE_LOGIN_SOURCE_NETWORKS
    )

def _is_safe_private_model_gateway_host(hostname: str) -> bool:
    if hostname == "model-gateway":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return any(address in network for network in _PRIVATE_MODEL_GATEWAY_NETWORKS)

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
    gateway_api_key: str = Field(
        default="",
        validation_alias="GATEWAY_API_KEY",
        repr=False,
    )
    gateway_signing_secret: str = Field(
        default="",
        validation_alias="GATEWAY_SIGNING_SECRET",
        repr=False,
    )
    gateway_legacy_api_key_enabled: bool = Field(
        default=True,
        validation_alias="GATEWAY_LEGACY_API_KEY_ENABLED",
    )
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
        default=16 * 1024 * 1024,
        ge=1024,
        le=16 * 1024 * 1024,
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
    model_gateway_base_url: str = Field(
        default="",
        validation_alias="MODEL_GATEWAY_BASE_URL",
    )
    model_gateway_allow_private_http: bool = Field(
        default=False,
        validation_alias="MODEL_GATEWAY_ALLOW_PRIVATE_HTTP",
    )
    model_gateway_api_key: str = Field(
        default="",
        validation_alias="MODEL_GATEWAY_API_KEY",
    )
    model_gateway_chat_model: str = Field(
        default=MEMORY_CHAT_ROUTE,
        validation_alias="MODEL_GATEWAY_CHAT_MODEL",
    )
    model_gateway_memory_extract_model: str = Field(
        default=MEMORY_EXTRACT_ROUTE,
        validation_alias="MODEL_GATEWAY_MEMORY_EXTRACT_MODEL",
    )
    model_gateway_memory_compact_model: str = Field(
        default=MEMORY_COMPACT_ROUTE,
        validation_alias="MODEL_GATEWAY_MEMORY_COMPACT_MODEL",
    )
    model_gateway_memory_core_model: str = Field(
        default=MEMORY_CORE_ROUTE,
        validation_alias="MODEL_GATEWAY_MEMORY_CORE_MODEL",
    )
    model_gateway_memory_review_model: str = Field(
        default=MEMORY_REVIEW_ROUTE,
        validation_alias="MODEL_GATEWAY_MEMORY_REVIEW_MODEL",
    )
    model_gateway_knowledge_fast_model: str = Field(
        default=KNOWLEDGE_FAST_ROUTE,
        validation_alias="MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL",
    )
    model_gateway_knowledge_pro_model: str = Field(
        default=KNOWLEDGE_PRO_ROUTE,
        validation_alias="MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL",
    )
    model_gateway_embedding_model: str = Field(
        default=MEMORY_EMBEDDING_ROUTE,
        validation_alias="MODEL_GATEWAY_EMBEDDING_MODEL",
    )
    model_gateway_embedding_space_id: str = Field(
        default="",
        validation_alias="MODEL_GATEWAY_EMBEDDING_SPACE_ID",
    )
    embedding_dimensions: int = Field(default=1024, validation_alias="EMBEDDING_DIMENSIONS")
    database_path: str = Field(default="data/memory.db", validation_alias="DATABASE_PATH")
    auth_database_path: str = Field(
        default="data/auth.db",
        validation_alias="AUTH_DATABASE_PATH",
    )
    knowledge_database_path: str = Field(
        default="data/knowledge.db",
        validation_alias="KNOWLEDGE_DATABASE_PATH",
    )
    disk_soft_reserve_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        le=1024 * 1024 * 1024 * 1024,
        validation_alias="DISK_SOFT_RESERVE_BYTES",
    )
    disk_hard_reserve_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=0,
        le=1024 * 1024 * 1024 * 1024,
        validation_alias="DISK_HARD_RESERVE_BYTES",
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

    @field_validator("model_gateway_base_url")
    @classmethod
    def _validate_model_gateway_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() or ord(character) < 32 for character in normalized)
        ):
            raise ValueError("MODEL_GATEWAY_BASE_URL 不是安全的服务 URL")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("MODEL_GATEWAY_BASE_URL 不是安全的服务 URL") from exc
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

    @field_validator("gateway_api_key")
    @classmethod
    def _validate_gateway_api_key(cls, value: str) -> str:
        if len(value) > 4096 or any(character in value for character in "\r\n\x00"):
            raise ValueError("GATEWAY_API_KEY 格式无效")
        return value

    @field_validator("gateway_signing_secret")
    @classmethod
    def _validate_gateway_signing_secret(cls, value: str) -> str:
        if not value:
            return ""
        if (
            len(value) < 32
            or len(value) > 4096
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError(
                "GATEWAY_SIGNING_SECRET 必须是至少 32 个字符且不含空白的随机密钥"
            )
        if value.startswith("mgw_"):
            raise ValueError("GATEWAY_SIGNING_SECRET 不得使用 scoped access token")
        return value

    @model_validator(mode="after")
    def _validate_model_gateway_credentials(self) -> Self:
        has_base_url = bool(self.model_gateway_base_url.strip())
        has_api_key = bool(self.model_gateway_api_key.strip())
        if has_base_url != has_api_key:
            raise ValueError(
                "MODEL_GATEWAY_BASE_URL 和 MODEL_GATEWAY_API_KEY 必须同时配置"
            )
        if has_base_url:
            parsed = urlsplit(self.model_gateway_base_url)
            hostname = str(parsed.hostname or "").lower()
            loopback = hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.scheme == "http" and not loopback:
                if not self.model_gateway_allow_private_http:
                    raise ValueError(
                        "非回环 HTTP Model Gateway 必须显式设置 "
                        "MODEL_GATEWAY_ALLOW_PRIVATE_HTTP=true"
                    )
                if not _is_safe_private_model_gateway_host(hostname):
                    raise ValueError(
                        "MODEL_GATEWAY_ALLOW_PRIVATE_HTTP 仅允许 RFC1918/ULA 私网地址"
                        "或精确 Docker 服务名 model-gateway"
                    )
        if self.gateway_signing_secret and hmac.compare_digest(
            self.gateway_signing_secret.encode("utf-8"),
            self.gateway_api_key.encode("utf-8"),
        ):
            raise ValueError("GATEWAY_SIGNING_SECRET 必须与 GATEWAY_API_KEY 独立")
        if self.gateway_signing_secret and hmac.compare_digest(
            self.gateway_signing_secret.encode("utf-8"),
            self.model_gateway_api_key.encode("utf-8"),
        ):
            raise ValueError(
                "GATEWAY_SIGNING_SECRET 必须与 MODEL_GATEWAY_API_KEY 独立"
            )
        return self

    @model_validator(mode="after")
    def _validate_disk_reserves(self) -> Self:
        if self.disk_soft_reserve_bytes < self.disk_hard_reserve_bytes:
            raise ValueError(
                "DISK_SOFT_RESERVE_BYTES 必须大于或等于 DISK_HARD_RESERVE_BYTES"
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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

@lru_cache
def get_settings() -> Settings:
    settings_path = os.getenv("MEMGW_SETTINGS_PATH", "").strip()
    if not settings_path:
        return Settings()
    return Settings.model_validate(_private_settings_values(Path(settings_path)))

def _private_settings_values(path: Path) -> dict[str, str]:
    """Read a private settings file without exporting its secrets.

    Long-lived split containers point ``MEMGW_SETTINGS_PATH`` at a mode-0600
    file. Secret-looking process environment variables are deliberately
    ignored in this mode; non-secret environment values remain useful for
    operational overrides such as a port or feature flag.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("MEMGW_SETTINGS_PATH 无法安全读取") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("MEMGW_SETTINGS_PATH 必须是普通文件")
        if os.name == "posix":
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError("MEMGW_SETTINGS_PATH 权限必须精确为 0600")
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise ValueError("MEMGW_SETTINGS_PATH 必须由当前服务用户持有")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, _SETTINGS_FILE_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _SETTINGS_FILE_MAX_BYTES:
                raise ValueError("MEMGW_SETTINGS_PATH 超过 1 MiB 安全上限")
    finally:
        os.close(descriptor)
    try:
        content = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("MEMGW_SETTINGS_PATH 必须使用 UTF-8") from exc
    parsed = dotenv_values(stream=StringIO(content), interpolate=False)
    values = {
        str(name): str(value)
        for name, value in parsed.items()
        if value is not None and _SETTINGS_NAME_RE.fullmatch(str(name))
    }
    for name, value in os.environ.items():
        if _SETTINGS_NAME_RE.fullmatch(name) and not _is_secret_setting_name(name):
            values[name] = value
    return values

def _is_secret_setting_name(name: str) -> bool:
    return name.upper().endswith(
        ("_API_KEY", "_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_CREDENTIALS")
    )
