import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryApi, ApiError } from "../src/api";
import { App } from "../src/App";

// App 的韧性逻辑（边界 / 未知 hash / 角标失败）与页面内部实现无关；
// 用轻量替身替换巨型页面组件，保持测试稳定且聚焦壳层行为。
const crashFlag = { enabled: false };
vi.mock("../src/pages/DashboardPage", () => ({
  DashboardPage: () => {
    if (crashFlag.enabled) throw new Error("合成渲染崩溃");
    return <h1>工作室页内容</h1>;
  }
}));
vi.mock("../src/pages/memory/MemoriesPage", () => ({
  MemoriesPage: () => <h1>记忆库</h1>
}));
vi.mock("../src/pages/knowledge/KnowledgeLibraryPage", () => ({
  KnowledgeLibraryPage: () => <h1>知识库</h1>
}));
vi.mock("../src/pages/knowledge/KnowledgeSearchPage", () => ({
  KnowledgeSearchPage: () => <h1>检索调试</h1>
}));
vi.mock("../src/pages/memory/CoreMemoryPage", () => ({
  CoreMemoryPage: () => <h1>核心记忆</h1>
}));
vi.mock("../src/pages/memory/DecisionLogsPage", () => ({
  DecisionLogsPage: () => <h1>决策日志</h1>
}));
vi.mock("../src/pages/memory/EvaluationPage", () => ({
  EvaluationPage: () => <h1>评测闭环</h1>
}));
vi.mock("../src/pages/memory/RecallExplainPage", () => ({
  RecallExplainPage: () => <h1>召回解释</h1>
}));
vi.mock("../src/pages/memory/RecentContextPage", () => ({
  RecentContextPage: () => <h1>对话上下文</h1>
}));
vi.mock("../src/pages/memory/ReportsPage", () => ({
  ReportsPage: () => <h1>报告与备份</h1>
}));
vi.mock("../src/pages/memory/ReviewPage", () => ({
  ReviewPage: () => <h1>记忆体检</h1>
}));
vi.mock("../src/pages/system/DeveloperPage", () => ({
  DeveloperPage: () => <h1>接入信息</h1>
}));
vi.mock("../src/pages/system/ProvidersPage", () => ({
  ProvidersPage: () => <h1>模型与路由</h1>
}));
vi.mock("../src/pages/system/UsagePage", () => ({
  UsagePage: () => <h1>用量页内容</h1>
}));
vi.mock("../src/components/MemoryDetailDrawer", () => ({
  MemoryDetailDrawer: () => <div role="dialog">记忆档案</div>
}));

const READY_SETUP = { state: "ready", chat_ready: true };

function okProvidersStatus() {
  return { setup: READY_SETUP };
}

function okMemoryReport() {
  return {
    user_id: "default",
    generated_at: "2026-08-16T00:00:00Z",
    counts: { active_memories: 0, deleted_memories: 0, core_sections: 0 },
    sections: []
  };
}

function seedCredentials() {
  localStorage.setItem("memory-console.gatewayApiKey", "mgw_test_console_key");
  localStorage.setItem("memory-console.userId", "default");
}

function stubAppApi(options?: { memoryReport?: () => Promise<unknown> }) {
  vi.spyOn(MemoryApi.prototype, "health").mockResolvedValue({ status: "ok" });
  vi.spyOn(MemoryApi.prototype, "memoryReport").mockImplementation(
    (options?.memoryReport ?? (async () => okMemoryReport())) as () => Promise<never>
  );
  vi.spyOn(MemoryApi.prototype, "providersStatus").mockResolvedValue(
    okProvidersStatus() as never
  );
  vi.spyOn(MemoryApi.prototype, "knowledgeStatus").mockResolvedValue(null as never);
  vi.spyOn(MemoryApi.prototype, "authTokens").mockResolvedValue(null as never);
}

describe("App resilience shell", () => {
  afterEach(() => {
    crashFlag.enabled = false;
    window.location.hash = "";
  });

  it("未知 hash 显示「页面不存在」，返回工作室后恢复", async () => {
    seedCredentials();
    stubAppApi();
    window.location.hash = "#/foo";
    const user = userEvent.setup();

    render(<App />);

    expect(await screen.findByText("页面不存在")).toBeInTheDocument();
    expect(screen.getByText("#/foo")).toBeInTheDocument();
    expect(screen.queryByText("工作室页内容")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "返回工作室" }));

    expect(await screen.findByText("工作室页内容")).toBeInTheDocument();
    expect(screen.queryByText("页面不存在")).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#/studio");
  });

  it("运行中切到未知 hash 提示，切回合法 hash 提示消失", async () => {
    seedCredentials();
    stubAppApi();
    window.location.hash = "#/studio";

    render(<App />);
    expect(await screen.findByText("工作室页内容")).toBeInTheDocument();

    window.location.hash = "#/definitely-not-a-page";
    expect(await screen.findByText("页面不存在")).toBeInTheDocument();

    window.location.hash = "#/memories";
    await waitFor(() => expect(screen.getAllByText("记忆库").length).toBeGreaterThan(0));
    expect(screen.queryByText("页面不存在")).not.toBeInTheDocument();
  });

  it("合法深链回归：记忆档案抽屉与叠加地址正常打开", async () => {
    seedCredentials();
    stubAppApi();
    window.location.hash = "#/usage?memory=mem-42";

    render(<App />);

    expect(await screen.findByText("用量页内容")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent("记忆档案");
    expect(screen.queryByText("页面不存在")).not.toBeInTheDocument();
  });

  it("角标刷新 500：侧栏出现失败提示，不发错误 toast，重试成功后消失", async () => {
    seedCredentials();
    let failing = true;
    stubAppApi({
      memoryReport: async () => {
        if (failing) {
          throw new Error("Failed to fetch");
        }
        return okMemoryReport();
      }
    });
    window.location.hash = "#/studio";
    const user = userEvent.setup();

    render(<App />);

    expect(await screen.findByText("待办角标暂时无法更新")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    failing = false;
    await user.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() =>
      expect(screen.queryByText("待办角标暂时无法更新")).not.toBeInTheDocument()
    );
  });

  it("角标刷新 401：仍强制回连接设置页", async () => {
    seedCredentials();
    stubAppApi({
      memoryReport: async () => {
        throw new ApiError(401, "unauthorized");
      }
    });
    window.location.hash = "#/studio";

    render(<App />);

    // 凭证门回退后由设置页标题与顶栏文案共同呈现。
    expect(
      await screen.findByRole("heading", { name: "更新访问密钥" })
    ).toBeInTheDocument();
    expect(screen.getByText("重新配置访问密钥")).toBeInTheDocument();
  });

  it("页面组件渲染崩溃时整站不白屏，「返回工作室」无需刷新即可恢复", async () => {
    silenceConsoleError();
    seedCredentials();
    stubAppApi();
    window.location.hash = "#/studio";
    const user = userEvent.setup();

    crashFlag.enabled = true;
    render(<App />);

    expect(await screen.findByText("页面出现错误")).toBeInTheDocument();
    // 侧栏与顶栏仍在边界外，导航可用。
    expect(screen.getAllByRole("button", { name: /记忆库/ }).length).toBeGreaterThan(0);

    // 修复崩溃源后点「返回工作室」：navigateToPage("dashboard") 与「重试」等价，
    // 重新渲染工作室，无需整页刷新即可恢复。
    crashFlag.enabled = false;
    await user.click(screen.getByRole("button", { name: "返回工作室" }));

    await screen.findByText("工作室页内容");
    expect(screen.queryByText("页面出现错误")).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#/studio");
  });
});

function silenceConsoleError() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}
