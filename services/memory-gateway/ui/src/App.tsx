import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MemoryApi } from "./api";
import { ConfirmDialog } from "./components/ConfirmDialog";
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
import { scrollWorkspaceToTop } from "./utils/scroll";

export function App() {
  const [settings, setSettings] = useState<ConnectionSettings>(() => loadSettings());
  const [theme, setTheme] = useState<ThemeMode>(() => loadTheme());
  const [uiMode, setUiMode] = useState<UiMode>(() => loadUiMode());
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
  const [knowledgeStatus, setKnowledgeStatus] = useState<KnowledgeStatus | null>(null);
  const [setupStatus, setSetupStatus] = useState<ProvidersStatus["setup"] | null>(null);

  const api = useMemo(() => new MemoryApi(settings), [settings]);
  const { toast, notify, clearToast } = useToast();
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const activePage: PageKey = !settings.apiKey ? "settings" : page;

  // 侧栏待办角标：体检建议、回收站、评测待标注。失败时静默，不影响主流程。
  const refreshSignals = useCallback(async () => {
    if (!settings.apiKey) {
      setNavSignals({});
      setKnowledgeStatus(null);
      return;
    }
    const next: NavSignals = {};
    try {
      const [report, review, workbench, knowledge, providers] = await Promise.all([
        api.memoryReport(),
        api.reviewMemories(),
        api.recallEvaluationWorkbench().catch(() => null),
        api.knowledgeStatus().catch(() => null),
        api.providersStatus().catch(() => null)
      ]);
      if (review.recommendations.length > 0) {
        next.review = { text: String(review.recommendations.length), tone: "warning" };
      }
      if (report.counts.deleted_memories > 0) {
        next.memories = { text: String(report.counts.deleted_memories), tone: "muted" };
      }
      if (workbench?.labels?.length) {
        const unlabeled = workbench.labels.filter((label) => {
          const judgment = label.judgment || (label.relevant_ids.length > 0 ? "relevant" : "unlabeled");
          return !(judgment === "no_answer" || (judgment === "relevant" && label.relevant_ids.length > 0));
        }).length;
        if (unlabeled > 0) {
          next.evaluation = { text: String(unlabeled), tone: "info" };
        }
      }
      const failedIndexes =
        knowledge?.failed_indexes ??
        knowledge?.indexing_failed ??
        knowledge?.failed_versions ??
        knowledge?.index_failures ??
        knowledge?.counts?.index_failed ??
        knowledge?.counts?.failed_indexes ??
        knowledge?.counts?.failed ??
        0;
      if (failedIndexes > 0) {
        next.knowledge = { text: String(failedIndexes), tone: "warning" };
      }
      if (providers && !providers.setup.chat_ready) {
        next.providers = { text: "配置", tone: "warning" };
        setSetupStatus(providers.setup);
      } else if (providers) {
        setSetupStatus(providers.setup);
      }
      setKnowledgeStatus(knowledge);
    } catch {
      // 角标只是辅助信号，拉取失败时保持无角标状态
    }
    setNavSignals(next);
  }, [api, settings.apiKey]);

  const lastSignalsRefreshRef = useRef(0);
  const seenRefreshKeyRef = useRef(memoryRefreshKey);
  const seenKnowledgeRefreshKeyRef = useRef(knowledgeRefreshKey);

  useEffect(() => {
    // 角标刷新节流：切页 60s 内不重复拉取（reviewMemories 是服务端全库扫描）；
    // 首次加载和记忆变更（memoryRefreshKey 递增）仍立即刷新。
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
        setPage(route.page);
        setMemoryId(route.memoryId);
        setKnowledgeId(route.knowledgeId);
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
        setServiceStatus({ loading: false, tone: "warning", message: "服务在线 · 待配置" });
        return;
      }
      const [, providers] = await Promise.all([api.memoryReport(), api.providersStatus()]);
      setSetupStatus(providers.setup);
      if (!providers.setup.chat_ready) {
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

  const applySettings = (next: ConnectionSettings, message = "设置已保存") => {
    const isFirstConnection = !settings.apiKey;
    const saved = saveSettings(next);
    setSettings(saved);
    notify(message, "success");
    if (isFirstConnection && saved.apiKey) {
      // 首次连接先进入模型配置。若这其实是已配置环境中的新浏览器，
      // 状态检查成功后再自动回到工作室。
      navigateToPage("providers");
      const savedApi = new MemoryApi(saved);
      void savedApi.providersStatus()
        .then((providers) => {
          setSetupStatus(providers.setup);
          if (providers.setup.chat_ready) navigateToPage("dashboard");
        })
        .catch(() => undefined);
    }
  };

  return (
    <>
      <AppShell
        activePage={activePage}
        settings={settings}
        serviceStatus={serviceStatus}
        theme={theme}
        uiMode={uiMode}
        signals={navSignals}
        onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
        onToggleUiMode={() => setUiMode((current) => (current === "simple" ? "expert" : "simple"))}
        onPageChange={navigateToPage}
        onRefreshService={() => void pingService()}
      >
        {activePage === "dashboard" && (
          <DashboardPage
            api={api}
            settings={settings}
            setPage={navigateToPage}
            openMemory={openMemory}
            notify={notify}
            confirm={confirm}
            refreshKey={memoryRefreshKey}
          />
        )}
        {activePage === "memories" && (
          <MemoriesPage api={api} notify={notify} openMemory={openMemory} refreshKey={memoryRefreshKey} />
        )}
        {activePage === "knowledge" && (
          <KnowledgeLibraryPage
            api={api}
            documentId={knowledgeId}
            notify={notify}
            confirm={confirm}
            maxDocumentBytes={knowledgeStatus?.max_document_bytes}
            onOpenDocument={openKnowledge}
            onCloseDocument={closeKnowledge}
            onChanged={() => setKnowledgeRefreshKey((current) => current + 1)}
          />
        )}
        {activePage === "knowledgeSearch" && (
          <KnowledgeSearchPage api={api} notify={notify} onOpenDocument={openKnowledge} status={knowledgeStatus} />
        )}
        {activePage === "core" && (
          <CoreMemoryPage api={api} notify={notify} confirm={confirm} />
        )}
        {activePage === "review" && (
          <ReviewPage api={api} notify={notify} confirm={confirm} openMemory={openMemory} />
        )}
        {activePage === "recall" && <RecallExplainPage api={api} notify={notify} openMemory={openMemory} />}
        {activePage === "evaluation" && <EvaluationPage api={api} notify={notify} />}
        {activePage === "recent" && (
          <RecentContextPage api={api} notify={notify} confirm={confirm} />
        )}
        {activePage === "reports" && (
          <ReportsPage api={api} settings={settings} notify={notify} confirm={confirm} />
        )}
        {activePage === "logs" && <DecisionLogsPage api={api} />}
        {activePage === "usage" && <UsagePage api={api} />}
        {activePage === "providers" && (
          <ProvidersPage
            api={api}
            initialSetup={!setupStatus?.chat_ready}
            expertMode={uiMode === "expert"}
          />
        )}
        {activePage === "settings" && (
          <SettingsPage settings={settings} onSave={applySettings} notify={notify} />
        )}
        {activePage === "developer" && (
          <DeveloperPage
            api={api}
            settings={settings}
            notify={notify}
            confirm={confirm}
          />
        )}
      </AppShell>
      {settings.apiKey && memoryId && (
        <MemoryDetailDrawer
          api={api}
          memoryId={memoryId}
          notify={notify}
          confirm={confirm}
          onClose={closeMemory}
          onOpenMemory={openMemory}
          onChanged={() => setMemoryRefreshKey((current) => current + 1)}
        />
      )}
      {toast && <ToastView toast={toast} onDismiss={clearToast} />}
      <ConfirmDialog state={confirmState} onResolve={resolveConfirm} />
    </>
  );
}
