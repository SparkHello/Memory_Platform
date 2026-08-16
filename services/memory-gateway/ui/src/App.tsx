import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, MemoryApi } from "./api";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { MemoryDetailDrawer } from "./components/MemoryDetailDrawer";
import { ToastView } from "./components/Toast";
import { useConfirm } from "./hooks/useConfirm";
import { useToast } from "./hooks/useToast";
import { AppShell, type NavSignals } from "./layout/AppShell";
import { hashForRoute, parseHash } from "./navigation";
import { DashboardPage } from "./pages/DashboardPage";
import { KnowledgeLibraryPage } from "./pages/knowledge/KnowledgeLibraryPage";
import { KnowledgeSearchPage } from "./pages/knowledge/KnowledgeSearchPage";
import { CoreMemoryPage } from "./pages/memory/CoreMemoryPage";
import { DecisionLogsPage } from "./pages/memory/DecisionLogsPage";
import { EvaluationPage } from "./pages/memory/EvaluationPage";
import { MemoriesPage } from "./pages/memory/MemoriesPage";
import { RecallExplainPage } from "./pages/memory/RecallExplainPage";
import { RecentContextPage } from "./pages/memory/RecentContextPage";
import { ReportsPage } from "./pages/memory/ReportsPage";
import { ReviewPage } from "./pages/memory/ReviewPage";
import { DeveloperPage } from "./pages/system/DeveloperPage";
import { ProvidersPage } from "./pages/system/ProvidersPage";
import { SettingsPage } from "./pages/system/SettingsPage";
import { UsagePage } from "./pages/system/UsagePage";
import {
  loadSettings,
  loadTheme,
  loadUiMode,
  saveSettings,
  saveTheme,
  saveUiMode,
  type ThemeMode,
  type UiMode
} from "./storage";
import type { ConnectionSettings, KnowledgeStatus, PageKey, ProvidersStatus } from "./types";
import { errorMessage } from "./utils/format";
import { isProviderSetupReady } from "./utils/providerSetup";
import { scrollWorkspaceToTop } from "./utils/scroll";

// e2e 专用崩溃探针：仅 e2e 构建模式存在，生产构建中被 Vite 消除。
function E2eCrashProbe() {
  if (import.meta.env.MODE === "e2e" && (window as { __CONSOLE_CRASH_PAGE__?: boolean }).__CONSOLE_CRASH_PAGE__) {
    throw new Error("e2e 合成渲染崩溃");
  }
  return null;
}

/**
 * memgw open 生成的一次性登录链接形态 #login=<code>。
 * 必须在 hash 路由解析（parseHash）之前单独识别：它不是页面路由，
 * 若交给路由层会被判成未知 hash 而显示「页面不存在」。
 */
function readLoginCodeFromHash(): string | null {
  const match = window.location.hash.match(/^#login=([^&]+)$/);
  return match ? decodeURIComponent(match[1]) : null;
}

export function App() {
  const [settings, setSettings] = useState<ConnectionSettings>(() => loadSettings());
  const [theme, setTheme] = useState<ThemeMode>(() => loadTheme());
  const [uiMode, setUiMode] = useState<UiMode>(() => loadUiMode());
  // 无密钥或密钥失效时挡住主站，必须先完成连接设置（简洁模式也不例外）。
  const [credentialsBlocked, setCredentialsBlocked] = useState(
    () => !loadSettings().apiKey
  );
  const [page, setPage] = useState<PageKey>(() => {
    const saved = loadSettings();
    return parseHash(window.location.hash)?.page ?? (saved.apiKey ? "dashboard" : "settings");
  });
  const [memoryId, setMemoryId] = useState<string | null>(
    () => parseHash(window.location.hash)?.memoryId ?? null
  );
  const [knowledgeId, setKnowledgeId] = useState<string | null>(
    () => parseHash(window.location.hash)?.knowledgeId ?? null
  );
  const [memoryRefreshKey, setMemoryRefreshKey] = useState(0);
  const [knowledgeRefreshKey, setKnowledgeRefreshKey] = useState(0);
  const [serviceStatus, setServiceStatus] = useState<{
    loading: boolean;
    tone: "ok" | "warning" | "bad";
    message: string;
  }>({ loading: true, tone: "warning", message: "检查中" });
  const [navSignals, setNavSignals] = useState<NavSignals>({});
  // 角标刷新失败的持久提示（不发 toast，避免连续失败刷屏）。
  const [signalsError, setSignalsError] = useState<string | null>(null);
  const [knowledgeStatus, setKnowledgeStatus] = useState<KnowledgeStatus | null>(null);
  const [setupStatus, setSetupStatus] = useState<ProvidersStatus["setup"] | null>(null);
  // 非空且 parseHash 失败时记录，用于「页面不存在」提示；合法导航时清除。
  const [unknownHash, setUnknownHash] = useState<string | null>(() => {
    const current = window.location.hash;
    if (!current || current === "#/" || readLoginCodeFromHash()) return null;
    return parseHash(current) ? null : current;
  });
  // null = 无登录链接；pending = 正在交换 code；failed = 交换失败，回退手动填 key。
  const [loginLinkStatus, setLoginLinkStatus] = useState<"pending" | "failed" | null>(
    () => (readLoginCodeFromHash() ? "pending" : null)
  );

  const api = useMemo(() => new MemoryApi(settings), [settings]);
  const { toast, notify, clearToast } = useToast();
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const needsCredentialSetup = !settings.apiKey || credentialsBlocked;
  const activePage: PageKey = needsCredentialSetup ? "settings" : page;

  useEffect(() => {
    document.documentElement.dataset.credentialsGate = !settings.apiKey
      ? "missing"
      : credentialsBlocked
        ? "reauth"
        : "ok";
  }, [settings.apiKey, credentialsBlocked]);

  // 侧栏待办角标只读取轻量状态；完整记忆体检必须由用户显式触发。
  const refreshSignals = useCallback(async () => {
    if (!settings.apiKey || credentialsBlocked) {
      setNavSignals({});
      setKnowledgeStatus(null);
      return;
    }
    const next: NavSignals = {};
    try {
      const [report, knowledge, providers, tokens] = await Promise.all([
        api.memoryReport(),
        api.knowledgeStatus().catch(() => null),
        api.providersStatus().catch(() => null),
        api.authTokens().catch(() => null)
      ]);
      if (report.counts.deleted_memories > 0) {
        next.memories = { text: String(report.counts.deleted_memories), tone: "muted" };
      }
      const failedIndexes = knowledge?.counts?.index_failed ?? 0;
      if (failedIndexes > 0) {
        next.knowledge = { text: String(failedIndexes), tone: "warning" };
      }
      if (providers && !isProviderSetupReady(providers.setup)) {
        next.providers = { text: "配置", tone: "warning" };
        setSetupStatus(providers.setup);
      } else if (providers) {
        setSetupStatus(providers.setup);
      }
      if (tokens?.legacy_key_enabled) {
        next.developer = {
          text: tokens.authenticated_with_legacy_key ? "旧密钥" : "迁移",
          tone: "warning"
        };
      }
      setKnowledgeStatus(knowledge);
      setCredentialsBlocked(false);
      setSignalsError(null);
    } catch (error) {
      // 角标只是辅助信号；若鉴权失败则强制回到连接设置。
      if (error instanceof ApiError && error.status === 401) {
        setCredentialsBlocked(true);
        setNavSignals({});
        setSignalsError(null);
        return;
      }
      // 非 401 失败改为持久提示位，由用户决定是否重试。
      setSignalsError(errorMessage(error));
    }
    setNavSignals(next);
  }, [api, settings.apiKey, credentialsBlocked]);

  const lastSignalsRefreshRef = useRef(0);
  const seenRefreshKeyRef = useRef(memoryRefreshKey);
  const seenKnowledgeRefreshKeyRef = useRef(knowledgeRefreshKey);

  useEffect(() => {
    // 角标刷新节流：切页 60s 内不重复拉取；首次加载和记忆/知识变更
    // （refreshKey 递增）仍立即刷新。
    const now = Date.now();
    const forced =
      memoryRefreshKey !== seenRefreshKeyRef.current ||
      knowledgeRefreshKey !== seenKnowledgeRefreshKeyRef.current;
    seenRefreshKeyRef.current = memoryRefreshKey;
    seenKnowledgeRefreshKeyRef.current = knowledgeRefreshKey;
    if (!forced && now - lastSignalsRefreshRef.current < 60_000) return;
    lastSignalsRefreshRef.current = now;
    void refreshSignals();
  }, [refreshSignals, memoryRefreshKey, knowledgeRefreshKey, activePage]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    saveTheme(theme);
  }, [theme]);

  useEffect(() => {
    document.documentElement.dataset.uiMode = uiMode;
    saveUiMode(uiMode);
  }, [uiMode]);

  useEffect(() => {
    scrollWorkspaceToTop();
    document.documentElement.dataset.page = activePage;
  }, [activePage, knowledgeId]);

  const syncHash = useCallback((nextPage: PageKey, nextMemoryId: string | null, nextKnowledgeId: string | null = null) => {
    const nextHash = hashForRoute(nextPage, nextMemoryId, nextKnowledgeId);
    if (window.location.hash !== nextHash) {
      window.location.hash = nextHash.slice(1);
    }
  }, []);

  const navigateToPage = useCallback((nextPage: PageKey) => {
    setPage(nextPage);
    setMemoryId(null);
    setKnowledgeId(null);
    setUnknownHash(null);
    syncHash(nextPage, null, null);
  }, [syncHash]);

  const openMemory = useCallback((id: string) => {
    setMemoryId(id);
    setPage((current) => {
      syncHash(current, id, null);
      return current;
    });
  }, [syncHash]);

  const closeMemory = useCallback(() => {
    setMemoryId(null);
    setPage((current) => {
      syncHash(current, null, null);
      return current;
    });
  }, [syncHash]);

  const openKnowledge = useCallback((id: string) => {
    setPage("knowledge");
    setMemoryId(null);
    setKnowledgeId(id);
    syncHash("knowledge", null, id);
  }, [syncHash]);

  const closeKnowledge = useCallback(() => {
    setKnowledgeId(null);
    setPage("knowledge");
    syncHash("knowledge", null, null);
  }, [syncHash]);

  useEffect(() => {
    if (!window.location.hash) {
      window.history.replaceState(null, "", hashForRoute(activePage, memoryId, knowledgeId));
    }
    const onHashChange = () => {
      const route = parseHash(window.location.hash);
      if (route) {
        setUnknownHash(null);
        setPage(route.page);
        setMemoryId(route.memoryId);
        setKnowledgeId(route.knowledgeId);
      } else {
        // 未知 hash 不再静默忽略：空 hash 交给上方的 replaceState 兜底。
        const current = window.location.hash;
        setUnknownHash(current && current !== "#/" ? current : null);
      }
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [activePage, memoryId, knowledgeId]);

  const pingService = useCallback(async () => {
    setServiceStatus((current) => ({ ...current, loading: true }));
    try {
      const result = await api.health();
      if (result.status !== "ok") {
        setServiceStatus({ loading: false, tone: "bad", message: result.status });
        return;
      }
      if (!settings.apiKey) {
        setCredentialsBlocked(true);
        setServiceStatus({ loading: false, tone: "warning", message: "服务在线 · 请填写访问密钥" });
        return;
      }
      const [, providers] = await Promise.all([api.memoryReport(), api.providersStatus()]);
      setCredentialsBlocked(false);
      setSetupStatus(providers.setup);
      if (!isProviderSetupReady(providers.setup)) {
        setServiceStatus({
          loading: false,
          tone: "warning",
          message:
            providers.setup.state === "configuration_error"
              ? "服务在线 · 模型配置需处理"
              : "服务在线 · 待配置模型"
        });
        return;
      }
      setServiceStatus({
        loading: false,
        tone: "ok",
        message: "聊天配置已就绪"
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401 && settings.apiKey) {
        setCredentialsBlocked(true);
        setServiceStatus({
          loading: false,
          tone: "warning",
          message: "访问密钥无效 · 请重新配置"
        });
        return;
      }
      setServiceStatus({
        loading: false,
        tone: "bad",
        message: errorMessage(error)
      });
    }
  }, [api, settings.apiKey]);

  useEffect(() => {
    void pingService();
  }, [pingService]);

  const retrySignals = useCallback(() => {
    // 绕过 60s 节流：用户显式点击重试应立即生效。
    lastSignalsRefreshRef.current = 0;
    void refreshSignals();
  }, [refreshSignals]);

  const applySettings = (next: ConnectionSettings, message = "设置已保存") => {
    const wasBlocked = !settings.apiKey || credentialsBlocked;
    const saved = saveSettings(next);
    setSettings(saved);
    notify(message, "success");
    if (wasBlocked && saved.apiKey) {
      // 验证通过后先放开主站；首次/重登优先模型配置，已就绪则回工作室。
      setCredentialsBlocked(false);
      navigateToPage("providers");
      const savedApi = new MemoryApi(saved);
      void savedApi.providersStatus()
        .then((providers) => {
          setSetupStatus(providers.setup);
          setCredentialsBlocked(false);
          if (isProviderSetupReady(providers.setup)) navigateToPage("dashboard");
        })
        .catch((error) => {
          if (error instanceof ApiError && error.status === 401) {
            setCredentialsBlocked(true);
          }
        });
    }
  };

  // 一次性登录链接交换：仅在挂载时消费一次 #login=<code>。无论成败都先
  // 把 code 从 URL 抹掉（replaceState 不触发 hashchange），避免泄露到历史
  // 记录或被路由层误判。成功后复用 applySettings 的首启路径进入主站；
  // 失败则落回手动粘贴 gateway.txt 的既有流程。
  const loginExchangeStartedRef = useRef(false);
  useEffect(() => {
    if (loginLinkStatus !== "pending" || loginExchangeStartedRef.current) return;
    loginExchangeStartedRef.current = true;
    const code = readLoginCodeFromHash();
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    if (!code) {
      setLoginLinkStatus(null);
      return;
    }
    void api.exchangeConsoleLoginCode(code)
      .then((token) => {
        applySettings({ ...loadSettings(), apiKey: token }, "已通过登录链接登录");
        setLoginLinkStatus(null);
      })
      .catch(() => {
        setLoginLinkStatus("failed");
        if (loadSettings().apiKey) {
          // 已有保存密钥：失效链接不踢出现有会话，仅提示；首启（无密钥）
          // 才强制落回手动粘贴 gateway.txt 的填 key 页。
          notify("登录链接已失效，请重新运行 memgw open", "error");
        } else {
          setCredentialsBlocked(true);
        }
      });
    // applySettings 每次渲染都会重建，挂载时的一次性交换不需要跟随它重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loginLinkStatus, api]);

  return (
    <>
      <AppShell
        activePage={activePage}
        settings={settings}
        serviceStatus={serviceStatus}
        theme={theme}
        uiMode={uiMode}
        needsCredentialSetup={needsCredentialSetup}
        signals={navSignals}
        signalsError={signalsError}
        onRetrySignals={retrySignals}
        onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
        onToggleUiMode={() => setUiMode((current) => (current === "simple" ? "expert" : "simple"))}
        onPageChange={navigateToPage}
        onRefreshService={() => void pingService()}
      >
        <ErrorBoundary
          variant="page"
          resetKeys={[activePage, knowledgeId]}
          onGoHome={() => navigateToPage("dashboard")}
        >
          <E2eCrashProbe />
          {!needsCredentialSetup && unknownHash && (
            <div className="page-stack">
              <div className="page-header">
                <div>
                  <h1>页面不存在</h1>
                  <p>链接可能有误或内容已移动。</p>
                </div>
              </div>
              <div className="panel">
                <code className="error-boundary-detail">{unknownHash}</code>
                <div className="error-boundary-actions">
                  <button
                    className="primary-button"
                    type="button"
                    autoFocus
                    onClick={() => navigateToPage("dashboard")}
                  >
                    返回工作室
                  </button>
                </div>
              </div>
            </div>
          )}
          {!unknownHash && activePage === "dashboard" && (
            <DashboardPage
              api={api}
              settings={settings}
              setPage={navigateToPage}
              openMemory={openMemory}
              notify={notify}
              confirm={confirm}
              refreshKey={memoryRefreshKey}
              expertMode={uiMode === "expert"}
            />
          )}
        {!unknownHash && activePage === "memories" && (
          <MemoriesPage api={api} notify={notify} openMemory={openMemory} refreshKey={memoryRefreshKey} setupStatus={setupStatus} />
        )}
        {!unknownHash && activePage === "knowledge" && (
          <KnowledgeLibraryPage
            api={api}
            documentId={knowledgeId}
            notify={notify}
            confirm={confirm}
            maxDocumentBytes={knowledgeStatus?.max_document_bytes}
            onOpenDocument={openKnowledge}
            onCloseDocument={closeKnowledge}
            onChanged={() => setKnowledgeRefreshKey((current) => current + 1)}
            setupStatus={setupStatus}
          />
        )}
        {!unknownHash && activePage === "knowledgeSearch" && (
          <KnowledgeSearchPage api={api} notify={notify} onOpenDocument={openKnowledge} status={knowledgeStatus} />
        )}
        {!unknownHash && activePage === "core" && (
          <CoreMemoryPage api={api} notify={notify} confirm={confirm} />
        )}
        {!unknownHash && activePage === "review" && (
          <ReviewPage api={api} notify={notify} confirm={confirm} openMemory={openMemory} setupStatus={setupStatus} />
        )}
        {!unknownHash && activePage === "recall" && <RecallExplainPage api={api} notify={notify} openMemory={openMemory} />}
        {!unknownHash && activePage === "evaluation" && <EvaluationPage api={api} notify={notify} />}
        {!unknownHash && activePage === "recent" && (
          <RecentContextPage api={api} notify={notify} confirm={confirm} />
        )}
        {!unknownHash && activePage === "reports" && (
          <ReportsPage api={api} settings={settings} notify={notify} confirm={confirm} />
        )}
        {!unknownHash && activePage === "logs" && <DecisionLogsPage api={api} setupStatus={setupStatus} />}
        {!unknownHash && activePage === "usage" && <UsagePage api={api} setupStatus={setupStatus} />}
        {!unknownHash && activePage === "providers" && (
          <ProvidersPage
            api={api}
            initialSetup={!isProviderSetupReady(setupStatus)}
            expertMode={uiMode === "expert"}
            onSetupChanged={() => pingService()}
          />
        )}
        {!unknownHash && activePage === "settings" && (
          <SettingsPage
            settings={settings}
            onSave={applySettings}
            notify={notify}
            loginLinkStatus={loginLinkStatus}
          />
        )}
        {!unknownHash && activePage === "developer" && (
          <DeveloperPage
            api={api}
            settings={settings}
            notify={notify}
            confirm={confirm}
          />
        )}
        </ErrorBoundary>
      </AppShell>
      {settings.apiKey && memoryId && (
        <ErrorBoundary variant="overlay" onDismiss={closeMemory}>
          <MemoryDetailDrawer
            api={api}
            memoryId={memoryId}
            notify={notify}
            confirm={confirm}
            onClose={closeMemory}
            onOpenMemory={openMemory}
            onChanged={() => setMemoryRefreshKey((current) => current + 1)}
          />
        </ErrorBoundary>
      )}
      {toast && <ToastView toast={toast} onDismiss={clearToast} />}
      <ErrorBoundary variant="overlay" onDismiss={() => resolveConfirm(false)}>
        <ConfirmDialog state={confirmState} onResolve={resolveConfirm} />
      </ErrorBoundary>
    </>
  );
}
