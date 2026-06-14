import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  ArrowDown,
  ArchiveRestore,
  BarChart3,
  Clipboard,
  CreditCard,
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
  Pencil,
  RefreshCcw,
  Save,
  Search,
  Server,
  SlidersHorizontal,
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
  BalanceRecord,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreSectionName,
  DecisionLog,
  GatewayProvidersResponse,
  MemoryAction,
  MemoryExport,
  MemoryRecord,
  MemoryReport,
  MemorySourceExplanation,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  MemoryUpdatePayload,
  PageKey,
  ProviderConfigPayload,
  ProviderConfigResponse,
  ProviderModelConfigPayload,
  ProviderModelSummary,
  ProviderSummary,
  RecentContextSummary,
  RestoreResult,
  ReviewAction,
  ReviewRecommendation,
  ReviewResult,
  RouteConfigPayload,
  RouteSummary,
  UsageEvent,
  UsageSummary
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
  { key: "gateway-config", label: "网关配置", icon: SlidersHorizontal },
  { key: "providers", label: "服务商", icon: Server },
  { key: "routes", label: "路由", icon: ListChecks },
  { key: "billing", label: "余额账本", icon: CreditCard },
  { key: "usage", label: "用量统计", icon: BarChart3 },
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
  "PROVIDERS_CONFIG_PATH",
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
  delete: "移入回收站",
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
  other: "其他记忆",
  success: "成功",
  error: "错误"
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
            <div className="brand-title">网关控制台</div>
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
          {activePage === "gateway-config" && (
            <GatewayConfigPage api={api} notify={notify} />
          )}
          {activePage === "providers" && <ProvidersPage api={api} notify={notify} />}
          {activePage === "routes" && <RoutesPage api={api} notify={notify} />}
          {activePage === "billing" && <BillingPage api={api} notify={notify} />}
          {activePage === "usage" && <UsagePage api={api} />}
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

type ProviderDraft = {
  mode: "create" | "edit";
  provider: string;
  name: string;
  base_url: string;
  api_key: string;
  enabled: boolean;
  timeout_seconds: number;
};

type ProviderModelDraft = {
  mode: "create" | "edit";
  id: string;
  provider: string;
  upstream_model: string;
  display_name: string;
  api_format: "openai_compatible" | "claude_sdk";
  pricing_mode: "flat" | "tiered";
  pricing_tiers_json: string;
  pricing_tiers: PriceTierDraft[];
  input_price_per_million: string;
  output_price_per_million: string;
  currency: string;
  enabled: boolean;
};

type PriceTierDraft = {
  up_to_tokens: string;
  input_price_per_million: string;
  output_price_per_million: string;
};

type RouteDraft = {
  mode: "create" | "edit";
  id: string;
  virtual_model: string;
  provider_model_id: string;
  provider: string;
  upstream_model: string;
  priority: number;
  input_price_per_million: string;
  output_price_per_million: string;
  currency: string;
  min_balance: number;
  enabled: boolean;
};

const EMPTY_PROVIDER_DRAFT: ProviderDraft = {
  mode: "create",
  provider: "",
  name: "",
  base_url: "",
  api_key: "",
  enabled: true,
  timeout_seconds: 60
};

const EMPTY_PROVIDER_MODEL_DRAFT: ProviderModelDraft = {
  mode: "create",
  id: "",
  provider: "",
  upstream_model: "",
  display_name: "",
  api_format: "openai_compatible",
  pricing_mode: "flat",
  pricing_tiers_json: "",
  pricing_tiers: createEmptyPriceTierDrafts(),
  input_price_per_million: "0",
  output_price_per_million: "0",
  currency: "CNY",
  enabled: true
};

const EMPTY_ROUTE_DRAFT: RouteDraft = {
  mode: "create",
  id: "",
  virtual_model: "",
  provider_model_id: "",
  provider: "",
  upstream_model: "",
  priority: 100,
  input_price_per_million: "0",
  output_price_per_million: "0",
  currency: "CNY",
  min_balance: 0,
  enabled: true
};

function GatewayConfigPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [state, setState] = useState<LoadState<ProviderConfigResponse>>({
    loading: true,
    error: null,
    data: null
  });
  const [providerDraft, setProviderDraft] = useState<ProviderDraft>(EMPTY_PROVIDER_DRAFT);
  const [routeDraft, setRouteDraft] = useState<RouteDraft>(EMPTY_ROUTE_DRAFT);
  const [exportText, setExportText] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.providerConfig() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const providers = data?.providers || [];
  const routes = data?.routes || [];

  const saveProvider = async () => {
    const provider = providerDraft.provider.trim();
    if (!provider || !providerDraft.name.trim() || !providerDraft.base_url.trim()) {
      notify("请填写服务商 ID、名称和基础地址", "error");
      return;
    }
    setBusy(true);
    try {
      const payload: ProviderConfigPayload = {
        provider,
        name: providerDraft.name.trim(),
        base_url: providerDraft.base_url.trim(),
        enabled: providerDraft.enabled,
        timeout_seconds: clampNumber(providerDraft.timeout_seconds, 1, 600)
      };
      if (providerDraft.api_key.trim()) {
        payload.api_key = providerDraft.api_key.trim();
      }
      if (providerDraft.mode === "edit") {
        await api.updateProviderConfig(provider, payload);
      } else {
        await api.createProviderConfig(payload);
      }
      notify("服务商已保存", "success");
      setProviderDraft(EMPTY_PROVIDER_DRAFT);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const clearProviderKey = async (provider: string) => {
    if (!window.confirm(`确认清除 ${provider} 的 API key？`)) {
      return;
    }
    setBusy(true);
    try {
      await api.updateProviderConfig(provider, { api_key: "" });
      notify("API key 已清除", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const disableProvider = async (provider: string) => {
    if (!window.confirm(`确认禁用服务商 ${provider}？`)) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteProviderConfig(provider);
      notify("服务商已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const testProvider = async (provider: string) => {
    setBusy(true);
    try {
      const result = await api.testProviderConfig(provider);
      notify(result.message, result.success ? "success" : "error");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const saveRoute = async () => {
    if (!routeDraft.virtual_model.trim() || !routeDraft.provider.trim() || !routeDraft.upstream_model.trim()) {
      notify("请填写虚拟模型、服务商和上游模型", "error");
      return;
    }
    const payload: RouteConfigPayload = {
      virtual_model: routeDraft.virtual_model.trim(),
      provider: routeDraft.provider.trim(),
      upstream_model: routeDraft.upstream_model.trim(),
      priority: Math.round(routeDraft.priority),
      input_price_per_million: clampNumber(
        decimalInputValue(routeDraft.input_price_per_million),
        0,
        1_000_000
      ),
      output_price_per_million: clampNumber(
        decimalInputValue(routeDraft.output_price_per_million),
        0,
        1_000_000
      ),
      currency: routeDraft.currency.trim() || "CNY",
      min_balance: clampNumber(routeDraft.min_balance, 0, 1_000_000_000),
      enabled: routeDraft.enabled
    };
    setBusy(true);
    try {
      if (routeDraft.mode === "edit") {
        await api.updateRouteConfig(routeDraft.id, payload);
      } else {
        await api.createRouteConfig(payload);
      }
      notify("路由已保存", "success");
      setRouteDraft(EMPTY_ROUTE_DRAFT);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const deleteRoute = async (routeId: string) => {
    if (!window.confirm("确认删除这条路由？")) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteRouteConfig(routeId);
      notify("路由已删除", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const importToml = async () => {
    if (!window.confirm("从 providers.toml 导入会合并到 UI 配置，真实 API key 不会导入。继续？")) {
      return;
    }
    setBusy(true);
    try {
      const result = await api.importProviderConfigToml();
      notify(`已导入 ${result.providers} 个服务商、${result.routes} 条路由`, "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const exportToml = async () => {
    setBusy(true);
    try {
      setExportText(await api.exportProviderConfigToml());
      notify("已生成 TOML", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };
  const showInlineConfig = false;

  return (
    <div className="page-stack">
      <PageHeader
        title="网关配置"
        subtitle="在本机 SQLite 中配置服务商、路由、价格和连接测试。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <div className="notice warning">
        <ShieldAlert size={18} />
        API key 会保存在本机 SQLite 数据库中，不会在页面回显；不要提交 data/ 目录，也不要把服务暴露到公网。
      </div>
      {state.loading && <LoadingBlock label="正在加载网关配置" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="当前来源" value={sourceText(data.source)} />
            <StatCard label="服务商" value={providers.length} />
            <StatCard label="路由" value={routes.length} />
            <StatCard label="密钥" value={providers.filter((provider) => provider.api_key_configured).length} />
          </div>

          {showInlineConfig && (
            <>
          <section className="panel">
            <div className="panel-header">
              <h2>服务商管理</h2>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setProviderDraft(EMPTY_PROVIDER_DRAFT)}
              >
                新增
              </button>
            </div>
            <div className="toolbar">
              <label className="field-block small">
                <span>服务商 ID</span>
                <input
                  value={providerDraft.provider}
                  disabled={providerDraft.mode === "edit"}
                  onChange={(event) => setProviderDraft({ ...providerDraft, provider: event.target.value })}
                  placeholder="zhipu"
                />
              </label>
              <label className="field-block small">
                <span>名称</span>
                <input
                  value={providerDraft.name}
                  onChange={(event) => setProviderDraft({ ...providerDraft, name: event.target.value })}
                  placeholder="智谱"
                />
              </label>
              <label className="field-block small wide-field">
                <span>基础地址</span>
                <input
                  value={providerDraft.base_url}
                  onChange={(event) => setProviderDraft({ ...providerDraft, base_url: event.target.value })}
                  placeholder="https://open.bigmodel.cn/api/paas/v4"
                />
              </label>
              <label className="field-block small">
                <span>API Key</span>
                <input
                  type="password"
                  value={providerDraft.api_key}
                  onChange={(event) => setProviderDraft({ ...providerDraft, api_key: event.target.value })}
                  placeholder={providerDraft.mode === "edit" ? "留空则不变" : "可稍后填写"}
                />
              </label>
              <label className="field-block small">
                <span>超时</span>
                <input
                  type="number"
                  min={1}
                  value={providerDraft.timeout_seconds}
                  onChange={(event) =>
                    setProviderDraft({ ...providerDraft, timeout_seconds: Number(event.target.value) })
                  }
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={providerDraft.enabled}
                  onChange={(event) => setProviderDraft({ ...providerDraft, enabled: event.target.checked })}
                />
                启用
              </label>
              <button className="primary-button" type="button" disabled={busy} onClick={saveProvider}>
                <Save size={16} />
                保存服务商
              </button>
            </div>
            {providers.length === 0 && <EmptyBlock label="暂无服务商" />}
            {providers.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>服务商</th>
                      <th>名称</th>
                      <th>启用</th>
                      <th>基础地址</th>
                      <th>API Key</th>
                      <th>超时</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {providers.map((provider) => (
                      <tr key={provider.id || provider.provider}>
                        <td>{provider.provider || provider.id}</td>
                        <td>{provider.name}</td>
                        <td>{provider.enabled ? "是" : "否"}</td>
                        <td>{provider.base_url}</td>
                        <td>{provider.api_key_configured ? "已配置" : "未配置"}</td>
                        <td>{provider.timeout_seconds}s</td>
                        <td>
                          <div className="button-row">
                            <button
                              className="secondary-button compact"
                              type="button"
                              onClick={() => setProviderDraft(providerToDraft(provider))}
                            >
                              编辑
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => testProvider(provider.provider || provider.id)}
                            >
                              测试
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => clearProviderKey(provider.provider || provider.id)}
                            >
                              清除密钥
                            </button>
                            <button
                              className="warning-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => disableProvider(provider.provider || provider.id)}
                            >
                              禁用
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>路由管理</h2>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setRouteDraft(EMPTY_ROUTE_DRAFT)}
              >
                新增
              </button>
            </div>
            <div className="toolbar">
              <label className="field-block small">
                <span>虚拟模型</span>
                <input
                  value={routeDraft.virtual_model}
                  onChange={(event) => setRouteDraft({ ...routeDraft, virtual_model: event.target.value })}
                  placeholder="glm-5.1"
                />
              </label>
              <label className="field-block small">
                <span>服务商</span>
                <select
                  value={routeDraft.provider}
                  onChange={(event) => setRouteDraft({ ...routeDraft, provider: event.target.value })}
                >
                  <option value="">选择服务商</option>
                  {providers.map((provider) => (
                    <option key={provider.provider || provider.id} value={provider.provider || provider.id}>
                      {provider.provider || provider.id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field-block small">
                <span>上游模型</span>
                <input
                  value={routeDraft.upstream_model}
                  onChange={(event) => setRouteDraft({ ...routeDraft, upstream_model: event.target.value })}
                  placeholder="glm-5.1"
                />
              </label>
              <label className="field-block small">
                <span>优先级</span>
                <input
                  type="number"
                  value={routeDraft.priority}
                  onChange={(event) => setRouteDraft({ ...routeDraft, priority: Number(event.target.value) })}
                />
              </label>
              <label className="field-block small">
                <span>输入价格 / 1M</span>
                <DecimalInput
                  value={routeDraft.input_price_per_million}
                  onChange={(value) => setRouteDraft({ ...routeDraft, input_price_per_million: value })}
                />
              </label>
              <label className="field-block small">
                <span>输出价格 / 1M</span>
                <DecimalInput
                  value={routeDraft.output_price_per_million}
                  onChange={(value) => setRouteDraft({ ...routeDraft, output_price_per_million: value })}
                />
              </label>
              <label className="field-block small">
                <span>币种</span>
                <input
                  value={routeDraft.currency}
                  onChange={(event) => setRouteDraft({ ...routeDraft, currency: event.target.value })}
                />
              </label>
              <label className="field-block small">
                <span>最低余额</span>
                <input
                  type="number"
                  min={0}
                  step="0.000001"
                  value={routeDraft.min_balance}
                  onChange={(event) => setRouteDraft({ ...routeDraft, min_balance: Number(event.target.value) })}
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={routeDraft.enabled}
                  onChange={(event) => setRouteDraft({ ...routeDraft, enabled: event.target.checked })}
                />
                启用
              </label>
              <button className="primary-button" type="button" disabled={busy} onClick={saveRoute}>
                <Save size={16} />
                保存路由
              </button>
            </div>
            {routes.length === 0 && <EmptyBlock label="暂无路由" />}
            {routes.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>虚拟模型</th>
                      <th>服务商</th>
                      <th>上游模型</th>
                      <th>优先级</th>
                      <th>价格</th>
                      <th>最低余额</th>
                      <th>启用</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routes.map((route) => (
                      <tr key={route.id || `${route.virtual_model}-${route.provider}-${route.upstream_model}`}>
                        <td>{route.virtual_model}</td>
                        <td>{route.provider}</td>
                        <td>{route.upstream_model}</td>
                        <td>{route.priority}</td>
                        <td>
                          {moneyText(route.input_price_per_million, route.currency)} /{" "}
                          {moneyText(route.output_price_per_million, route.currency)}
                        </td>
                        <td>{moneyText(route.min_balance, route.currency)}</td>
                        <td>{route.enabled === false ? "否" : "是"}</td>
                        <td>
                          <div className="button-row">
                            <button
                              className="secondary-button compact"
                              type="button"
                              onClick={() => setRouteDraft(routeToDraft(route))}
                            >
                              编辑
                            </button>
                            {route.id && !route.id.startsWith("toml:") && (
                              <button
                                className="danger-button compact"
                                type="button"
                                disabled={busy}
                                onClick={() => deleteRoute(route.id || "")}
                              >
                                删除
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

            </>
          )}

          <section className="panel">
            <div className="panel-header">
              <h2>导入 / 导出</h2>
              <div className="button-row">
                <button className="secondary-button" type="button" disabled={busy} onClick={importToml}>
                  <Upload size={16} />
                  从 TOML 导入
                </button>
                <button className="secondary-button" type="button" disabled={busy} onClick={exportToml}>
                  <Download size={16} />
                  导出 TOML
                </button>
              </div>
            </div>
            {exportText && (
              <label className="field-block">
                <span>导出结果不包含真实 API key</span>
                <textarea value={exportText} readOnly rows={12} />
              </label>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function ProvidersPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [state, setState] = useState<LoadState<ProviderConfigResponse>>({
    loading: true,
    error: null,
    data: null
  });
  const [providerDraft, setProviderDraft] = useState<ProviderDraft>(EMPTY_PROVIDER_DRAFT);
  const [modelDraft, setModelDraft] = useState<ProviderModelDraft>(EMPTY_PROVIDER_MODEL_DRAFT);
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.providerConfig() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const providers = data?.providers || [];
  const providerModels = data?.provider_models || [];
  const selectedProvider = selectedProviderId
    ? providers.find((provider) => (provider.provider || provider.id) === selectedProviderId)
    : null;
  const selectedProviderModels = selectedProviderId
    ? providerModels.filter((model) => model.provider === selectedProviderId)
    : [];

  const saveProvider = async () => {
    const provider = providerDraft.provider.trim();
    if (!provider || !providerDraft.name.trim() || !providerDraft.base_url.trim()) {
      notify("请填写服务商 ID、名称和基础地址", "error");
      return;
    }
    setBusy(true);
    try {
      const payload: ProviderConfigPayload = {
        provider,
        name: providerDraft.name.trim(),
        base_url: providerDraft.base_url.trim(),
        enabled: providerDraft.enabled,
        timeout_seconds: clampNumber(providerDraft.timeout_seconds, 1, 600)
      };
      if (providerDraft.api_key.trim()) {
        payload.api_key = providerDraft.api_key.trim();
      }
      if (providerDraft.mode === "edit") {
        await api.updateProviderConfig(provider, payload);
      } else {
        await api.createProviderConfig(payload);
      }
      notify("服务商已保存", "success");
      setProviderDraft(EMPTY_PROVIDER_DRAFT);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const clearProviderKey = async (provider: string) => {
    if (!window.confirm(`确认清除 ${provider} 的 API key？`)) {
      return;
    }
    setBusy(true);
    try {
      await api.updateProviderConfig(provider, { api_key: "" });
      notify("API key 已清除", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const disableProvider = async (provider: string) => {
    if (!window.confirm(`确认禁用服务商 ${provider}？`)) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteProviderConfig(provider);
      notify("服务商已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const testProvider = async (provider: string) => {
    setBusy(true);
    try {
      const result = await api.testProviderConfig(provider);
      notify(result.message, result.success ? "success" : "error");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const saveProviderModel = async () => {
    const providerForModel = modelDraft.provider.trim() || selectedProviderId || "";
    if (!providerForModel || !modelDraft.upstream_model.trim()) {
      notify("请填写服务商和服务商模型 ID", "error");
      return;
    }
    const payload: ProviderModelConfigPayload = {
      provider: providerForModel,
      upstream_model: modelDraft.upstream_model.trim(),
      display_name: modelDraft.display_name.trim(),
      api_format: modelDraft.api_format,
      pricing_mode: modelDraft.pricing_mode,
      pricing_tiers_json:
        modelDraft.pricing_mode === "tiered" ? priceTierDraftsToJson(modelDraft.pricing_tiers) : "",
      input_price_per_million: clampNumber(
        decimalInputValue(modelDraft.input_price_per_million),
        0,
        1_000_000
      ),
      output_price_per_million: clampNumber(
        decimalInputValue(modelDraft.output_price_per_million),
        0,
        1_000_000
      ),
      currency: modelDraft.currency.trim() || "CNY",
      enabled: modelDraft.enabled
    };
    setBusy(true);
    try {
      if (modelDraft.mode === "edit") {
        await api.updateProviderModelConfig(modelDraft.id, payload);
      } else {
        await api.createProviderModelConfig(payload);
      }
      notify("服务商模型已保存", "success");
      setModelDraft({
        ...EMPTY_PROVIDER_MODEL_DRAFT,
        provider: selectedProviderId || "",
        pricing_tiers: createEmptyPriceTierDrafts()
      });
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const disableProviderModel = async (modelId: string) => {
    if (!window.confirm("确认禁用这个服务商模型？已绑定的路由也会停止使用它。")) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteProviderModelConfig(modelId);
      notify("服务商模型已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const openProviderDetail = (provider: string) => {
    setSelectedProviderId(provider);
    setModelDraft({
      ...EMPTY_PROVIDER_MODEL_DRAFT,
      provider,
      pricing_tiers: createEmptyPriceTierDrafts()
    });
  };

  const startNewProviderModel = () => {
    setModelDraft({
      ...EMPTY_PROVIDER_MODEL_DRAFT,
      provider: selectedProviderId || "",
      pricing_tiers: createEmptyPriceTierDrafts()
    });
  };

  const updatePricingTier = (index: number, patch: Partial<PriceTierDraft>) => {
    setModelDraft((current) => ({
      ...current,
      pricing_tiers: ensureTwoPriceTierDrafts(current.pricing_tiers).map((tier, tierIndex) =>
        tierIndex === index ? { ...tier, ...patch } : tier
      )
    }));
  };

  if (data && selectedProvider) {
    const providerId = selectedProvider.provider || selectedProvider.id;
    return (
      <div className="page-stack">
        <PageHeader
          title={selectedProvider.name || providerId}
          subtitle="配置这个服务商下面的真实模型 ID、接口类型和价格。"
          action={
            <div className="button-row">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setSelectedProviderId(null)}
              >
                返回服务商
              </button>
              <button className="secondary-button" type="button" onClick={load}>
                <RefreshCcw size={16} />
                刷新
              </button>
            </div>
          }
        />

        <div className="stats-grid">
          <StatCard label="服务商模型" value={selectedProviderModels.length} />
          <StatCard
            label="OpenAI-compatible"
            value={
              selectedProviderModels.filter((model) => model.api_format === "openai_compatible").length
            }
          />
          <StatCard
            label="Claude SDK"
            value={selectedProviderModels.filter((model) => model.api_format === "claude_sdk").length}
          />
        </div>

        <section className="panel">
          <div className="panel-header">
            <h2>服务商信息</h2>
            <div className="button-row">
              <button
                className="secondary-button compact"
                type="button"
                onClick={() => {
                  setProviderDraft(providerToDraft(selectedProvider));
                  setSelectedProviderId(null);
                }}
              >
                编辑
              </button>
              <button
                className="secondary-button compact"
                type="button"
                disabled={busy}
                onClick={() => testProvider(providerId)}
              >
                测试
              </button>
            </div>
          </div>
          <dl className="field-list compact">
            <div>
              <dt>服务商 ID</dt>
              <dd>{providerId}</dd>
            </div>
            <div>
              <dt>基础地址</dt>
              <dd>{selectedProvider.base_url}</dd>
            </div>
            <div>
              <dt>API Key</dt>
              <dd>{selectedProvider.api_key_configured ? "已配置" : "未配置"}</dd>
            </div>
            <div>
              <dt>状态</dt>
              <dd>{selectedProvider.enabled ? "启用" : "禁用"}</dd>
            </div>
          </dl>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>模型配置</h2>
            <button className="secondary-button" type="button" onClick={startNewProviderModel}>
              新增
            </button>
          </div>
          <div className="toolbar">
            <label className="field-block small">
              <span>显示名称</span>
              <input
                value={modelDraft.display_name}
                onChange={(event) => setModelDraft({ ...modelDraft, display_name: event.target.value })}
                placeholder="GLM 5.1"
              />
            </label>
            <label className="field-block small">
              <span>服务商模型 ID</span>
              <input
                value={modelDraft.upstream_model}
                onChange={(event) => setModelDraft({ ...modelDraft, upstream_model: event.target.value })}
                placeholder="glm-5-1"
              />
            </label>
            <label className="field-block small">
              <span>接口类型</span>
              <select
                value={modelDraft.api_format}
                onChange={(event) =>
                  setModelDraft({
                    ...modelDraft,
                    api_format: event.target.value as ProviderModelDraft["api_format"]
                  })
                }
              >
                <option value="openai_compatible">OpenAI-compatible</option>
                <option value="claude_sdk">Claude SDK</option>
              </select>
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={modelDraft.pricing_mode === "tiered"}
                onChange={(event) =>
                  setModelDraft({
                    ...modelDraft,
                    pricing_mode: event.target.checked ? "tiered" : "flat",
                    pricing_tiers: ensureTwoPriceTierDrafts(modelDraft.pricing_tiers)
                  })
                }
              />
              分级价格
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={modelDraft.enabled}
                onChange={(event) => setModelDraft({ ...modelDraft, enabled: event.target.checked })}
              />
              启用
            </label>
          </div>

          <div className="toolbar">
            <label className="field-block small">
              <span>输入价格 / 1M</span>
              <DecimalInput
                value={modelDraft.input_price_per_million}
                onChange={(value) => setModelDraft({ ...modelDraft, input_price_per_million: value })}
              />
            </label>
            <label className="field-block small">
              <span>输出价格 / 1M</span>
              <DecimalInput
                value={modelDraft.output_price_per_million}
                onChange={(value) => setModelDraft({ ...modelDraft, output_price_per_million: value })}
              />
            </label>
            <label className="field-block small">
              <span>币种</span>
              <input
                value={modelDraft.currency}
                onChange={(event) => setModelDraft({ ...modelDraft, currency: event.target.value })}
              />
            </label>
            <button className="primary-button" type="button" disabled={busy} onClick={saveProviderModel}>
              <Save size={16} />
              保存模型
            </button>
          </div>

          {modelDraft.pricing_mode === "tiered" && (
            <div className="tier-editor">
              {ensureTwoPriceTierDrafts(modelDraft.pricing_tiers).map((tier, index) => (
                <div className="tier-row" key={index}>
                  <strong>第 {index + 1} 档</strong>
                  <label className="field-block small">
                    <span>Token 上限</span>
                    <DecimalInput
                      step="1"
                      placeholder={index === 0 ? "1000000" : "不限"}
                      emptyValueOnBlur={index === 0 ? "0" : ""}
                      value={tier.up_to_tokens}
                      onChange={(value) => updatePricingTier(index, { up_to_tokens: value })}
                    />
                  </label>
                  <label className="field-block small">
                    <span>输入价格 / 1M</span>
                    <DecimalInput
                      value={tier.input_price_per_million}
                      onChange={(value) =>
                        updatePricingTier(index, { input_price_per_million: value })
                      }
                    />
                  </label>
                  <label className="field-block small">
                    <span>输出价格 / 1M</span>
                    <DecimalInput
                      value={tier.output_price_per_million}
                      onChange={(value) =>
                        updatePricingTier(index, { output_price_per_million: value })
                      }
                    />
                  </label>
                </div>
              ))}
            </div>
          )}

          {selectedProviderModels.length === 0 && <EmptyBlock label="暂无服务商模型" />}
          {selectedProviderModels.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>显示名称</th>
                    <th>服务商模型 ID</th>
                    <th>接口类型</th>
                    <th>计费</th>
                    <th>价格 / 1M</th>
                    <th>启用</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedProviderModels.map((model) => (
                    <tr key={model.id}>
                      <td>{model.display_name || "-"}</td>
                      <td>{model.upstream_model}</td>
                      <td>{apiFormatText(model.api_format)}</td>
                      <td>{pricingModeText(model.pricing_mode)}</td>
                      <td>
                        {moneyText(model.input_price_per_million, model.currency)} /{" "}
                        {moneyText(model.output_price_per_million, model.currency)}
                      </td>
                      <td>{model.enabled ? "是" : "否"}</td>
                      <td>
                        <div className="button-row">
                          <button
                            className="secondary-button compact"
                            type="button"
                            onClick={() => setModelDraft(providerModelToDraft(model))}
                          >
                            编辑
                          </button>
                          <button
                            className="warning-button compact"
                            type="button"
                            disabled={busy}
                            onClick={() => disableProviderModel(model.id)}
                          >
                            禁用
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    );
  }

  return (
    <div className="page-stack">
      <PageHeader
        title="服务商"
        subtitle="配置上游 API 服务商；点进某个服务商后再配置它下面的真实模型和价格。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载服务商" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="服务商" value={providers.length} />
            <StatCard label="服务商模型" value={providerModels.length} />
            <StatCard label="已配置密钥" value={providers.filter((provider) => provider.api_key_configured).length} />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>服务商管理</h2>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setProviderDraft(EMPTY_PROVIDER_DRAFT)}
              >
                新增
              </button>
            </div>
            <div className="toolbar">
              <label className="field-block small">
                <span>服务商 ID</span>
                <input
                  value={providerDraft.provider}
                  disabled={providerDraft.mode === "edit"}
                  onChange={(event) => setProviderDraft({ ...providerDraft, provider: event.target.value })}
                  placeholder="zhipu"
                />
              </label>
              <label className="field-block small">
                <span>名称</span>
                <input
                  value={providerDraft.name}
                  onChange={(event) => setProviderDraft({ ...providerDraft, name: event.target.value })}
                  placeholder="智谱"
                />
              </label>
              <label className="field-block small wide-field">
                <span>基础地址</span>
                <input
                  value={providerDraft.base_url}
                  onChange={(event) => setProviderDraft({ ...providerDraft, base_url: event.target.value })}
                  placeholder="https://open.bigmodel.cn/api/paas/v4"
                />
              </label>
              <label className="field-block small">
                <span>API Key</span>
                <input
                  type="password"
                  value={providerDraft.api_key}
                  onChange={(event) => setProviderDraft({ ...providerDraft, api_key: event.target.value })}
                  placeholder={providerDraft.mode === "edit" ? "留空则不变" : "可稍后填写"}
                />
              </label>
              <label className="field-block small">
                <span>超时</span>
                <input
                  type="number"
                  min={1}
                  value={providerDraft.timeout_seconds}
                  onChange={(event) =>
                    setProviderDraft({ ...providerDraft, timeout_seconds: Number(event.target.value) })
                  }
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={providerDraft.enabled}
                  onChange={(event) => setProviderDraft({ ...providerDraft, enabled: event.target.checked })}
                />
                启用
              </label>
              <button className="primary-button" type="button" disabled={busy} onClick={saveProvider}>
                <Save size={16} />
                保存服务商
              </button>
            </div>
            {providers.length === 0 && <EmptyBlock label="暂无服务商配置" />}
            {providers.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>名称</th>
                      <th>启用</th>
                      <th>基础地址</th>
                      <th>API Key</th>
                      <th>超时</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {providers.map((provider) => (
                      <tr key={provider.id || provider.provider}>
                        <td>{provider.provider || provider.id}</td>
                        <td>{provider.name}</td>
                        <td>{provider.enabled ? "是" : "否"}</td>
                        <td>{provider.base_url}</td>
                        <td>{provider.api_key_configured ? "已配置" : "未配置"}</td>
                        <td>{provider.timeout_seconds}s</td>
                        <td>
                          <div className="button-row">
                            <button
                              className="secondary-button compact"
                              type="button"
                              onClick={() => setProviderDraft(providerToDraft(provider))}
                            >
                              编辑
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              onClick={() => openProviderDetail(provider.provider || provider.id)}
                            >
                              模型
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => testProvider(provider.provider || provider.id)}
                            >
                              测试
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => clearProviderKey(provider.provider || provider.id)}
                            >
                              清除密钥
                            </button>
                            <button
                              className="warning-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => disableProvider(provider.provider || provider.id)}
                            >
                              禁用
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

        </>
      )}
    </div>
  );
}

function RoutesPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [state, setState] = useState<LoadState<ProviderConfigResponse>>({
    loading: true,
    error: null,
    data: null
  });
  const [routeDraft, setRouteDraft] = useState<RouteDraft>(EMPTY_ROUTE_DRAFT);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.providerConfig() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const providerModels = data?.provider_models || [];
  const routes = data?.routes || [];
  const enabledProviderModels = providerModels.filter((model) => model.enabled);

  const saveRoute = async () => {
    if (!routeDraft.virtual_model.trim() || !routeDraft.provider_model_id.trim()) {
      notify("请填写对外模型名，并选择一个服务商模型", "error");
      return;
    }
    const selectedModel = providerModels.find((model) => model.id === routeDraft.provider_model_id);
    if (!selectedModel) {
      notify("选择的服务商模型不存在", "error");
      return;
    }
    const payload: RouteConfigPayload = {
      virtual_model: routeDraft.virtual_model.trim(),
      provider_model_id: selectedModel.id,
      priority: Math.round(routeDraft.priority),
      min_balance: clampNumber(routeDraft.min_balance, 0, 1_000_000_000),
      enabled: routeDraft.enabled
    };
    setBusy(true);
    try {
      if (routeDraft.mode === "edit") {
        await api.updateRouteConfig(routeDraft.id, payload);
      } else {
        await api.createRouteConfig(payload);
      }
      notify("路由已保存", "success");
      setRouteDraft(EMPTY_ROUTE_DRAFT);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const deleteRoute = async (routeId: string) => {
    if (!window.confirm("确认删除这条路由？")) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteRouteConfig(routeId);
      notify("路由已删除", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="路由"
        subtitle="把对外统一模型名映射到一个或多个服务商模型。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载路由" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="对外模型" value={new Set(routes.map((route) => route.virtual_model)).size} />
            <StatCard label="路由" value={routes.length} />
            <StatCard label="可选服务商模型" value={enabledProviderModels.length} />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>路由管理</h2>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setRouteDraft(EMPTY_ROUTE_DRAFT)}
              >
                新增
              </button>
            </div>
            <div className="toolbar">
              <label className="field-block small">
                <span>对外模型名</span>
                <input
                  value={routeDraft.virtual_model}
                  onChange={(event) => setRouteDraft({ ...routeDraft, virtual_model: event.target.value })}
                  placeholder="glm-5.1"
                />
              </label>
              <label className="field-block small wide-field">
                <span>服务商模型</span>
                <select
                  value={routeDraft.provider_model_id}
                  onChange={(event) => setRouteDraft({ ...routeDraft, provider_model_id: event.target.value })}
                >
                  <option value="">选择服务商模型</option>
                  {enabledProviderModels.map((model) => (
                    <option key={model.id} value={model.id}>
                      {providerModelLabel(model)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field-block small">
                <span>优先级</span>
                <input
                  type="number"
                  value={routeDraft.priority}
                  onChange={(event) => setRouteDraft({ ...routeDraft, priority: Number(event.target.value) })}
                />
              </label>
              <label className="field-block small">
                <span>最低余额</span>
                <input
                  type="number"
                  min={0}
                  step="0.000001"
                  value={routeDraft.min_balance}
                  onChange={(event) => setRouteDraft({ ...routeDraft, min_balance: Number(event.target.value) })}
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={routeDraft.enabled}
                  onChange={(event) => setRouteDraft({ ...routeDraft, enabled: event.target.checked })}
                />
                启用
              </label>
              <button className="primary-button" type="button" disabled={busy} onClick={saveRoute}>
                <Save size={16} />
                保存路由
              </button>
            </div>
            {providerModels.length === 0 && (
              <div className="notice warning">
                <ShieldAlert size={18} />
                先到“服务商”页面新增至少一个服务商模型，再配置路由。
              </div>
            )}
            {routes.length === 0 && <EmptyBlock label="暂无路由" />}
            {routes.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>对外模型名</th>
                      <th>服务商模型</th>
                      <th>服务商</th>
                      <th>真实模型 ID</th>
                      <th>优先级</th>
                      <th>价格 / 1M</th>
                      <th>最低余额</th>
                      <th>启用</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routes.map((route) => {
                      const providerModel = providerModels.find((model) => model.id === route.provider_model_id);
                      return (
                        <tr key={route.id || `${route.virtual_model}-${route.provider}-${route.upstream_model}`}>
                          <td>{route.virtual_model}</td>
                          <td>{providerModel ? providerModelLabel(providerModel) : "旧路由"}</td>
                          <td>{route.provider}</td>
                          <td>{route.upstream_model}</td>
                          <td>{route.priority}</td>
                          <td>
                            {moneyText(route.input_price_per_million, route.currency)} /{" "}
                            {moneyText(route.output_price_per_million, route.currency)}
                          </td>
                          <td>{moneyText(route.min_balance, route.currency)}</td>
                          <td>{route.enabled === false ? "否" : "是"}</td>
                          <td>
                            <div className="button-row">
                              <button
                                className="secondary-button compact"
                                type="button"
                                onClick={() => setRouteDraft(routeToDraft(route))}
                              >
                                编辑
                              </button>
                              {route.id && !route.id.startsWith("toml:") && (
                                <button
                                  className="danger-button compact"
                                  type="button"
                                  disabled={busy}
                                  onClick={() => deleteRoute(route.id || "")}
                                >
                                  删除
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function BillingPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: (message: string, kind?: Toast["kind"]) => void;
}) {
  const [state, setState] = useState<LoadState<BalanceRecord[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [draft, setDraft] = useState({
    provider: "",
    amount: "",
    currency: "CNY",
    reason: "手动调整"
  });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.balances() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const firstProvider = state.data?.[0]?.provider;
    if (firstProvider && !draft.provider) {
      setDraft((current) => ({ ...current, provider: firstProvider }));
    }
  }, [draft.provider, state.data]);

  const adjust = async () => {
    const provider = draft.provider.trim();
    const amount = Number(draft.amount);
    if (!provider || Number.isNaN(amount)) {
      notify("请填写服务商和有效金额", "error");
      return;
    }
    setSaving(true);
    try {
      await api.adjustBalance(provider, {
        amount_delta: amount,
        currency: draft.currency.trim() || "CNY",
        reason: draft.reason.trim()
      });
      notify("余额已调整", "success");
      setDraft((current) => ({ ...current, amount: "", reason: "手动调整" }));
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setSaving(false);
    }
  };

  const balances = state.data || [];

  return (
    <div className="page-stack">
      <PageHeader
        title="余额账本"
        subtitle="本地服务商余额账本和手动调整。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <section className="panel">
        <div className="panel-header">
          <h2>手动调整余额</h2>
        </div>
        <div className="toolbar">
          <label className="field-block small">
            <span>服务商</span>
            <input
              value={draft.provider}
              onChange={(event) => setDraft({ ...draft, provider: event.target.value })}
              placeholder="zhipu"
              list="provider-balance-list"
            />
            <datalist id="provider-balance-list">
              {balances.map((balance) => (
                <option key={balance.provider} value={balance.provider} />
              ))}
            </datalist>
          </label>
          <label className="field-block small">
            <span>调整金额</span>
            <input
              type="number"
              step="0.000001"
              value={draft.amount}
              onChange={(event) => setDraft({ ...draft, amount: event.target.value })}
              placeholder="100"
            />
          </label>
          <label className="field-block small">
            <span>币种</span>
            <input
              value={draft.currency}
              onChange={(event) => setDraft({ ...draft, currency: event.target.value })}
            />
          </label>
          <label className="field-block small">
            <span>原因</span>
            <input
              value={draft.reason}
              onChange={(event) => setDraft({ ...draft, reason: event.target.value })}
            />
          </label>
          <button className="primary-button" type="button" disabled={saving} onClick={adjust}>
            <Save size={16} />
            保存
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>余额</h2>
        </div>
        {state.loading && <LoadingBlock label="正在加载余额" />}
        {state.error && <ErrorBlock message={state.error} onRetry={load} />}
        {!state.loading && !state.error && balances.length === 0 && <EmptyBlock label="暂无余额记录" />}
        {balances.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>服务商</th>
                  <th>余额</th>
                  <th>币种</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {balances.map((balance) => (
                  <tr key={balance.provider}>
                    <td>{balance.provider}</td>
                    <td>{numberText(balance.balance)}</td>
                    <td>{balance.currency}</td>
                    <td>{dateText(balance.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function UsagePage({ api }: { api: MemoryApi }) {
  const [state, setState] = useState<LoadState<{ events: UsageEvent[]; summary: UsageSummary[] }>>({
    loading: true,
    error: null,
    data: null
  });

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      const [events, summary] = await Promise.all([api.usage(100), api.usageSummary()]);
      setState({ loading: false, error: null, data: { events, summary } });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const totalCalls = data?.summary.reduce((sum, item) => sum + Number(item.calls || 0), 0) || 0;
  const totalTokens = data?.summary.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0) || 0;
  const totalCost = data?.summary.reduce((sum, item) => sum + Number(item.total_cost || 0), 0) || 0;
  const currency = data?.summary[0]?.currency || "CNY";

  return (
    <div className="page-stack">
      <PageHeader
        title="用量统计"
        subtitle="服务商调用记录和按服务商/模型聚合的用量。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载用量" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="调用数" value={totalCalls} />
            <StatCard label="总 Tokens" value={numberText(totalTokens)} />
            <StatCard label="费用" value={moneyText(totalCost, currency)} />
            <StatCard label="最近记录" value={data.events.length} />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>用量汇总</h2>
            </div>
            {data.summary.length === 0 && <EmptyBlock label="暂无用量汇总" />}
            {data.summary.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>服务商</th>
                      <th>虚拟模型</th>
                      <th>调用数</th>
                      <th>输入 Tokens</th>
                      <th>输出 Tokens</th>
                      <th>总 Tokens</th>
                      <th>总费用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.summary.map((item) => (
                      <tr key={`${item.provider}-${item.virtual_model}-${item.currency}`}>
                        <td>{item.provider}</td>
                        <td>{item.virtual_model}</td>
                        <td>{numberText(item.calls)}</td>
                        <td>{numberText(item.prompt_tokens)}</td>
                        <td>{numberText(item.completion_tokens)}</td>
                        <td>{numberText(item.total_tokens)}</td>
                        <td>{moneyText(item.total_cost, item.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>最近调用</h2>
            </div>
            {data.events.length === 0 && <EmptyBlock label="暂无调用记录" />}
            {data.events.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>状态</th>
                      <th>服务商</th>
                      <th>虚拟模型</th>
                      <th>上游模型</th>
                      <th>Tokens</th>
                      <th>费用</th>
                      <th>估算</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.events.map((event) => (
                      <tr key={event.id}>
                        <td>{dateText(event.created_at)}</td>
                        <td>{badge(event.status)}</td>
                        <td>{event.provider}</td>
                        <td>{event.virtual_model}</td>
                        <td>{event.upstream_model}</td>
                        <td>{numberText(event.total_tokens)}</td>
                        <td>{moneyText(event.total_cost, event.currency)}</td>
                        <td>{event.estimated ? "是" : "否"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
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

type MemoryEditDraft = {
  content: string;
  type: MemoryType;
  importance: number;
  confidence: number;
  stability: MemoryStability;
  sensitivity: MemorySensitivity;
  valid_until: string;
  review_after: string;
  source_message: string;
  source_conversation_id: string;
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
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState<MemoryEditDraft | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
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
    setEditing(false);
    setEditDraft(null);
    setEditError(null);
    setSavingEdit(false);
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

  const startEdit = () => {
    if (!selected) return;
    setEditDraft(memoryToEditDraft(selected));
    setEditError(null);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setEditDraft(null);
    setEditError(null);
  };

  const saveEdit = async () => {
    if (!selected || !editDraft) return;
    if (!editDraft.content.trim()) {
      setEditError("content 不能为空");
      return;
    }
    if (!window.confirm("确定要更新这条记忆吗？这会影响后续检索和回答注入。")) {
      return;
    }
    setSavingEdit(true);
    setEditError(null);
    try {
      const result = await api.updateMemory(selected.id, editDraftToPayload(editDraft));
      setSelected(result.memory);
      setState((current) =>
        current.data
          ? {
              ...current,
              data: current.data.map((memory) =>
                memory.id === result.memory.id ? result.memory : memory
              )
            }
          : current
      );
      notify("记忆已更新", "success");
      setEditing(false);
      setEditDraft(null);
      await load("active");
    } catch (error) {
      setEditError(errorMessage(error));
    } finally {
      setSavingEdit(false);
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
            {editing && editDraft ? (
              <div className="edit-form">
                <label className="field-block">
                  <span>内容</span>
                  <textarea
                    value={editDraft.content}
                    rows={5}
                    onChange={(event) =>
                      setEditDraft({ ...editDraft, content: event.target.value })
                    }
                  />
                </label>
                {editDraft.content.trim() !== selected.content && (
                  <div className="notice warning">
                    修改内容后，旧 embedding 会失效；后续版本可提供重建 embedding。
                  </div>
                )}
                <div className="edit-grid">
                  <label className="field-block">
                    <span>类型</span>
                    <select
                      value={editDraft.type}
                      onChange={(event) =>
                        setEditDraft({ ...editDraft, type: event.target.value as MemoryType })
                      }
                    >
                      {MEMORY_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {displayText(type)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>重要度</span>
                    <input
                      type="number"
                      min={1}
                      max={10}
                      step={1}
                      value={editDraft.importance}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          importance: Math.round(clampNumber(Number(event.target.value), 1, 10))
                        })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>置信度</span>
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={editDraft.confidence}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          confidence: clampNumber(Number(event.target.value), 0, 1)
                        })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>稳定性</span>
                    <select
                      value={editDraft.stability}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          stability: event.target.value as MemoryStability
                        })
                      }
                    >
                      {STABILITIES.map((stability) => (
                        <option key={stability} value={stability}>
                          {displayText(stability)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>敏感级别</span>
                    <select
                      value={editDraft.sensitivity}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          sensitivity: event.target.value as MemorySensitivity
                        })
                      }
                    >
                      {SENSITIVITIES.map((sensitivity) => (
                        <option key={sensitivity} value={sensitivity}>
                          {displayText(sensitivity)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>有效期</span>
                    <input
                      value={editDraft.valid_until}
                      onChange={(event) =>
                        setEditDraft({ ...editDraft, valid_until: event.target.value })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>复核时间</span>
                    <input
                      value={editDraft.review_after}
                      onChange={(event) =>
                        setEditDraft({ ...editDraft, review_after: event.target.value })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>来源对话 ID</span>
                    <input
                      value={editDraft.source_conversation_id}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          source_conversation_id: event.target.value
                        })
                      }
                    />
                  </label>
                </div>
                <label className="field-block">
                  <span>来源原文</span>
                  <textarea
                    value={editDraft.source_message}
                    rows={3}
                    onChange={(event) =>
                      setEditDraft({ ...editDraft, source_message: event.target.value })
                    }
                  />
                </label>
                {editError && (
                  <div className="notice warning">
                    <ShieldAlert size={16} />
                    {editError}
                  </div>
                )}
              </div>
            ) : (
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
            )}
            <div className="drawer-actions">
              {tab === "active" && editing && (
                <>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={savingEdit}
                    onClick={saveEdit}
                  >
                    <Save size={16} />
                    保存
                  </button>
                  <button
                    className="ghost-button"
                    type="button"
                    disabled={savingEdit}
                    onClick={cancelEdit}
                  >
                    <X size={16} />
                    取消
                  </button>
                </>
              )}
              {tab === "active" && !editing && (
                <>
                  <button className="secondary-button" type="button" onClick={startEdit}>
                    <Pencil size={16} />
                    编辑
                  </button>
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
            {!editing && why.loading && <LoadingBlock label="正在读取来源" />}
            {!editing && why.error && <ErrorBlock message={why.error} />}
            {!editing && why.data && (
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

  const applyLower = async (recommendation: ReviewRecommendation) => {
    if (recommendation.memory_ids.length !== 1) {
      notify("降权建议需要只包含一条记忆", "error");
      return;
    }
    const memoryId = recommendation.memory_ids[0];
    const memory = memoryMap.get(memoryId);
    if (!memory) {
      notify("未在当前活跃记忆中找到这条记忆", "error");
      return;
    }
    if (!window.confirm("确定要降低这条记忆的重要度吗？")) {
      return;
    }
    setApplying(true);
    try {
      const nextImportance = Math.max(1, memory.importance - 1);
      await api.updateMemory(memoryId, { importance: nextImportance });
      notify("已降低记忆重要度", "success");
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
                            <button
                              className="warning-button"
                              type="button"
                              disabled={applying || recommendation.memory_ids.length !== 1}
                              onClick={() => applyLower(recommendation)}
                            >
                              <ArrowDown size={16} />
                              应用降权
                            </button>
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
          <FieldList
            entries={[
              ["记忆 ID", mergeDraft.memory_ids],
              ["原因", mergeDraft.reason],
              ["建议内容", mergeDraft.suggested_content || "未提供，确认后由后端默认拼接"]
            ]}
          />
          <div className="linked-memories merge-preview-list">
            {mergeDraft.memory_ids.map((id) => {
              const memory = memoryMap.get(id);
              return (
                <div className="linked-memory" key={id}>
                  <strong>{shortId(id)}</strong>
                  <span>{memory?.content || "未在当前活跃记忆中找到"}</span>
                </div>
              );
            })}
          </div>
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
            ["模型", "填写 providers.toml 中的 virtual model"]
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

function DecimalInput({
  value,
  onChange,
  step = "0.000001",
  placeholder,
  emptyValueOnBlur = "0"
}: {
  value: string;
  onChange: (value: string) => void;
  step?: string;
  placeholder?: string;
  emptyValueOnBlur?: string;
}) {
  return (
    <input
      type="number"
      min={0}
      step={step}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(normalizeDecimalInput(event.target.value))}
      onBlur={(event) => onChange(normalizeDecimalInputOnBlur(event.target.value, emptyValueOnBlur))}
    />
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

function sourceText(source: string): string {
  if (source === "sqlite") return "SQLite UI 配置";
  if (source === "toml") return "providers.toml";
  return "legacy UPSTREAM_*";
}

function providerToDraft(provider: ProviderSummary): ProviderDraft {
  return {
    mode: "edit",
    provider: provider.provider || provider.id,
    name: provider.name,
    base_url: provider.base_url,
    api_key: "",
    enabled: provider.enabled,
    timeout_seconds: provider.timeout_seconds
  };
}

function providerModelToDraft(model: ProviderModelSummary): ProviderModelDraft {
  return {
    mode: "edit",
    id: model.id,
    provider: model.provider,
    upstream_model: model.upstream_model,
    display_name: model.display_name,
    api_format: model.api_format || "openai_compatible",
    pricing_mode: model.pricing_mode || "flat",
    pricing_tiers_json: model.pricing_tiers_json || "",
    pricing_tiers: priceTierDraftsFromJson(model.pricing_tiers_json),
    input_price_per_million: decimalInputText(model.input_price_per_million),
    output_price_per_million: decimalInputText(model.output_price_per_million),
    currency: model.currency,
    enabled: model.enabled !== false
  };
}

function routeToDraft(route: RouteSummary): RouteDraft {
  return {
    mode: "edit",
    id: route.id || "",
    virtual_model: route.virtual_model,
    provider_model_id: route.provider_model_id || "",
    provider: route.provider,
    upstream_model: route.upstream_model,
    priority: route.priority,
    input_price_per_million: decimalInputText(route.input_price_per_million),
    output_price_per_million: decimalInputText(route.output_price_per_million),
    currency: route.currency,
    min_balance: route.min_balance,
    enabled: route.enabled !== false
  };
}

function providerModelLabel(model: ProviderModelSummary): string {
  const name = model.display_name ? `${model.display_name} · ` : "";
  const apiFormat = model.api_format === "claude_sdk" ? " · Claude SDK" : "";
  return `${model.provider} / ${name}${model.upstream_model}${apiFormat}`;
}

function apiFormatText(value?: string | null): string {
  if (value === "claude_sdk") return "Claude SDK";
  return "OpenAI-compatible";
}

function pricingModeText(value?: string | null): string {
  if (value === "tiered") return "分级价格";
  return "固定价格";
}

function numberText(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 6
  });
}

function moneyText(value?: number | null, currency?: string | null) {
  const amount = numberText(value);
  return `${amount} ${currency || ""}`.trim();
}

function createEmptyPriceTierDrafts(): PriceTierDraft[] {
  return [
    {
      up_to_tokens: "1000000",
      input_price_per_million: "0",
      output_price_per_million: "0"
    },
    {
      up_to_tokens: "",
      input_price_per_million: "0",
      output_price_per_million: "0"
    }
  ];
}

function ensureTwoPriceTierDrafts(tiers?: PriceTierDraft[] | null): PriceTierDraft[] {
  const defaults = createEmptyPriceTierDrafts();
  return defaults.map((fallback, index) => ({
    ...fallback,
    ...(tiers?.[index] || {})
  }));
}

function priceTierDraftsFromJson(raw?: string | null): PriceTierDraft[] {
  if (!raw?.trim()) {
    return createEmptyPriceTierDrafts();
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) {
      return createEmptyPriceTierDrafts();
    }
    const defaults = createEmptyPriceTierDrafts();
    return defaults.map((fallback, index) => {
      const tier = parsed[index];
      if (!isRecord(tier)) {
        return fallback;
      }
      return {
        up_to_tokens:
          tier.up_to_tokens === null || tier.up_to_tokens === undefined
            ? fallback.up_to_tokens
            : decimalInputText(tier.up_to_tokens, fallback.up_to_tokens),
        input_price_per_million: decimalInputText(
          tier.input_price_per_million ?? tier.input,
          fallback.input_price_per_million
        ),
        output_price_per_million: decimalInputText(
          tier.output_price_per_million ?? tier.output,
          fallback.output_price_per_million
        )
      };
    });
  } catch {
    return createEmptyPriceTierDrafts();
  }
}

function priceTierDraftsToJson(tiers?: PriceTierDraft[] | null): string {
  return JSON.stringify(
    ensureTwoPriceTierDrafts(tiers).map((tier) => ({
      up_to_tokens: tier.up_to_tokens.trim()
        ? Math.round(clampNumber(decimalInputValue(tier.up_to_tokens), 0, Number.MAX_SAFE_INTEGER))
        : null,
      input: clampNumber(decimalInputValue(tier.input_price_per_million), 0, 1_000_000),
      output: clampNumber(decimalInputValue(tier.output_price_per_million), 0, 1_000_000)
    }))
  );
}

function normalizeDecimalInput(raw: string): string {
  const value = raw.trim().replace(",", ".");
  if (!value) {
    return "";
  }
  const clean = value.replace(/[^\d.]/g, "");
  if (!clean) {
    return "";
  }
  const dotIndex = clean.indexOf(".");
  const hasDecimal = dotIndex !== -1;
  const wholeRaw = hasDecimal ? clean.slice(0, dotIndex) : clean;
  const fraction = hasDecimal ? clean.slice(dotIndex + 1).replace(/\./g, "") : "";
  const whole = wholeRaw.replace(/^0+(?=\d)/, "") || "0";
  return hasDecimal ? `${whole}.${fraction}` : whole;
}

function normalizeDecimalInputOnBlur(raw: string, emptyValue = "0"): string {
  const normalized = normalizeDecimalInput(raw);
  if (!normalized) {
    return emptyValue;
  }
  if (normalized === "0.") {
    return "0";
  }
  if (normalized.endsWith(".")) {
    return normalized.slice(0, -1) || "0";
  }
  return normalized;
}

function decimalInputValue(value: string): number {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function decimalInputText(value: unknown, fallback = "0"): string {
  if (typeof value === "string" && value.trim() === "") {
    return fallback;
  }
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    return fallback;
  }
  return number.toLocaleString("en-US", {
    useGrouping: false,
    maximumFractionDigits: 12
  });
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

function memoryToEditDraft(memory: MemoryRecord): MemoryEditDraft {
  return {
    content: memory.content,
    type: memory.type,
    importance: memory.importance,
    confidence: memory.confidence,
    stability: memory.stability,
    sensitivity: memory.sensitivity,
    valid_until: memory.valid_until || "",
    review_after: memory.review_after || "",
    source_message: memory.source_message || "",
    source_conversation_id: memory.source_conversation_id || ""
  };
}

function editDraftToPayload(draft: MemoryEditDraft): MemoryUpdatePayload {
  return {
    content: draft.content.trim(),
    type: draft.type,
    importance: Math.round(clampNumber(draft.importance, 1, 10)),
    confidence: clampNumber(draft.confidence, 0, 1),
    stability: draft.stability,
    sensitivity: draft.sensitivity,
    valid_until: nullableText(draft.valid_until),
    review_after: nullableText(draft.review_after),
    source_message: nullableText(draft.source_message),
    source_conversation_id: nullableText(draft.source_conversation_id)
  };
}

function nullableText(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function clampNumber(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
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
