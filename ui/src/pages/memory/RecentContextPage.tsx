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

export function RecentContextPage({ api }: { api: MemoryApi }) {
  const [state, setState] = useState<LoadState<RecentContextSummary[]>>({
    loading: true,
    error: null,
    data: null
  });

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.recentContext() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page-stack">
      <PageHeader
        title="近期上下文"
        subtitle="近期上下文用于恢复最近对话，不属于长期记忆，也不会进入核心记忆。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载近期上下文" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {state.data && state.data.length === 0 && <EmptyBlock label="暂无近期上下文" />}
      {state.data && state.data.length > 0 && (
        <div className="context-list">
          {state.data.map((item) => (
            <article className="panel" key={item.id}>
              <div className="panel-header">
                <h2>{item.conversation_id || "未命名对话"}</h2>
                <span className="muted">{dateText(item.updated_at)}</span>
              </div>
              <p>{item.summary}</p>
              <div className="muted">
                创建时间 {dateText(item.created_at)} · 更新时间 {dateText(item.updated_at)}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}



