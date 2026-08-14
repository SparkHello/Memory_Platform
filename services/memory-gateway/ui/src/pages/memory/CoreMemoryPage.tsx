import { useCallback, useMemo, useState, type CSSProperties } from "react";
import { RefreshCcw, X } from "lucide-react";
import { MemoryApi } from "../../api";
import type {
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreSectionName
} from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import { useAsyncData } from "../../hooks/useAsyncData";
import { useDialogA11y } from "../../hooks/useDialogA11y";
import { CORE_SECTIONS, CORE_SECTION_COLOR_VAR } from "../../utils/constants";
import { dateText, errorMessage, percent, sectionTitle } from "../../utils/format";
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
  const { state: sections, reload: reloadSections } = useAsyncData<CoreMemorySection[]>(
    (signal) => api.coreMemory(signal),
    [api]
  );
  const { state: history, reload: reloadHistory } = useAsyncData<CoreMemoryHistoryItem[]>(
    (signal) => api.coreHistory(signal),
    [api]
  );
  const [consolidating, setConsolidating] = useState(false);
  const historyDrawerRef = useDialogA11y<HTMLElement>(
    () => setTab("current"),
    tab === "history"
  );

  const load = useCallback(async () => {
    await Promise.all([reloadSections(), reloadHistory()]);
  }, [reloadSections, reloadHistory]);

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
        message:
          "确认重新整理核心记忆？该操作会调用上游模型，只保存能通过逐字证据校验的内容；模型可能漏掉部分分区，请在完成后复核结果。",
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
        subtitle="按分区查看核心记忆和历史版本；AI 整理会严格校验证据，但仍可能漏项。"
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
        <aside
          ref={historyDrawerRef}
          className="detail-drawer memory-detail-drawer core-history-drawer"
          role="dialog"
          aria-modal="true"
          aria-label="核心记忆历史版本"
          tabIndex={-1}
        >
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

