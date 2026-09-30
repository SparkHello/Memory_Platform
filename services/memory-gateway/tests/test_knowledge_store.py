from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import app.knowledge.store as store_module
from app.knowledge.store import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeSensitivityConfirmationRequired,
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
    confirm_sensitivity_override: bool = False,
):
    session = store.begin_upload(
        user_id,
        title,
        replace_document_ref=replace_document_ref,
        sensitivity=sensitivity,
    )
    store.append_upload(user_id, session.id, 0, text)
    return store.commit_upload(
        user_id,
        session.id,
        1,
        confirm_sensitivity_override=confirm_sensitivity_override,
    )


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


def test_title_length_limit_is_300_for_upload_and_update(
    knowledge_store: KnowledgeStore,
) -> None:
    upload = knowledge_store.begin_upload("alice", "标" * 300)
    knowledge_store.append_upload("alice", upload.id, 0, "正文")
    committed = knowledge_store.commit_upload("alice", upload.id, 1)
    assert len(committed.document.title) == 300

    with pytest.raises(KnowledgeValidationError):
        knowledge_store.begin_upload("alice", "标" * 301)
    with pytest.raises(KnowledgeValidationError):
        knowledge_store.update_document(
            "alice",
            document_ref=committed.document.ref,
            title="题" * 301,
        )
    updated = knowledge_store.update_document(
        "alice",
        document_ref=committed.document.ref,
        title="题" * 300,
    )
    assert len(updated.title) == 300


def test_sensitive_document_read_is_hidden_without_explicit_opt_in(
    knowledge_store: KnowledgeStore,
) -> None:
    text = "deployment api_key=sk-abcdefghijklmnop must remain local"
    result = _commit(knowledge_store, text, sensitivity="sensitive")
    assert result.document.sensitivity == "sensitive"
    hits = knowledge_store.search_chunks("alice", "deployment", include_sensitive=True)
    chunk_ref = hits[0].chunk_ref

    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read_reference(
            "alice", result.version.ref, signing_key="test-key"
        )
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read_reference("alice", chunk_ref, signing_key="test-key")

    version_page = knowledge_store.read_reference(
        "alice", result.version.ref, include_sensitive=True, signing_key="test-key"
    )
    chunk_page = knowledge_store.read_reference(
        "alice", chunk_ref, include_sensitive=True, signing_key="test-key"
    )
    assert version_page["content"] == text
    assert chunk_page["content"] == text


def test_user_isolation_applies_to_documents_search_chunks_and_uploads(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(knowledge_store, "Alice only secret marker")
    hits = knowledge_store.search_chunks("alice", "secret marker")
    assert hits
    chunk_ref = hits[0].chunk_ref

    assert knowledge_store.list_documents("bob", include_sensitive=True) == []
    assert knowledge_store.search_chunks("bob", "secret marker") == []
    assert knowledge_store.get_chunks_by_refs("bob", [chunk_ref]) == []
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read_reference(
            "bob", chunk_ref, include_sensitive=True, signing_key="test-key"
        )
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.get_document_detail("bob", document_ref=result.document.ref)
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read_reference(
            "bob", result.version.ref, include_sensitive=True, signing_key="test-key"
        )

    upload = knowledge_store.begin_upload("alice", "Alice upload")
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.append_upload("bob", upload.id, 0, "stolen")


def test_detected_sensitive_document_requires_confirmation_then_uses_user_choice(
    knowledge_store: KnowledgeStore,
) -> None:
    text = "deployment api_key=sk-abcdefghijklmnop must remain local"
    upload = knowledge_store.begin_upload(
        "alice",
        "测试文档",
        sensitivity="normal",
    )
    knowledge_store.append_upload("alice", upload.id, 0, text)

    with pytest.raises(KnowledgeSensitivityConfirmationRequired) as error:
        knowledge_store.commit_upload("alice", upload.id, 1)

    assert error.value.declared_sensitivity == "normal"
    assert error.value.detected_sensitivity == "sensitive"
    assert knowledge_store.list_documents("alice", include_sensitive=True) == []

    result = knowledge_store.commit_upload(
        "alice",
        upload.id,
        1,
        confirm_sensitivity_override=True,
    )

    assert result.document.sensitivity == "normal"
    assert result.document.detected_sensitivity == "sensitive"
    assert result.document.sensitivity_override_confirmed is True
    assert knowledge_store.list_documents("alice")[0].ref == result.document.ref
    assert knowledge_store.search_chunks("alice", "deployment")

    updated = knowledge_store.update_document(
        "alice",
        document_ref=result.document.ref,
        title="确认后的新标题",
    )
    assert updated.sensitivity == "normal"
    assert updated.sensitivity_override_confirmed is True


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


def test_deduplicated_upload_repairs_failed_index(
    knowledge_store: KnowledgeStore,
) -> None:
    first = _commit(knowledge_store, "相同正文带有修复关键字")
    with knowledge_store._connect() as connection:
        connection.execute(
            """
            UPDATE knowledge_versions
            SET index_status = 'failed', index_error = 'simulated', indexed_at = NULL
            WHERE id = ?
            """,
            (first.version.id,),
        )
    assert knowledge_store.search_chunks("alice", "修复关键字") == []

    second = _commit(
        knowledge_store,
        "相同正文带有修复关键字",
        replace_document_ref=first.document.ref,
    )

    assert second.deduplicated is True
    assert second.version.id == first.version.id
    assert second.version.index_status == "ready"
    assert knowledge_store.counts("alice")["versions"] == 1
    hits = knowledge_store.search_chunks("alice", "修复关键字")
    assert hits and hits[0].version_ref == first.version.ref


def test_reindex_failed_version_keeps_ready_current_version(
    knowledge_store: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _commit(knowledge_store, "第一版保持当前")
    upload = knowledge_store.begin_upload(
        "alice", "测试文档", replace_document_ref=first.document.ref
    )
    knowledge_store.append_upload("alice", upload.id, 0, "第二版索引失败")

    def fail_chunking(_text: str):
        raise RuntimeError("forced index failure")

    monkeypatch.setattr(store_module, "chunk_knowledge_text", fail_chunking)
    failed = knowledge_store.commit_upload("alice", upload.id, 1)
    assert failed.version.index_status == "failed"
    monkeypatch.undo()

    reindexed = knowledge_store.reindex_version(
        user_id="alice",
        document_ref=first.document.ref,
        version_ref=failed.version.ref,
    )

    assert reindexed.version.index_status == "ready"
    assert reindexed.document.current_version_id == first.version.id
    detail = knowledge_store.get_document_detail(
        "alice", document_ref=first.document.ref
    )
    assert detail["document"].current_version_id == first.version.id


def test_reindex_without_any_ready_version_becomes_current(
    knowledge_store: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_chunking(_text: str):
        raise RuntimeError("forced index failure")

    monkeypatch.setattr(store_module, "chunk_knowledge_text", fail_chunking)
    failed = _commit(knowledge_store, "唯一版本初始失败")
    assert failed.version.index_status == "failed"
    assert failed.document.current_version_id is None
    monkeypatch.undo()

    reindexed = knowledge_store.reindex_version(
        user_id="alice",
        version_ref=failed.version.ref,
    )

    assert reindexed.version.index_status == "ready"
    assert reindexed.document.current_version_id == failed.version.id


def test_restore_export_is_idempotent(
    knowledge_store: KnowledgeStore,
    tmp_path: Path,
) -> None:
    _commit(knowledge_store, "幂等恢复 marker")
    exported = knowledge_store.export_user("alice")

    restored_store = KnowledgeStore(str(tmp_path / "restored.db"))
    restored_store.init_db()
    first = restored_store.restore_export("bob", exported)
    assert first["restored_documents"] == 1
    assert first["skipped_documents"] == 0

    second = restored_store.restore_export("bob", exported)
    assert set(second) == {
        "restored_documents",
        "restored_versions",
        "failed_versions",
        "skipped_documents",
        "document_refs",
        "chunks_rebuilt",
        "fts_rebuilt",
    }
    assert second["restored_documents"] == 0
    assert second["restored_versions"] == 0
    assert second["skipped_documents"] == 1
    assert second["document_refs"] == []
    assert len(restored_store.list_documents("bob", include_sensitive=True)) == 1


def test_restore_rejects_oversized_total_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported = {
        "format": "memory-gateway-knowledge",
        "documents": [
            {
                "source_document_ref": "knowledge://document/oversize",
                "title": "过大",
                "versions": [
                    {"version_number": 1, "content": "x" * 64},
                    {"version_number": 2, "content": "y" * 64},
                ],
            }
        ],
    }
    monkeypatch.setattr(store_module, "_MAX_RESTORE_TOTAL_BYTES", 100)
    restored_store = KnowledgeStore(str(tmp_path / "limited.db"))
    restored_store.init_db()

    with pytest.raises(KnowledgeValidationError):
        restored_store.restore_export("bob", exported)
    assert restored_store.list_documents("bob", include_sensitive=True) == []


def test_commit_cleans_upload_parts_and_is_idempotent(
    knowledge_store: KnowledgeStore,
) -> None:
    upload = knowledge_store.begin_upload("alice", "清理片段")
    knowledge_store.append_upload("alice", upload.id, 0, "正文片段")
    committed = knowledge_store.commit_upload("alice", upload.id, 1)

    with knowledge_store._connect() as connection:
        parts = connection.execute(
            "SELECT COUNT(*) AS count FROM knowledge_upload_parts WHERE upload_id = ?",
            (upload.id,),
        ).fetchone()["count"]
        session = connection.execute(
            "SELECT * FROM knowledge_upload_sessions WHERE id = ?",
            (upload.id,),
        ).fetchone()
    assert parts == 0
    assert session["status"] == "committed"
    assert session["committed_document_ref"] == committed.document.ref
    assert session["committed_version_ref"] == committed.version.ref

    repeated = knowledge_store.commit_upload("alice", upload.id, 1)
    assert repeated.document.ref == committed.document.ref
    assert repeated.version.ref == committed.version.ref
    assert repeated.deduplicated is True
    counts = knowledge_store.counts("alice")
    assert counts["documents"] == 1
    assert counts["versions"] == 1


def test_cancel_allows_expired_and_committing_sessions(
    knowledge_store: KnowledgeStore,
) -> None:
    expired = knowledge_store.begin_upload("alice", "过期")
    with knowledge_store._connect() as connection:
        connection.execute(
            "UPDATE knowledge_upload_sessions SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000Z", expired.id),
        )
    with pytest.raises(KnowledgeConflictError):
        knowledge_store.append_upload("alice", expired.id, 0, "正文")
    assert knowledge_store.cancel_upload("alice", expired.id) is True

    stuck = knowledge_store.begin_upload("alice", "卡住")
    knowledge_store.append_upload("alice", stuck.id, 0, "残留片段")
    with knowledge_store._connect() as connection:
        connection.execute(
            "UPDATE knowledge_upload_sessions SET status = 'committing' WHERE id = ?",
            (stuck.id,),
        )
    assert knowledge_store.cancel_upload("alice", stuck.id) is True
    with knowledge_store._connect() as connection:
        parts = connection.execute(
            "SELECT COUNT(*) AS count FROM knowledge_upload_parts WHERE upload_id = ?",
            (stuck.id,),
        ).fetchone()["count"]
    assert parts == 0

    committed = _commit(knowledge_store, "已提交不可取消")
    upload = knowledge_store.begin_upload("alice", "再提交")
    knowledge_store.append_upload("alice", upload.id, 0, "另一份正文")
    knowledge_store.commit_upload("alice", upload.id, 1)
    with pytest.raises(KnowledgeConflictError):
        knowledge_store.cancel_upload("alice", upload.id)
    assert committed.document.ref != ""


def test_begin_upload_sweeps_stale_sessions(
    knowledge_store: KnowledgeStore,
) -> None:
    stale = knowledge_store.begin_upload("alice", "待清扫")
    knowledge_store.append_upload("alice", stale.id, 0, "残留片段")
    with knowledge_store._connect() as connection:
        connection.execute(
            "UPDATE knowledge_upload_sessions SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000Z", stale.id),
        )

    knowledge_store.begin_upload("alice", "触发懒清理")

    with knowledge_store._connect() as connection:
        sessions = connection.execute(
            "SELECT COUNT(*) AS count FROM knowledge_upload_sessions WHERE id = ?",
            (stale.id,),
        ).fetchone()["count"]
        parts = connection.execute(
            "SELECT COUNT(*) AS count FROM knowledge_upload_parts WHERE upload_id = ?",
            (stale.id,),
        ).fetchone()["count"]
    assert sessions == 0
    assert parts == 0


def test_two_character_chinese_words_are_matched_through_substring_channel(
    knowledge_store: KnowledgeStore,
) -> None:
    _commit(
        knowledge_store,
        "# 邮件服务器配置\n\n管理员需要在后台填写 SMTP 服务器地址、端口（通常为 465 或 587）。",
    )
    _commit(knowledge_store, "# 数据导出\n\n在数据管理页可以导出 CSV。", title="其他")

    # Both words are two characters: invisible to the trigram index, so they
    # used to produce no result at all.
    hits = knowledge_store.search_chunks("alice", "端口 配置")
    assert hits and "邮件服务器配置" in hits[0].title_path
    assert "substring" in hits[0].match_signals

    # Reordered four-character run whose trigrams never occur verbatim.
    reordered = knowledge_store.search_chunks("alice", "配置端口")
    assert reordered and "邮件服务器配置" in reordered[0].title_path

    # Mixed query: trigram and substring channels are fused, not exclusive.
    fused = knowledge_store.search_chunks("alice", "SMTP 端口")
    assert fused and fused[0].title_path == ["邮件服务器配置"]


def test_long_natural_language_request_keeps_trailing_keywords(
    knowledge_store: KnowledgeStore,
) -> None:
    filler = "用户可以在系统设置页面中查看当前账户的基本信息，点击对应的选项即可进入详细配置。"
    text = (
        "# 产品使用手册\n\n"
        f"## 通知设置\n\n{filler * 3}\n夜间免打扰时段默认为 22:00 到 8:00。\n\n"
        f"## 邮件服务器配置\n\n{filler * 3}\n管理员需要填写 SMTP 服务器地址和授权码。\n\n"
    )
    _commit(knowledge_store, text)
    request = (
        "请在用户导入的产品使用手册里找到相关章节，并给出逐字证据，版本不限，"
        "重点是这个问题：夜间免打扰的默认时段是几点"
    )

    hits = knowledge_store.search_chunks("alice", request, limit=3)

    assert hits[0].title_path == ["产品使用手册", "通知设置"]
    # The excerpt window is anchored on the dense span of matched terms, not
    # on whichever generic word appears first in the chunk.
    assert "22:00" in hits[0].excerpt
    # A heading-only "# 产品使用手册" chunk no longer exists to steal a slot.
    assert all("22:00" in hit.excerpt or "SMTP" in hit.excerpt for hit in hits)


def test_heading_only_sections_are_merged_into_following_chunk(
    knowledge_store: KnowledgeStore,
) -> None:
    from app.knowledge.chunking import chunk_knowledge_text

    drafts = chunk_knowledge_text("# 手册\n\n## 空章节\n\n## 通知设置\n\n正文\n\n## 结尾空标题\n")

    assert [draft.title_path for draft in drafts] == [
        ("手册", "通知设置"),
        ("手册", "结尾空标题"),
    ]
    assert drafts[0].content.startswith("# 手册")
    assert drafts[0].line_start == 1 and drafts[1].line_start == 9


def test_chunk_line_numbers_are_linear_and_exact() -> None:
    import random
    import time

    from app.knowledge.chunking import chunk_knowledge_text

    random.seed(7)
    sample = "# T\n\n" + "".join(
        random.choice(["段落文字。", "\n", "\n\n", "## 小节\n"]) for _ in range(5000)
    )
    for chunk in chunk_knowledge_text(sample):
        assert chunk.line_start == sample.count("\n", 0, chunk.char_start) + 1
        assert chunk.line_end == sample.count("\n", 0, chunk.char_end - 1) + 1

    started = time.perf_counter()
    chunk_knowledge_text("一行文字。\n" * 400_000)
    assert time.perf_counter() - started < 5.0


def test_manual_mentioning_passwords_is_not_flagged_as_credential(
    knowledge_store: KnowledgeStore,
) -> None:
    manual = "# 重置密码\n\n如果忘记了登录密码，可以在登录页点击「忘记密码」，输入验证码后设置新密码。"
    result = _commit(knowledge_store, manual)
    assert result.document.sensitivity == "normal"
    assert result.document.detected_sensitivity == "normal"

    with pytest.raises(KnowledgeSensitivityConfirmationRequired):
        _commit(knowledge_store, "运维备忘：数据库密码是 Hunter2!2026", title="备忘")


def test_tag_scope_is_not_truncated_by_query_limit(
    knowledge_store: KnowledgeStore,
) -> None:
    wanted = knowledge_store.begin_upload("alice", "带标签的旧文档", tags=["wanted"])
    knowledge_store.append_upload("alice", wanted.id, 0, "旧文档正文")
    target = knowledge_store.commit_upload("alice", wanted.id, 1)
    for index in range(3):
        _commit(knowledge_store, f"新文档正文 {index}", title=f"新文档 {index}")

    refs = knowledge_store.resolve_document_refs("alice", tags=["wanted"], limit=2)

    assert refs == [target.document.ref]


def test_embedding_search_returns_best_match_regardless_of_position(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "\n\n".join(f"## 段落 {index}\n\n正文 {index} " + "填充。" * 40 for index in range(60)),
    )
    chunks = knowledge_store.list_chunks_for_embedding("alice", result.version.ref)
    assert len(chunks) >= 50
    vectors = {chunk.ref: [0.0, 1.0] for chunk in chunks}
    vectors[chunks[-1].ref] = [1.0, 0.0]
    vectors[chunks[-2].ref] = [0.9, 0.1]
    knowledge_store.replace_chunk_embeddings(
        "alice",
        result.version.ref,
        model="test-embedding",
        embedding_space_id="knowledge-space-a",
        vectors=vectors,
        total_chunks=len(chunks),
    )

    hits = knowledge_store.search_chunks_by_embedding(
        "alice",
        [1.0, 0.0],
        embedding_space_id="knowledge-space-a",
        min_cosine=0.5,
        limit=2,
    )

    assert [hit.chunk_ref for hit in hits] == [chunks[-1].ref, chunks[-2].ref]
    assert hits[0].score > hits[1].score


def test_update_document_only_reconsiders_current_version(
    knowledge_store: KnowledgeStore,
) -> None:
    first = _commit(
        knowledge_store,
        "旧版本包含 api_key=sk-abcdefghijklmnop",
        confirm_sensitivity_override=True,
    )
    assert first.document.sensitivity_override_confirmed is True
    second = _commit(
        knowledge_store,
        "新版本已经删掉了所有密钥内容",
        replace_document_ref=first.document.ref,
    )
    assert second.document.sensitivity == "normal"

    updated = knowledge_store.update_document(
        "alice", document_ref=second.document.ref, title="只改标题"
    )
    assert updated.sensitivity == "normal"
    assert updated.detected_sensitivity == "normal"

    # Raising a confirmed override keeps the confirmation instead of jumping
    # straight to the detected level.
    third = _commit(
        knowledge_store,
        "内部备忘：api_key=sk-zzzzzzzzzzzzzzzz",
        title="备忘",
        confirm_sensitivity_override=True,
    )
    raised = knowledge_store.update_document(
        "alice", document_ref=third.document.ref, sensitivity="private"
    )
    assert raised.sensitivity == "private"
    assert raised.sensitivity_override_confirmed is True


def test_restore_requires_fresh_confirmation_for_overridden_sensitivity(
    knowledge_store: KnowledgeStore,
) -> None:
    payload = {
        "format": "memory-gateway-knowledge",
        "schema_version": 3,
        "documents": [
            {
                "title": "凭据",
                "sensitivity": "normal",
                "sensitivity_override_confirmed": True,
                "current_version_number": 1,
                "versions": [
                    {"version_number": 1, "content": "api_key=sk-abcdefghijklmnop"}
                ],
            }
        ],
    }

    with pytest.raises(KnowledgeSensitivityConfirmationRequired):
        knowledge_store.restore_export("bob", payload)
    assert knowledge_store.list_documents("bob", include_sensitive=True) == []

    outcome = knowledge_store.restore_export(
        "bob", payload, confirm_sensitivity_override=True
    )
    assert outcome["restored_documents"] == 1
    document = knowledge_store.list_documents("bob")[0]
    assert document.sensitivity == "normal"
    assert document.sensitivity_override_confirmed is True


def test_finished_upload_sessions_are_pruned_and_open_sessions_are_capped(
    knowledge_store: KnowledgeStore,
) -> None:
    import sqlite3

    from app.knowledge.store import constants

    for index in range(3):
        _commit(knowledge_store, f"正文 {index}", title=f"文档 {index}")
    with sqlite3.connect(knowledge_store.database_path) as connection:
        connection.execute(
            "UPDATE knowledge_upload_sessions SET updated_at = '2000-01-01T00:00:00.000000Z'"
        )
    knowledge_store.begin_upload("alice", "触发清理")
    with sqlite3.connect(knowledge_store.database_path) as connection:
        statuses = [
            row[0]
            for row in connection.execute("SELECT status FROM knowledge_upload_sessions")
        ]
    assert statuses == ["open"]

    for index in range(constants._MAX_OPEN_UPLOADS_PER_USER - 1):
        knowledge_store.begin_upload("alice", f"并发上传 {index}")
    with pytest.raises(KnowledgeConflictError, match="too many open upload sessions"):
        knowledge_store.begin_upload("alice", "超出上限")
    # Other users are unaffected.
    assert knowledge_store.begin_upload("bob", "别人的上传").status == "open"
