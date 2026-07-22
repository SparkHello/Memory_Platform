import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
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
  CORE_SECTION_COLOR_VAR,
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

  const load = useCallback(async (signal?: AbortSignal) => {
    setSections({ loading: true, error: null, data: null });
    setHistory({ loading: true, error: null, data: null });
    try {
      const [coreData, historyData] = await Promise.all([
        api.coreMemory(signal),
        api.coreHistory(signal)
      ]);
      setSections({ loading: false, error: null, data: coreData });
      setHistory({ loading: false, error: null, data: historyData });
    } catch (error) {
      // 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
      if (isAbortError(error)) return;
      const message = errorMessage(error);
      setSections({ loading: false, error: message, data: null });
      setHistory({ loading: false, error: message, data: null });
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
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
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => setTab("history")}>
              历史版本
            </button>
            <button
              className="warning-button"
              type="button"
              disabled={consolidating}
              onClick={consolidate}
            >
              <RefreshCcw size={16} />
              重新整理核心记忆
            </button>
          </div>
        }
      />

      {sections.loading && <LoadingBlock label="正在加载核心记忆" />}
      {sections.error && <ErrorBlock message={sections.error} onRetry={() => void load()} />}
      {!sections.loading && !sections.error && bySection.size === 0 && (
        <EmptyBlock
          label="核心记忆还没有内容"
          hint="核心记忆只从已保存的长期记忆中提炼。先在日常对话中积累记忆，再运行一次整理。"
          action={{ label: "重新整理核心记忆", onClick: () => void consolidate() }}
        />
      )}
      {!sections.loading && !sections.error && bySection.size > 0 && (
        <div className="core-grid">
          {CORE_SECTIONS.map((section) => {
            const item = bySection.get(section.key);
            return (
              <article
                className="core-card"
                key={section.key}
                style={{ "--tc": CORE_SECTION_COLOR_VAR[section.key] } as CSSProperties}
              >
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

      {tab === "history" && (
        <button className="drawer-scrim detail-drawer-scrim" type="button" aria-label="关闭历史版本" onClick={() => setTab("current")} />
      )}
      {tab === "history" && (
        <aside className="detail-drawer memory-detail-drawer core-history-drawer" role="dialog" aria-modal="true" aria-label="核心记忆历史版本">
          <div className="drawer-header">
            <div>
              <span className="panel-kicker">版本历史</span>
              <h2>历史版本</h2>
            </div>
            <button className="icon-button" type="button" onClick={() => setTab("current")} aria-label="关闭历史版本"><X size={18} /></button>
          </div>
          <label className="field-block core-history-filter">
            <span>分区</span>
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
          </label>
          {history.loading && <LoadingBlock label="正在加载历史版本" />}
          {history.error && <ErrorBlock message={history.error} onRetry={() => void load()} />}
          {!history.loading && !history.error && visibleHistory.length === 0 && (
            <EmptyBlock label="暂无历史版本" />
          )}
          <div className="timeline">
            {visibleHistory.map((item) => (
              <article className="timeline-item" key={item.id}>
                <div
                  className="timeline-dot"
                  style={{ background: CORE_SECTION_COLOR_VAR[item.section] }}
                />
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
        </aside>
      )}
    </div>
  );
}


