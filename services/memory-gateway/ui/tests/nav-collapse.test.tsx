import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "../src/layout/AppShell";
import { loadCollapsedNavSections } from "../src/storage";

const baseProps = {
  settings: { apiBaseUrl: "http://127.0.0.1:2026", apiKey: "mgw_test", userId: "default" },
  serviceStatus: { loading: false, tone: "ok" as const, message: "聊天配置已就绪" },
  theme: "dark" as const,
  onToggleTheme: vi.fn(),
  onToggleUiMode: vi.fn(),
  onPageChange: vi.fn(),
  onRefreshService: vi.fn()
};

describe("expert nav collapsible sections", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("collapses a section, persists it, and keeps the active section open", async () => {
    const user = userEvent.setup();
    render(
      <AppShell {...baseProps} activePage="dashboard" uiMode="expert">
        <div />
      </AppShell>
    );

    const nav = within(screen.getByRole("navigation", { name: "Memory Console" }));

    // 全部分组默认展开
    expect(nav.getByRole("button", { name: "记忆体检" })).toBeInTheDocument();

    // 折叠「治理」分组后条目消失，偏好被持久化
    await user.click(nav.getByRole("button", { name: "治理" }));
    expect(nav.queryByRole("button", { name: "记忆体检" })).not.toBeInTheDocument();
    expect(loadCollapsedNavSections()).toContain("governance");

    // 含当前页面的分组即使被标记折叠也保持展开
    await user.click(nav.getByRole("button", { name: "工作室" }));
    expect(loadCollapsedNavSections()).toContain("studio");
    expect(nav.getByRole("button", { name: "记忆工作室" })).toBeInTheDocument();

    // 再点一次恢复展开
    await user.click(nav.getByRole("button", { name: "治理" }));
    expect(nav.getByRole("button", { name: "记忆体检" })).toBeInTheDocument();
    expect(loadCollapsedNavSections()).not.toContain("governance");
  });

  it("keeps simple mode labels non-interactive", () => {
    render(
      <AppShell {...baseProps} activePage="dashboard" uiMode="simple">
        <div />
      </AppShell>
    );
    // 简洁模式分组标签不是按钮
    expect(screen.queryByRole("button", { name: "开始" })).not.toBeInTheDocument();
    expect(screen.getByText("开始")).toBeInTheDocument();
  });
});
