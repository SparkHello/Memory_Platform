import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArchiveRestore,
  ChevronLeft,
  ChevronRight,
  Columns3,
  Download,
  RefreshCcw,
  Search,
  ShieldAlert,
  SlidersHorizontal,
  Trash2,
  X
} from "lucide-react";
import { MemoryApi, isAbortError } from "../../api";
import type { MemoryExport, MemoryRecord, MemorySpace } from "../../types";
import { badge } from "../../components/Badge";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { FilterSelect, RangeFields } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { LoadState } from "../../hooks/useAsyncData";
import { useConfirm } from "../../hooks/useConfirm";
import { useDialogA11y } from "../../hooks/useDialogA11y";
import { downloadFile } from "../../utils/files";
import {
  MEMORY_TYPES,
  MEMORY_STATUSES,
  SENSITIVITIES,
  STABILITIES
} from "../../utils/constants";
import { dateText, displayText, errorMessage, percent } from "../../utils/format";
import { normalizeTags } from "../../utils/memory";
import type { MemoryFilters } from "../../utils/memory";
import type { Notify } from "../pageTypes";

const DEFAULT_FILTERS: MemoryFilters = {
  type: "all",
  status: "all",
  sensitivity: "all",
  stability: "all",
  minImportance: 1,
  maxImportance: 10,
  minConfidence: 0,
  maxConfidence: 1,
  hasValidUntil: false,
  hasReviewAfter: false,
  spaceId: "all",
  topicQuery: "",
  entityQuery: ""
};

type MemoryColumn =
  | "classification"
  | "typeStatus"
  | "importance"
  | "updated"
  | "confidence"
  | "stability"
  | "sensitivity"
  | "usage"
  | "lastUsed";

const MEMORY_COLUMNS: Array<{ key: MemoryColumn; label: string }> = [
  { key: "classification", label: "分类" },
  { key: "typeStatus", label: "类型 / 状态" },
  { key: "importance", label: "重要度" },
  { key: "updated", label: "更新时间" },
  { key: "confidence", label: "置信度" },
  { key: "stability", label: "稳定性" },
  { key: "sensitivity", label: "敏感级别" },
  { key: "usage", label: "激活次数" },
  { key: "lastUsed", label: "最近使用" }
];

// 从 hash 查询参数读初始 tab：#/memories?tab=recycle 落到回收站。
// 非记忆库 hash（如 #/memories/<id> 档案地址）返回 null，不动当前 tab。
function tabFromHash(hash: string): "active" | "deleted" | null {
  const [path, queryText = ""] = hash.split("?");
  if (path.replace(/\/$/, "") !== "#/memories") return null;
  return new URLSearchParams(queryText).get("tab") === "recycle" ? "deleted" : "active";
}

export function MemoriesPage({
  api,
  notify,
  openMemory,
  refreshKey
}: {
  api: MemoryApi;
  notify: Notify;
  openMemory: (id: string) => void;
  refreshKey: number;
}) {
  const [tab, setTab] = useState<"active" | "deleted">(
    () => tabFromHash(window.location.hash) || "active"
  );
  const [state, setState] = useState<LoadState<MemoryRecord[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [spaces, setSpaces] = useState<MemorySpace[]>([]);
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [columnsOpen, setColumnsOpen] = useState(false);
  const [pageIndex, setPageIndex] = useState(0);
  const [visibleColumns, setVisibleColumns] = useState<Set<MemoryColumn>>(
    () => new Set(["classification", "typeStatus", "importance", "updated"])
  );
  const [filters, setFilters] = useState<MemoryFilters>({ ...DEFAULT_FILTERS });
  const filterDrawerRef = useDialogA11y<HTMLElement>(
    () => setFiltersOpen(false),
    filtersOpen
  );

  const load = useCallback(
    async (mode = tab, signal?: AbortSignal) => {
      setState({ loading: true, error: null, data: null });
      try {
        const memoryPromise =
          mode === "deleted"
            ? api.listDeletedMemories({ redactSensitive: true }, signal)
            : submittedQuery
              ? api.searchMemories(
                  submittedQuery,
                  20,
                  {
                    includeSensitive: true,
                    redactSensitive: true
                  },
                  signal
                )
              : api.listMemories({ redactSensitive: true, status: "all" }, signal);
        const [memories, loadedSpaces] = await Promise.all([
          memoryPromise,
          api.listMemorySpaces(signal)
        ]);
        setSpaces(loadedSpaces);
        setState({ loading: false, error: null, data: memories });
      } catch (error) {
        // 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
        if (isAbortError(error)) return;
        setState({ loading: false, error: errorMessage(error), data: null });
      }
    },
    [api, submittedQuery, tab]
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(tab, controller.signal);
    return () => controller.abort();
  }, [load, tab]);

  // 输入防抖：停顿 300ms 后才真正发搜索请求；Enter / 搜索按钮立即触发。
  // 回收站是客户端过滤，不发搜索请求。
  useEffect(() => {
    if (tab === "deleted") return;
    const handle = setTimeout(() => setSubmittedQuery(query.trim()), 300);
    return () => clearTimeout(handle);
  }, [query, tab]);

  // hash 查询参数变化时同步 tab（例如从工作室跳到 #/memories?tab=recycle）。
  useEffect(() => {
    const onHashChange = () => {
      const next = tabFromHash(window.location.hash);
      if (next) setTab(next);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const runSearch = () => {
    const value = query.trim();
    if (value === submittedQuery) void load("active");
    else setSubmittedQuery(value);
  };

  // 全局记忆档案抽屉改动记忆后，回表刷新。
  useEffect(() => {
    if (refreshKey > 0) void load(tab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const memories = useMemo(() => {
    // 回收站数据已全量在本地，搜索词走客户端 contains 过滤（大小写不敏感）。
    const deletedQuery = tab === "deleted" ? query.trim().toLocaleLowerCase() : "";
    return (state.data || []).filter((memory) => {
      if (deletedQuery && !memory.content.toLocaleLowerCase().includes(deletedQuery)) return false;
      if (filters.type !== "all" && memory.type !== filters.type) return false;
      if (filters.status !== "all" && (memory.status || "dynamic") !== filters.status) return false;
      if (filters.sensitivity !== "all" && memory.sensitivity !== filters.sensitivity) return false;
      if (filters.stability !== "all" && memory.stability !== filters.stability) return false;
      if (memory.importance < filters.minImportance || memory.importance > filters.maxImportance) return false;
      if (memory.confidence < filters.minConfidence || memory.confidence > filters.maxConfidence) return false;
      if (filters.hasValidUntil && !memory.valid_until) return false;
      if (filters.hasReviewAfter && !memory.review_after) return false;
      if (filters.spaceId !== "all" && !(memory.space_ids || []).includes(filters.spaceId)) return false;
      if (filters.topicQuery && !hasMatchingTag(memory.topics || [], filters.topicQuery)) return false;
      if (filters.entityQuery && !hasMatchingTag(memory.entities || [], filters.entityQuery)) return false;
      return true;
    });
  }, [filters, query, state.data, tab]);

  const pageSize = 25;
  const pageCount = Math.max(1, Math.ceil(memories.length / pageSize));
  const pagedMemories = useMemo(
    () => memories.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    [memories, pageIndex]
  );

  useEffect(() => {
    setPageIndex(0);
    setSelectedIds(new Set());
  }, [filters, query, tab]);

  useEffect(() => {
    if (pageIndex >= pageCount) setPageIndex(pageCount - 1);
  }, [pageCount, pageIndex]);

  const activeFilters: Array<{ key: string; label: string; clear: () => void }> = [];
  if (query.trim()) {
    activeFilters.push({ key: "query", label: `搜索：${query.trim()}`, clear: () => setQuery("") });
  }
  if (filters.type !== "all") activeFilters.push({ key: "type", label: `类型：${displayText(filters.type)}`, clear: () => setFilters({ ...filters, type: "all" }) });
  if (filters.status !== "all") activeFilters.push({ key: "status", label: `状态：${displayText(filters.status)}`, clear: () => setFilters({ ...filters, status: "all" }) });
  if (filters.sensitivity !== "all") activeFilters.push({ key: "sensitivity", label: `敏感级别：${displayText(filters.sensitivity)}`, clear: () => setFilters({ ...filters, sensitivity: "all" }) });
  if (filters.stability !== "all") activeFilters.push({ key: "stability", label: `稳定性：${displayText(filters.stability)}`, clear: () => setFilters({ ...filters, stability: "all" }) });
  if (filters.spaceId !== "all") activeFilters.push({ key: "space", label: `空间：${spaces.find((space) => space.id === filters.spaceId)?.name || "已选"}`, clear: () => setFilters({ ...filters, spaceId: "all" }) });
  if (filters.topicQuery) activeFilters.push({ key: "topic", label: `主题：${filters.topicQuery}`, clear: () => setFilters({ ...filters, topicQuery: "" }) });
  if (filters.entityQuery) activeFilters.push({ key: "entity", label: `实体：${filters.entityQuery}`, clear: () => setFilters({ ...filters, entityQuery: "" }) });
  if (filters.minImportance !== 1 || filters.maxImportance !== 10) activeFilters.push({ key: "importance", label: `重要度：${filters.minImportance}–${filters.maxImportance}`, clear: () => setFilters({ ...filters, minImportance: 1, maxImportance: 10 }) });
  if (filters.minConfidence !== 0 || filters.maxConfidence !== 1) activeFilters.push({ key: "confidence", label: `置信度：${percent(filters.minConfidence)}–${percent(filters.maxConfidence)}`, clear: () => setFilters({ ...filters, minConfidence: 0, maxConfidence: 1 }) });
  if (filters.hasValidUntil) activeFilters.push({ key: "validUntil", label: "有有效期", clear: () => setFilters({ ...filters, hasValidUntil: false }) });
  if (filters.hasReviewAfter) activeFilters.push({ key: "reviewAfter", label: "有复核时间", clear: () => setFilters({ ...filters, hasReviewAfter: false }) });

  const resetFilters = () => {
    setFilters({ ...DEFAULT_FILTERS });
    setQuery("");
  };

  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const pageIds = pagedMemories.map((memory) => memory.id);
  const pageSelectedCount = pageIds.filter((id) => selectedIds.has(id)).length;
  const allPageSelected = pageIds.length > 0 && pageSelectedCount === pageIds.length;

  const togglePageSelection = () => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (allPageSelected) pageIds.forEach((id) => next.delete(id));
      else pageIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const toggleRowSelection = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const runBulk = async (
    action: (id: string) => Promise<unknown>,
    successMessage: (done: number) => string
  ) => {
    const ids = [...selectedIds];
    if (!ids.length) return;
    setBulkBusy(true);
    try {
      const results = await Promise.allSettled(ids.map(action));
      const done = results.filter((result) => result.status === "fulfilled").length;
      const failed = results.length - done;
      if (failed === 0) notify(successMessage(done), "success");
      else notify(`完成 ${done} 条，失败 ${failed} 条`, "error");
      setSelectedIds(new Set());
      void load(tab);
    } finally {
      setBulkBusy(false);
    }
  };

  const bulkDelete = async () => {
    const ok = await confirm({
      title: "移入回收站",
      message: `将把已选的 ${selectedIds.size} 条记忆移入回收站，之后可以随时恢复。`,
      confirmLabel: "移入回收站",
      tone: "warning"
    });
    if (!ok) return;
    await runBulk((id) => api.deleteMemory(id), (done) => `已将 ${done} 条记忆移入回收站`);
  };

  const bulkRestore = async () => {
    await runBulk((id) => api.restoreMemory(id), (done) => `已恢复 ${done} 条记忆`);
  };

  const bulkPurge = async () => {
    const ok = await confirm({
      title: "永久删除所选记忆",
      message: `将永久删除已选的 ${selectedIds.size} 条记忆。此操作无法撤销，建议先导出备份。`,
      confirmLabel: "永久删除",
      tone: "danger"
    });
    if (!ok) return;
    await runBulk((id) => api.purgeDeletedMemory(id), (done) => `已永久删除 ${done} 条记忆`);
  };

  const bulkExport = async () => {
    setBulkBusy(true);
    try {
      const exportData = (await api.exportMemories("json")) as MemoryExport;
      const chosen = new Set(selectedIds);
      const filtered: MemoryExport = {
        ...exportData,
        memories: (exportData.memories || []).filter((memory) => chosen.has(memory.id))
      };
      downloadFile(
        `memory-selected-${new Date().toISOString().slice(0, 10)}.json`,
        JSON.stringify(filtered, null, 2),
        "application/json"
      );
      notify(`已导出 ${filtered.memories?.length || 0} 条所选记忆`, "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBulkBusy(false);
    }
  };

  // "/" 聚焦搜索；Esc 清除选择（浮层打开时交给浮层自己处理）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (filtersOpen || columnsOpen || confirmState) return;
        setSelectedIds((current) => (current.size ? new Set() : current));
        return;
      }
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      event.preventDefault();
      searchInputRef.current?.focus();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [tab, filtersOpen, columnsOpen, confirmState]);

  return (
    <div className="page-stack">
      <PageHeader
        title="记忆库"
        subtitle="查看、搜索和筛选记忆；点任意一条打开它的档案。"
        action={
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => load(tab)}>
              <RefreshCcw size={16} />
              刷新
            </button>
          </div>
        }
      />

      <div className="tabs">
        <button
          className={tab === "active" ? "active" : ""}
          type="button"
          onClick={() => setTab("active")}
        >
          活跃记忆
        </button>
        <button
          className={tab === "deleted" ? "active" : ""}
          type="button"
          onClick={() => setTab("deleted")}
        >
          回收站
        </button>
      </div>

      <div className="notice">
        <ShieldAlert size={16} />
        当前为遮罩视图，私密和敏感正文只在档案里显式查看后显示。
      </div>

      <div className="memory-layout">
        {filtersOpen && (
          <button
            className="drawer-scrim filter-drawer-scrim"
            type="button"
            aria-label="关闭高级筛选"
            onClick={() => setFiltersOpen(false)}
          />
        )}
        <aside
          ref={filterDrawerRef}
          className={`filter-panel filter-drawer ${filtersOpen ? "open" : ""}`}
          role="dialog"
          aria-modal="true"
          aria-label="高级筛选"
          aria-hidden={!filtersOpen}
          tabIndex={-1}
        >
          <div className="drawer-header filter-drawer-header">
            <div>
              <span className="panel-kicker">筛选</span>
              <h2>高级筛选</h2>
            </div>
            <button className="icon-button" type="button" onClick={() => setFiltersOpen(false)} aria-label="关闭高级筛选">
              <X size={18} />
            </button>
          </div>

          <FilterSelect
            label="类型"
            value={filters.type}
            options={["all", ...MEMORY_TYPES]}
            onChange={(value) => setFilters({ ...filters, type: value as MemoryFilters["type"] })}
          />
          <FilterSelect
            label="状态"
            value={filters.status}
            options={["all", ...MEMORY_STATUSES]}
            onChange={(value) =>
              setFilters({ ...filters, status: value as MemoryFilters["status"] })
            }
          />
          <FilterSelect
            label="敏感级别"
            value={filters.sensitivity}
            options={["all", ...SENSITIVITIES]}
            onChange={(value) =>
              setFilters({ ...filters, sensitivity: value as MemoryFilters["sensitivity"] })
            }
          />
          <FilterSelect
            label="稳定性"
            value={filters.stability}
            options={["all", ...STABILITIES]}
            onChange={(value) =>
              setFilters({ ...filters, stability: value as MemoryFilters["stability"] })
            }
          />
          <label className="field-block">
            <span>空间</span>
            <select
              value={filters.spaceId}
              onChange={(event) => setFilters({ ...filters, spaceId: event.target.value })}
            >
              <option value="all">全部</option>
              {spaces.map((space) => (
                <option key={space.id} value={space.id}>
                  {space.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field-block">
            <span>主题包含</span>
            <input
              value={filters.topicQuery}
              onChange={(event) => setFilters({ ...filters, topicQuery: event.target.value })}
            />
          </label>
          <label className="field-block">
            <span>实体包含</span>
            <input
              value={filters.entityQuery}
              onChange={(event) => setFilters({ ...filters, entityQuery: event.target.value })}
            />
          </label>
          <RangeFields
            label="重要度"
            min={1}
            max={10}
            step={1}
            from={filters.minImportance}
            to={filters.maxImportance}
            onChange={(from, to) =>
              setFilters({ ...filters, minImportance: from, maxImportance: to })
            }
          />
          <RangeFields
            label="置信度"
            min={0}
            max={1}
            step={0.05}
            from={filters.minConfidence}
            to={filters.maxConfidence}
            onChange={(from, to) =>
              setFilters({ ...filters, minConfidence: from, maxConfidence: to })
            }
          />
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={filters.hasValidUntil}
              onChange={(event) => setFilters({ ...filters, hasValidUntil: event.target.checked })}
            />
            有有效期
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={filters.hasReviewAfter}
              onChange={(event) => setFilters({ ...filters, hasReviewAfter: event.target.checked })}
            />
            有复核时间
          </label>
          <div className="filter-drawer-actions">
            <button className="ghost-button" type="button" onClick={resetFilters}>重置全部</button>
            <button className="primary-button" type="button" onClick={() => setFiltersOpen(false)}>查看 {memories.length} 条结果</button>
          </div>
        </aside>

        <section className="panel memory-table-panel">
          <div className="memory-table-toolbar">
            <div className="search-box memory-search-box">
              <Search size={17} />
              <input
                ref={searchInputRef}
                value={query}
                placeholder={tab === "deleted" ? "过滤回收站内容（按 / 聚焦）" : "搜索记忆内容（按 / 聚焦）"}
                aria-label="搜索记忆"
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") runSearch();
                }}
              />
              {query && (
                <button className="search-clear" type="button" onClick={() => setQuery("")} aria-label="清空搜索">
                  <X size={15} />
                </button>
              )}
            </div>
            <button className="secondary-button" type="button" disabled={tab === "deleted" || !query.trim()} onClick={runSearch}>搜索</button>
            <span className="result-count">{memories.length} 条结果</span>
            <div className="table-toolbar-spacer" />
            <button className={`secondary-button ${activeFilters.length ? "active" : ""}`} type="button" onClick={() => setFiltersOpen(true)}>
              <SlidersHorizontal size={16} />
              筛选{activeFilters.length ? ` ${activeFilters.length}` : ""}
            </button>
            <div className="column-picker-wrap">
              <button className="secondary-button" type="button" onClick={() => setColumnsOpen((current) => !current)} aria-expanded={columnsOpen}>
                <Columns3 size={16} />
                列
              </button>
              {columnsOpen && (
                <div className="column-picker" role="dialog" aria-label="选择表格列">
                  <strong>显示字段</strong>
                  {MEMORY_COLUMNS.map((column) => (
                    <label className="checkbox-row" key={column.key}>
                      <input
                        type="checkbox"
                        checked={visibleColumns.has(column.key)}
                        onChange={() => setVisibleColumns((current) => {
                          const next = new Set(current);
                          if (next.has(column.key)) next.delete(column.key);
                          else next.add(column.key);
                          return next;
                        })}
                      />
                      {column.label}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>

          {activeFilters.length > 0 && (
            <div className="active-filter-row">
              {activeFilters.map((filter) => (
                <button key={filter.key} type="button" onClick={filter.clear}>
                  {filter.label}
                  <X size={13} />
                </button>
              ))}
              <button className="clear-all-filters" type="button" onClick={resetFilters}>清除全部</button>
            </div>
          )}

          {selectedIds.size > 0 && (
            <div className="bulk-bar" role="region" aria-label="批量操作">
              <strong>已选 {selectedIds.size} 条</strong>
              {tab === "active" ? (
                <>
                  <button className="secondary-button compact" type="button" disabled={bulkBusy} onClick={() => void bulkExport()}>
                    <Download size={15} />
                    导出所选
                  </button>
                  <button className="danger-button compact" type="button" disabled={bulkBusy} onClick={() => void bulkDelete()}>
                    <Trash2 size={15} />
                    移入回收站
                  </button>
                </>
              ) : (
                <>
                  <button className="secondary-button compact" type="button" disabled={bulkBusy} onClick={() => void bulkRestore()}>
                    <ArchiveRestore size={15} />
                    恢复
                  </button>
                  <button className="danger-button compact" type="button" disabled={bulkBusy} onClick={() => void bulkPurge()}>
                    <Trash2 size={15} />
                    永久删除
                  </button>
                </>
              )}
              <button className="ghost-button compact" type="button" onClick={() => setSelectedIds(new Set())}>
                清除选择
              </button>
            </div>
          )}

          {tab === "active" && submittedQuery && (state.data || []).length === 20 && (
            <div className="notice">
              <Search size={16} />
              仅显示前 20 条匹配，请细化搜索词。
            </div>
          )}

          {state.loading && <LoadingBlock label="正在加载记忆" />}
          {state.error && <ErrorBlock message={state.error} onRetry={() => load(tab)} />}
          {!state.loading && !state.error && memories.length === 0 && (
            <EmptyBlock
              label={tab === "deleted" ? "回收站为空" : "没有匹配的记忆"}
              hint={tab === "deleted" ? "被软删除的记忆会保留在这里，可以恢复或彻底清理。" : "调整搜索词或筛选条件后再试。"}
            />
          )}
          {memories.length > 0 && (
            <>
            <div className="table-wrap memory-table-desktop">
              <table className="data-table">
                <thead>
                  <tr>
                    <th className="row-select-cell">
                      <input
                        type="checkbox"
                        aria-label="选择本页全部记忆"
                        checked={allPageSelected}
                        ref={(element) => {
                          if (element) element.indeterminate = pageSelectedCount > 0 && !allPageSelected;
                        }}
                        onChange={togglePageSelection}
                      />
                    </th>
                    <th>内容</th>
                    {visibleColumns.has("classification") && <th>分类</th>}
                    {visibleColumns.has("typeStatus") && <th>类型 / 状态</th>}
                    {visibleColumns.has("importance") && <th>重要度</th>}
                    {visibleColumns.has("updated") && <th>更新时间</th>}
                    {visibleColumns.has("confidence") && <th>置信度</th>}
                    {visibleColumns.has("stability") && <th>稳定性</th>}
                    {visibleColumns.has("sensitivity") && <th>敏感级别</th>}
                    {visibleColumns.has("usage") && <th>激活次数</th>}
                    {visibleColumns.has("lastUsed") && <th>最近使用</th>}
                  </tr>
                </thead>
                <tbody>
                  {pagedMemories.map((memory) => (
                    <tr
                      key={memory.id}
                      tabIndex={0}
                      className={selectedIds.has(memory.id) ? "selected" : ""}
                      onClick={() => openMemory(memory.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          openMemory(memory.id);
                        }
                      }}
                    >
                      <td className="row-select-cell" onClick={(event) => event.stopPropagation()}>
                        <input
                          type="checkbox"
                          aria-label={`选择记忆：${memory.content.slice(0, 24)}`}
                          checked={selectedIds.has(memory.id)}
                          onChange={() => toggleRowSelection(memory.id)}
                          onKeyDown={(event) => event.stopPropagation()}
                        />
                      </td>
                      <td className="content-cell">{memory.content}</td>
                      {visibleColumns.has("classification") && <td className="classification-cell">{classificationSummary(memory, spaces)}</td>}
                      {visibleColumns.has("typeStatus") && <td><div className="table-badge-stack">{badge(memory.type)}{badge(memory.status || "dynamic")}</div></td>}
                      {visibleColumns.has("importance") && <td>
                        <span className={`imp ${memory.importance >= 8 ? "high" : ""}`}>
                          {memory.importance}
                        </span>
                      </td>}
                      {visibleColumns.has("updated") && <td>{dateText(memory.updated_at)}</td>}
                      {visibleColumns.has("confidence") && <td>
                        <span className="conf">{percent(memory.confidence)}</span>
                      </td>}
                      {visibleColumns.has("stability") && <td>{badge(memory.stability)}</td>}
                      {visibleColumns.has("sensitivity") && <td>{badge(memory.sensitivity)}</td>}
                      {visibleColumns.has("usage") && <td>{memory.usage_count}</td>}
                      {visibleColumns.has("lastUsed") && <td>{dateText(memory.last_used_at)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="memory-card-list">
              {pagedMemories.map((memory) => (
                <button key={memory.id} type="button" onClick={() => openMemory(memory.id)}>
                  <span className="memory-card-kicker">{badge(memory.type)} {badge(memory.status || "dynamic")}</span>
                  <strong>{memory.content}</strong>
                  <span className="memory-card-classification">{classificationSummary(memory, spaces)}</span>
                  <span className="memory-card-meta"><b>重要度 {memory.importance}</b><span>{dateText(memory.updated_at)}</span></span>
                </button>
              ))}
            </div>
            {pageCount > 1 && (
            <div className="table-pagination">
              <span>第 {pageIndex + 1} / {pageCount} 页</span>
              <div className="button-row">
                <button className="icon-button" type="button" disabled={pageIndex === 0} onClick={() => setPageIndex((current) => Math.max(0, current - 1))} aria-label="上一页"><ChevronLeft size={17} /></button>
                <button className="icon-button" type="button" disabled={pageIndex >= pageCount - 1} onClick={() => setPageIndex((current) => Math.min(pageCount - 1, current + 1))} aria-label="下一页"><ChevronRight size={17} /></button>
              </div>
            </div>
            )}
            </>
          )}
        </section>
      </div>

      <ConfirmDialog state={confirmState} onResolve={resolveConfirm} />
    </div>
  );
}

function spaceNamesForMemory(memory: MemoryRecord, spaces: MemorySpace[]): string[] {
  const namesById = new Map(spaces.map((space) => [space.id, space.name]));
  return (memory.space_ids || []).map((spaceId) => namesById.get(spaceId) || spaceId);
}

function classificationSummary(memory: MemoryRecord, spaces: MemorySpace[]): string {
  const parts = [
    ...(memory.topics || []).slice(0, 2),
    ...(memory.entities || []).slice(0, 1),
    ...spaceNamesForMemory(memory, spaces).slice(0, 2)
  ];
  if (!parts.length) return "-";
  const unique = normalizeTags(parts);
  const extra =
    (memory.topics?.length || 0) +
    (memory.entities?.length || 0) +
    (memory.space_ids?.length || 0) -
    unique.length;
  return extra > 0 ? `${unique.join("、")} +${extra}` : unique.join("、");
}

function hasMatchingTag(values: string[], query: string): boolean {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery) return true;
  return values.some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
}
