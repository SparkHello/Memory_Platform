import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  ArchiveRestore,
  Clipboard,
  Database,
  Download,
  Eye,
  EyeOff,
  FileText,
  Gauge,
  History,
  KeyRound,
  Layers3,
  ListChecks,
  RefreshCcw,
  Search,
  Settings as SettingsIcon,
  ShieldAlert,
  Trash2,
  Upload,
  Wrench,
  X
} from "lucide-react";
import { ApiError, MemoryApi } from "./api";
import { loadSettings, normalizeBaseUrl, saveSettings } from "./storage";
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
} from "./types";

type Toast = {
  kind: "success" | "error" | "info";
  message: string;
};

type LoadState<T> = {
  loading: boolean;
  error: string | null;
  data: T | null;
};

const NAV_ITEMS: Array<{ key: PageKey; label: string; icon: typeof Gauge }> = [
  { key: "dashboard", label: "总览", icon: Gauge },
  { key: "memories", label: "记忆库", icon: Database },
  { key: "core", label: "核心记忆", icon: Layers3 },
  { key: "review", label: "记忆体检", icon: ListChecks },
  { key: "recent", label: "近期上下文", icon: History },
  { key: "reports", label: "报告与备份", icon: FileText },
  { key: "logs", label: "决策日志", icon: Activity },
  { key: "settings", label: "设置", icon: SettingsIcon },
  { key: "developer", label: "接入信息", icon: Wrench }
];

const MEMORY_TYPES: MemoryType[] = [
  "project",
  "preference",
  "fact",
  "learning",
  "style",
  "person",
  "relationship"
];

const STABILITIES: MemoryStability[] = ["temporary", "medium", "stable"];
const SENSITIVITIES: MemorySensitivity[] = ["normal", "private", "sensitive"];
const REVIEW_ACTIONS: ReviewAction[] = ["merge", "delete", "lower", "review", "keep"];
const DECISIONS: MemoryAction[] = ["create", "update", "ignore"];

const CORE_SECTIONS: Array<{ key: CoreSectionName; title: string }> = [
  { key: "profile", title: "个人背景" },
  { key: "preferences", title: "偏好" },
  { key: "relationships", title: "关系" },
  { key: "routines", title: "日常习惯" },
  { key: "goals", title: "目标计划" },
  { key: "communication", title: "沟通方式" }
];

const CONFIG_KEYS = [
  "GATEWAY_API_KEY",
  "UPSTREAM_BASE_URL",
  "UPSTREAM_API_KEY",
  "UPSTREAM_MODEL",
  "EMBEDDING_BASE_URL",
  "EMBEDDING_API_KEY",
  "EMBEDDING_MODEL",
  "EMBEDDING_DIMENSIONS",
  "DATABASE_PATH",
  "REQUEST_TIMEOUT_SECONDS"
];

const DISPLAY_TEXT: Record<string, string> = {
  all: "全部",
  project: "项目",
  preference: "偏好",
  fact: "事实",
  learning: "学习",
  style: "风格",
  person: "人物",
  relationship: "关系",
  temporary: "临时",
  medium: "中期",
  stable: "稳定",
  normal: "普通",
  private: "私密",
  sensitive: "敏感",
  create: "创建",
  update: "更新",
  ignore: "忽略",
  keep: "保留",
  merge: "合并",
  lower: "降权",
  delete: "删除",
  review: "复核",
  none: "无",
  same: "重复",
  supplement: "补充",
  conflict: "冲突",
  supersede: "替代",
  profile: "个人背景",
  preferences: "偏好",
  relationships: "关系",
  routines: "日常习惯",
  goals: "目标计划",
  communication: "沟通方式",
  other: "其他记忆"
};

export function App() {
  const [settings, setSettings] = useState<ConnectionSettings>(() => loadSettings());
  const [page, setPage] = useState<PageKey>(() =>
    loadSettings().apiKey ? "dashboard" : "settings"
  );
  const [toast, setToast] = useState<Toast | null>(null);
  const [serviceStatus, setServiceStatus] = useState<{
    loading: boolean;
    ok: boolean;
    message: string;
  }>({ loading: true, ok: false, message: "检查中" });

  const api = useMemo(() => new MemoryApi(settings), [settings]);
  const activePage = settings.apiKey ? page : "settings";

  const notify = useCallback((message: string, kind: Toast["kind"] = "info") => {
    setToast({ message, kind });
  }, []);

  useEffect(() => {
    if (!toast) {
      return;
    }
    const timer = window.setTimeout(() => setToast(null), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const pingService = useCallback(async () => {
    setServiceStatus((current) => ({ ...current, loading: true }));
    try {
      const result = await api.health();
      setServiceStatus({
        loading: false,
        ok: result.status === "ok",
        message: result.status === "ok" ? "在线" : result.status
      });
    } catch (error) {
      setServiceStatus({
        loading: false,
        ok: false,
        message: errorMessage(error)
      });
    }
  }, [api]);

  useEffect(() => {
    void pingService();
  }, [pingService]);

  const applySettings = (next: ConnectionSettings, message = "设置已保存") => {
    const saved = saveSettings(next);
    setSettings(saved);
    notify(message, "success");
  };

  const updateTopbarUser = (userId: string) => {
    const saved = saveSettings({ ...settings, userId });
    setSettings(saved);
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">M</div>
          <div>
            <div className="brand-title">记忆控制台</div>
            <div className="brand-subtitle">本地网关</div>
          </div>
        </div>
        <nav className="nav-list" aria-label="Memory Console">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                className={`nav-item ${activePage === item.key ? "active" : ""}`}
                onClick={() => setPage(item.key)}
                type="button"
              >
                <Icon size={17} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="status-cluster">
            <span className={`status-dot ${serviceStatus.ok ? "ok" : "bad"}`} />
            <span>{serviceStatus.loading ? "检查中" : serviceStatus.message}</span>
            <button className="icon-button" type="button" onClick={pingService} title="刷新服务状态">
              <RefreshCcw size={16} />
            </button>
          </div>
          <div className="topbar-right">
            <label className="compact-field">
              <span>用户 ID</span>
              <input
                value={settings.userId}
                onChange={(event) => updateTopbarUser(event.target.value)}
                placeholder="default"
              />
            </label>
            <button
              className="secondary-button"
              type="button"
              onClick={() => setPage("settings")}
            >
              <SettingsIcon size={16} />
              设置
            </button>
          </div>
        </header>

        <section className="content-area">
          {!settings.apiKey && (
            <div className="notice warning">
              <KeyRound size={18} />
              首次使用请先保存 Gateway API Key。
            </div>
          )}
          {activePage === "dashboard" && (
            <DashboardPage
              api={api}
              settings={settings}
              setPage={setPage}
              notify={notify}
            />
          )}
          {activePage === "memories" && <MemoriesPage api={api} notify={notify} />}
          {activePage === "core" && <CoreMemoryPage api={api} notify={notify} />}
          {activePage === "review" && <ReviewPage api={api} notify={notify} />}
          {activePage === "recent" && <RecentContextPage api={api} />}
          {activePage === "reports" && (
            <ReportsPage api={api} settings={settings} notify={notify} />
          )}
          {activePage === "logs" && <DecisionLogsPage api={api} />}
          {activePage === "settings" && (
            <SettingsPage
              settings={settings}
              onSave={applySettings}
              notify={notify}
            />
          )}
          {activePage === "developer" && (
            <DeveloperPage settings={settings} notify={notify} />
          )}
        </section>
      </main>

      {toast && <ToastView toast={toast} />}
    </div>
  );
}

function DashboardPage({
  api,
  settings,
  setPage,
  notify
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  setPage: (page: PageKey) => void;
  notify: (message: string, kind?: Toast["kind"]) => void;
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

type MemoryFilters = {
  type: "all" | MemoryType;
  sensitivity: "all" | MemorySensitivity;
  stability: "all" | MemoryStability;
  minImportance: number;
  maxImportance: number;
  minConfidence: number;
  maxConfidence: number;
  hasValidUntil: boolean;
  hasReviewAfter: boolean;
};

function MemoriesPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [tab, setTab] = useState<"active" | "deleted">("active");
  const [state, setState] = useState<LoadState<MemoryRecord[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<MemoryRecord | null>(null);
  const [why, setWhy] = useState<LoadState<MemorySourceExplanation>>({
    loading: false,
    error: null,
    data: null
  });
  const [filters, setFilters] = useState<MemoryFilters>({
    type: "all",
    sensitivity: "all",
    stability: "all",
    minImportance: 1,
    maxImportance: 10,
    minConfidence: 0,
    maxConfidence: 1,
    hasValidUntil: false,
    hasReviewAfter: false
  });

  const load = useCallback(
    async (mode = tab) => {
      setState({ loading: true, error: null, data: null });
      setWhy({ loading: false, error: null, data: null });
      try {
        const memories =
          mode === "deleted"
            ? await api.listDeletedMemories()
            : query.trim()
              ? await api.searchMemories(query.trim(), 20)
              : await api.listMemories();
        setState({ loading: false, error: null, data: memories });
      } catch (error) {
        setState({ loading: false, error: errorMessage(error), data: null });
      }
    },
    [api, query, tab]
  );

  useEffect(() => {
    void load(tab);
  }, [load, tab]);

  useEffect(() => {
    setWhy({ loading: false, error: null, data: null });
  }, [selected?.id]);

  const memories = useMemo(() => {
    return (state.data || []).filter((memory) => {
      if (filters.type !== "all" && memory.type !== filters.type) return false;
      if (filters.sensitivity !== "all" && memory.sensitivity !== filters.sensitivity) return false;
      if (filters.stability !== "all" && memory.stability !== filters.stability) return false;
      if (memory.importance < filters.minImportance || memory.importance > filters.maxImportance) return false;
      if (memory.confidence < filters.minConfidence || memory.confidence > filters.maxConfidence) return false;
      if (filters.hasValidUntil && !memory.valid_until) return false;
      if (filters.hasReviewAfter && !memory.review_after) return false;
      return true;
    });
  }, [filters, state.data]);

  const runWhy = async (memoryId: string) => {
    setWhy({ loading: true, error: null, data: null });
    try {
      setWhy({ loading: false, error: null, data: await api.whyRemember(memoryId) });
    } catch (error) {
      setWhy({ loading: false, error: errorMessage(error), data: null });
    }
  };

  const deleteMemory = async (memory: MemoryRecord) => {
    if (!window.confirm(`确认将这条记忆移入回收站？\n\n${memory.content}`)) {
      return;
    }
    try {
      await api.deleteMemory(memory.id);
      notify("已移入回收站，可在回收站恢复。", "success");
      setSelected(null);
      await load("active");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const restoreMemory = async (memory: MemoryRecord) => {
    try {
      await api.restoreMemory(memory.id);
      notify("已恢复记忆", "success");
      setSelected(null);
      await load("deleted");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="记忆库"
        subtitle="查看、搜索、筛选、解释、删除和恢复记忆。"
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
          onClick={() => {
            setTab("active");
            setSelected(null);
          }}
        >
          活跃记忆
        </button>
        <button
          className={tab === "deleted" ? "active" : ""}
          type="button"
          onClick={() => {
            setTab("deleted");
            setSelected(null);
          }}
        >
          回收站
        </button>
      </div>

      <div className={`memory-layout ${selected ? "has-detail" : ""}`}>
        <aside className="filter-panel">
          <div className="search-box">
            <Search size={16} />
            <input
              value={query}
              disabled={tab === "deleted"}
              placeholder="搜索记忆"
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void load("active");
                }
              }}
            />
          </div>
          <button
            className="primary-button full-width"
            type="button"
            disabled={tab === "deleted" || !query.trim()}
            onClick={() => load("active")}
          >
            搜索
          </button>
          <button
            className="ghost-button full-width"
            type="button"
            onClick={() => {
              setQuery("");
              void load(tab);
            }}
          >
            清空搜索
          </button>

          <FilterSelect
            label="类型"
            value={filters.type}
            options={["all", ...MEMORY_TYPES]}
            onChange={(value) => setFilters({ ...filters, type: value as MemoryFilters["type"] })}
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
        </aside>

        <section className="panel memory-table-panel">
          {state.loading && <LoadingBlock label="正在加载记忆" />}
          {state.error && <ErrorBlock message={state.error} onRetry={() => load(tab)} />}
          {!state.loading && !state.error && memories.length === 0 && (
            <EmptyBlock label={tab === "deleted" ? "回收站为空" : "没有匹配的记忆"} />
          )}
          {memories.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>内容</th>
                    <th>类型</th>
                    <th>重要度</th>
                    <th>置信度</th>
                    <th>稳定性</th>
                    <th>敏感级别</th>
                    <th>使用次数</th>
                    <th>最近使用</th>
                    <th>更新时间</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {memories.map((memory) => (
                    <tr
                      key={memory.id}
                      className={selected?.id === memory.id ? "selected" : ""}
                      onClick={() => setSelected(memory)}
                    >
                      <td className="content-cell">{memory.content}</td>
                      <td>{badge(memory.type)}</td>
                      <td>{memory.importance}</td>
                      <td>{percent(memory.confidence)}</td>
                      <td>{badge(memory.stability)}</td>
                      <td>{badge(memory.sensitivity)}</td>
                      <td>{memory.usage_count}</td>
                      <td>{dateText(memory.last_used_at)}</td>
                      <td>{dateText(memory.updated_at)}</td>
                      <td>
                        {tab === "deleted" ? (
                          <button
                            className="secondary-button compact"
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void restoreMemory(memory);
                            }}
                          >
                            恢复
                          </button>
                        ) : (
                          <button
                            className="danger-button compact"
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void deleteMemory(memory);
                            }}
                          >
                            移入回收站
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {selected && (
          <aside className="detail-drawer">
            <div className="drawer-header">
              <h2>记忆详情</h2>
              <button className="icon-button" type="button" onClick={() => setSelected(null)} title="关闭">
                <X size={18} />
              </button>
            </div>
            <FieldList
              entries={[
                ["id", selected.id],
                ["内容", selected.content],
                ["类型", displayText(selected.type)],
                ["重要度", selected.importance],
                ["置信度", percent(selected.confidence)],
                ["来源原文", selected.source_message],
                ["来源对话 ID", selected.source_conversation_id],
                ["使用次数", selected.usage_count],
                ["最近使用", selected.last_used_at],
                ["稳定性", displayText(selected.stability)],
                ["有效期", selected.valid_until],
                ["复核时间", selected.review_after],
                ["敏感级别", displayText(selected.sensitivity)],
                ["证据记忆 ID", selected.evidence_memory_ids],
                ["创建时间", selected.created_at],
                ["更新时间", selected.updated_at]
              ]}
            />
            <div className="drawer-actions">
              {tab === "active" && (
                <>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => runWhy(selected.id)}
                  >
                    为什么记得？
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    onClick={() => deleteMemory(selected)}
                  >
                    <Trash2 size={16} />
                    移入回收站
                  </button>
                </>
              )}
              {tab === "deleted" && (
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => restoreMemory(selected)}
                >
                  <ArchiveRestore size={16} />
                  恢复
                </button>
              )}
            </div>
            {why.loading && <LoadingBlock label="正在读取来源" />}
            {why.error && <ErrorBlock message={why.error} />}
            {why.data && (
              <section className="subpanel">
                <h3>为什么记得？</h3>
                <FieldList
                  entries={[
                    ["来源摘录", why.data.source_excerpt],
                    ["来源对话 ID", why.data.source_conversation_id],
                    ["保存时间", why.data.saved_at],
                    ["更新时间", why.data.updated_at],
                    ["置信度", percent(why.data.confidence)],
                    ["是否核心记忆证据", why.data.is_core_memory_evidence ? "是" : "否"],
                    ["核心记忆分区", why.data.core_memory_sections.map(displayText)],
                    ["证据记忆 ID", why.data.evidence_memory_ids]
                  ]}
                />
              </section>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

function CoreMemoryPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
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
      !window.confirm(
        "确认重新整理核心记忆？该操作会调用上游模型，并可能更新核心记忆。"
      )
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

function ReviewPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [state, setState] = useState<
    LoadState<{ review: ReviewResult; memories: MemoryRecord[] }>
  >({ loading: true, error: null, data: null });
  const [mergeDraft, setMergeDraft] = useState<ReviewRecommendation | null>(null);
  const [mergeContent, setMergeContent] = useState("");
  const [applying, setApplying] = useState(false);

  const load = useCallback(async (showToast = false) => {
    setState({ loading: true, error: null, data: null });
    try {
      const [review, memories] = await Promise.all([
        api.reviewMemories(),
        api.listMemories()
      ]);
      setState({ loading: false, error: null, data: { review, memories } });
      if (showToast) {
        notify(`体检完成，共 ${review.recommendations.length} 条建议`, "success");
      }
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
      if (showToast) {
        notify(errorMessage(error), "error");
      }
    }
  }, [api, notify]);

  useEffect(() => {
    void load();
  }, [load]);

  const memoryMap = useMemo(() => {
    return new Map((state.data?.memories || []).map((memory) => [memory.id, memory]));
  }, [state.data?.memories]);

  const grouped = useMemo(() => {
    const map = new Map<ReviewAction, ReviewRecommendation[]>(
      REVIEW_ACTIONS.map((action) => [action, []])
    );
    for (const recommendation of state.data?.review.recommendations || []) {
      map.get(recommendation.action)?.push(recommendation);
    }
    return map;
  }, [state.data?.review.recommendations]);

  const applyDelete = async (recommendation: ReviewRecommendation) => {
    if (
      !window.confirm(
        `确认将 ${recommendation.memory_ids.length} 条建议删除的记忆移入回收站？`
      )
    ) {
      return;
    }
    setApplying(true);
    try {
      for (const memoryId of recommendation.memory_ids) {
        await api.deleteMemory(memoryId);
      }
      notify("已移入回收站，可在回收站恢复。", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const applyMerge = async () => {
    if (!mergeDraft) return;
    if (!window.confirm("确认合并这些记忆？合并后多余记忆会进入回收站。")) {
      return;
    }
    setApplying(true);
    try {
      await api.mergeMemories(mergeDraft.memory_ids, mergeContent.trim() || mergeDraft.suggested_content);
      notify("已合并记忆", "success");
      setMergeDraft(null);
      setMergeContent("");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="记忆体检"
        subtitle="记忆体检只返回建议，不会自动修改数据。"
        action={
          <button
            className="primary-button"
            type="button"
            disabled={state.loading}
            onClick={() => load(true)}
          >
            <ListChecks size={16} />
            {state.loading ? "体检中" : "运行体检"}
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在运行体检" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {state.data && (
        <>
          <div className="stats-grid">
            <StatCard label="扫描记忆" value={state.data.review.total} />
            <StatCard label="建议数量" value={state.data.review.recommendations.length} />
          </div>
          {state.data.review.recommendations.length === 0 && <EmptyBlock label="暂无体检建议" />}
          <div className="review-groups">
            {REVIEW_ACTIONS.map((action) => {
              const items = grouped.get(action) || [];
              if (items.length === 0) return null;
              return (
                <section className="panel" key={action}>
                  <div className="panel-header">
                    <h2>{reviewActionText(action)}</h2>
                    <span className="count-pill">{items.length}</span>
                  </div>
                  <div className="recommendation-list">
                    {items.map((recommendation, index) => (
                      <article className="recommendation-card" key={`${action}-${index}`}>
                        <div className="recommendation-topline">
                          {badge(recommendation.action)}
                          {badge(recommendation.relation)}
                        </div>
                        <p>{recommendation.reason}</p>
                        <FieldList
                          compact
                          entries={[
                            ["记忆 ID", recommendation.memory_ids],
                            ["建议内容", recommendation.suggested_content]
                          ]}
                        />
                        <div className="linked-memories">
                          {recommendation.memory_ids.map((id) => {
                            const memory = memoryMap.get(id);
                            return (
                              <div className="linked-memory" key={id}>
                                <strong>{shortId(id)}</strong>
                                <span>{memory?.content || "未在当前活跃记忆中找到"}</span>
                              </div>
                            );
                          })}
                        </div>
                        <div className="button-row">
                          {recommendation.action === "delete" && (
                            <button
                              className="danger-button"
                              type="button"
                              disabled={applying}
                              onClick={() => applyDelete(recommendation)}
                            >
                              移入回收站
                            </button>
                          )}
                          {recommendation.action === "merge" && (
                            <button
                              className="secondary-button"
                              type="button"
                              disabled={applying}
                              onClick={() => {
                                setMergeDraft(recommendation);
                                setMergeContent(recommendation.suggested_content || "");
                              }}
                            >
                              打开合并预览
                            </button>
                          )}
                          {recommendation.action === "lower" && (
                            <span className="muted">当前阶段仅展示建议</span>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        </>
      )}
      {mergeDraft && (
        <Modal title="合并预览" onClose={() => setMergeDraft(null)}>
          <FieldList entries={[["记忆 ID", mergeDraft.memory_ids], ["原因", mergeDraft.reason]]} />
          <label className="field-block">
            <span>合并内容</span>
            <textarea
              value={mergeContent}
              onChange={(event) => setMergeContent(event.target.value)}
              rows={6}
            />
          </label>
          <div className="button-row end">
            <button className="ghost-button" type="button" onClick={() => setMergeDraft(null)}>
              取消
            </button>
            <button className="primary-button" type="button" disabled={applying} onClick={applyMerge}>
              确认合并
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function RecentContextPage({ api }: { api: MemoryApi }) {
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

function ReportsPage({
  api,
  settings,
  notify
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  notify: (message: string, kind?: Toast["kind"]) => void;
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
    if (!window.confirm("执行导入前建议先导出备份。确认继续导入？")) {
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

function DecisionLogsPage({ api }: { api: MemoryApi }) {
  const [state, setState] = useState<LoadState<DecisionLog[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [decision, setDecision] = useState<"all" | MemoryAction>("all");
  const [conversationId, setConversationId] = useState("");
  const [selected, setSelected] = useState<DecisionLog | null>(null);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.decisionLogs(100) });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
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
          <button className="secondary-button" type="button" onClick={load}>
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
            onChange={(value) => setDecision(value as "all" | MemoryAction)}
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
        {state.error && <ErrorBlock message={state.error} onRetry={load} />}
        {!state.loading && !state.error && logs.length === 0 && <EmptyBlock label="暂无决策日志" />}
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
          <pre className="json-block">{prettyJson(selected.candidate_json)}</pre>
        </Modal>
      )}
    </div>
  );
}

function SettingsPage({
  settings,
  onSave,
  notify
}: {
  settings: ConnectionSettings;
  onSave: (settings: ConnectionSettings, message?: string) => void;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [form, setForm] = useState(settings);
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    setForm(settings);
  }, [settings]);

  const testConnection = async () => {
    setTesting(true);
    try {
      const client = new MemoryApi({
        ...form,
        apiBaseUrl: normalizeBaseUrl(form.apiBaseUrl),
        userId: form.userId || "default"
      });
      await client.health();
      await client.memoryReport();
      notify("连接测试通过", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="设置"
        subtitle="本地 UI 连接设置和项目配置说明。"
        action={
          <button className="primary-button" type="button" onClick={() => onSave(form)}>
            保存设置
          </button>
        }
      />
      <section className="panel settings-panel">
        <div className="panel-header">
          <h2>连接设置</h2>
        </div>
        <label className="field-block">
          <span>API 基础地址</span>
          <input
            value={form.apiBaseUrl}
            onChange={(event) => setForm({ ...form, apiBaseUrl: event.target.value })}
            placeholder={window.location.origin}
          />
        </label>
        <label className="field-block">
          <span>网关 API Key</span>
          <div className="secret-field">
            <input
              type={showKey ? "text" : "password"}
              value={form.apiKey}
              onChange={(event) => setForm({ ...form, apiKey: event.target.value })}
              placeholder="GATEWAY_API_KEY"
            />
            <button
              className="icon-button"
              type="button"
              onClick={() => setShowKey(!showKey)}
              title={showKey ? "隐藏 API Key" : "显示 API Key"}
            >
              {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
        <label className="field-block">
          <span>用户 ID</span>
          <input
            value={form.userId}
            onChange={(event) => setForm({ ...form, userId: event.target.value })}
            placeholder="default"
          />
        </label>
        <div className="button-row">
          <button className="primary-button" type="button" onClick={() => onSave(form)}>
            保存到本机浏览器
          </button>
          <button
            className="secondary-button"
            type="button"
            disabled={testing}
            onClick={testConnection}
          >
            测试连接
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>项目配置说明</h2>
        </div>
        <div className="notice">
          当前版本设置页只保存 UI 连接信息；服务端 .env 修改将在后续版本实现。
        </div>
        <div className="config-grid">
          {CONFIG_KEYS.map((key) => (
            <code key={key}>{key}</code>
          ))}
        </div>
      </section>
    </div>
  );
}

function DeveloperPage({
  settings,
  notify
}: {
  settings: ConnectionSettings;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const openAiBase = joinUrl(settings.apiBaseUrl, "/v1");
  const mcpUrl = joinUrl(settings.apiBaseUrl, "/mcp");
  const headers = `Authorization: Bearer ${settings.apiKey}\nX-User-Id: ${settings.userId}`;
  const endpoints = [
    "GET /health",
    "GET /memories",
    "POST /memories/search",
    "GET /memories/core",
    "POST /memories/review",
    "GET /memories/export"
  ];

  const copy = async (text: string) => {
    await copyText(text);
    notify("已复制", "success");
  };

  return (
    <div className="page-stack">
      <PageHeader title="接入信息" subtitle="OpenAI 兼容接口、MCP 和 REST 常用接入信息。" />
      <section className="panel access-card">
        <div className="panel-header">
          <h2>OpenAI-compatible</h2>
          <button className="secondary-button" type="button" onClick={() => copy(openAiBase)}>
            <Clipboard size={16} />
            复制基础地址
          </button>
        </div>
        <FieldList
          entries={[
            ["基础地址", openAiBase],
            ["API Key", maskSecret(settings.apiKey)],
            ["模型", "任意，服务端会映射到 UPSTREAM_MODEL"]
          ]}
        />
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <h2>MCP</h2>
          <button className="secondary-button" type="button" onClick={() => copy(`${mcpUrl}\n${headers}`)}>
            <Clipboard size={16} />
            复制
          </button>
        </div>
        <FieldList entries={[["地址", mcpUrl], ["请求头", headers]]} />
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <h2>REST</h2>
          <button className="secondary-button" type="button" onClick={() => copy(endpoints.join("\n"))}>
            <Clipboard size={16} />
            复制端点
          </button>
        </div>
        <div className="endpoint-list">
          {endpoints.map((endpoint) => (
            <code key={endpoint}>{endpoint}</code>
          ))}
        </div>
      </section>
    </div>
  );
}

function PageHeader({
  title,
  subtitle,
  action
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {action && <div className="page-action">{action}</div>}
    </div>
  );
}

function InfoCard({ label, value }: { label: string; value: string }) {
  return (
    <article className="info-card">
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </article>
  );
}

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <article className="stat-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="field-block small">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option value={option} key={option}>
            {displayText(option)}
          </option>
        ))}
      </select>
    </label>
  );
}

function RangeFields({
  label,
  min,
  max,
  step,
  from,
  to,
  onChange
}: {
  label: string;
  min: number;
  max: number;
  step: number;
  from: number;
  to: number;
  onChange: (from: number, to: number) => void;
}) {
  return (
    <div className="range-fields">
      <span>{label}</span>
      <div>
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={from}
          onChange={(event) => onChange(Number(event.target.value), to)}
        />
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={to}
          onChange={(event) => onChange(from, Number(event.target.value))}
        />
      </div>
    </div>
  );
}

function FieldList({
  entries,
  compact = false
}: {
  entries: Array<[string, unknown]>;
  compact?: boolean;
}) {
  return (
    <dl className={`field-list ${compact ? "compact" : ""}`}>
      {entries.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{valueText(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Modal({
  title,
  children,
  onClose
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal-card">
        <div className="drawer-header">
          <h2>{title}</h2>
          <button className="icon-button" type="button" onClick={onClose} title="关闭">
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="state-block">
      <RefreshCcw size={18} className="spin" />
      {label}
    </div>
  );
}

function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-block error">
      <ShieldAlert size={18} />
      <span>{message}</span>
      {onRetry && (
        <button className="secondary-button compact" type="button" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

function EmptyBlock({ label, compact = false }: { label: string; compact?: boolean }) {
  return <div className={`state-block empty ${compact ? "compact" : ""}`}>{label}</div>;
}

function ToastView({ toast }: { toast: Toast }) {
  return <div className={`toast ${toast.kind}`}>{toast.message}</div>;
}

function badge(value: string) {
  return <span className={`badge badge-${value}`}>{displayText(value)}</span>;
}

function displayText(value: string): string {
  return DISPLAY_TEXT[value] || value;
}

function reviewActionText(action: ReviewAction): string {
  return `${displayText(action)}建议`;
}

function reportSectionTitle(section: string, fallback: string): string {
  return DISPLAY_TEXT[section] || fallback;
}

function percent(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${Math.round(value * 100)}%`;
}

function dateText(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function sectionTitle(section: CoreSectionName): string {
  return CORE_SECTIONS.find((item) => item.key === section)?.title || section;
}

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

function joinUrl(baseUrl: string, path: string): string {
  return `${normalizeBaseUrl(baseUrl)}${path}`;
}

function maskSecret(secret: string): string {
  if (!secret) return "未设置";
  if (secret.length <= 6) return "••••••";
  return `${secret.slice(0, 3)}••••${secret.slice(-3)}`;
}

function candidateSummary(raw: string): string {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (isRecord(parsed)) {
      const memory = parsed.memory || parsed.content || parsed.reason || parsed.source_quote;
      if (memory) return String(memory);
    }
    return JSON.stringify(parsed).slice(0, 160);
  } catch {
    return raw.slice(0, 160);
  }
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.status}: ${error.detail}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "操作失败";
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function downloadFile(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
