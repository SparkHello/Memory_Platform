from collections.abc import Iterator
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.config import get_settings
from app.knowledge.store import KnowledgeStore
from app.main import create_app
from app.memory.search import NullEmbeddingClient
from app.memory.store import MemoryStore


class FakeLLMClient:
    """伪装普通聊天、记忆提取、核心记忆整理三类上游调用。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.extraction_messages: list[dict] = []
        self.core_messages: list[dict] = []
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
        if self._is_extraction_call(messages):
            self.extraction_messages = messages
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
            return {
                "id": "chatcmpl-core-memory",
                "object": "chat.completion",
                "created": 0,
                "model": "test-upstream",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.core_content},
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


@pytest.fixture
def memory_store(tmp_path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.init_db()
    # 清除模块级搜索缓存，避免测试间残留
    from app.memory.search import _EMBEDDING_CACHE, _SEARCH_CACHE
    _EMBEDDING_CACHE.clear()
    _SEARCH_CACHE.clear()
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
def client(
    monkeypatch,
    memory_store: MemoryStore,
    knowledge_store: KnowledgeStore,
    fake_llm: FakeLLMClient,
) -> Iterator[TestClient]:
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DATABASE_PATH", memory_store.database_path)
    monkeypatch.setenv(
        "KNOWLEDGE_DATABASE_PATH",
        knowledge_store.database_path,
    )
    monkeypatch.setenv("KNOWLEDGE_AGENT_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER_PRIORITY", "D")
    monkeypatch.setenv("KNOWLEDGE_AGENT_MIMO_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_KIMI_API_KEY", "")
    monkeypatch.setenv("LLM_DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("LLM_MIMO_API_KEY", "")
    monkeypatch.setenv("LLM_KIMI_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_EGRESS_POLICY", "none")
    monkeypatch.setenv("EVAL_DIR", str(Path(memory_store.database_path).with_name("eval")))
    monkeypatch.setenv("UPSTREAM_API_KEY", "")
    monkeypatch.setenv("UPSTREAM_MODEL", "glm-5.1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("TIME_RIPPLE_DELTA", "0.0")
    monkeypatch.setenv("TIME_RIPPLE_WINDOW_HOURS", "48")
    get_settings.cache_clear()

    # MCP 的 session manager 不允许重复启动，每个测试都构建全新应用实例
    app = create_app()
    app.dependency_overrides[deps.get_memory_store] = lambda: memory_store
    app.dependency_overrides[deps.get_knowledge_store] = lambda: knowledge_store
    app.dependency_overrides[deps.get_embedding_client] = lambda: NullEmbeddingClient()
    app.dependency_overrides[deps.get_llm_client] = lambda: fake_llm

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-gateway-key"}
