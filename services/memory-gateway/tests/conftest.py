from collections.abc import Iterator
import atexit
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
from typing import Any


_RUNTIME_ENVIRONMENT_EXACT = {
    "ALLOW_SENSITIVE_EGRESS",
    "ALL_PROXY",
    "AUTH_DATABASE_PATH",
    "DATABASE_PATH",
    "EVAL_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "KNOWLEDGE_DATABASE_PATH",
    "MEMGW_HOME",
    "MEMGW_PROJECT_ROOT",
    "MEMGW_SETTINGS_PATH",
    "MEMORY_CONSOLE_ADMIN_KEY",
    "MODEL_CATALOG_PATH",
    "MODEL_GATEWAY_CONFIG_PATH",
    "MODEL_GATEWAY_HOME",
    "MODEL_GATEWAY_SECRETS_PATH",
    "MODEL_GATEWAY_USAGE_DATABASE_PATH",
    "MODEL_ROUTES_PATH",
    "NO_PROXY",
    "PRICING_CATALOG_PATH",
    "PROVIDERS_PATH",
    "ROUTES_PATH",
    "USAGE_DATABASE_PATH",
}
_RUNTIME_ENVIRONMENT_PREFIXES = (
    "EMBEDDING_",
    "GATEWAY_",
    "KNOWLEDGE_AGENT_",
    "LLM_",
    "MEMGW_",
    "MODEL_GATEWAY_",
    "PROVIDER_",
    "UPSTREAM_",
)


def _is_memory_runtime_environment(name: str) -> bool:
    return name in _RUNTIME_ENVIRONMENT_EXACT or name.startswith(
        _RUNTIME_ENVIRONMENT_PREFIXES
    )


_SESSION_ORIGINAL_ENVIRONMENT = {
    name: value
    for name, value in os.environ.items()
    if _is_memory_runtime_environment(name)
}
_SESSION_RUNTIME_ROOT = Path(
    tempfile.mkdtemp(prefix="memory-platform-pytest-session-")
)
os.chmod(_SESSION_RUNTIME_ROOT, 0o700)
_SESSION_MEMORY_HOME = _SESSION_RUNTIME_ROOT / "memgw-home"
_SESSION_MODEL_HOME = _SESSION_RUNTIME_ROOT / "modelgw-home"
_SESSION_SETTINGS = _SESSION_RUNTIME_ROOT / "memory-secrets" / "settings.env"
for directory in (
    _SESSION_MEMORY_HOME,
    _SESSION_MODEL_HOME,
    _SESSION_SETTINGS.parent,
    _SESSION_RUNTIME_ROOT / "model-secrets",
):
    directory.mkdir(mode=0o700)
settings_descriptor = os.open(
    _SESSION_SETTINGS,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    os.write(
        settings_descriptor,
        (
            "GATEWAY_SIGNING_SECRET='pytest-only-signing-secret-32-bytes-minimum'\n"
            "GATEWAY_LEGACY_API_KEY_ENABLED='true'\n"
        ).encode("ascii"),
    )
    os.fsync(settings_descriptor)
finally:
    os.close(settings_descriptor)
if stat.S_IMODE(_SESSION_SETTINGS.stat().st_mode) != 0o600:
    raise RuntimeError("pytest session settings mode is unsafe")
for name in list(os.environ):
    if _is_memory_runtime_environment(name):
        os.environ.pop(name, None)
os.environ.update(
    {
        "MEMGW_HOME": str(_SESSION_MEMORY_HOME),
        "MEMGW_SETTINGS_PATH": str(_SESSION_SETTINGS),
        "MEMGW_PROJECT_ROOT": str(Path(__file__).resolve().parents[1]),
        "MODEL_GATEWAY_HOME": str(_SESSION_MODEL_HOME),
        "MODEL_GATEWAY_CONFIG_PATH": str(_SESSION_MODEL_HOME / "config.json"),
        "MODEL_GATEWAY_SECRETS_PATH": str(
            _SESSION_RUNTIME_ROOT / "model-secrets" / "secrets.env"
        ),
        "DATABASE_PATH": str(_SESSION_RUNTIME_ROOT / "memory.db"),
        "AUTH_DATABASE_PATH": str(_SESSION_RUNTIME_ROOT / "auth.db"),
        "KNOWLEDGE_DATABASE_PATH": str(_SESSION_RUNTIME_ROOT / "knowledge.db"),
        "EVAL_DIR": str(_SESSION_RUNTIME_ROOT / "eval"),
        "GATEWAY_API_KEY": "",
        "MODEL_GATEWAY_API_KEY": "",
        "MODEL_GATEWAY_BASE_URL": "",
        "KNOWLEDGE_AGENT_EGRESS_POLICY": "none",
        "ALLOW_SENSITIVE_EGRESS": "false",
    }
)
_SESSION_ENVIRONMENT_RESTORED = False


def _restore_session_environment() -> None:
    global _SESSION_ENVIRONMENT_RESTORED
    if _SESSION_ENVIRONMENT_RESTORED:
        return
    _SESSION_ENVIRONMENT_RESTORED = True
    for name in list(os.environ):
        if _is_memory_runtime_environment(name):
            os.environ.pop(name, None)
    os.environ.update(_SESSION_ORIGINAL_ENVIRONMENT)
    shutil.rmtree(_SESSION_RUNTIME_ROOT, ignore_errors=True)


atexit.register(_restore_session_environment)

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings

# Test processes must never consult the repository's gitignored ``.env``.
# Keep the production class untouched outside pytest and preserve every other
# pydantic-settings source and precedence rule.
_ORIGINAL_SETTINGS_MODEL_CONFIG = dict(Settings.model_config)
Settings.model_config = {**Settings.model_config, "env_file": None}

from app.api import deps
from app.api.chat_gateway import clear_chat_gateway_state
from app.knowledge.store import KnowledgeStore
from app.main import create_app
from app.memory.search import NullEmbeddingClient
from app.memory.store import MemoryStore
from app.openai_compat.gateway_client import GatewayHTTPResult
from app.openai_compat.gateway_client import GatewayUpstreamHTTPError


def pytest_unconfigure(config) -> None:
    del config
    Settings.model_config = _ORIGINAL_SETTINGS_MODEL_CONFIG
    _restore_session_environment()


@pytest.fixture(autouse=True)
def isolate_test_runtime(tmp_path) -> Iterator[None]:
    """Keep every test away from developer secrets and repository state.

    Settings also reads the local ``.env`` file, while the provider catalog
    resolves ``PROVIDER_*_API_KEY`` directly from ``os.environ``. Explicit
    empty environment values therefore form the test boundary: no test may
    accidentally discover a real provider or initialize the default databases.
    """

    original_environment = {
        name: value
        for name, value in os.environ.items()
        if _is_memory_runtime_environment(name)
    }
    sandbox = pytest.MonkeyPatch()
    for name in original_environment:
        sandbox.setenv(name, "")

    memory_home = tmp_path / "memgw-home"
    model_home = tmp_path / "modelgw-home"
    model_secrets = tmp_path / "model-secrets" / "secrets.env"
    controlled = {
        "MEMGW_HOME": str(memory_home),
        # Settings remains in normal environment mode so explicit per-test
        # monkeypatch overrides retain their real precedence semantics. Its
        # pytest-only model config disables only the repository ``.env``.
        "MEMGW_SETTINGS_PATH": "",
        "MEMGW_PROJECT_ROOT": str(Path(__file__).resolve().parents[1]),
        "MODEL_GATEWAY_HOME": str(model_home),
        "MODEL_GATEWAY_CONFIG_PATH": str(model_home / "config.json"),
        "MODEL_GATEWAY_SECRETS_PATH": str(model_secrets),
        "MODEL_GATEWAY_USAGE_DATABASE_PATH": str(model_home / "usage.db"),
        "DATABASE_PATH": str(tmp_path / "runtime-memory.db"),
        "AUTH_DATABASE_PATH": str(tmp_path / "runtime-auth.db"),
        "KNOWLEDGE_DATABASE_PATH": str(tmp_path / "runtime-knowledge.db"),
        "USAGE_DATABASE_PATH": str(tmp_path / "runtime-usage.db"),
        "EVAL_DIR": str(tmp_path / "eval"),
        "ALLOW_SENSITIVE_EGRESS": "false",
        "KNOWLEDGE_AGENT_EGRESS_POLICY": "none",
        "GATEWAY_SIGNING_SECRET": "pytest-only-signing-secret-32-bytes-minimum",
        "GATEWAY_LEGACY_API_KEY_ENABLED": "true",
        # Memory Gateway only supports Model Gateway. Tests that need the
        # unconfigured state must explicitly clear both fields.
        "MODEL_GATEWAY_BASE_URL": "http://127.0.0.1:2030/v1",
        "MODEL_GATEWAY_API_KEY": "pytest-central-backend-key",
        "MODEL_GATEWAY_EMBEDDING_SPACE_ID": "",
    }
    for name in (
        "GATEWAY_API_KEY",
        "UPSTREAM_API_KEY",
        "EMBEDDING_API_KEY",
        "LLM_DEEPSEEK_API_KEY",
        "LLM_MIMO_API_KEY",
        "LLM_KIMI_API_KEY",
        "KNOWLEDGE_AGENT_API_KEY",
        "KNOWLEDGE_AGENT_MIMO_API_KEY",
        "KNOWLEDGE_AGENT_KIMI_API_KEY",
        "MEMORY_CONSOLE_ADMIN_KEY",
    ):
        controlled[name] = ""
    for name, value in controlled.items():
        sandbox.setenv(name, value)

    get_settings.cache_clear()

    try:
        yield
    finally:
        get_settings.cache_clear()
        sandbox.undo()
        # Direct os.environ writes and a test-local monkeypatch.undo() cannot
        # escape this independent sandbox's exact teardown restoration.
        for name in list(os.environ):
            if _is_memory_runtime_environment(name) and name not in original_environment:
                os.environ.pop(name, None)
        for name, value in original_environment.items():
            os.environ[name] = value
        get_settings.cache_clear()


class FakeLLMClient:
    """伪装普通聊天、记忆提取、核心记忆整理三类上游调用。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.extraction_messages: list[dict] = []
        self.extraction_calls = 0
        self.extraction_request = None
        self.extraction_thinking: str | None = None
        self.core_messages: list[dict] = []
        self.core_request = None
        self.core_thinking: str | None = None
        self.core_structured_tool: dict | None = None
        self.core_tool_arguments: str | None = None
        self.context_compaction_messages: list[dict] = []
        self.context_compaction_calls = 0
        self.context_compaction_request = None
        self.context_compaction_content = json.dumps(
            {"summary": "较早对话的测试压缩摘要。"},
            ensure_ascii=False,
        )
        self.response = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-upstream",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "好的，我会参考这些信息。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        # 提取调用的默认回复：忽略，避免普通聊天测试意外写入记忆
        self.extraction_content = json.dumps(
            {
                "action": "ignore",
                "memory": "",
                "type": "semantic",
                "importance": 1,
                "confidence": 0.0,
                "reason": "测试默认不保存",
                "source_quote": "",
            },
            ensure_ascii=False,
        )
        self.core_content = json.dumps(
            {"sections": [], "reason": "测试默认不整理核心记忆"},
            ensure_ascii=False,
        )
        self.review_revision_messages: list[dict] = []
        self.review_revision_request = None
        self.review_revision_thinking: str | None = None
        self.review_revision_structured_tool: dict | None = None
        self.review_revision_tool_arguments: str | None = None
        self.review_revision_content = json.dumps(
            {"operations": [{"operation": "no_change", "reason": "测试默认不修改"}]},
            ensure_ascii=False,
        )

    async def create_chat_completion(
        self,
        request,
        messages: list[dict],
        *,
        thinking: str | None = None,
        structured_tool: dict | None = None,
    ) -> dict:
        if self._is_context_compaction_call(messages):
            self.context_compaction_calls += 1
            self.context_compaction_messages = messages
            self.context_compaction_request = request
            return {
                "id": "chatcmpl-context-compaction",
                "object": "chat.completion",
                "created": 0,
                "model": "test-upstream",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": self.context_compaction_content,
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        if self._is_extraction_call(messages):
            self.extraction_calls += 1
            self.extraction_messages = messages
            self.extraction_request = request
            self.extraction_thinking = thinking
            return {
                "id": "chatcmpl-extraction",
                "object": "chat.completion",
                "created": 0,
                "model": "test-upstream",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.extraction_content},
                        "finish_reason": "stop",
                    }
                ],
            }
        if self._is_core_consolidation_call(messages):
            self.core_messages = messages
            self.core_request = request
            self.core_thinking = thinking
            self.core_structured_tool = structured_tool
            message = {"role": "assistant", "content": self.core_content}
            if self.core_tool_arguments is not None:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "submit_core_memory_sections",
                                "arguments": self.core_tool_arguments,
                            },
                        }
                    ],
                }
            return {
                "id": "chatcmpl-core-memory",
                "object": "chat.completion",
                "created": 0,
                "model": "test-upstream",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop",
                    }
                ],
            }
        if self._is_review_revision_call(messages):
            self.review_revision_messages = messages
            self.review_revision_request = request
            self.review_revision_thinking = thinking
            self.review_revision_structured_tool = structured_tool
            message = {"role": "assistant", "content": self.review_revision_content}
            if self.review_revision_tool_arguments is not None:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "submit_memory_review_revision",
                                "arguments": self.review_revision_tool_arguments,
                            },
                        }
                    ],
                }
            return {
                "id": "chatcmpl-review-revision",
                "object": "chat.completion",
                "created": 0,
                "model": "test-upstream",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop",
                    }
                ],
            }
        self.messages = messages
        return self.response

    @staticmethod
    def _is_extraction_call(messages: list[dict]) -> bool:
        if not messages:
            return False
        first = messages[0]
        return first.get("role") == "system" and "记忆提取器" in first.get("content", "")

    @staticmethod
    def _is_context_compaction_call(messages: list[dict]) -> bool:
        if not messages:
            return False
        first = messages[0]
        return (
            first.get("role") == "system"
            and "会话上下文压缩器" in first.get("content", "")
        )

    @staticmethod
    def _is_core_consolidation_call(messages: list[dict]) -> bool:
        if not messages:
            return False
        first = messages[0]
        return first.get("role") == "system" and "核心记忆整理器" in first.get("content", "")

    @staticmethod
    def _is_review_revision_call(messages: list[dict]) -> bool:
        if not messages:
            return False
        first = messages[0]
        return first.get("role") == "system" and "记忆体检编辑器" in first.get("content", "")


class FakeGatewayStream:
    def __init__(self, chunks: list[bytes], *, provider: Any) -> None:
        self.chunks = chunks
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False
        self.provider = provider

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class FakeChatGatewayClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.stream_payloads: list[dict] = []
        self.preferred_provider_codes: list[str | None] = []
        self.stream_preferred_provider_codes: list[str | None] = []
        self.response = {
            "id": "chatcmpl-gateway-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-upstream",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "好的，我会参考这些信息。",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
        self.stream_chunks = [
            'data: {"id":"chatcmpl-stream","choices":[{"index":0,"delta":{"role":"assistant","content":"你好"},"finish_reason":null}]}\n\n'.encode(),
            b'data: {"id":"chatcmpl-stream","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n',
            b"data: [DONE]\n\n",
        ]
        self.last_stream: FakeGatewayStream | None = None
        self.error: GatewayUpstreamHTTPError | None = None
        self.provider = SimpleNamespace(
            code="D",
            base_url="https://upstream.invalid/v1",
            api_key="test",
            model="test-upstream",
        )

    def list_models(self) -> list[str]:
        return ["memory-auto", self.provider.model]

    async def complete(
        self,
        payload: dict,
        *,
        preferred_provider_code: str | None = None,
    ) -> GatewayHTTPResult:
        self.payloads.append(payload)
        self.preferred_provider_codes.append(preferred_provider_code)
        if self.error is not None:
            raise self.error
        return GatewayHTTPResult(
            content=json.dumps(self.response, ensure_ascii=False).encode("utf-8"),
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            provider=self.provider,
        )

    async def open_stream(
        self,
        payload: dict,
        *,
        preferred_provider_code: str | None = None,
    ) -> FakeGatewayStream:
        self.stream_payloads.append(payload)
        self.stream_preferred_provider_codes.append(preferred_provider_code)
        if self.error is not None:
            raise self.error
        self.last_stream = FakeGatewayStream(
            list(self.stream_chunks),
            provider=self.provider,
        )
        return self.last_stream


@pytest.fixture
def memory_store(tmp_path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    # 清除模块级搜索缓存，避免测试间残留
    from app.memory.search import _CACHE_METRICS, _EMBEDDING_CACHE, SEARCH_CACHE
    _EMBEDDING_CACHE.clear()
    SEARCH_CACHE.clear()
    _CACHE_METRICS.clear()
    return store


@pytest.fixture
def knowledge_store(tmp_path) -> KnowledgeStore:
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    store.init_db()
    return store


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def fake_gateway() -> FakeChatGatewayClient:
    return FakeChatGatewayClient()


@pytest.fixture
def client(
    monkeypatch,
    memory_store: MemoryStore,
    knowledge_store: KnowledgeStore,
    fake_llm: FakeLLMClient,
    fake_gateway: FakeChatGatewayClient,
) -> Iterator[TestClient]:
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    # Multi-user fixtures explicitly exercise the legacy migration mode.
    monkeypatch.setenv("GATEWAY_ALLOW_USER_ID_HEADER", "true")
    monkeypatch.setenv("DATABASE_PATH", memory_store.database_path)
    monkeypatch.setenv(
        "AUTH_DATABASE_PATH",
        str(Path(memory_store.database_path).with_name("auth.db")),
    )
    monkeypatch.setenv(
        "KNOWLEDGE_DATABASE_PATH",
        knowledge_store.database_path,
    )
    monkeypatch.setenv("KNOWLEDGE_AGENT_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_MIMO_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_KIMI_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_EGRESS_POLICY", "none")
    # Egress-blocking assertions must not depend on the developer's local .env.
    monkeypatch.setenv("ALLOW_SENSITIVE_EGRESS", "false")
    monkeypatch.setenv("EVAL_DIR", str(Path(memory_store.database_path).with_name("eval")))
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "http://127.0.0.1:2030/v1")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "pytest-central-backend-key")
    monkeypatch.setenv("MODEL_GATEWAY_EMBEDDING_SPACE_ID", "")
    monkeypatch.setenv("TIME_RIPPLE_DELTA", "0.0")
    monkeypatch.setenv("TIME_RIPPLE_WINDOW_HOURS", "48")
    monkeypatch.setenv("CHAT_GATEWAY_MAX_REQUEST_BODY_BYTES", "65536")
    get_settings.cache_clear()
    clear_chat_gateway_state()

    # MCP 的 session manager 不允许重复启动，每个测试都构建全新应用实例
    app = create_app()
    app.dependency_overrides[deps.get_memory_store] = lambda: memory_store
    app.dependency_overrides[deps.get_knowledge_store] = lambda: knowledge_store
    app.dependency_overrides[deps.get_embedding_client] = lambda: NullEmbeddingClient()
    app.dependency_overrides[deps.get_llm_client] = lambda: fake_llm
    app.dependency_overrides[deps.get_chat_gateway_client] = lambda: fake_gateway

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-gateway-key"}
