import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArchiveRestore,
  Clipboard,
  Database,
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
import { MemoryApi } from "../api";
import { normalizeBaseUrl } from "../storage";
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
} from "../types";
import { badge } from "../components/Badge";
import { FieldList, FilterSelect, RangeFields } from "../components/FormControls";
import { PageHeader } from "../components/PageHeader";
import { InfoCard, StatCard } from "../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/StateBlocks";
import type { ConfirmFn } from "../hooks/useConfirm";
import type { LoadState } from "../hooks/useAsyncData";
import {
  CONFIG_KEYS,
  CORE_SECTIONS,
  DECISIONS,
  MEMORY_TYPES,
  REVIEW_ACTIONS,
  SENSITIVITIES,
  STABILITIES
} from "../utils/constants";
import { downloadFile, copyText } from "../utils/files";
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
} from "../utils/format";
import { editDraftToPayload, memoryToEditDraft } from "../utils/memory";
import type { MemoryEditDraft, MemoryFilters } from "../utils/memory";
import type { Notify } from "./pageTypes";

export function DashboardPage({
  api,
  settings,
  setPage,
  notify
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  setPage: (page: PageKey) => void;
  notify: Notify;
}) {
  const [state, setState] = useState<
    LoadState<{
      health: string;
      report: MemoryReport;
      review: ReviewResult;
      logs: DecisionLog[];
    }>
  >({ loading: true, error: null, data: null });

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      const [health, report, review, logs] = await Promise.all([
        api.health(),
        api.memoryReport(),
        api.reviewMemories(),
        api.decisionLogs(10)
      ]);
      setState({
        loading: false,
        error: null,
        data: {
          health: health.status,
          report,
          review,
          logs
        }
      });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;

  return (
    <div className="page-stack">
      <PageHeader
        title="总览"
        subtitle="当前服务、用户记忆库和待处理建议概览。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载总览" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="card-grid status-grid">
            <InfoCard label="服务状态" value={data.health === "ok" ? "正常" : data.health} />
            <InfoCard label="API 基础地址" value={settings.apiBaseUrl} />
            <InfoCard label="当前用户 ID" value={settings.userId} />
            <InfoCard label="OpenAI 兼容地址" value={joinUrl(settings.apiBaseUrl, "/v1")} />
            <InfoCard label="MCP 地址" value={joinUrl(settings.apiBaseUrl, "/mcp")} />
          </div>

          <div className="stats-grid">
            <StatCard label="活跃记忆" value={data.report.counts.active_memories} />
            <StatCard label="回收站记忆" value={data.report.counts.deleted_memories} />
            <StatCard label="核心分区" value={data.report.counts.core_sections} />
            <StatCard label="最近决策日志" value={data.logs.length} />
            <StatCard label="体检建议" value={data.review.recommendations.length} />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>快捷操作</h2>
            </div>
            <div className="quick-actions">
              <button className="primary-button" type="button" onClick={() => setPage("memories")}>
                <Database size={16} />
                查看记忆库
              </button>
              <button className="secondary-button" type="button" onClick={() => setPage("review")}>
                <ListChecks size={16} />
                运行体检
              </button>
              <button className="secondary-button" type="button" onClick={() => setPage("core")}>
                <Layers3 size={16} />
                查看核心记忆
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={async () => {
                  try {
                    const exportData = (await api.exportMemories("json")) as MemoryExport;
                    downloadFile(
                      `memory-export-${settings.userId}.json`,
                      JSON.stringify(exportData, null, 2),
                      "application/json"
                    );
                    notify("已生成 JSON 备份", "success");
                  } catch (error) {
                    notify(errorMessage(error), "error");
                  }
                }}
              >
                <Download size={16} />
                导出备份
              </button>
              <button className="secondary-button" type="button" onClick={() => setPage("developer")}>
                <Wrench size={16} />
                查看接入信息
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}



