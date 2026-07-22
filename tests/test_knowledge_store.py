from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import app.knowledge.store as store_module
from app.knowledge.store import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeStore,
    KnowledgeValidationError,
)


@pytest.fixture
def knowledge_store(tmp_path: Path) -> KnowledgeStore:
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    store.init_db()
    return store


def _commit(
    store: KnowledgeStore,
    text: str,
    *,
    user_id: str = "alice",
    title: str = "测试文档",
    replace_document_ref: str = "",
    sensitivity: str = "normal",
):
    session = store.begin_upload(
        user_id,
        title,
        replace_document_ref=replace_document_ref,
        sensitivity=sensitivity,
    )
    store.append_upload(user_id, session.id, 0, text)
    return store.commit_upload(user_id, session.id, 1)


def test_chunks_preserve_source_offsets_and_use_trigram_fts(
    knowledge_store: KnowledgeStore,
) -> None:
    text = (
        "# 第一章\n\n"
        + ("甲乙丙丁，保留逐字偏移。" * 150)
        + "\n\n## 第二节\n\n"
        + ("代码与说明不会改写。" * 150)
    )
    result = _commit(knowledge_store, text)

    with knowledge_store._connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM knowledge_chunks
            WHERE version_id = ? ORDER BY ordinal
            """,
            (result.version.id,),
        ).fetchall()
        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'knowledge_chunks_fts'"
        ).fetchone()["sql"]

    assert len(rows) > 2
    assert "trigram" in fts_sql
    for row in rows:
        assert row["content"] == text[row["char_start"] : row["char_end"]]
        assert row["line_start"] == text.count("\n", 0, row["char_start"]) + 1
    assert json.loads(rows[-1]["title_path_json"]) == ["第一章", "第二节"]


def test_chinese_short_term_and_code_quote_search(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        '# API 说明\n\n项目代号是「极光」。调用 `client.get("a/b")` 获取结果。',
    )

    chinese = knowledge_store.search_chunks("alice", "项目代号")
    short = knowledge_store.search_chunks("alice", "极光")
    code = knowledge_store.search_chunks("alice", 'client.get("a/b")')

    assert chinese[0].version_ref == result.version.ref
    assert chinese[0].excerpt in (
        '# API 说明\n\n项目代号是「极光」。调用 `client.get("a/b")` 获取结果。',
    )
    assert short and "substring" in short[0].match_signals
    assert code and "client.get" in code[0].excerpt


def test_same_current_hash_is_deduplicated(knowledge_store: KnowledgeStore) -> None:
    first = _commit(knowledge_store, "完全相同的正文")
    second = _commit(
        knowledge_store,
        "完全相同的正文",
        title="更新后的标题",
        replace_document_ref=first.document.ref,
    )

    assert second.deduplicated is True
    assert second.version.id == first.version.id
    assert second.document.title == "更新后的标题"
    assert knowledge_store.counts("alice")["versions"] == 1


def test_restore_version_copies_history_as_a_new_version(
    knowledge_store: KnowledgeStore,
) -> None:
    first = _commit(knowledge_store, "第一版逐字原文")
    second = _commit(
        knowledge_store,
        "第二版逐字原文",
        replace_document_ref=first.document.ref,
    )

    restored = knowledge_store.restore_version(
        user_id="alice",
        document_ref=first.document.ref,
        version_ref=first.version.ref,
    )

    assert second.version.version_number == 2
    assert restored.version.version_number == 3
    assert restored.version.id not in {first.version.id, second.version.id}
    page = knowledge_store.read_reference(
        "alice", restored.version.ref, signing_key="test-key"
    )
    assert page["content"] == "第一版逐字原文"


def test_failed_new_index_does_not_replace_current_version(
    knowledge_store: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _commit(knowledge_store, "仍然可搜索的旧版本关键字")
    upload = knowledge_store.begin_upload(
        "alice", "测试文档", replace_document_ref=first.document.ref
    )
    knowledge_store.append_upload("alice", upload.id, 0, "新版本正文")

    def fail_chunking(_text: str):
        raise RuntimeError("forced index failure")

    monkeypatch.setattr(store_module, "chunk_knowledge_text", fail_chunking)
    failed = knowledge_store.commit_upload("alice", upload.id, 1)

    assert failed.version.index_status == "failed"
    assert failed.document.current_version_id == first.version.id
    assert knowledge_store.search_chunks("alice", "旧版本关键字")[0].version_ref == first.version.ref


def test_upload_parts_are_idempotent_and_can_arrive_out_of_order(
    knowledge_store: KnowledgeStore,
) -> None:
    upload = knowledge_store.begin_upload("alice", "分段上传")
    knowledge_store.append_upload("alice", upload.id, 1, "后半段")
    knowledge_store.append_upload("alice", upload.id, 0, "前半段")
    duplicate = knowledge_store.append_upload("alice", upload.id, 0, "前半段")

    assert duplicate.duplicate is True
    with pytest.raises(KnowledgeConflictError):
        knowledge_store.append_upload("alice", upload.id, 0, "冲突片段")
    committed = knowledge_store.commit_upload("alice", upload.id, 2)
    page = knowledge_store.read_reference(
        "alice", committed.version.ref, signing_key="test-key"
    )
    assert page["content"] == "前半段后半段"


def test_upload_rejects_missing_parts_hash_mismatch_expiry_and_size(
    knowledge_store: KnowledgeStore,
    tmp_path: Path,
) -> None:
    missing = knowledge_store.begin_upload("alice", "缺片")
    knowledge_store.append_upload("alice", missing.id, 1, "only second")
    with pytest.raises(KnowledgeConflictError):
        knowledge_store.commit_upload("alice", missing.id, 2)

    mismatched = knowledge_store.begin_upload("alice", "哈希")
    knowledge_store.append_upload("alice", mismatched.id, 0, "正文")
    with pytest.raises(KnowledgeConflictError):
        knowledge_store.commit_upload("alice", mismatched.id, 1, "0" * 64)

    blank = knowledge_store.begin_upload("alice", "空白正文")
    knowledge_store.append_upload("alice", blank.id, 0, " \n\t ")
    with pytest.raises(KnowledgeValidationError):
        knowledge_store.commit_upload("alice", blank.id, 1)

    expired = knowledge_store.begin_upload("alice", "过期")
    with knowledge_store._connect() as connection:
        connection.execute(
            "UPDATE knowledge_upload_sessions SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000Z", expired.id),
        )
    with pytest.raises(KnowledgeConflictError):
        knowledge_store.append_upload("alice", expired.id, 0, "正文")

    small = KnowledgeStore(str(tmp_path / "small.db"), max_document_bytes=8)
    small.init_db()
    over = small.begin_upload("alice", "过大")
    with pytest.raises(KnowledgeValidationError):
        small.append_upload("alice", over.id, 0, "中文超出八字节")

    bounded = knowledge_store.begin_upload("alice", "边界")
    with pytest.raises(KnowledgeValidationError):
        knowledge_store.append_upload("alice", bounded.id, 100_000, "越界")
    with pytest.raises(KnowledgeValidationError):
        knowledge_store.commit_upload("alice", bounded.id, 100_001)


def test_upload_uses_optimistic_current_version_check(
    knowledge_store: KnowledgeStore,
) -> None:
    first = _commit(knowledge_store, "v1")
    left = knowledge_store.begin_upload(
        "alice", "测试文档", replace_document_ref=first.document.ref
    )
    right = knowledge_store.begin_upload(
        "alice", "测试文档", replace_document_ref=first.document.ref
    )
    knowledge_store.append_upload("alice", left.id, 0, "v2-left")
    knowledge_store.append_upload("alice", right.id, 0, "v2-right")
    knowledge_store.commit_upload("alice", left.id, 1)

    with pytest.raises(KnowledgeConflictError):
        knowledge_store.commit_upload("alice", right.id, 1)


def test_version_cursor_pages_reconstruct_text_without_gaps(
    knowledge_store: KnowledgeStore,
) -> None:
    text = "".join(f"第{index:04d}行：不重叠也不缺字。\n" for index in range(500))
    result = _commit(knowledge_store, text)
    cursor = ""
    pages: list[str] = []
    starts: list[int] = []

    while True:
        page = knowledge_store.read_reference(
            "alice",
            result.version.ref,
            cursor=cursor,
            max_chars=137,
            signing_key="test-key",
        )
        pages.append(page["content"])
        starts.append(page["char_start"])
        if page["complete"]:
            assert page["next_cursor"] == ""
            break
        cursor = page["next_cursor"]

    assert "".join(pages) == text
    assert starts == list(range(0, len(text), 137))
    with pytest.raises(KnowledgeValidationError):
        knowledge_store.read_reference(
            "alice",
            result.version.ref,
            cursor="tampered.cursor",
            max_chars=137,
            signing_key="test-key",
        )
    with pytest.raises(KnowledgeValidationError):
        knowledge_store.read_reference(
            "alice",
            result.version.ref,
            cursor="x" * 4001,
            signing_key="test-key",
        )


def test_user_isolation_applies_to_documents_search_chunks_and_uploads(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(knowledge_store, "Alice only secret marker")

    assert knowledge_store.list_documents("bob", include_sensitive=True) == []
    assert knowledge_store.search_chunks("bob", "secret marker") == []
    assert knowledge_store.get_chunks_by_refs("bob", [result.version.ref.replace("version", "chunk")]) == []
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.get_document_detail("bob", document_ref=result.document.ref)
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read_reference(
            "bob", result.version.ref, include_sensitive=True, signing_key="test-key"
        )

    upload = knowledge_store.begin_upload("alice", "Alice upload")
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.append_upload("bob", upload.id, 0, "stolen")


def test_sensitive_floor_and_default_hiding(knowledge_store: KnowledgeStore) -> None:
    result = _commit(
        knowledge_store,
        "deployment api_key=sk-abcdefghijklmnop must remain local",
        sensitivity="normal",
    )

    assert result.document.sensitivity == "sensitive"
    assert knowledge_store.list_documents("alice") == []
    assert knowledge_store.search_chunks("alice", "deployment") == []
    visible = knowledge_store.list_documents("alice", include_sensitive=True)
    hits = knowledge_store.search_chunks("alice", "deployment", include_sensitive=True)
    assert visible[0].ref == result.document.ref
    assert hits[0].sensitivity == "sensitive"


def test_export_omits_derived_index_and_restore_rebinds_user(
    knowledge_store: KnowledgeStore,
    tmp_path: Path,
) -> None:
    first = _commit(knowledge_store, "第一版 backup marker")
    _commit(
        knowledge_store,
        "第二版 backup marker",
        replace_document_ref=first.document.ref,
    )
    exported = knowledge_store.export_user("alice")

    assert len(exported["documents"][0]["versions"]) == 2
    assert "chunks" not in exported["documents"][0]
    assert "fts" not in exported["documents"][0]
    assert all("content" in item for item in exported["documents"][0]["versions"])

    restored_store = KnowledgeStore(str(tmp_path / "restored.db"))
    restored_store.init_db()
    outcome = restored_store.restore_export("bob", exported)

    assert outcome["restored_documents"] == 1
    assert outcome["restored_versions"] == 2
    assert restored_store.list_documents("alice", include_sensitive=True) == []
    restored = restored_store.list_documents("bob", include_sensitive=True)[0]
    versions = restored_store.list_versions(
        "bob", document_ref=restored.ref, include_content=True
    )
    assert [item.content for item in versions] == [
        "第一版 backup marker",
        "第二版 backup marker",
    ]
    assert restored_store.search_chunks("bob", "backup marker")[0].document_ref == restored.ref


def test_upload_commit_checks_optional_exact_sha256(
    knowledge_store: KnowledgeStore,
) -> None:
    content = "逐字 SHA 校验"
    upload = knowledge_store.begin_upload("alice", "哈希成功")
    knowledge_store.append_upload("alice", upload.id, 0, content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    result = knowledge_store.commit_upload("alice", upload.id, 1, digest.upper())
    assert result.version.content_sha256 == digest
