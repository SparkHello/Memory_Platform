"""GET /memories/evaluation/diagnosis 的 snapshot_initialized 两态测试。

前端用 snapshot_initialized 区分「评测快照未初始化」与「诊断出错」；
未初始化时 diagnosis 必须正常返回 200，而 workbench 仍保持 404 兜底。
"""

from __future__ import annotations

from app.memory.store import MemoryStore


def test_diagnosis_reports_snapshot_not_initialized(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")

    diagnosis = client.get("/memories/evaluation/diagnosis", headers=auth_headers)
    workbench = client.get(
        "/memories/evaluation/recall/workbench",
        headers=auth_headers,
    )

    assert diagnosis.status_code == 200
    assert diagnosis.json()["snapshot_initialized"] is False
    # workbench 未初始化时 404 的兜底行为保持不变
    assert workbench.status_code == 404


def test_diagnosis_reports_snapshot_initialized_after_init(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡。")

    init_response = client.post(
        "/memories/evaluation/recall/init",
        headers=auth_headers,
    )
    assert init_response.status_code == 200

    diagnosis = client.get("/memories/evaluation/diagnosis", headers=auth_headers)

    assert diagnosis.status_code == 200
    assert diagnosis.json()["snapshot_initialized"] is True
