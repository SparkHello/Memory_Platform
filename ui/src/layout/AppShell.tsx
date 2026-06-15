import {
  Activity,
  BarChart3,
  Database,
  FileText,
  Gauge,
  History,
  KeyRound,
  Layers3,
  ListChecks,
  RefreshCcw,
  Server,
  Settings as SettingsIcon,
  SlidersHorizontal,
  Upload,
  Wrench
} from "lucide-react";
import type { ReactNode } from "react";
import type { ConnectionSettings, PageKey } from "../types";

type NavItem = {
  key: PageKey;
  label: string;
  icon: typeof Gauge;
};

const NAV_GROUPS: Array<{ title: string; items: NavItem[] }> = [
  {
    title: "总览",
    items: [{ key: "dashboard", label: "总览", icon: Gauge }]
  },
  {
    title: "网关",
    items: [
      { key: "gateway-overview", label: "网关概览", icon: SlidersHorizontal },
      { key: "providers", label: "服务商与模型", icon: Server },
      { key: "routes", label: "路由", icon: ListChecks },
      { key: "gateway-import-export", label: "导入 / 导出", icon: Upload }
    ]
  },
  {
    title: "成本",
    items: [
      { key: "usage", label: "用量统计", icon: BarChart3 }
    ]
  },
  {
    title: "记忆",
    items: [
      { key: "memories", label: "记忆库", icon: Database },
      { key: "core", label: "核心记忆", icon: Layers3 },
      { key: "review", label: "记忆体检", icon: ListChecks },
      { key: "recent", label: "近期上下文", icon: History },
      { key: "reports", label: "报告与备份", icon: FileText },
      { key: "logs", label: "决策日志", icon: Activity }
    ]
  },
  {
    title: "系统",
    items: [
      { key: "settings", label: "设置", icon: SettingsIcon },
      { key: "developer", label: "接入信息", icon: Wrench }
    ]
  }
];

export function AppShell({
  activePage,
  settings,
  serviceStatus,
  onPageChange,
  onRefreshService,
  onUserIdChange,
  children
}: {
  activePage: PageKey;
  settings: ConnectionSettings;
  serviceStatus: {
    loading: boolean;
    ok: boolean;
    message: string;
  };
  onPageChange: (page: PageKey) => void;
  onRefreshService: () => void;
  onUserIdChange: (userId: string) => void;
  children: ReactNode;
}) {
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
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.title}>
              <div className="nav-group-label">{group.title}</div>
              {group.items.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.key}
                    className={`nav-item ${activePage === item.key ? "active" : ""}`}
                    onClick={() => onPageChange(item.key)}
                    type="button"
                  >
                    <Icon size={17} />
                    <span>{item.label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="status-cluster">
            <span className={`status-dot ${serviceStatus.ok ? "ok" : "bad"}`} />
            <span>{serviceStatus.loading ? "检查中" : serviceStatus.message}</span>
            <button
              className="icon-button"
              type="button"
              onClick={onRefreshService}
              title="刷新服务状态"
            >
              <RefreshCcw size={16} />
            </button>
          </div>
          <div className="topbar-right">
            <label className="compact-field">
              <span>用户 ID</span>
              <input
                value={settings.userId}
                onChange={(event) => onUserIdChange(event.target.value)}
                placeholder="default"
              />
            </label>
            <button
              className="secondary-button"
              type="button"
              onClick={() => onPageChange("settings")}
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
          {children}
        </section>
      </main>
    </div>
  );
}
