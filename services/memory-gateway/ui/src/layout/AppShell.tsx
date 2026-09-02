import {
  Activity,
  ChevronDown,
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
  TriangleAlert,
  Wrench,
  X,
  Settings2
} from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { NAV_SECTIONS, PAGE_META, SIMPLE_NAV_SECTIONS, sectionForPage } from "../navigation";
import { useDialogA11y } from "../hooks/useDialogA11y";
import {
  loadCollapsedNavSections,
  loadDismissedStatus,
  saveCollapsedNavSections,
  saveDismissedStatus,
  type ThemeMode,
  type UiMode
} from "../storage";
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

// 客户端接入放在主导航：首通后复制 chat token 是最高频步骤之一。
// 底栏放首配与日常最高频入口；providers（模型）必须在，否则首配阶段移动端没有配模型入口。
const MOBILE_PRIMARY_PAGES: PageKey[] = ["dashboard", "memories", "knowledge", "providers", "developer"];

// warning 横幅（待配置模型 / 配置需处理）只在这四个可操作页面出现；
// bad（服务异常）不受页限且不可关闭，ok / loading 照旧全局显示。
const WARNING_STATUS_PAGES: PageKey[] = ["dashboard", "providers", "settings", "developer"];

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
  needsCredentialSetup = false,
  signals = {},
  signalsError = null,
  onRetrySignals,
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
  /** 无密钥或密钥失效：隐藏主导航，只做连接设置。 */
  needsCredentialSetup?: boolean;
  signals?: NavSignals;
  /** 角标拉取失败的持久提示文案；为 null 表示正常。 */
  signalsError?: string | null;
  onRetrySignals?: () => void;
  onToggleTheme: () => void;
  onToggleUiMode: () => void;
  onPageChange: (page: PageKey) => void;
  onRefreshService: () => void;
  children: ReactNode;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  // warning 横幅可关闭：记录被关闭的消息文本，消息内容变化时重新出现。
  const [dismissedStatus, setDismissedStatus] = useState(() => loadDismissedStatus());
  // 专家模式导航有 15 个入口，分组可折叠；折叠偏好跨会话保留。
  const [collapsedSections, setCollapsedSections] = useState<string[]>(() =>
    loadCollapsedNavSections()
  );
  // 配好密钥且鉴权有效后才展示日常导航；连接设置本身在简洁模式里不进侧栏。
  const showMainNav = Boolean(settings.apiKey) && !needsCredentialSetup;
  const userId = settings.userId || "default";
  const activeSection = sectionForPage(activePage);
  const navSections = uiMode === "expert" ? NAV_SECTIONS : SIMPLE_NAV_SECTIONS;

  const toggleSection = (key: string) => {
    setCollapsedSections((current) => {
      const next = current.includes(key)
        ? current.filter((item) => item !== key)
        : [...current, key];
      saveCollapsedNavSections(next);
      return next;
    });
  };

  const isWarningStatus = !serviceStatus.loading && serviceStatus.tone === "warning";
  const showStatusPill =
    !isWarningStatus ||
    (WARNING_STATUS_PAGES.includes(activePage) && serviceStatus.message !== dismissedStatus);

  const dismissWarningStatus = () => {
    saveDismissedStatus(serviceStatus.message);
    setDismissedStatus(serviceStatus.message);
  };

  const go = (nextPage: PageKey) => {
    setMoreOpen(false);
    if (nextPage === activePage) {
      scrollWorkspaceToTop();
      return;
    }
    onPageChange(nextPage);
  };

  return (
    <div className={`app-shell ${!showMainNav ? "setup-shell" : ""}`}>
      <div className="ambient-field" aria-hidden="true">
        <i className="af-a" />
        <i className="af-b" />
        <i className="af-c" />
        <i className="af-glow" />
        <i className="af-grain" />
      </div>
      {showMainNav && (
        <aside className="sidebar">
          <div className="brand" aria-label="Memory Console">
            <div className="brand-mark">M</div>
            <div className="brand-copy">
              <strong>Memory Console</strong>
              <span>记忆与知识</span>
            </div>
          </div>

          <nav className="nav-list" aria-label="Memory Console">
            {navSections.map((section) => {
              // 含当前页面的分组永远展开，避免折叠后丢失位置感。
              const collapsible = uiMode === "expert";
              const collapsed =
                collapsible &&
                collapsedSections.includes(section.key) &&
                !section.items.includes(activePage);
              const hasSignal = section.items.some((key) => signals[key]);
              return (
                <div className="nav-group" key={section.key}>
                  {collapsible ? (
                    <button
                      className="nav-group-label nav-group-toggle"
                      type="button"
                      aria-expanded={!collapsed}
                      onClick={() => toggleSection(section.key)}
                    >
                      <span>{section.label}</span>
                      {collapsed && hasSignal && (
                        <i className="nav-group-signal" aria-hidden="true" />
                      )}
                      <ChevronDown size={12} className={collapsed ? "" : "open"} />
                    </button>
                  ) : (
                    <span className="nav-group-label">{section.label}</span>
                  )}
                  {!collapsed &&
                    section.items.map((key) => {
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
              );
            })}
          </nav>
          {signalsError && (
            <div className="nav-signals-error" role="status">
              <TriangleAlert size={14} aria-hidden="true" />
              <span>待办角标暂时无法更新</span>
              <button
                className="ghost-button compact"
                type="button"
                onClick={() => onRetrySignals?.()}
              >
                重试
              </button>
            </div>
          )}
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
            {!showMainNav && <span className="topbar-brand-mark">M</span>}
            <div className="topbar-title">
              {showMainNav && <span>{activeSection.label}</span>}
              <strong>
                {showMainNav
                  ? PAGE_META[activePage].label
                  : needsCredentialSetup && settings.apiKey
                    ? "重新配置访问密钥"
                    : "Memory Console 初始设置"}
              </strong>
            </div>
          </div>
          <div className="topbar-right">
            {showStatusPill && (
              <div className="status-cluster">
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
                {isWarningStatus && (
                  <button
                    className="status-dismiss"
                    type="button"
                    onClick={dismissWarningStatus}
                    title="关闭此提示（内容变化后会重新出现）"
                    aria-label="关闭状态提示"
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                )}
              </div>
            )}
            <button
              className="icon-button"
              type="button"
              onClick={onToggleTheme}
              title={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
              aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
            >
              {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </button>
            {showMainNav && (
              <button
                className="avatar-chip"
                type="button"
                onClick={() => go("settings")}
                title={
                  uiMode === "simple"
                    ? `连接设置（访问密钥）· 用户 ${userId}`
                    : `用户 ${userId} · 打开连接设置`
                }
                aria-label={`连接设置 · 用户 ${userId}`}
              >
                <Settings2 size={17} aria-hidden="true" />
              </button>
            )}
          </div>
        </header>

        <section className="content-area">{children}</section>
      </main>

      {showMainNav && (
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

      {showMainNav && moreOpen && (
        <MobileMoreSheet
          activePage={activePage}
          uiMode={uiMode}
          signalsError={signalsError}
          onRetrySignals={onRetrySignals}
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
  signalsError = null,
  onRetrySignals,
  onClose,
  onPageChange,
  onToggleUiMode
}: {
  activePage: PageKey;
  uiMode: UiMode;
  signalsError?: string | null;
  onRetrySignals?: () => void;
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
        {signalsError && (
          <div className="nav-signals-error" role="status">
            <TriangleAlert size={14} aria-hidden="true" />
            <span>待办角标暂时无法更新</span>
            <button
              className="ghost-button compact"
              type="button"
              onClick={() => onRetrySignals?.()}
            >
              重试
            </button>
          </div>
        )}
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
