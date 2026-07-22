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
import { CoreMemoryPage } from "./pages/memory/CoreMemoryPage";
import { DecisionLogsPage } from "./pages/memory/DecisionLogsPage";
import { EvaluationPage } from "./pages/memory/EvaluationPage";
import { MemoriesPage } from "./pages/memory/MemoriesPage";
import { RecallExplainPage } from "./pages/memory/RecallExplainPage";
import { RecentContextPage } from "./pages/memory/RecentContextPage";
import { ReportsPage } from "./pages/memory/ReportsPage";
import { ReviewPage } from "./pages/memory/ReviewPage";
import { DeveloperPage } from "./pages/system/DeveloperPage";
import { SettingsPage } from "./pages/system/SettingsPage";
import { loadSettings, loadTheme, saveSettings, saveTheme, type ThemeMode } from "./storage";
import type { ConnectionSettings, PageKey } from "./types";
import { errorMessage } from "./utils/format";

export function App() {
  const [settings, setSettings] = useState<ConnectionSettings>(() => loadSettings());
  const [theme, setTheme] = useState<ThemeMode>(() => loadTheme());
  const [page, setPage] = useState<PageKey>(() => {
    const saved = loadSettings();
    return parseHash(window.location.hash)?.page ?? (saved.apiKey ? "dashboard" : "settings");
  });
  const [memoryId, setMemoryId] = useState<string | null>(
    () => parseHash(window.location.hash)?.memoryId ?? null
  );
  const [memoryRefreshKey, setMemoryRefreshKey] = useState(0);
  const [serviceStatus, setServiceStatus] = useState<{
    loading: boolean;
    tone: "ok" | "warning" | "bad";
    message: string;
  }>({ loading: true, tone: "warning", message: "检查中" });
  const [navSignals, setNavSignals] = useState<NavSignals>({});

  const api = useMemo(() => new MemoryApi(settings), [settings]);
  const { toast, notify, clearToast } = useToast();
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const activePage: PageKey = !settings.apiKey ? "settings" : page;

  // 侧栏待办角标：体检建议、回收站、评测待标注。失败时静默，不影响主流程。
  const refreshSignals = useCallback(async () => {
    if (!settings.apiKey) {
      setNavSignals({});
      return;
    }
    const next: NavSignals = {};
    try {
      const [report, review, workbench] = await Promise.all([
        api.memoryReport(),
        api.reviewMemories(),
        api.recallEvaluationWorkbench().catch(() => null)
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
    } catch {
      // 角标只是辅助信号，拉取失败时保持无角标状态
    }
    setNavSignals(next);
  }, [api, settings.apiKey]);

  const lastSignalsRefreshRef = useRef(0);
  const seenRefreshKeyRef = useRef(memoryRefreshKey);

  useEffect(() => {
    // 角标刷新节流：切页 60s 内不重复拉取（reviewMemories 是服务端全库扫描）；
    // 首次加载和记忆变更（memoryRefreshKey 递增）仍立即刷新。
    const now = Date.now();
    const forced = memoryRefreshKey !== seenRefreshKeyRef.current;
    seenRefreshKeyRef.current = memoryRefreshKey;
    if (!forced && now - lastSignalsRefreshRef.current < 60_000) return;
    lastSignalsRefreshRef.current = now;
    void refreshSignals();
  }, [refreshSignals, memoryRefreshKey, activePage]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    saveTheme(theme);
  }, [theme]);

  useEffect(() => {
    document.querySelector(".content-area")?.scrollTo(0, 0);
    document.documentElement.dataset.page = activePage;
  }, [activePage]);

  const syncHash = useCallback((nextPage: PageKey, nextMemoryId: string | null) => {
    const nextHash = hashForRoute(nextPage, nextMemoryId);
    if (window.location.hash !== nextHash) {
      window.location.hash = nextHash.slice(1);
    }
  }, []);

  const navigateToPage = useCallback((nextPage: PageKey) => {
    setPage(nextPage);
    setMemoryId(null);
    syncHash(nextPage, null);
  }, [syncHash]);

  const openMemory = useCallback((id: string) => {
    setMemoryId(id);
    setPage((current) => {
      syncHash(current, id);
      return current;
    });
  }, [syncHash]);

  const closeMemory = useCallback(() => {
    setMemoryId(null);
    setPage((current) => {
      syncHash(current, null);
      return current;
    });
  }, [syncHash]);

  useEffect(() => {
    if (!window.location.hash) {
      window.history.replaceState(null, "", hashForRoute(activePage, memoryId));
    }
    const onHashChange = () => {
      const route = parseHash(window.location.hash);
      if (route) {
        setPage(route.page);
        setMemoryId(route.memoryId);
      }
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [activePage, memoryId]);

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
      await api.memoryReport();
      setServiceStatus({
        loading: false,
        tone: "ok",
        message: "服务与鉴权正常"
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
      navigateToPage("dashboard");
    }
  };

  return (
    <>
      <AppShell
        activePage={activePage}
        settings={settings}
        serviceStatus={serviceStatus}
        theme={theme}
        signals={navSignals}
        onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
        onPageChange={navigateToPage}
        onRefreshService={() => void pingService()}
      >
        {activePage === "dashboard" && (
          <DashboardPage api={api} settings={settings} setPage={navigateToPage} openMemory={openMemory} notify={notify} />
        )}
        {activePage === "memories" && (
          <MemoriesPage api={api} notify={notify} openMemory={openMemory} refreshKey={memoryRefreshKey} />
        )}
        {activePage === "core" && (
          <CoreMemoryPage api={api} notify={notify} confirm={confirm} />
        )}
        {activePage === "review" && (
          <ReviewPage api={api} notify={notify} confirm={confirm} openMemory={openMemory} />
        )}
        {activePage === "recall" && <RecallExplainPage api={api} notify={notify} openMemory={openMemory} />}
        {activePage === "evaluation" && <EvaluationPage api={api} notify={notify} />}
        {activePage === "recent" && <RecentContextPage api={api} />}
        {activePage === "reports" && (
          <ReportsPage api={api} settings={settings} notify={notify} confirm={confirm} />
        )}
        {activePage === "logs" && <DecisionLogsPage api={api} />}
        {activePage === "settings" && (
          <SettingsPage settings={settings} onSave={applySettings} notify={notify} />
        )}
        {activePage === "developer" && <DeveloperPage settings={settings} notify={notify} />}
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
