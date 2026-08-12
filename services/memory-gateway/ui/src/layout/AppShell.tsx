import {
  Activity,
  ClipboardCheck,
  Database,
  FileText,
  Gauge,
  History,
  LibraryBig,
  Layers3,
  ListChecks,
  Menu,
  Moon,
  ReceiptText,
  Route as RouteIcon,
  Search,
  ScanSearch,
  Settings as SettingsIcon,
  Sun,
  Wrench,
  X
} from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { NAV_SECTIONS, PAGE_META, SIMPLE_NAV_SECTIONS, sectionForPage } from "../navigation";
import { useDialogA11y } from "../hooks/useDialogA11y";
import type { ThemeMode, UiMode } from "../storage";
import type { ConnectionSettings, PageKey } from "../types";
import { scrollWorkspaceToTop } from "../utils/scroll";

const PAGE_ICONS: Record<PageKey, typeof Gauge> = {
  dashboard: Gauge,
  memories: Database,
  knowledge: LibraryBig,
  knowledgeSearch: ScanSearch,
  core: Layers3,
  recent: History,
  review: ListChecks,
  recall: Search,
  evaluation: ClipboardCheck,
  reports: FileText,
  logs: Activity,
  usage: ReceiptText,
  providers: RouteIcon,
  settings: SettingsIcon,
  developer: Wrench
};

// 接入信息放在主导航：首通后复制 chat token 是最高频步骤之一。
const MOBILE_PRIMARY_PAGES: PageKey[] = ["dashboard", "memories", "knowledge", "developer"];

export type NavBadge = {
  text: string;
  tone: "warning" | "info" | "muted";
};

export type NavSignals = Partial<Record<PageKey, NavBadge>>;

export function AppShell({
  activePage,
  settings,
  serviceStatus,
  theme,
  uiMode,
  signals = {},
  onToggleTheme,
  onToggleUiMode,
  onPageChange,
  onRefreshService,
  children
}: {
  activePage: PageKey;
  settings: ConnectionSettings;
  serviceStatus: {
    loading: boolean;
    tone: "ok" | "warning" | "bad";
    message: string;
  };
  theme: ThemeMode;
  uiMode: UiMode;
  signals?: NavSignals;
  onToggleTheme: () => void;
  onToggleUiMode: () => void;
  onPageChange: (page: PageKey) => void;
  onRefreshService: () => void;
  children: ReactNode;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const configured = Boolean(settings.apiKey);
  const userId = settings.userId || "default";
  const activeSection = sectionForPage(activePage);
  const navSections = uiMode === "expert" ? NAV_SECTIONS : SIMPLE_NAV_SECTIONS;

  const go = (nextPage: PageKey) => {
    setMoreOpen(false);
    if (nextPage === activePage) {
      scrollWorkspaceToTop();
      return;
    }
    onPageChange(nextPage);
  };

  return (
    <div className={`app-shell ${!configured ? "setup-shell" : ""}`}>
      <div className="ambient-field" aria-hidden="true">
        <i className="af-a" />
        <i className="af-b" />
        <i className="af-c" />
        <i className="af-glow" />
        <i className="af-grain" />
      </div>
      {configured && (
        <aside className="sidebar">
          <div className="brand" aria-label="Memory Console">
            <div className="brand-mark">M</div>
            <div className="brand-copy">
              <strong>Memory Console</strong>
              <span>记忆与知识</span>
            </div>
          </div>

          <nav className="nav-list" aria-label="Memory Console">
            {navSections.map((section) => (
              <div className="nav-group" key={section.key}>
                <span className="nav-group-label">{section.label}</span>
                {section.items.map((key) => {
                  const Icon = PAGE_ICONS[key];
                  const badge = signals[key];
                  const isActive = activePage === key;
                  return (
                    <button
                      key={key}
                      className={`nav-item ${isActive ? "active" : ""}`}
                      type="button"
                      onClick={() => go(key)}
                      aria-current={isActive ? "page" : undefined}
                    >
                      <Icon size={16} />
                      <span>{PAGE_META[key].label}</span>
                      {badge && (
                        <em className={`nav-badge nav-badge-${badge.tone}`}>{badge.text}</em>
                      )}
                    </button>
                  );
                })}
              </div>
            ))}
          </nav>
          <button
            className="nav-item nav-mode-toggle"
            type="button"
            aria-pressed={uiMode === "expert"}
            onClick={onToggleUiMode}
            title={uiMode === "expert" ? "隐藏高级工具，返回常用页面" : "显示评测、日志等高级工具"}
          >
            <Wrench size={16} />
            <span>{uiMode === "expert" ? "返回简洁模式" : "切换到专家模式"}</span>
          </button>
        </aside>
      )}

      <main className="workspace">
        <header className="topbar">
          <div className="topbar-page">
            {!configured && <span className="topbar-brand-mark">M</span>}
            <div className="topbar-title">
              {configured && <span>{activeSection.label}</span>}
              <strong>{configured ? PAGE_META[activePage].label : "Memory Console 初始设置"}</strong>
            </div>
          </div>
          <div className="topbar-right">
            <button
              className={`status-pill status-${serviceStatus.tone}`}
              type="button"
              onClick={onRefreshService}
              title={
                !serviceStatus.loading && serviceStatus.tone === "bad"
                  ? `${serviceStatus.message} · 点击重新检查`
                  : "重新检查服务和访问密钥"
              }
              aria-live="polite"
            >
              <span className={`status-dot ${serviceStatus.tone}`} />
              <span className="status-text">
                {serviceStatus.loading
                  ? "检查中"
                  : serviceStatus.tone === "bad"
                    ? "服务异常"
                    : serviceStatus.message}
              </span>
            </button>
            <button
              className="icon-button"
              type="button"
              onClick={onToggleTheme}
              title={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
              aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
            >
              {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </button>
            {configured && (
              <button
                className="avatar-chip"
                type="button"
                onClick={() => go("settings")}
                title={`用户 ${userId} · 打开设置`}
                aria-label={`用户 ${userId} · 打开设置`}
              >
                {userId.slice(0, 1).toUpperCase()}
              </button>
            )}
          </div>
        </header>

        <section className="content-area">{children}</section>
      </main>

      {configured && (
        <nav className="mobile-bottom-nav" aria-label="移动端导航">
          {MOBILE_PRIMARY_PAGES.map((page) => {
            const Icon = PAGE_ICONS[page];
            const isActive = activePage === page;
            return (
              <button
                key={page}
                type="button"
                className={isActive ? "active" : ""}
                onClick={() => go(page)}
                aria-current={isActive ? "page" : undefined}
              >
                <Icon size={19} />
                <span>{PAGE_META[page].shortLabel}</span>
              </button>
            );
          })}
          <button
            type="button"
            className={!MOBILE_PRIMARY_PAGES.includes(activePage) ? "active" : ""}
            onClick={() => setMoreOpen(true)}
          >
            <Menu size={19} />
            <span>更多</span>
          </button>
        </nav>
      )}

      {configured && moreOpen && (
        <MobileMoreSheet
          activePage={activePage}
          uiMode={uiMode}
          onClose={() => setMoreOpen(false)}
          onPageChange={go}
          onToggleUiMode={onToggleUiMode}
        />
      )}
    </div>
  );
}

function MobileMoreSheet({
  activePage,
  uiMode,
  onClose,
  onPageChange,
  onToggleUiMode
}: {
  activePage: PageKey;
  uiMode: UiMode;
  onClose: () => void;
  onPageChange: (page: PageKey) => void;
  onToggleUiMode: () => void;
}) {
  const titleId = useId();
  const sheetRef = useDialogA11y<HTMLElement>(onClose);
  const navSections = uiMode === "expert" ? NAV_SECTIONS : SIMPLE_NAV_SECTIONS;
  return (
    <div className="mobile-more-layer" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section ref={sheetRef} className="mobile-more-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <header>
          <div>
            <span>{uiMode === "expert" ? "全部页面" : "常用页面"}</span>
            <strong id={titleId}>{uiMode === "expert" ? "记忆与知识工具" : "Memory Console"}</strong>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </header>
        {navSections.map((section) => (
          <div className="mobile-more-section" key={section.key}>
            <span className="mobile-more-label">{section.label}</span>
            <div className="mobile-more-grid">
              {section.items.map((key) => {
                const Icon = PAGE_ICONS[key];
                return (
                  <button key={key} type="button" className={activePage === key ? "active" : ""} onClick={() => onPageChange(key)}>
                    <Icon size={19} />
                    <span>{PAGE_META[key].label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
        <button
          className="secondary-button mobile-mode-toggle"
          type="button"
          aria-pressed={uiMode === "expert"}
          onClick={onToggleUiMode}
        >
          <Wrench size={16} />
          {uiMode === "expert" ? "返回简洁模式" : "显示专家工具"}
        </button>
      </section>
    </div>
  );
}
