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
import { MemoryApi, isAbortError } from "../../api";
import { normalizeBaseUrl } from "../../storage";
import type {
  ConnectionSettings,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreSectionName,
  DecisionLog,
  DecisionLogAction,
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
import { Modal } from "../../components/Modal";
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

export function DecisionLogsPage({ api }: { api: MemoryApi }) {
  const [state, setState] = useState<LoadState<DecisionLog[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [decision, setDecision] = useState<"all" | DecisionLogAction>("all");
  const [conversationId, setConversationId] = useState("");
  const [selected, setSelected] = useState<DecisionLog | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.decisionLogs(100, {}, signal) });
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

  const logs = useMemo(() => {
    return (state.data || []).filter((log) => {
      if (decision !== "all" && log.decision !== decision) return false;
      if (conversationId.trim()) {
        return (log.conversation_id || "").includes(conversationId.trim());
      }
      return true;
    });
  }, [conversationId, decision, state.data]);

  return (
    <div className="page-stack">
      <PageHeader
        title="决策日志"
        subtitle="查看记忆保存、更新和忽略决策。"
        action={
          <button className="secondary-button" type="button" onClick={() => void load()}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <section className="panel">
        <div className="toolbar log-toolbar">
          <FilterSelect
            label="决策"
            value={decision}
            options={["all", ...DECISIONS]}
            onChange={(value) => setDecision(value as "all" | DecisionLogAction)}
          />
          <label className="field-block small log-conversation-field">
            <span>对话 ID</span>
            <input
              value={conversationId}
              onChange={(event) => setConversationId(event.target.value)}
              placeholder="过滤对话 ID"
            />
          </label>
        </div>
        {state.loading && <LoadingBlock label="正在加载决策日志" />}
        {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}
        {!state.loading && !state.error && logs.length === 0 && (
          <EmptyBlock
            label="暂无决策日志"
            hint="记忆每一次被保存、更新或忽略的决策都会记录在这里。"
            action={
              decision !== "all" || conversationId.trim()
                ? {
                    label: "清除筛选",
                    onClick: () => {
                      setDecision("all");
                      setConversationId("");
                    }
                  }
                : undefined
            }
          />
        )}
        {logs.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>决策</th>
                  <th>原因</th>
                  <th>对话 ID</th>
                  <th>创建时间</th>
                  <th>候选记忆摘要</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <tr key={log.id} onClick={() => setSelected(log)}>
                    <td>{badge(log.decision)}</td>
                    <td>{log.reason}</td>
                    <td>{log.conversation_id || "-"}</td>
                    <td>{dateText(log.created_at)}</td>
                    <td className="content-cell">{candidateSummary(log.candidate_json)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {selected && (
        <Modal title="日志详情" onClose={() => setSelected(null)}>
          <FieldList
            entries={[
              ["决策", selected.decision],
              ["原因", selected.reason],
              ["对话 ID", selected.conversation_id],
              ["创建时间", selected.created_at]
            ]}
          />
          <details className="raw-json-details">
            <summary>查看原始 JSON</summary>
            <pre className="json-block">{prettyJson(selected.candidate_json)}</pre>
          </details>
        </Modal>
      )}
    </div>
  );
}


