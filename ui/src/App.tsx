import { useCallback, useEffect, useMemo, useState } from "react";
import { MemoryApi } from "./api";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { ToastView } from "./components/Toast";
import { useConfirm } from "./hooks/useConfirm";
import { useToast } from "./hooks/useToast";
import { AppShell } from "./layout/AppShell";
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
import { loadSettings, saveSettings } from "./storage";
import type { ConnectionSettings, PageKey } from "./types";
import { errorMessage } from "./utils/format";

export function App() {
  const [settings, setSettings] = useState<ConnectionSettings>(() => loadSettings());
  const [page, setPage] = useState<PageKey>(() =>
    loadSettings().apiKey ? "dashboard" : "settings"
  );
  const [serviceStatus, setServiceStatus] = useState<{
    loading: boolean;
    ok: boolean;
    message: string;
  }>({ loading: true, ok: false, message: "检查中" });

  const api = useMemo(() => new MemoryApi(settings), [settings]);
  const { toast, notify } = useToast();
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const activePage: PageKey = !settings.apiKey ? "settings" : page;

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
    <>
      <AppShell
        activePage={activePage}
        settings={settings}
        serviceStatus={serviceStatus}
        onPageChange={setPage}
        onRefreshService={() => void pingService()}
        onUserIdChange={updateTopbarUser}
      >
        {activePage === "dashboard" && (
          <DashboardPage api={api} settings={settings} setPage={setPage} notify={notify} />
        )}
        {activePage === "memories" && (
          <MemoriesPage api={api} notify={notify} confirm={confirm} />
        )}
        {activePage === "core" && (
          <CoreMemoryPage api={api} notify={notify} confirm={confirm} />
        )}
        {activePage === "review" && (
          <ReviewPage api={api} notify={notify} confirm={confirm} />
        )}
        {activePage === "recall" && <RecallExplainPage api={api} notify={notify} />}
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
      {toast && <ToastView toast={toast} />}
      <ConfirmDialog state={confirmState} onResolve={resolveConfirm} />
    </>
  );
}
