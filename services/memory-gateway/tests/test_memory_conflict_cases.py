"""第一人称中文冲突用例：一条旧陈述之后来了一条新陈述，系统应该怎么处理。

每个用例写入旧记忆，再经 resolver 写入新陈述，然后同时检查三处结果：
存储（是否自动替换）、召回注入的配对说明（``context_pairs``）、体检建议。

期望结果三选一：

* ``superseded``：带明确转变的同一属性，旧记忆被自动关闭并保留为历史；
* ``paired``：可能是同一事实的新旧版本但证据不够自动替换，两条都保留，
  注入时标注、体检提示用户确认；
* ``separate``：可以并存或根本不是同一属性，不替换也不标注。

``same_topic`` 模拟 embedding 是否把两句判成同主题（余弦 ≥0.80）。真实向量
是否达到这个门槛不在这里测，这里只测拿到相似度之后的判定规则。按
TANGLE 的分类组织：值替换、转变、极性翻转、可并存偏好、上下文分区、
行为反复，以及区分不同事实的守卫。来源冲突（自述 vs 推断）不适用：
自动记忆只保存用户自述。
"""

from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from app.memory.models import CandidateMemory
from app.memory.pairing import context_pairs
from app.memory.resolver import MemoryResolver
from app.memory.review import MemoryReviewer
from app.memory.store import MemoryStore


SAME_TOPIC_OLD = [1.0, 0.0, 0.0]
SAME_TOPIC_NEW = [0.98, 0.2, 0.0]
OTHER_TOPIC = [0.0, 0.0, 1.0]


@dataclass(frozen=True)
class ConflictCase:
    id: str
    older: str
    newer: str
    expected: str
    type: str = "semantic"
    same_topic: bool = True


CASES = [
    # 值替换，没有否定也没有转变词：不能自动替换，但必须被看见。
    ConflictCase("value-city", "用户住在常德。", "用户住在西宁。", "paired"),
    ConflictCase("value-book", "用户在读 CSAPP 第 8 章。", "用户在读 OSTEP。", "paired"),
    ConflictCase("value-employer", "用户在字节跳动工作。", "用户在阿里巴巴工作。", "paired"),
    ConflictCase("value-phone", "用户平时用 iPhone 手机。", "用户平时用安卓手机。", "paired"),
    ConflictCase("value-age", "用户今年 30 岁。", "用户今年 31 岁。", "paired"),
    # 明确转变：自动替换，旧值保留为历史。
    ConflictCase("transition-phone", "用户平时用 iPhone 手机。", "用户现在改用安卓手机。", "superseded"),
    ConflictCase("transition-city", "用户住在北京。", "用户现在住在上海。", "superseded"),
    ConflictCase("transition-employer", "用户在字节跳动工作。", "用户现在在阿里巴巴工作。", "superseded"),
    ConflictCase("transition-quit", "用户每天吃辣。", "用户不再吃辣了。", "superseded"),
    # 纯极性翻转：可能是说错、可能是变了，交给用户确认。
    ConflictCase("polarity-coffee", "用户喜欢喝咖啡。", "用户不喜欢喝咖啡。", "paired", type="emotional"),
    # 偏好可以并存：喜欢绿茶不等于不再喜欢咖啡。
    ConflictCase("additive-drinks", "用户喜欢绿茶。", "用户喜欢黑咖啡。", "separate"),
    ConflictCase("additive-films", "用户喜欢看科幻电影。", "用户喜欢看纪录片。", "separate"),
    ConflictCase("additive-transition", "用户喜欢喝绿茶。", "用户现在只喝黑咖啡。", "paired"),
    # 上下文分区：两条在各自场景下都成立。
    ConflictCase("context-drinks", "用户工作日早上喝咖啡。", "用户周末喝茶。", "separate"),
    # 行为反复：停了又开始，没有转变词时只标注不替换。
    ConflictCase("oscillation-running", "用户坚持每天晨跑。", "用户没有坚持晨跑。", "paired"),
    # 守卫：不同属性、不同主体、复合事实、事件、无关主题。
    ConflictCase("guard-attribute", "用户养了一只叫年糕的橘猫。", "年糕现在三岁。", "separate"),
    ConflictCase("guard-subject-age", "用户三十岁。", "用户的橘猫三岁。", "separate"),
    ConflictCase("guard-subject-city", "用户的女朋友住在杭州。", "用户住在上海。", "separate"),
    ConflictCase("guard-compound", "用户住在北京，在阿里巴巴工作。", "用户在字节跳动工作。", "paired"),
    ConflictCase(
        "guard-events",
        "用户上周去杭州出差。",
        "用户昨天去苏州出差。",
        "separate",
        type="episodic",
    ),
    ConflictCase("guard-unrelated", "用户住在常德。", "用户喜欢黑咖啡。", "separate", same_topic=False),
]


class _FixedEmbeddingClient:
    embedding_space_id = "test-space"

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    async def embed(self, text: str) -> list[float]:
        return self.vector


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_conflict_case(memory_store: MemoryStore, case: ConflictCase) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content=case.older,
        type=case.type,
        importance=8,
        embedding_json=json.dumps(SAME_TOPIC_OLD),
        embedding_space_id="test-space",
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=_FixedEmbeddingClient(
            SAME_TOPIC_NEW if case.same_topic else OTHER_TOPIC
        ),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=CandidateMemory(
            action="create",
            memory=case.newer,
            type=case.type,
            importance=8,
            confidence=0.9,
            source_quote=case.newer,
        ),
    )

    assert result.action in {"create", "update"}, result.reason
    newer = result.memory
    assert newer is not None
    older_after = memory_store.get_memory(memory_id=older.id, user_id="default")
    assert older_after is not None
    pair_kinds = {
        pair.kind
        for pair in context_pairs([newer, older_after])
        if {pair.older_id, pair.newer_id} == {older.id, newer.id}
    }
    reviewed = any(
        set(recommendation.memory_ids) == {older.id, newer.id}
        for recommendation in MemoryReviewer(store=memory_store)
        .review(user_id="default")
        .recommendations
    )

    if case.expected == "superseded":
        assert older_after.superseded_by == newer.id
        assert pair_kinds == {"superseded"}
        assert not reviewed
    elif case.expected == "paired":
        assert older_after.superseded_by is None
        assert pair_kinds == {"parallel"}
        assert reviewed
    else:
        assert older_after.superseded_by is None
        assert pair_kinds == set()
        assert not reviewed
