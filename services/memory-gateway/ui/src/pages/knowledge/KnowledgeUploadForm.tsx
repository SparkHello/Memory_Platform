import { FileText, ShieldAlert, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, type MemoryApi } from "../../api";
import type {
  KnowledgeUploadCommitResult,
  MemorySensitivity
} from "../../types";
import { errorMessage } from "../../utils/format";
import type { Notify } from "../pageTypes";

const DEFAULT_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024;
// Web 走 REST 的大分片通道；MCP 工具自身仍保持 20,000 字符上限。
const PART_SIZE = 256_000;

type SourceMode = "paste" | "file";
type SensitivityConfirmation = {
  declared: MemorySensitivity;
  detected: MemorySensitivity;
};

export function KnowledgeUploadForm({
  api,
  notify,
  replaceDocumentRef = "",
  initialTitle = "",
  initialSourceName = "",
  initialSensitivity = "normal",
  initialContentType = "text/markdown",
  initialTags = [],
  initialMetadata = {},
  maxDocumentBytes = DEFAULT_MAX_DOCUMENT_BYTES,
  onComplete,
  onCancel
}: {
  api: MemoryApi;
  notify: Notify;
  replaceDocumentRef?: string;
  initialTitle?: string;
  initialSourceName?: string;
  initialSensitivity?: MemorySensitivity;
  initialContentType?: string;
  initialTags?: string[];
  initialMetadata?: Record<string, string | number | boolean>;
  maxDocumentBytes?: number;
  onComplete: (result: KnowledgeUploadCommitResult) => void;
  onCancel?: () => void;
}) {
  const [mode, setMode] = useState<SourceMode>("paste");
  const [title, setTitle] = useState(initialTitle);
  const [sourceName, setSourceName] = useState(initialSourceName);
  const [contentType, setContentType] = useState(
    initialContentType.startsWith("text/") ? initialContentType : "text/markdown"
  );
  const [sensitivity, setSensitivity] = useState<MemorySensitivity>(initialSensitivity);
  const [tagsText, setTagsText] = useState(initialTags.join(", "));
  const [metadataText, setMetadataText] = useState(
    Object.keys(initialMetadata).length ? JSON.stringify(initialMetadata, null, 2) : ""
  );
  const [pasteText, setPasteText] = useState("");
  const [fileText, setFileText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sensitivityConfirmation, setSensitivityConfirmation] =
    useState<SensitivityConfirmation | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0, label: "" });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const sensitivityConfirmationRef = useRef<HTMLDivElement>(null);
  const text = mode === "paste" ? pasteText : fileText;

  useEffect(() => {
    if (sensitivityConfirmation) {
      sensitivityConfirmationRef.current?.focus({ preventScroll: true });
      sensitivityConfirmationRef.current?.scrollIntoView({
        behavior: "auto",
        block: "center"
      });
    }
  }, [sensitivityConfirmation]);

  const chooseMode = (next: SourceMode) => {
    if (busy) return;
    setSensitivityConfirmation(null);
    setMode(next);
    if (next === "paste") {
      setContentType(initialContentType.startsWith("text/") ? initialContentType : "text/markdown");
    } else if (file) {
      setContentType(contentTypeForFile(file.name));
    }
  };

  const readFile = async (file: File | null) => {
    if (!file) return;
    setSensitivityConfirmation(null);
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (!extension || !["txt", "md", "markdown", "pdf", "docx", "epub"].includes(extension)) {
      notify("支持 .txt、.md、.pdf、.docx 和 .epub 文件", "error");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    if (file.size > maxDocumentBytes) {
      notify(`单个文档不能超过 ${formatBytes(maxDocumentBytes)}`, "error");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    try {
      let preview = "";
      if (["txt", "md", "markdown"].includes(extension)) {
        const buffer = await file.arrayBuffer();
        preview = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
        if (!preview.trim()) throw new Error("文件内容为空");
      }
      setFile(file);
      setFileText(preview);
      setContentType(contentTypeForFile(file.name));
      setSourceName(file.name);
      if (!title.trim()) setTitle(file.name.replace(/\.(txt|md|markdown|pdf|docx|epub)$/i, ""));
    } catch (error) {
      const message = error instanceof TypeError ? "文件不是有效的 UTF-8 文本" : errorMessage(error);
      notify(message, "error");
      setFileText("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const submit = async (confirmSensitivityOverride = false) => {
    const cleanTitle = title.trim();
    if (!cleanTitle) {
      notify("请填写文档标题", "error");
      return;
    }
    if (mode === "paste" && !text.trim()) {
      notify("请粘贴正文", "error");
      return;
    }
    if (mode === "file" && !file) {
      notify("请选择一个知识文件", "error");
      return;
    }
    const byteLength = mode === "file" ? file!.size : new TextEncoder().encode(text).byteLength;
    if (byteLength > maxDocumentBytes) {
      notify(`单个文档不能超过 ${formatBytes(maxDocumentBytes)}`, "error");
      return;
    }

    setBusy(true);
    let uploadId = "";
    try {
      const tags = parseTags(tagsText);
      const metadata = parseMetadata(metadataText);
      if (mode === "file") {
        setProgress({ completed: 0, total: 1, label: "正在解析、保存并建立索引" });
        const result = await api.importKnowledgeFile(file!, {
          title: cleanTitle,
          source_name: sourceName.trim(),
          replace_document_ref: replaceDocumentRef,
          sensitivity,
          confirm_sensitivity_override: confirmSensitivityOverride,
          tags,
          metadata
        });
        setProgress({ completed: 1, total: 1, label: "处理完成" });
        notifyCommit(result, replaceDocumentRef, notify);
        onComplete(result);
        return;
      }
      setProgress({ completed: 0, total: 1, label: "正在创建上传会话" });
      const session = await api.beginKnowledgeUpload({
        title: cleanTitle,
        content_type: contentType,
        source_name: sourceName.trim(),
        replace_document_ref: replaceDocumentRef,
        sensitivity,
        tags,
        metadata
      });
      uploadId = session.upload_id || session.id;
      const parts = splitText(text, PART_SIZE);
      setProgress({ completed: 0, total: parts.length, label: "正在上传正文" });
      for (let index = 0; index < parts.length; index += 1) {
        await api.appendKnowledgeUpload(uploadId, index, parts[index]);
        setProgress({ completed: index + 1, total: parts.length, label: "正在上传正文" });
      }
      setProgress({ completed: parts.length, total: parts.length, label: "正在保存并建立索引" });
      const result = await api.commitKnowledgeUpload(
        uploadId,
        parts.length,
        await sha256(text),
        confirmSensitivityOverride
      );
      uploadId = "";
      notifyCommit(result, replaceDocumentRef, notify);
      onComplete(result);
    } catch (error) {
      if (uploadId) {
        void api.cancelKnowledgeUpload(uploadId).catch(() => undefined);
      }
      if (
        error instanceof ApiError
        && error.code === "sensitivity_confirmation_required"
      ) {
        setSensitivityConfirmation({
          declared: sensitivityValue(error.data?.declared_sensitivity, sensitivity),
          detected: sensitivityValue(error.data?.detected_sensitivity, "sensitive")
        });
        return;
      }
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
      setProgress({ completed: 0, total: 0, label: "" });
    }
  };

  const bytes = mode === "file" ? file?.size || 0 : new TextEncoder().encode(text).byteLength;

  return (
    <div className="knowledge-upload-form">
      <div className="tabs knowledge-source-tabs" role="tablist" aria-label="正文来源">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "paste"}
          className={mode === "paste" ? "active" : ""}
          onClick={() => chooseMode("paste")}
        >
          粘贴文本
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "file"}
          className={mode === "file" ? "active" : ""}
          onClick={() => chooseMode("file")}
        >
          上传文件
        </button>
      </div>

      <div className="knowledge-form-grid">
        <label className="field-block knowledge-title-field">
          <span>标题</span>
          <input
            value={title}
            maxLength={240}
            disabled={busy}
            onChange={(event) => {
              setTitle(event.target.value);
              setSensitivityConfirmation(null);
            }}
            placeholder="例如：项目架构说明"
            data-autofocus
          />
        </label>
        <label className="field-block">
          <span>格式</span>
          <select value={contentType} disabled={busy || mode === "file"} onChange={(event) => setContentType(event.target.value)}>
            <option value="text/markdown">Markdown</option>
            <option value="text/plain">纯文本</option>
            <option value="application/pdf">PDF</option>
            <option value="application/vnd.openxmlformats-officedocument.wordprocessingml.document">Word</option>
            <option value="application/epub+zip">EPUB</option>
          </select>
        </label>
        <label className="field-block">
          <span>敏感级别</span>
          <select
            value={sensitivity}
            disabled={busy}
            onChange={(event) => {
              setSensitivity(event.target.value as MemorySensitivity);
              setSensitivityConfirmation(null);
            }}
          >
            <option value="normal">普通</option>
            <option value="private">私密</option>
            <option value="sensitive">敏感</option>
          </select>
        </label>
        <label className="field-block knowledge-source-field">
          <span>来源名称（可选）</span>
          <input
            value={sourceName}
            maxLength={500}
            disabled={busy}
            onChange={(event) => {
              setSourceName(event.target.value);
              setSensitivityConfirmation(null);
            }}
            placeholder="仅记录名称，不读取服务器路径"
          />
        </label>
        <label className="field-block knowledge-source-field">
          <span>标签（可选）</span>
          <input
            value={tagsText}
            maxLength={2000}
            disabled={busy}
            onChange={(event) => setTagsText(event.target.value)}
            placeholder="例如：产品, 架构, 2026"
          />
        </label>
      </div>

      <details className="knowledge-metadata-details">
        <summary>结构化元数据（可选）</summary>
        <label className="field-block">
          <span>JSON 对象</span>
          <textarea
            value={metadataText}
            disabled={busy}
            rows={4}
            onChange={(event) => setMetadataText(event.target.value)}
            placeholder={'例如：{"department":"研发","year":2026}'}
          />
        </label>
      </details>

      {mode === "file" ? (
        <div className="knowledge-file-picker">
          <label className="upload-box">
            <Upload size={17} />
            {file ? "更换文件" : "选择知识文件"}
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.md,.markdown,.pdf,.docx,.epub,text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/epub+zip"
              disabled={busy}
              onChange={(event) => void readFile(event.target.files?.[0] || null)}
            />
          </label>
          {file && (
            <span className="knowledge-file-name">
              <FileText size={15} /> {file.name}
            </span>
          )}
        </div>
      ) : (
        <label className="field-block knowledge-text-field">
          <span>正文</span>
          <textarea
            value={pasteText}
            disabled={busy}
            rows={14}
            onChange={(event) => {
              setPasteText(event.target.value);
              setSensitivityConfirmation(null);
            }}
            placeholder="在这里粘贴长文本。知识不会进入记忆衰减或自动记忆召回。"
          />
        </label>
      )}

      {mode === "file" && fileText && (
        <div className="knowledge-file-preview" aria-label="文件预览">
          {fileText.slice(0, 800)}{fileText.length > 800 ? "…" : ""}
        </div>
      )}

      {sensitivityConfirmation && (
        <div
          ref={sensitivityConfirmationRef}
          className="knowledge-sensitivity-confirmation"
          role="alert"
          tabIndex={-1}
        >
          <ShieldAlert size={19} />
          <div>
            <strong>请确认文档敏感级别</strong>
            <p>
              你选择了“{sensitivityLabel(sensitivityConfirmation.declared)}”，
              但本地规则检测为“{sensitivityLabel(sensitivityConfirmation.detected)}”。
              规则可能因教材示例、号码或“密码”等词误判。
            </p>
            <p>
              只有点击确认后，系统才会以你的选择为准继续导入，并记录这次确认。
            </p>
            <div className="button-row">
              <button
                type="button"
                className="ghost-button"
                disabled={busy}
                onClick={() => setSensitivityConfirmation(null)}
              >
                返回检查
              </button>
              <button
                type="button"
                className="warning-button"
                disabled={busy}
                onClick={() => void submit(true)}
              >
                确认按“{sensitivityLabel(sensitivityConfirmation.declared)}”导入
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="knowledge-upload-footer">
        <div className="knowledge-upload-note">
          <ShieldAlert size={15} />
          <span>支持 UTF-8 文本、Markdown、PDF、DOCX 和 EPUB，单版本上限 {formatBytes(maxDocumentBytes)}；解析在本机服务端完成。</span>
        </div>
        <span className={bytes > maxDocumentBytes ? "danger-text" : "muted"}>
          {formatBytes(bytes)} / {formatBytes(maxDocumentBytes)}
        </span>
      </div>

      {busy && (
        <div className="knowledge-progress" role="status" aria-live="polite">
          <div>
            <span>{progress.label}</span>
            <strong>{progress.total ? `${progress.completed}/${progress.total}` : ""}</strong>
          </div>
          <progress max={Math.max(progress.total, 1)} value={progress.completed} />
        </div>
      )}

      <div className="button-row end">
        {onCancel && (
          <button className="ghost-button" type="button" disabled={busy} onClick={onCancel}>
            <X size={16} />
            取消
          </button>
        )}
        <button
          className="primary-button"
          type="button"
          disabled={
            busy
            || Boolean(sensitivityConfirmation)
            || !title.trim()
            || (mode === "file" ? !file : !text.trim())
          }
          onClick={() => void submit(false)}
        >
          <Upload size={16} />
          {busy ? "正在处理" : replaceDocumentRef ? "创建新版本" : "保存并建立索引"}
        </button>
      </div>
    </div>
  );
}

function contentTypeForFile(filename: string): string {
  const extension = filename.split(".").pop()?.toLowerCase();
  if (extension === "md" || extension === "markdown") return "text/markdown";
  if (extension === "pdf") return "application/pdf";
  if (extension === "docx") return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  if (extension === "epub") return "application/epub+zip";
  return "text/plain";
}

function sensitivityValue(
  value: unknown,
  fallback: MemorySensitivity
): MemorySensitivity {
  return value === "normal" || value === "private" || value === "sensitive"
    ? value
    : fallback;
}

function sensitivityLabel(value: MemorySensitivity): string {
  if (value === "sensitive") return "敏感";
  if (value === "private") return "私密";
  return "普通";
}

function parseTags(value: string): string[] {
  return [...new Set(value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean))];
}

function parseMetadata(value: string): Record<string, string | number | boolean> {
  if (!value.trim()) return {};
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("结构化元数据必须是 JSON 对象");
  }
  for (const entry of Object.values(parsed as Record<string, unknown>)) {
    if (!["string", "number", "boolean"].includes(typeof entry)) {
      throw new Error("元数据值仅支持字符串、数字或布尔值");
    }
  }
  return parsed as Record<string, string | number | boolean>;
}

function notifyCommit(result: KnowledgeUploadCommitResult, replaceDocumentRef: string, notify: Notify): void {
  if (result.version.index_status === "failed") {
    notify(
      replaceDocumentRef
        ? "正文已保存，但索引失败；旧版本继续服务，请在详情中重建索引"
        : "正文已保存，但索引失败；请在详情中重建索引",
      "error"
    );
    return;
  }
  const embeddingFailed = result.embedding?.status === "failed";
  notify(
    result.duplicate || result.deduplicated
      ? "正文未变化，已保留当前版本"
      : embeddingFailed
        ? "文档已建立关键词索引；向量索引失败，可稍后重建"
        : replaceDocumentRef
          ? "新版本已建立索引"
          : "文档已加入知识库",
    embeddingFailed ? "error" : "success"
  );
}

function splitText(text: string, maxLength: number): string[] {
  const parts: string[] = [];
  let offset = 0;
  while (offset < text.length) {
    let end = Math.min(text.length, offset + maxLength);
    const lastCodeUnit = text.charCodeAt(end - 1);
    if (end < text.length && lastCodeUnit >= 0xd800 && lastCodeUnit <= 0xdbff) end -= 1;
    parts.push(text.slice(offset, end));
    offset = end;
  }
  return parts.length ? parts : [""];
}

async function sha256(text: string): Promise<string> {
  if (!globalThis.crypto?.subtle) return "";
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function formatBytes(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
