import { useCallback, useEffect, useState } from "react";
import { Clipboard, Download, RefreshCcw, ShieldAlert, Upload } from "lucide-react";
import { MemoryApi, isAbortError } from "../../api";
import type {
  ConnectionSettings,
  MemoryExport,
  MemoryReport,
  RestoreResult
} from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import { downloadBlob, downloadFile, copyText } from "../../utils/files";
import { errorMessage, reportSectionTitle } from "../../utils/format";
import type { Notify } from "../pageTypes";

export function ReportsPage({
  api,
  settings,
  notify,
  confirm
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [state, setState] = useState<LoadState<MemoryReport>>({
    loading: true,
    error: null,
    data: null
  });
  const [restorePreview, setRestorePreview] = useState<MemoryExport | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [restoreResult, setRestoreResult] = useState<RestoreResult | null>(null);
  const [stackAdminKey, setStackAdminKey] = useState("");
  const [stackBusy, setStackBusy] = useState(false);
  const exportUserId = safeExportUserId(settings.userId || "default");

  const load = useCallback(async (signal?: AbortSignal) => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.memoryReport(signal) });
    } catch (error) {
      // 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
      if (isAbortError(error)) return;
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const copyMarkdown = async () => {
    try {
      const markdown = await api.memoryReportMarkdown();
      await copyText(markdown);
      notify("Markdown 已复制", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const exportFile = async (format: "json" | "markdown") => {
    try {
      const data = await api.exportMemories(format);
      if (format === "json") {
        downloadFile(
          `memory-export-${exportUserId}.json`,
          JSON.stringify(data, null, 2),
          "application/json"
        );
      } else {
        downloadFile(`memory-export-${exportUserId}.md`, String(data), "text/markdown");
      }
      notify(`已下载 ${format.toUpperCase()} 导出`, "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const exportObsidianZip = async () => {
    try {
      const blob = await api.exportObsidianZip();
      downloadBlob(`memory-obsidian-export-${exportUserId}.zip`, blob);
      notify("已下载 Obsidian Zip", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const exportStackZip = async () => {
    const confirmed = await confirm({
      title: "下载整栈便携备份",
      message: (
        <span>
          将打包记忆库、知识库、访问令牌哈希、脱敏模型配置等。
          注意这是<strong>整实例</strong>备份：包含本部署<strong>所有用户</strong>的数据，
          不只是当前登录身份。
          <strong>不含</strong>供应商 API Key 与 admin 密钥；换机后需重新填写密钥。
          备份内含明文记忆与知识正文，请当敏感文件保管。
        </span>
      ),
      confirmLabel: "下载备份",
      tone: "warning"
    });
    if (!confirmed) return;
    setStackBusy(true);
    try {
      const blob = await api.exportStackBackup({
        modelGatewayAdminKey: stackAdminKey.trim() || undefined
      });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      downloadBlob(`memory-stack-backup-${stamp}.zip`, blob);
      // 备份完成后立即丢弃 admin 密钥明文，不让它留在表单里。
      setStackAdminKey("");
      notify("已下载整栈便携备份（不含密钥）", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setStackBusy(false);
    }
  };

  const chooseRestoreFile = async (file: File | null) => {
    setRestoreResult(null);
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text()) as MemoryExport;
      setRestorePreview(parsed);
      notify("已读取导入文件", "success");
    } catch {
      setRestorePreview(null);
      notify("JSON 文件无法解析", "error");
    }
  };

  const runRestore = async () => {
    if (!restorePreview) return;
    if (
      !(await confirm({
        title: "恢复导入",
        message: (
          <span>
            执行导入前建议先导出备份。
            {overwrite
              ? "已勾选「覆盖已有」，同 ID 的现有记忆将被导入内容更新。"
              : "未勾选「覆盖已有」，同 ID 的现有记忆将保持不变。"}
            确认继续导入？
          </span>
        ),
        tone: "warning",
        confirmLabel: "继续导入"
      }))
    ) {
      return;
    }
    try {
      const result = await api.restoreFromExport(restorePreview, overwrite, includeDeleted);
      setRestoreResult(result);
      notify("导入完成", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="报告与备份"
        subtitle="查看报告、导出备份和谨慎导入。"
        action={
          <button className="secondary-button" type="button" onClick={() => void load()}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <section className="panel">
        <div className="panel-header">
          <h2>记忆报告</h2>
          <button className="secondary-button" type="button" onClick={copyMarkdown}>
            <Clipboard size={16} />
            复制 Markdown
          </button>
        </div>
        {state.loading && <LoadingBlock label="正在加载报告" />}
        {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}
        {state.data && (
          <>
            <div className="stats-grid">
              <StatCard label="活跃记忆" value={state.data.counts.active_memories} />
              <StatCard label="回收站记忆" value={state.data.counts.deleted_memories} />
              <StatCard label="核心分区" value={state.data.counts.core_sections} />
            </div>
            <div className="section-list">
              {state.data.sections.map((section) => (
                <article className="section-summary" key={section.section}>
                  <div>
                    <strong>{reportSectionTitle(section.section, section.title)}</strong>
                    <span className="muted"> {section.section}</span>
                  </div>
                  <span>{section.memories.length} 条记忆</span>
                </article>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="panel export-panel">
        <div className="panel-header">
          <h2>导出备份</h2>
        </div>
        <div className="notice warning">
          <ShieldAlert size={18} />
          导出的 JSON、Markdown 和 Obsidian Zip 会包含完整私密/敏感正文。
        </div>
        <div className="button-row">
          <button className="primary-button" type="button" onClick={() => exportFile("json")}>
            <Download size={16} />
            下载 JSON
          </button>
          <button className="secondary-button" type="button" onClick={() => exportFile("markdown")}>
            <Download size={16} />
            下载 Markdown
          </button>
          <button className="secondary-button" type="button" onClick={exportObsidianZip}>
            <Download size={16} />
            下载 Obsidian Zip
          </button>
        </div>
      </section>

      <section className="panel export-panel stack-backup-panel">
        <div className="panel-header">
          <h2>整栈便携备份</h2>
        </div>
        <p className="muted">
          适合换机或升级前：记忆 + 知识 + 令牌哈希 + 模型路由配置（不含任何 API Key）。
          备份覆盖本部署<strong>所有用户</strong>的完整数据，不按当前登录身份过滤。
          Docker 双容器部署时，Memory 进程通常读不到 Model 数据卷，需要临时填写下方
          admin 密钥以便通过内网拉取脱敏配置。
        </p>
        <label className="field-block">
          <span>Model Gateway admin 密钥（可选）</span>
          <input
            type="password"
            value={stackAdminKey}
            onChange={(event) => setStackAdminKey(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="Docker 拆分部署时填写；同机源码安装通常可留空"
          />
        </label>
        <div className="button-row">
          <button
            className="primary-button"
            type="button"
            disabled={stackBusy}
            onClick={() => void exportStackZip()}
          >
            <Download size={16} />
            {stackBusy ? "正在打包…" : "下载整栈备份 Zip"}
          </button>
        </div>
      </section>

      <section className="panel restore-panel">
        <div className="panel-header">
          <h2>恢复导入</h2>
        </div>
        <div className="notice warning">
          <ShieldAlert size={18} />
          执行导入前建议先导出备份。
        </div>
        <label className="upload-box">
          <Upload size={18} />
          上传 JSON
          <input
            type="file"
            accept="application/json,.json"
            onChange={(event) => void chooseRestoreFile(event.target.files?.[0] || null)}
          />
        </label>
        {restorePreview && (
          <div className="restore-preview">
            <StatCard label="活跃记忆" value={restorePreview.memories?.length || 0} />
            <StatCard label="回收站记忆" value={restorePreview.deleted_memories?.length || 0} />
            <StatCard label="核心分区" value={restorePreview.core_memory_sections?.length || 0} />
          </div>
        )}
        <div className="button-row">
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={overwrite}
              onChange={(event) => setOverwrite(event.target.checked)}
            />
            覆盖已有
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={includeDeleted}
              onChange={(event) => setIncludeDeleted(event.target.checked)}
            />
            包含回收站
          </label>
          <button
            className="warning-button"
            type="button"
            disabled={!restorePreview}
            onClick={runRestore}
          >
            确认导入
          </button>
        </div>
        {restoreResult && (
          <div className="result-grid">
            <StatCard label="新增" value={restoreResult.created} />
            <StatCard label="更新" value={restoreResult.updated} />
            <StatCard label="跳过" value={restoreResult.skipped} />
            <StatCard label="无效" value={restoreResult.invalid} />
          </div>
        )}
      </section>
    </div>
  );
}

function safeExportUserId(value: string): string {
  const cleaned = Array.from(value)
    .map((character) =>
      /^[A-Za-z0-9_-]$/.test(character) ? character : "-"
    )
    .join("")
    .replace(/[-_]+$/g, "")
    .replace(/^[-_]+/g, "");
  return cleaned.slice(0, 80) || "default";
}



