"""测试两层模块级缓存：L1 embedding 缓存 + L2 搜索结果缓存"""
import time

from app.memory.search import (
    _EMBEDDING_CACHE,
    _EMBEDDING_CACHE_TTL,
    _SEARCH_CACHE,
    _SEARCH_CACHE_TTL,
    _normalize_query,
    _now,
    MemorySearchService,
    NullEmbeddingClient,
)


class FakeEmbeddingClient:
    """可控的 fake embedding client，记录调用次数"""
    def __init__(self, vector: list[float] | None = None):
        self.vector = vector or [0.1, 0.2, 0.3]
        self.call_count = 0

    async def embed(self, text: str) -> list[float] | None:
        self.call_count += 1
        return self.vector


class TestNormalizeQuery:
    def test_lowercase(self):
        assert _normalize_query("Hello WORLD") == "hello world"

    def test_truncate(self):
        long_text = "a" * 300
        assert len(_normalize_query(long_text)) == 200

    def test_collapse_whitespace(self):
        assert _normalize_query("  hello    world  ") == "hello world"


class TestEmbeddingCache:
    """L1: query embedding 缓存"""

    async def test_cache_hit_avoids_embed_call(self, memory_store):
        fake = FakeEmbeddingClient()
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        await service.search(query="hello test", user_id="default", limit=5)
        first_count = fake.call_count

        await service.search(query="  HELLO   TEST  ", user_id="default", limit=5)
        # 相同规范化 query，不应再调 embedding
        assert fake.call_count == first_count

    async def test_different_query_calls_embed_again(self, memory_store):
        fake = FakeEmbeddingClient()
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        await service.search(query="hello", user_id="default", limit=5)
        await service.search(query="world", user_id="default", limit=5)
        assert fake.call_count == 2

    async def test_expired_cache_re_embeds(self, memory_store, monkeypatch):
        fake = FakeEmbeddingClient()
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        await service.search(query="hello", user_id="default", limit=5)
        assert fake.call_count == 1

        # 模拟缓存过期
        monkeypatch.setattr(
            "app.memory.search._now",
            lambda: _now() + _EMBEDDING_CACHE_TTL + 10,
        )

        await service.search(query="HELLO", user_id="default", limit=5)
        assert fake.call_count == 2


class TestSearchCache:
    """L2: 搜索结果缓存"""

    async def test_cache_hit_returns_cached_result(self, memory_store):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
            embedding_json='[0.5, 0.5, 0.5]',
        )
        fake = FakeEmbeddingClient(vector=[0.5, 0.5, 0.5])
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        result1 = await service.search(query="咖啡偏好", user_id="default", limit=5)
        first_count = fake.call_count

        result2 = await service.search(query="  咖啡偏好  ", user_id="default", limit=5)
        # L2 命中，不应再调 embedding
        assert fake.call_count == first_count
        # 结果应该一致
        assert [m.id for m in result1] == [m.id for m in result2]

    async def test_memory_change_invalidates_cache(self, memory_store):
        fake = FakeEmbeddingClient(vector=[0.5, 0.5, 0.5])
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        await service.search(query="咖啡", user_id="default", limit=5)
        first_count = fake.call_count

        # 写入新记忆 → max_updated_at 变化 → L2 失效
        memory_store.create_memory(
            user_id="default",
            content="用户每天喝三杯咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
            embedding_json='[0.5, 0.5, 0.5]',
        )
        import time as _time
        _time.sleep(0.01)  # 确保 updated_at 有时间差

        result_after = await service.search(query="咖啡", user_id="default", limit=5)
        # L2 失效 → 重新搜索 → 但 L1 embedding 缓存仍有效 → embed 不重复调用
        assert fake.call_count == first_count

        # 验证新增的记忆出现在搜索结果中（L2 确实失效并重新评分了）
        all_contents = [m.content for m in result_after]
        assert any("三杯咖啡" in c for c in all_contents)

    async def test_soft_delete_invalidates_cache_by_count(self, memory_store):
        """归档非最新记忆时，active_count 变化应使 L2 缓存失效。"""
        import time as _time

        # 创建两条记忆
        m1 = memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
            embedding_json='[0.5, 0.5, 0.5]',
        )
        _time.sleep(0.02)
        m2 = memory_store.create_memory(
            user_id="default",
            content="用户每天喝三杯咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
            embedding_json='[0.5, 0.5, 0.5]',
        )

        fake = FakeEmbeddingClient(vector=[0.5, 0.5, 0.5])
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        result_before = await service.search(query="咖啡", user_id="default", limit=5)
        first_count = fake.call_count
        assert len(result_before) == 2

        # 归档 m1（非最新记忆），m2 的 updated_at 更大 → max_updated_at 不变
        memory_store.archive_memory(memory_id=m1.id, user_id="default")
        _time.sleep(0.01)

        result_after = await service.search(query="咖啡", user_id="default", limit=5)
        # active_count 从 2 变为 1 → L2 应失效 → 重新评分
        # L1 embedding 缓存仍有效 → embed 不重复调用
        assert fake.call_count == first_count
        # 结果应只剩 m2
        assert len(result_after) == 1
        assert result_after[0].id == m2.id

    async def test_different_limit_is_different_cache_key(self, memory_store):
        memory_store.create_memory(
            user_id="default",
            content="用户喜欢黑咖啡",
            type="emotional",
            importance=8,
            confidence=0.95,
            embedding_json='[0.5, 0.5, 0.5]',
        )
        fake = FakeEmbeddingClient(vector=[0.5, 0.5, 0.5])
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        await service.search(query="咖啡", user_id="default", limit=5)
        await service.search(query="咖啡", user_id="default", limit=3)
        # 不同 limit → L2 cache miss → 但 L1 embedding cache 命中 → 不再调 embed
        assert fake.call_count == 1

    async def test_no_memories_no_cache_write(self, memory_store):
        fake = FakeEmbeddingClient(vector=[0.5, 0.5, 0.5])
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        cache_size_before = len(_SEARCH_CACHE)
        result = await service.search(query="nothing", user_id="default", limit=5)
        assert result == []
        # 空结果不应写入 L2
        assert len(_SEARCH_CACHE) == cache_size_before


class TestCacheMaxSize:
    async def test_embedding_cache_eviction_on_full(self, memory_store, monkeypatch):
        from app.memory import search as search_module

        # 临时把 max 设小
        monkeypatch.setattr(search_module, "_EMBEDDING_CACHE_MAX", 3)
        fake = FakeEmbeddingClient()
        service = MemorySearchService(store=memory_store, embedding_client=fake)

        # 填满缓存
        for i in range(5):
            await service.search(query=f"query{i}", user_id="default", limit=5)

        # 不应超过 max（或略微超出，因为先检查再写入）
        assert len(_EMBEDDING_CACHE) <= 4  # 可能刚好 4 条

        # 恢复
        monkeypatch.setattr(search_module, "_EMBEDDING_CACHE_MAX", 512)


class TestEvalCacheBypass:
    """评测隔离：enable_cache=False 时既不写入也不读取进程级搜索缓存。"""

    async def test_disabled_cache_does_not_write_search_cache(self, memory_store):
        memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
        service = MemorySearchService(
            store=memory_store,
            embedding_client=NullEmbeddingClient(),
            enable_cache=False,
        )

        hits = await service.search_hits(
            query="咖啡", user_id="default", limit=8, record_usage=False
        )
        assert hits  # 确实命中
        # 评测搜索不得污染线上共享缓存
        assert _SEARCH_CACHE == {}

    async def test_disabled_cache_ignores_existing_entry(self, memory_store):
        coffee = memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")
        typescript = memory_store.create_memory(user_id="default", content="用户喜欢写 TypeScript。")

        # 投毒：手工塞入一个对该 (user, query, limit) 校验有效、但指向无关记忆的缓存条目，
        # 模拟另一种模式（或线上检索）先跑过、把结果留在了共享缓存里。
        key = ("default", _normalize_query("咖啡"), 8)
        _SEARCH_CACHE[key] = (
            _now() + 999,
            memory_store.get_memories_max_updated_at(user_id="default"),
            memory_store.get_active_memory_count(user_id="default"),
            [
                {
                    "id": typescript.id,
                    "relevance": 99.0,
                    "channels": ["keyword"],
                    "topic_score": 99.0,
                    "total_score": 99.0,
                    "score_breakdown": None,
                }
            ],
        )

        # sanity：开启缓存的服务确实会吃到投毒结果
        cached = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())
        poisoned = await cached.search_hits(
            query="咖啡", user_id="default", limit=8, record_usage=False
        )
        assert [hit.memory.id for hit in poisoned] == [typescript.id]

        # 评测服务忽略缓存，按关键词真实命中咖啡记忆
        isolated = MemorySearchService(
            store=memory_store,
            embedding_client=NullEmbeddingClient(),
            enable_cache=False,
        )
        fresh = await isolated.search_hits(
            query="咖啡", user_id="default", limit=8, record_usage=False
        )
        fresh_ids = [hit.memory.id for hit in fresh]
        assert coffee.id in fresh_ids
        assert typescript.id not in fresh_ids

