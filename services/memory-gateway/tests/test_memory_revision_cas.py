from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Barrier

from app.memory.models import MemoryRecord
from app.memory.store import MemoryStore, RevisionConflictError


def _etag(resource: str, resource_id: str, revision: int) -> str:
    identity = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()[:24]
    return f'W/"{resource}:{identity}:r{revision}"'


def _update_content(
    store: MemoryStore,
    memory: MemoryRecord,
    content: str,
    *,
    expected_revision: int | None,
) -> MemoryRecord:
    updated = store.update_memory(
        memory_id=memory.id,
        user_id=memory.user_id,
        content=content,
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
        source_message=memory.source_message,
        source_conversation_id=memory.source_conversation_id,
        embedding_json=memory.embedding_json,
        stability=memory.stability,
        review_after=memory.review_after,
        sensitivity=memory.sensitivity,
        evidence_memory_ids=memory.evidence_memory_ids,
        topics=memory.topics,
        entities=memory.entities,
        expected_revision=expected_revision,
    )
    assert updated is not None
    return updated


def test_memory_compare_and_swap_and_legacy_last_write(
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(user_id="default", content="initial")
    assert memory.revision == 1

    updated = _update_content(
        memory_store,
        memory,
        "revision two",
        expected_revision=1,
    )
    assert updated.revision == 2

    try:
        _update_content(
            memory_store,
            updated,
            "stale write",
            expected_revision=1,
        )
    except RevisionConflictError as exc:
        assert exc.expected_revision == 1
        assert exc.current_revision == 2
    else:  # pragma: no cover - the assertion explains the failed contract
        raise AssertionError("stale memory update unexpectedly succeeded")

    legacy = _update_content(
        memory_store,
        updated,
        "legacy last write",
        expected_revision=None,
    )
    assert legacy.content == "legacy last write"
    assert legacy.revision == 3


def test_memory_import_preserves_new_revision_and_bumps_overwritten_revision(
    memory_store: MemoryStore,
) -> None:
    action, imported = memory_store.import_memory_record(
        user_id="default",
        data={"id": "imported", "content": "from backup", "revision": 7},
    )
    assert action == "created"
    assert imported is not None
    assert imported.revision == 7

    action, overwritten = memory_store.import_memory_record(
        user_id="default",
        data={"id": "imported", "content": "replacement", "revision": 1},
        overwrite=True,
    )
    assert action == "updated"
    assert overwritten is not None
    assert overwritten.content == "replacement"
    assert overwritten.revision == 8


def test_two_sqlite_connections_allow_only_one_memory_cas_writer(
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(user_id="default", content="initial")
    stores = [
        MemoryStore(memory_store.database_path),
        MemoryStore(memory_store.database_path),
    ]
    barrier = Barrier(2)

    def write(index: int) -> tuple[str, int]:
        barrier.wait()
        try:
            updated = _update_content(
                stores[index],
                memory,
                f"writer-{index}",
                expected_revision=memory.revision,
            )
        except RevisionConflictError as exc:
            return "conflict", exc.current_revision
        return "updated", updated.revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))

    assert sorted(result[0] for result in results) == ["conflict", "updated"]
    assert {result[1] for result in results} == {2}
    final = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert final is not None
    assert final.revision == 2
    assert final.content in {"writer-0", "writer-1"}


def test_two_sqlite_connections_allow_only_one_core_memory_cas_writer(
    memory_store: MemoryStore,
) -> None:
    _, section = memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="initial",
        evidence_memory_ids=[],
        confidence=0.8,
    )
    stores = [
        MemoryStore(memory_store.database_path),
        MemoryStore(memory_store.database_path),
    ]
    barrier = Barrier(2)

    def write(index: int) -> tuple[str, int]:
        barrier.wait()
        try:
            _, updated = stores[index].upsert_core_memory_section(
                user_id="default",
                section="preferences",
                content=f"writer-{index}",
                evidence_memory_ids=[],
                confidence=0.8,
                expected_revision=section.revision,
            )
        except RevisionConflictError as exc:
            return "conflict", exc.current_revision
        return "updated", updated.revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))

    assert sorted(result[0] for result in results) == ["conflict", "updated"]
    assert {result[1] for result in results} == {2}
    active = memory_store.list_core_memory_sections(user_id="default")
    assert len(active) == 1
    assert active[0].revision == 2
    assert active[0].content in {"writer-0", "writer-1"}
    history = memory_store.list_core_memory_section_history(
        user_id="default",
        section="preferences",
    )
    assert len(history) == 1
    assert history[0].revision == 1


def test_memory_http_revision_etag_conflict_and_legacy_compatibility(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(user_id="default", content="initial")

    fetched = client.get(f"/memories/{memory.id}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["memory"]["revision"] == 1
    assert fetched.headers["etag"] == _etag("memory", memory.id, 1)

    updated = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"content": "updated", "expected_revision": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["memory"]["revision"] == 2
    assert updated.headers["etag"] == _etag("memory", memory.id, 2)

    stale = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"importance": 9, "expected_revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "revision_conflict",
        "resource": "memory",
        "resource_id": memory.id,
        "expected_revision": 1,
        "current_revision": 2,
        "message": "记录已被其他操作更新，请刷新后重试。",
    }

    legacy = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"importance": 8},
    )
    assert legacy.status_code == 200
    assert legacy.json()["memory"]["revision"] == 3

    spaces = client.patch(
        f"/memories/{memory.id}/spaces",
        headers=auth_headers,
        json={"create_space_names": ["重要"], "expected_revision": 3},
    )
    assert spaces.status_code == 200
    assert spaces.json()["memory"]["revision"] == 4
    assert spaces.headers["etag"] == _etag("memory", memory.id, 4)

    stale_spaces = client.patch(
        f"/memories/{memory.id}/spaces",
        headers=auth_headers,
        json={"space_ids": [], "expected_revision": 3},
    )
    assert stale_spaces.status_code == 409
    assert stale_spaces.json()["detail"]["code"] == "revision_conflict"
    assert stale_spaces.json()["detail"]["current_revision"] == 4

    archivable = memory_store.create_memory(user_id="default", content="archive me")
    archived = client.patch(
        f"/memories/{archivable.id}",
        headers=auth_headers,
        json={"status": "archived", "expected_revision": 1},
    )
    assert archived.status_code == 200
    assert archived.json()["revision"] == 2
    assert archived.headers["etag"] == _etag("memory", archivable.id, 2)


def test_core_memory_http_revision_etag_and_conflict(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    _, section = memory_store.upsert_core_memory_section(
        user_id="default",
        section="goals",
        content="initial goal",
        evidence_memory_ids=[],
        confidence=0.8,
    )

    fetched = client.get("/memories/core/goals", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["core_memory"]["revision"] == 1
    assert fetched.headers["etag"] == _etag("core-memory", section.id, 1)

    updated = client.patch(
        "/memories/core/goals",
        headers=auth_headers,
        json={"content": "updated goal", "expected_revision": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["core_memory"]["revision"] == 2
    assert updated.headers["etag"] == _etag("core-memory", section.id, 2)

    stale = client.patch(
        "/memories/core/goals",
        headers=auth_headers,
        json={"confidence": 0.9, "expected_revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert stale.json()["detail"]["resource"] == "core_memory"
    assert stale.json()["detail"]["current_revision"] == 2

    legacy = client.patch(
        "/memories/core/goals",
        headers=auth_headers,
        json={"confidence": 0.9},
    )
    assert legacy.status_code == 200
    assert legacy.json()["core_memory"]["revision"] == 3
