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

export function CoreMemoryPage({
  api,
  notify,
  confirm
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [tab, setTab] = useState<"current" | "history">("current");
  const [sectionFilter, setSectionFilter] = useState<"all" | CoreSectionName>("all");
  const [sections, setSections] = useState<LoadState<CoreMemorySection[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [history, setHistory] = useState<LoadState<CoreMemoryHistoryItem[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [consolidating, setConsolidating] = useState(false);

  const load = useCallback(async () => {
    setSections({ loading: true, error: null, data: null });
    setHistory({ loading: true, error: null, data: null });
    try {
      const [coreData, historyData] = await Promise.all([
        api.coreMemory(),
        api.coreHistory()
      ]);
      setSections({ loading: false, error: null, data: coreData });
      setHistory({ loading: false, error: null, data: historyData });
    } catch (error) {
      const message = errorMessage(error);
      setSections({ loading: false, error: message, data: null });
      setHistory({ loading: false, error: message, data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const bySection = useMemo(() => {
    return new Map((sections.data || []).map((item) => [item.section, item]));
  }, [sections.data]);

  const visibleHistory = useMemo(() => {
    return (history.data || []).filter((item) =>
      sectionFilter === "all" ? true : item.section === sectionFilter
    );
  }, [history.data, sectionFilter]);

  const consolidate = async () => {
    if (
      !(await confirm({
        title: "重新整理核心记忆",
        message: "确认重新整理核心记忆？该操作会调用上游模型，并可能更新核心记忆。",
        tone: "warning",
        confirmLabel: "重新整理"
      }))
    ) {
      return;
    }
    setConsolidating(true);
    try {
      await api.consolidateCoreMemory();
      notify("核心记忆已重新整理", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setConsolidating(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="核心记忆"
        subtitle="按分区查看核心记忆和历史版本。"
        action={
          <button
            className="warning-button"
            type="button"
            disabled={consolidating}
            onClick={consolidate}
          >
            <RefreshCcw size={16} />
            重新整理核心记忆
          </button>
        }
      />
      <div className="tabs">
        <button
          className={tab === "current" ? "active" : ""}
          type="button"
          onClick={() => setTab("current")}
        >
          当前版本
        </button>
        <button
          className={tab === "history" ? "active" : ""}
          type="button"
          onClick={() => setTab("history")}
        >
          历史版本
        </button>
      </div>

      {tab === "current" && (
        <>
          {sections.loading && <LoadingBlock label="正在加载核心记忆" />}
          {sections.error && <ErrorBlock message={sections.error} onRetry={load} />}
          {!sections.loading && !sections.error && (
            <div className="core-grid">
              {CORE_SECTIONS.map((section) => {
                const item = bySection.get(section.key);
                return (
                  <article className="core-card" key={section.key}>
                    <div className="core-card-header">
                      <h2>{section.title}</h2>
                      <span className="muted">{section.key}</span>
                    </div>
                    {item ? (
                      <>
                        <p>{item.content}</p>
                        <div className="meta-grid">
                          <span>置信度</span>
                          <strong>{percent(item.confidence)}</strong>
                          <span>版本</span>
                          <strong>{item.version}</strong>
                          <span>证据记忆</span>
                          <strong>{item.evidence_memory_ids.length}</strong>
                          <span>更新时间</span>
                          <strong>{dateText(item.updated_at)}</strong>
                        </div>
                      </>
                    ) : (
                      <EmptyBlock label="暂无内容" compact />
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </>
      )}

      {tab === "history" && (
        <section className="panel">
          <div className="panel-header">
            <h2>历史版本</h2>
            <select
              value={sectionFilter}
              onChange={(event) => setSectionFilter(event.target.value as "all" | CoreSectionName)}
            >
              <option value="all">全部分区</option>
              {CORE_SECTIONS.map((section) => (
                <option key={section.key} value={section.key}>
                  {section.title}
                </option>
              ))}
            </select>
          </div>
          {history.loading && <LoadingBlock label="正在加载历史版本" />}
          {history.error && <ErrorBlock message={history.error} onRetry={load} />}
          {!history.loading && !history.error && visibleHistory.length === 0 && (
            <EmptyBlock label="暂无历史版本" />
          )}
          <div className="timeline">
            {visibleHistory.map((item) => (
              <article className="timeline-item" key={item.id}>
                <div className="timeline-dot" />
                <div>
                  <div className="timeline-title">
                    {sectionTitle(item.section)} · v{item.version}
                  </div>
                  <p>{item.content}</p>
                  <div className="muted">
                    置信度 {percent(item.confidence)} · 替换时间 {dateText(item.replaced_at)}
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}



