import {
  ArchiveRestore,
  ArrowLeft,
  BookOpenText,
  ChevronDown,
  Clipboard,
  Download,
  FileClock,
  FilePlus2,
  Pencil,
  RefreshCcw,
  RotateCcw,
  Save,
  Search,
  ShieldAlert,
  Trash2,
  Upload,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isAbortError, type MemoryApi } from "../../api";
import { Badge } from "../../components/Badge";
import { DataTable } from "../../components/DataTable";
import { Modal } from "../../components/Modal";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type {
  KnowledgeDocument,
  KnowledgeDocumentDetail,
  KnowledgeDocumentStatus,
  KnowledgeExport,
  KnowledgeReadResponse,
  KnowledgeVersion,
  MemorySensitivity
} from "../../types";
import { copyText, downloadFile } from "../../utils/files";
import { dateText, errorMessage, shortId } from "../../utils/format";
import type { Notify } from "../pageTypes";
import {
  knowledgeDocumentBytes,
  knowledgeDocumentRef,
  knowledgeVersionBytes,
  knowledgeVersionRef,
  knowledgeVersionSha
} from "./knowledgeData";
import { formatBytes, KnowledgeUploadForm } from "./KnowledgeUploadForm";

export function KnowledgeLibraryPage({
  api,
  documentId,
  notify,
  confirm,
  onOpenDocument,
  onCloseDocument,
  onChanged
}: {
  api: MemoryApi;
  documentId: string | null;
  notify: Notify;
  confirm: ConfirmFn;
  onOpenDocument: (id: string) => void;
  onCloseDocument: () => void;
  onChanged: () => void;
}) {
  if (documentId) {
    return (
      <KnowledgeDetailPage
        api={api}
        documentId={documentId}
        notify={notify}
        confirm={confirm}
        onBack={onCloseDocument}
        onChanged={onChanged}
      />
    );
  }
  return (
    <KnowledgeListPage
      api={api}
      notify={notify}
      confirm={confirm}
      onOpenDocument={onOpenDocument}
      onChanged={onChanged}
    />
  );
}

function KnowledgeListPage({
  api,
  notify,
  confirm,
  onOpenDocument,
  onChanged
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
  onOpenDocument: (id: string) => void;
  onChanged: () => void;
}) {
  const [tab, setTab] = useState<KnowledgeDocumentStatus>("active");
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [documents, setDocuments] = useState<KnowledgeDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showUpload, setShowUpload] = useState(false);
  const [purgeTarget, setPurgeTarget] = useState<KnowledgeDocument | null>(null);
  const [restorePreview, setRestorePreview] = useState<KnowledgeExport | null>(null);
  const [restoring, setRestoring] = useState(false);
  const restoreInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      setDocuments(await api.listKnowledgeDocuments({ status: tab, query: submittedQuery, limit: 500 }, signal));
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setDocuments(null);
      setError(errorMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, [api, submittedQuery, tab]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const runSearch = () => setSubmittedQuery(query.trim());

  const restoreDocument = async (document: KnowledgeDocument) => {
    try {
      await api.restoreKnowledgeDocument(document.id);
      notify("文档已从回收站恢复", "success");
      onChanged();
      await load();
    } catch (restoreError) {
      notify(errorMessage(restoreError), "error");
    }
  };

  const exportBackup = async () => {
    try {
      const exported = await api.exportKnowledge();
      downloadFile(
        `knowledge-export-${new Date().toISOString().slice(0, 10)}.json`,
        JSON.stringify(exported, null, 2),
        "application/json"
      );
      notify("知识库备份已下载", "success");
    } catch (exportError) {
      notify(errorMessage(exportError), "error");
    }
  };

  const chooseRestoreFile = async (file: File | null) => {
    if (!file) return;
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      const parsed = JSON.parse(text) as KnowledgeExport;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("备份格式无效");
      setRestorePreview(parsed);
      notify("备份文件已读取，请确认后恢复", "success");
    } catch (restoreError) {
      setRestorePreview(null);
      notify(restoreError instanceof TypeError ? "备份文件不是有效的 UTF-8 JSON" : errorMessage(restoreError), "error");
      if (restoreInputRef.current) restoreInputRef.current.value = "";
    }
  };

  const restoreBackup = async () => {
    if (!restorePreview) return;
    const ok = await confirm({
      title: "恢复知识库备份",
      message: "正文和版本历史会恢复到当前用户，并重新建立索引。现有同标识内容由服务端按恢复规则处理。",
      confirmLabel: "开始恢复",
      tone: "warning"
    });
    if (!ok) return;
    setRestoring(true);
    try {
      const result = await api.restoreKnowledge(restorePreview);
      const restored = result.restored_documents ?? 0;
      const skipped = result.skipped_documents ?? 0;
      const failed = result.failed_versions ?? 0;
      if (failed > 0) {
        notify(`恢复完成：新增 ${restored}，跳过 ${skipped}；但有 ${failed} 个版本索引失败，请在文档详情中重建索引`, "error");
      } else {
        notify(`恢复完成：新增 ${restored}，跳过 ${skipped}`, "success");
      }
      setRestorePreview(null);
      if (restoreInputRef.current) restoreInputRef.current.value = "";
      onChanged();
      await load();
    } catch (restoreError) {
      notify(errorMessage(restoreError), "error");
    } finally {
      setRestoring(false);
    }
  };

  return (
    <div className="page-stack knowledge-page">
      <PageHeader
        title="知识库"
        subtitle="保存不适合一次提交的长文本。知识与长期记忆物理隔离，只会在 AI 明确调用知识工具时检索。"
        action={
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => void load()}>
              <RefreshCcw size={16} /> 刷新
            </button>
            <button className="primary-button" type="button" onClick={() => setShowUpload((current) => !current)} aria-expanded={showUpload}>
              {showUpload ? <X size={16} /> : <FilePlus2 size={16} />}
              {showUpload ? "收起" : "添加文档"}
            </button>
          </div>
        }
      />

      <div className="notice knowledge-boundary-note">
        <BookOpenText size={17} />
        <span>这里的内容不参与记忆 RAG、自动上下文、活跃度统计或记忆衰减。</span>
      </div>

      {showUpload && (
        <section className="panel knowledge-compose-panel" aria-label="添加知识文档">
          <div className="panel-header">
            <div>
              <h2>添加长文本</h2>
              <p className="muted">粘贴正文或选择本机 UTF-8 文本文件。</p>
            </div>
          </div>
          <KnowledgeUploadForm
            api={api}
            notify={notify}
            onCancel={() => setShowUpload(false)}
            onComplete={(result) => {
              setShowUpload(false);
              onChanged();
              onOpenDocument(result.document.id);
            }}
          />
        </section>
      )}

      <section className="panel knowledge-table-panel">
        <div className="knowledge-library-toolbar">
          <div className="tabs" role="tablist" aria-label="文档状态">
            <button type="button" role="tab" aria-selected={tab === "active"} className={tab === "active" ? "active" : ""} onClick={() => setTab("active")}>有效文档</button>
            <button type="button" role="tab" aria-selected={tab === "deleted"} className={tab === "deleted" ? "active" : ""} onClick={() => setTab("deleted")}>回收站</button>
          </div>
          <form className="search-box knowledge-search-box" onSubmit={(event) => { event.preventDefault(); runSearch(); }} role="search">
            <Search size={16} aria-hidden="true" />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或来源" aria-label="搜索知识文档" />
            {query && <button className="search-clear" type="button" onClick={() => { setQuery(""); setSubmittedQuery(""); }} aria-label="清除搜索"><X size={14} /></button>}
          </form>
          <span className="result-count">{documents?.length ?? 0} 个文档</span>
        </div>

        {loading && <LoadingBlock label="正在加载知识文档" />}
        {error && <ErrorBlock message={error} onRetry={() => void load()} />}
        {!loading && !error && documents?.length === 0 && (
          <EmptyBlock
            label={tab === "deleted" ? "回收站为空" : submittedQuery ? "没有匹配的文档" : "知识库还是空的"}
            hint={tab === "deleted" ? "软删除的文档会保留在这里，直到你永久清理。" : submittedQuery ? "换一个标题或来源关键词试试。" : "添加项目文档、笔记或需要精确引用的长文本。"}
            action={tab === "active" && !submittedQuery ? { label: "添加第一个文档", onClick: () => setShowUpload(true) } : undefined}
          />
        )}
        {!loading && !error && documents && documents.length > 0 && (
          <DataTable>
            <thead>
              <tr>
                <th>文档</th>
                <th>格式</th>
                <th>大小</th>
                <th>当前版本</th>
                <th>索引</th>
                <th>更新时间</th>
                <th className="actions-cell">操作</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => (
                <tr key={document.id}>
                  <td>
                    <button className="knowledge-document-link" type="button" onClick={() => onOpenDocument(document.id)}>
                      <strong>{document.title}</strong>
                      <span>{document.source_name || shortId(knowledgeDocumentRef(document))}</span>
                    </button>
                  </td>
                  <td>{contentTypeLabel(document.content_type)}</td>
                  <td>{formatBytes(knowledgeDocumentBytes(document))}</td>
                  <td>{documentVersionNumber(document) ? `v${documentVersionNumber(document)}` : "—"}</td>
                  <td><IndexBadge status={documentIndexStatus(document)} /></td>
                  <td>{dateText(document.updated_at)}</td>
                  <td className="actions-cell">
                    {tab === "active" ? (
                      <button className="secondary-button compact" type="button" onClick={() => onOpenDocument(document.id)}>打开</button>
                    ) : (
                      <div className="table-action-row">
                        <button className="secondary-button compact" type="button" onClick={() => void restoreDocument(document)}><ArchiveRestore size={14} />恢复</button>
                        <button className="danger-button compact" type="button" onClick={() => setPurgeTarget(document)}><Trash2 size={14} />永久删除</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </section>

      <section className="panel knowledge-backup-panel">
        <div className="panel-header">
          <div>
            <h2>独立备份</h2>
            <p className="muted">知识备份包含原文、元数据与版本历史，不包含可重建的 chunk 或 FTS 索引。</p>
          </div>
          <button className="secondary-button" type="button" onClick={() => void exportBackup()}><Download size={16} />下载 JSON</button>
        </div>
        <div className="notice warning"><ShieldAlert size={17} /><span>导出文件包含完整普通、私密与敏感正文，请按敏感备份保管。</span></div>
        <div className="knowledge-restore-row">
          <label className="upload-box">
            <Upload size={16} />选择知识备份
            <input ref={restoreInputRef} type="file" accept="application/json,.json" disabled={restoring} onChange={(event) => void chooseRestoreFile(event.target.files?.[0] || null)} />
          </label>
          {restorePreview && (
            <>
              <span className="muted">已读取 {restorePreview.documents?.length ?? "若干"} 个文档</span>
              <button className="warning-button" type="button" disabled={restoring} onClick={() => void restoreBackup()}><RotateCcw size={16} />{restoring ? "正在恢复" : "确认恢复"}</button>
              <button className="ghost-button compact" type="button" disabled={restoring} onClick={() => setRestorePreview(null)}>取消</button>
            </>
          )}
        </div>
      </section>

      {purgeTarget && (
        <PurgeKnowledgeDialog
          api={api}
          document={purgeTarget}
          notify={notify}
          onClose={() => setPurgeTarget(null)}
          onPurged={async () => {
            setPurgeTarget(null);
            onChanged();
            await load();
          }}
        />
      )}
    </div>
  );
}

function KnowledgeDetailPage({
  api,
  documentId,
  notify,
  confirm,
  onBack,
  onChanged
}: {
  api: MemoryApi;
  documentId: string;
  notify: Notify;
  confirm: ConfirmFn;
  onBack: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<KnowledgeDocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedVersionRef, setSelectedVersionRef] = useState("");
  const [readPages, setReadPages] = useState<KnowledgeReadResponse[]>([]);
  const [readError, setReadError] = useState<string | null>(null);
  const [reading, setReading] = useState(false);
  const [showNewVersion, setShowNewVersion] = useState(false);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [title, setTitle] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [sensitivity, setSensitivity] = useState<MemorySensitivity>("normal");
  const [purgeOpen, setPurgeOpen] = useState(false);
  // 正文读取请求序号：每次发起新读取（含切换版本、清空）都会递增，
  // 在途的旧请求（尤其是“继续加载”的 append）resolve 后据此丢弃，避免串版本追加和 loading 态串扰。
  const readRequestRef = useRef(0);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.knowledgeDocument(documentId, signal);
      setDetail(next);
      setTitle(next.document.title);
      setSourceName(next.document.source_name || "");
      setSensitivity(next.document.sensitivity || "normal");
      const current = currentVersion(next);
      const latest = latestVersion(next.versions);
      setSelectedVersionRef((existing) =>
        next.versions.some((version) => knowledgeVersionRef(version) === existing)
          ? existing
          : (current ? knowledgeVersionRef(current) : "") || next.document.current_version_ref || (latest ? knowledgeVersionRef(latest) : "")
      );
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setDetail(null);
      setError(errorMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, [api, documentId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const loadRead = useCallback(async (reference: string, cursor = "", append = false, signal?: AbortSignal) => {
    if (!reference) return;
    const requestId = ++readRequestRef.current;
    setReading(true);
    setReadError(null);
    try {
      const page = await api.readKnowledge({ reference, cursor, maxChars: 20000, includeSensitive: true }, signal);
      if (requestId !== readRequestRef.current) return;
      setReadPages((current) => append ? [...current, page] : [page]);
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      if (requestId !== readRequestRef.current) return;
      if (!append) setReadPages([]);
      setReadError(errorMessage(loadError));
    } finally {
      if (requestId === readRequestRef.current) setReading(false);
    }
  }, [api]);

  useEffect(() => {
    if (!selectedVersionRef || detail?.document.status === "deleted") {
      readRequestRef.current += 1;
      setReadPages([]);
      setReadError(null);
      setReading(false);
      return;
    }
    const controller = new AbortController();
    setReadPages([]);
    void loadRead(selectedVersionRef, "", false, controller.signal);
    return () => controller.abort();
  }, [detail?.document.status, loadRead, selectedVersionRef]);

  const saveMetadata = async () => {
    if (!detail || !title.trim()) return;
    setSaving(true);
    try {
      await api.updateKnowledgeDocument(detail.document.id, {
        title: title.trim(),
        source_name: sourceName.trim(),
        sensitivity
      });
      notify("文档信息已更新", "success");
      setEditing(false);
      onChanged();
      await load();
    } catch (saveError) {
      notify(errorMessage(saveError), "error");
    } finally {
      setSaving(false);
    }
  };

  const softDelete = async () => {
    if (!detail) return;
    const ok = await confirm({
      title: "移入知识回收站",
      message: `“${detail.document.title}”将停止出现在搜索和读取结果中，之后可以恢复。`,
      confirmLabel: "移入回收站",
      tone: "warning"
    });
    if (!ok) return;
    try {
      await api.deleteKnowledgeDocument(detail.document.id);
      notify("文档已移入回收站", "success");
      onChanged();
      onBack();
    } catch (deleteError) {
      notify(errorMessage(deleteError), "error");
    }
  };

  const restoreDocument = async () => {
    if (!detail) return;
    try {
      await api.restoreKnowledgeDocument(detail.document.id);
      notify("文档已恢复", "success");
      onChanged();
      await load();
    } catch (restoreError) {
      notify(errorMessage(restoreError), "error");
    }
  };

  const restoreVersion = async (version: KnowledgeVersion) => {
    if (!detail) return;
    const ok = await confirm({
      title: `恢复 v${version.version_number}`,
      message: "历史正文会复制成一个新的递增版本，现有版本不会被覆盖。",
      confirmLabel: "创建恢复版本",
      tone: "warning"
    });
    if (!ok) return;
    try {
      const result = await api.restoreKnowledgeVersion(detail.document.id, version.id);
      notify("历史正文已恢复为新版本", "success");
      setSelectedVersionRef(knowledgeVersionRef(result.version));
      onChanged();
      await load();
    } catch (restoreError) {
      notify(errorMessage(restoreError), "error");
    }
  };

  const reindex = async (version: KnowledgeVersion) => {
    if (!detail) return;
    try {
      await api.reindexKnowledgeDocument(detail.document.id, version.id);
      notify("重新索引已完成", "success");
      onChanged();
      await load();
    } catch (indexError) {
      notify(errorMessage(indexError), "error");
    }
  };

  if (loading) return <div className="page-stack"><LoadingBlock label="正在加载知识文档" /></div>;
  if (error || !detail) return <div className="page-stack"><button className="ghost-button knowledge-back-button" type="button" onClick={onBack}><ArrowLeft size={16} />返回知识库</button><ErrorBlock message={error || "文档不存在"} onRetry={() => void load()} /></div>;

  const document = detail.document;
  const selectedVersion = detail.versions.find((version) => knowledgeVersionRef(version) === selectedVersionRef);
  const current = currentVersion(detail);
  const lastPage = readPages[readPages.length - 1];
  const content = readPages.map(readPageText).join("");

  return (
    <div className="page-stack knowledge-page knowledge-detail-page">
      <div className="breadcrumb">
        <button type="button" onClick={onBack}>知识库</button>
        <span className="separator">/</span>
        <span>{document.title}</span>
      </div>
      <PageHeader
        title={document.title}
        subtitle="完整路由页面 · 正文按不可变版本读取，Markdown 以纯文本显示。"
        action={
          <div className="button-row">
            {document.status === "deleted" ? (
              <>
                <button className="secondary-button" type="button" onClick={() => void restoreDocument()}><ArchiveRestore size={16} />恢复</button>
                <button className="danger-button" type="button" onClick={() => setPurgeOpen(true)}><Trash2 size={16} />永久删除</button>
              </>
            ) : (
              <>
                <button className="secondary-button" type="button" onClick={() => setEditing((currentValue) => !currentValue)}><Pencil size={16} />编辑信息</button>
                <button className="primary-button" type="button" onClick={() => setShowNewVersion((currentValue) => !currentValue)}><FilePlus2 size={16} />新版本</button>
                <button className="danger-button" type="button" onClick={() => void softDelete()}><Trash2 size={16} />移入回收站</button>
              </>
            )}
          </div>
        }
      />

      <section className="knowledge-meta-strip" aria-label="文档信息">
        <div><span>格式</span><strong>{contentTypeLabel(document.content_type)}</strong></div>
        <div><span>大小</span><strong>{formatBytes(knowledgeDocumentBytes(document))}</strong></div>
        <div><span>当前版本</span><strong>{documentVersionNumber(document) ? `v${documentVersionNumber(document)}` : "—"}</strong></div>
        <div><span>敏感级别</span><Badge value={document.sensitivity || "normal"} /></div>
        <div><span>索引状态</span><IndexBadge status={documentIndexStatus(document)} /></div>
      </section>

      {editing && (
        <section className="panel knowledge-metadata-editor">
          <div className="panel-header"><h2>文档信息</h2><button className="icon-button" type="button" onClick={() => setEditing(false)} aria-label="关闭编辑"><X size={17} /></button></div>
          <div className="knowledge-form-grid">
            <label className="field-block knowledge-title-field"><span>标题</span><input value={title} maxLength={240} onChange={(event) => setTitle(event.target.value)} /></label>
            <label className="field-block knowledge-source-field"><span>来源名称</span><input value={sourceName} maxLength={500} onChange={(event) => setSourceName(event.target.value)} /></label>
            <label className="field-block"><span>敏感级别</span><select value={sensitivity} onChange={(event) => setSensitivity(event.target.value as MemorySensitivity)}><option value="normal">普通</option><option value="private">私密</option><option value="sensitive">敏感</option></select></label>
          </div>
          <div className="button-row end"><button className="ghost-button" type="button" disabled={saving} onClick={() => setEditing(false)}>取消</button><button className="primary-button" type="button" disabled={saving || !title.trim()} onClick={() => void saveMetadata()}><Save size={16} />{saving ? "正在保存" : "保存"}</button></div>
        </section>
      )}

      {showNewVersion && document.status === "active" && (
        <section className="panel knowledge-compose-panel">
          <div className="panel-header"><div><h2>创建新版本</h2><p className="muted">索引成功后才会切换当前版本；失败时旧版本继续可用。</p></div></div>
          <KnowledgeUploadForm
            api={api}
            notify={notify}
            replaceDocumentRef={knowledgeDocumentRef(document)}
            initialTitle={document.title}
            initialSourceName={document.source_name}
            initialSensitivity={document.sensitivity}
            initialContentType={document.content_type}
            onCancel={() => setShowNewVersion(false)}
            onComplete={(result) => {
              setShowNewVersion(false);
              setSelectedVersionRef(knowledgeVersionRef(result.version));
              onChanged();
              void load();
            }}
          />
        </section>
      )}

      <div className="knowledge-detail-layout">
        <section className="panel knowledge-reader-panel">
          <div className="panel-header knowledge-reader-header">
            <div>
              <h2>{selectedVersion ? `正文 · v${selectedVersion.version_number}` : "正文"}</h2>
              <span className="muted">{selectedVersion && knowledgeVersionSha(selectedVersion) ? `SHA-256 ${knowledgeVersionSha(selectedVersion).slice(0, 12)}…` : document.source_name || "本地知识文档"}</span>
            </div>
            {selectedVersionRef && <button className="secondary-button compact" type="button" onClick={() => void copyText(selectedVersionRef).then(() => notify("版本引用已复制", "success"))}><Clipboard size={14} />复制引用</button>}
          </div>
          {document.status === "deleted" && <EmptyBlock label="正文暂不可读" hint="该文档位于回收站；恢复后才能通过 Web 或 MCP 读取正文。" />}
          {document.status !== "deleted" && reading && readPages.length === 0 && <LoadingBlock label="正在读取正文" />}
          {document.status !== "deleted" && readError && <ErrorBlock message={readError} onRetry={() => void loadRead(selectedVersionRef)} />}
          {document.status !== "deleted" && !readError && readPages.length > 0 && (
            <>
              <pre className="knowledge-reader">{content}</pre>
              {!lastPage.complete && lastPage.next_cursor && (
                <button className="secondary-button knowledge-load-more" type="button" disabled={reading} onClick={() => void loadRead(selectedVersionRef, lastPage.next_cursor, true)}>
                  <ChevronDown size={16} />{reading ? "正在读取" : "继续加载正文"}
                </button>
              )}
              <div className="knowledge-reader-foot muted">已加载 {content.length.toLocaleString()} 个字符{lastPage.complete ? " · 已到文末" : ""}</div>
            </>
          )}
        </section>

        <aside className="panel knowledge-version-panel">
          <div className="panel-header"><h2>版本历史</h2><span className="muted">{detail.versions.length} 个版本</span></div>
          <div className="knowledge-version-list">
            {[...detail.versions].sort((a, b) => b.version_number - a.version_number).map((version) => {
              const isCurrent = current?.id === version.id || (current ? knowledgeVersionRef(current) : "") === knowledgeVersionRef(version);
              const isSelected = selectedVersionRef === knowledgeVersionRef(version);
              return (
                <article className={`knowledge-version-item ${isSelected ? "selected" : ""}`} key={version.id}>
                  <button className="knowledge-version-select" type="button" onClick={() => setSelectedVersionRef(knowledgeVersionRef(version))} aria-pressed={isSelected}>
                    <span><strong>v{version.version_number}</strong>{isCurrent && <em>当前</em>}</span>
                    <small>{dateText(version.created_at)}</small>
                    <small>{formatBytes(knowledgeVersionBytes(version))}</small>
                  </button>
                  <div className="knowledge-version-actions">
                    <IndexBadge status={version.index_status} />
                    {version.index_status === "failed" && <button className="ghost-button compact" type="button" onClick={() => void reindex(version)}><RefreshCcw size={13} />重建索引</button>}
                    {!isCurrent && document.status === "active" && <button className="ghost-button compact" type="button" onClick={() => void restoreVersion(version)}><FileClock size={13} />恢复为新版本</button>}
                  </div>
                  {version.index_error && <p className="danger-text">{version.index_error}</p>}
                </article>
              );
            })}
          </div>
        </aside>
      </div>

      {purgeOpen && <PurgeKnowledgeDialog api={api} document={document} notify={notify} onClose={() => setPurgeOpen(false)} onPurged={() => { setPurgeOpen(false); onChanged(); onBack(); }} />}
    </div>
  );
}

function PurgeKnowledgeDialog({
  api,
  document,
  notify,
  onClose,
  onPurged
}: {
  api: MemoryApi;
  document: KnowledgeDocument;
  notify: Notify;
  onClose: () => void;
  onPurged: () => void | Promise<void>;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const required = document.id;

  const purge = async () => {
    if (confirmation !== required) return;
    setBusy(true);
    try {
      await api.purgeKnowledgeDocument(document.id, confirmation);
      notify("知识文档及其版本已永久删除", "success");
      await onPurged();
    } catch (purgeError) {
      notify(errorMessage(purgeError), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="永久删除知识文档" onClose={onClose} closeDisabled={busy} className="knowledge-purge-dialog">
      <div className="notice warning"><ShieldAlert size={18} /><span>此操作无法撤销。文档正文、全部版本、chunk 与索引都会被清理。</span></div>
      <p>请输入完整文档 ID 以确认删除“{document.title}”：</p>
      <code className="knowledge-confirm-id">{required}</code>
      <label className="field-block"><span>完整文档 ID</span><input value={confirmation} disabled={busy} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" data-autofocus /></label>
      <div className="drawer-actions end"><button className="ghost-button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="danger-button" type="button" disabled={busy || confirmation !== required} onClick={() => void purge()}><Trash2 size={16} />{busy ? "正在删除" : "永久删除"}</button></div>
    </Modal>
  );
}

function IndexBadge({ status }: { status: string }) {
  const label = status === "ready" || status === "indexed" ? "已索引" : status === "indexing" ? "索引中" : status === "failed" ? "失败" : "等待中";
  return <span className={`knowledge-index-badge knowledge-index-${status}`}>{label}</span>;
}

function currentVersion(detail: KnowledgeDocumentDetail): KnowledgeVersion | undefined {
  const document = detail.document;
  return detail.versions.find((version) =>
    version.id === document.current_version_id ||
    knowledgeVersionRef(version) === document.current_version_ref ||
    version.version_number === document.current_version_number
  );
}

function latestVersion(versions: KnowledgeVersion[]): KnowledgeVersion | undefined {
  return versions.reduce<KnowledgeVersion | undefined>(
    (latest, version) => !latest || version.version_number > latest.version_number ? version : latest,
    undefined
  );
}

function documentVersionNumber(document: KnowledgeDocument): number | null {
  return document.current_version_number ?? document.current_version?.version_number ?? null;
}

function documentIndexStatus(document: KnowledgeDocument): string {
  return document.index_status || document.current_version?.index_status || "pending";
}

function contentTypeLabel(value: string): string {
  if (value === "text/markdown") return "Markdown";
  if (value === "text/plain") return "纯文本";
  return value || "文本";
}

function readPageText(page: KnowledgeReadResponse): string {
  return page.content ?? page.text ?? "";
}
