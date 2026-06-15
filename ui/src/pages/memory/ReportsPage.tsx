import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArchiveRestore,
  Clipboard,
  Download,
  Eye,
  EyeOff,
  FileText,
  KeyRound,
  Layers3,
  ListChecks,
  Pencil,
  RefreshCcw,
  Save,
  Search,
  ShieldAlert,
  Trash2,
  Upload,
  Wrench,
  X
} from "lucide-react";
import { MemoryApi } from "../../api";
import { normalizeBaseUrl } from "../../storage";
import type {
  ConnectionSettings,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreSectionName,
  DecisionLog,
  MemoryAction,
  MemoryExport,
  MemoryRecord,
  MemoryReport,
  MemorySourceExplanation,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  PageKey,
  RecentContextSummary,
  RestoreResult,
  ReviewAction,
  ReviewRecommendation,
  ReviewResult
} from "../../types";
import { badge } from "../../components/Badge";
import { FieldList, FilterSelect, RangeFields } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { InfoCard, StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import {
  CONFIG_KEYS,
  CORE_SECTIONS,
  DECISIONS,
  MEMORY_TYPES,
  REVIEW_ACTIONS,
  SENSITIVITIES,
  STABILITIES
} from "../../utils/constants";
import { downloadFile, copyText } from "../../utils/files";
import {
  candidateSummary,
  clampNumber,
  dateText,
  displayText,
  errorMessage,
  joinUrl,
  maskSecret,
  percent,
  prettyJson,
  reportSectionTitle,
  reviewActionText,
  sectionTitle,
  shortId
} from "../../utils/format";
import { editDraftToPayload, memoryToEditDraft } from "../../utils/memory";
import type { MemoryEditDraft, MemoryFilters } from "../../utils/memory";
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

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.memoryReport() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
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
          `memory-export-${settings.userId}.json`,
          JSON.stringify(data, null, 2),
          "application/json"
        );
      } else {
        downloadFile(`memory-export-${settings.userId}.md`, String(data), "text/markdown");
      }
      notify(`已下载 ${format.toUpperCase()} 导出`, "success");
    } catch (error) {
      notify(errorMessage(error), "error");
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
        message: "执行导入前建议先导出备份。确认继续导入？",
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
          <button className="secondary-button" type="button" onClick={load}>
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
        {state.error && <ErrorBlock message={state.error} onRetry={load} />}
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
        <div className="button-row">
          <button className="primary-button" type="button" onClick={() => exportFile("json")}>
            <Download size={16} />
            下载 JSON
          </button>
          <button className="secondary-button" type="button" onClick={() => exportFile("markdown")}>
            <Download size={16} />
            下载 Markdown
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



