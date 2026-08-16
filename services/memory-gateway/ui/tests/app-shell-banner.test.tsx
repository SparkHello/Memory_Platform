import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AppShell } from "../src/layout/AppShell";
import type { ConnectionSettings, PageKey } from "../src/types";

const SETTINGS: ConnectionSettings = {
  apiBaseUrl: "http://127.0.0.1:4173",
  apiKey: "mgw_test_console_key",
  userId: "tester"
};

type ServiceStatus = {
  loading: boolean;
  tone: "ok" | "warning" | "bad";
  message: string;
};

const WARNING: ServiceStatus = {
  loading: false,
  tone: "warning",
  message: "服务在线 · 待配置模型"
};

function shellProps(activePage: PageKey, serviceStatus: ServiceStatus) {
  return {
    activePage,
    settings: SETTINGS,
    serviceStatus,
    theme: "dark" as const,
    uiMode: "simple" as const,
    onToggleTheme: vi.fn(),
    onToggleUiMode: vi.fn(),
    onPageChange: vi.fn(),
    onRefreshService: vi.fn(),
    children: <div>页面内容</div>
  };
}

describe("AppShell 状态横幅", () => {
  it("warning 在无关页面（记忆库）不渲染", () => {
    render(<AppShell {...shellProps("memories", WARNING)} />);
    expect(screen.queryByText(WARNING.message)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关闭状态提示" })).not.toBeInTheDocument();
  });

  it("warning 在可操作页面渲染，点击关闭后消失", async () => {
    const user = userEvent.setup();
    render(<AppShell {...shellProps("dashboard", WARNING)} />);
    expect(screen.getByText(WARNING.message)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭状态提示" }));
    expect(screen.queryByText(WARNING.message)).not.toBeInTheDocument();
  });

  it("关闭后同消息不再出现，消息变化后复现", async () => {
    const user = userEvent.setup();
    const props = shellProps("dashboard", WARNING);
    const { rerender } = render(<AppShell {...props} />);
    await user.click(screen.getByRole("button", { name: "关闭状态提示" }));
    expect(screen.queryByText(WARNING.message)).not.toBeInTheDocument();

    rerender(<AppShell {...props} serviceStatus={{ ...WARNING }} />);
    expect(screen.queryByText(WARNING.message)).not.toBeInTheDocument();

    const changed: ServiceStatus = { ...WARNING, message: "服务在线 · 模型配置需处理" };
    rerender(<AppShell {...props} serviceStatus={changed} />);
    expect(screen.getByText(changed.message)).toBeInTheDocument();
  });

  it("关闭写入 memory-console.dismissedStatus，重新挂载后同消息仍隐藏", async () => {
    const user = userEvent.setup();
    const first = render(<AppShell {...shellProps("providers", WARNING)} />);
    await user.click(screen.getByRole("button", { name: "关闭状态提示" }));
    expect(localStorage.getItem("memory-console.dismissedStatus")).toBe(WARNING.message);
    first.unmount();

    render(<AppShell {...shellProps("providers", WARNING)} />);
    expect(screen.queryByText(WARNING.message)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关闭状态提示" })).not.toBeInTheDocument();
  });

  it("bad 服务异常不受页限且不可关闭", () => {
    const bad: ServiceStatus = { loading: false, tone: "bad", message: "连接被拒绝" };
    render(<AppShell {...shellProps("memories", bad)} />);
    expect(screen.getByText("服务异常")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关闭状态提示" })).not.toBeInTheDocument();
  });

  it("loading 显示检查中且无关闭按钮", () => {
    render(
      <AppShell {...shellProps("dashboard", { loading: true, tone: "warning", message: "检查中" })} />
    );
    expect(screen.getByText("检查中")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关闭状态提示" })).not.toBeInTheDocument();
  });
});
