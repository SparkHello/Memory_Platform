import { FileText, ShieldAlert, Upload, X } from "lucide-react";
import { useRef, useState } from "react";
import type { MemoryApi } from "../../api";
import type {
  KnowledgeUploadCommitResult,
  MemorySensitivity
} from "../../types";
import { errorMessage } from "../../utils/format";
import type { Notify } from "../pageTypes";

const MAX_DOCUMENT_BYTES = 10 * 1024 * 1024;
// Web 走 REST 的大分片通道；MCP 工具自身仍保持 20,000 字符上限。
const PART_SIZE = 256_000;

type SourceMode = "paste" | "file";

export function KnowledgeUploadForm({
  api,
  notify,
  replaceDocumentRef = "",
  initialTitle = "",
  initialSourceName = "",
  initialSensitivity = "normal",
  initialContentType = "text/markdown",
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
  onComplete: (result: KnowledgeUploadCommitResult) => void;
  onCancel?: () => void;
}) {
  const [mode, setMode] = useState<SourceMode>("paste");
  const [title, setTitle] = useState(initialTitle);
  const [sourceName, setSourceName] = useState(initialSourceName);
  const [contentType, setContentType] = useState(initialContentType);
  const [sensitivity, setSensitivity] = useState<MemorySensitivity>(initialSensitivity);
  const [pasteText, setPasteText] = useState("");
  const [fileText, setFileText] = useState("");
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0, label: "" });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const text = mode === "paste" ? pasteText : fileText;

  const chooseMode = (next: SourceMode) => {
    if (busy) return;
    setMode(next);
    if (next === "paste") {
      setContentType(initialContentType);
    } else if (fileName) {
      setContentType(fileName.toLowerCase().endsWith(".md") ? "text/markdown" : "text/plain");
    }
  };

  const readFile = async (file: File | null) => {
    if (!file) return;
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (extension !== "txt" && extension !== "md") {
      notify("仅支持 UTF-8 编码的 .txt 或 .md 文件", "error");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    if (file.size > MAX_DOCUMENT_BYTES) {
      notify("单个文档不能超过 10 MiB", "error");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    try {
      const buffer = await file.arrayBuffer();
      const decoded = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
      if (!decoded.trim()) throw new Error("文件内容为空");
      setFileText(decoded);
      setFileName(file.name);
      setContentType(extension === "md" ? "text/markdown" : "text/plain");
      setSourceName(file.name);
      if (!title.trim()) setTitle(file.name.replace(/\.(txt|md)$/i, ""));
    } catch (error) {
      const message = error instanceof TypeError ? "文件不是有效的 UTF-8 文本" : errorMessage(error);
      notify(message, "error");
      setFileText("");
      setFileName("");
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const submit = async () => {
    const cleanTitle = title.trim();
    if (!cleanTitle) {
      notify("请填写文档标题", "error");
      return;
    }
    if (!text.trim()) {
      notify(mode === "file" ? "请选择包含正文的文件" : "请粘贴正文", "error");
      return;
    }
    const byteLength = new TextEncoder().encode(text).byteLength;
    if (byteLength > MAX_DOCUMENT_BYTES) {
      notify("单个文档不能超过 10 MiB", "error");
      return;
    }

    setBusy(true);
    let uploadId = "";
    try {
      setProgress({ completed: 0, total: 1, label: "正在创建上传会话" });
      const session = await api.beginKnowledgeUpload({
        title: cleanTitle,
        content_type: contentType,
        source_name: sourceName.trim(),
        replace_document_ref: replaceDocumentRef,
        sensitivity
      });
      uploadId = session.upload_id || session.id;
      const parts = splitText(text, PART_SIZE);
      setProgress({ completed: 0, total: parts.length, label: "正在上传正文" });
      for (let index = 0; index < parts.length; index += 1) {
        await api.appendKnowledgeUpload(uploadId, index, parts[index]);
        setProgress({ completed: index + 1, total: parts.length, label: "正在上传正文" });
      }
      setProgress({ completed: parts.length, total: parts.length, label: "正在保存并建立索引" });
      const result = await api.commitKnowledgeUpload(uploadId, parts.length, await sha256(text));
      uploadId = "";
      if (result.version.index_status === "failed") {
        notify(
          replaceDocumentRef
            ? "正文已保存，但索引失败；旧版本继续服务，请在详情中重建索引"
            : "正文已保存，但索引失败；请在详情中重建索引",
          "error"
        );
      } else {
        notify(result.duplicate || result.deduplicated ? "正文未变化，已保留当前版本" : replaceDocumentRef ? "新版本已建立索引" : "文档已加入知识库", "success");
      }
      onComplete(result);
    } catch (error) {
      if (uploadId) {
        void api.cancelKnowledgeUpload(uploadId).catch(() => undefined);
      }
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
      setProgress({ completed: 0, total: 0, label: "" });
    }
  };

  const bytes = new TextEncoder().encode(text).byteLength;

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
            onChange={(event) => setTitle(event.target.value)}
            placeholder="例如：项目架构说明"
            data-autofocus
          />
        </label>
        <label className="field-block">
          <span>格式</span>
          <select value={contentType} disabled={busy || mode === "file"} onChange={(event) => setContentType(event.target.value)}>
            <option value="text/markdown">Markdown</option>
            <option value="text/plain">纯文本</option>
          </select>
        </label>
        <label className="field-block">
          <span>敏感级别</span>
          <select value={sensitivity} disabled={busy} onChange={(event) => setSensitivity(event.target.value as MemorySensitivity)}>
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
            onChange={(event) => setSourceName(event.target.value)}
            placeholder="仅记录名称，不读取服务器路径"
          />
        </label>
      </div>

      {mode === "file" ? (
        <div className="knowledge-file-picker">
          <label className="upload-box">
            <Upload size={17} />
            {fileName ? "更换文件" : "选择 .txt / .md"}
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              disabled={busy}
              onChange={(event) => void readFile(event.target.files?.[0] || null)}
            />
          </label>
          {fileName && (
            <span className="knowledge-file-name">
              <FileText size={15} /> {fileName}
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
            onChange={(event) => setPasteText(event.target.value)}
            placeholder="在这里粘贴长文本。知识不会进入记忆衰减或自动记忆召回。"
          />
        </label>
      )}

      {mode === "file" && text && (
        <div className="knowledge-file-preview" aria-label="文件预览">
          {text.slice(0, 800)}{text.length > 800 ? "…" : ""}
        </div>
      )}

      <div className="knowledge-upload-footer">
        <div className="knowledge-upload-note">
          <ShieldAlert size={15} />
          <span>仅接受严格 UTF-8 文本，单版本上限 10 MiB。正文按片段上传，失败不会替换当前版本。</span>
        </div>
        <span className={bytes > MAX_DOCUMENT_BYTES ? "danger-text" : "muted"}>
          {formatBytes(bytes)} / 10 MiB
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
        <button className="primary-button" type="button" disabled={busy || !title.trim() || !text.trim()} onClick={() => void submit()}>
          <Upload size={16} />
          {busy ? "正在处理" : replaceDocumentRef ? "创建新版本" : "保存并建立索引"}
        </button>
      </div>
    </div>
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
