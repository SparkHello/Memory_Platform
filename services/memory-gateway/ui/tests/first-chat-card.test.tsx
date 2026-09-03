import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { DashboardPage } from "../src/pages/DashboardPage";

const readySetup = {
  state: "ready",
  service_ready: true,
  model_gateway_connected: true,
  chat_ready: true,
  required_chat_routes: ["memory.chat"],
  usable_chat_routes: ["memory.chat"],
  missing_chat_routes: [],
  next_action: "connect_client"
};

function report(active: number) {
  return {
    user_id: "default",
    generated_at: "2026-09-03T00:00:00Z",
    counts: { active_memories: active, deleted_memories: 0, core_sections: 0 },
    sections: []
  };
}

function apiDouble(overrides: Partial<Record<keyof MemoryApi, unknown>> = {}) {
  return {
    health: vi.fn().mockResolvedValue({ status: "ok" }),
    memoryReport: vi.fn().mockResolvedValue(report(0)),
    decisionLogs: vi.fn().mockResolvedValue([]),
    surfaceMemories: vi.fn().mockResolvedValue([]),
    listMemorySpaces: vi.fn().mockResolvedValue([]),
    providersStatus: vi.fn().mockResolvedValue({ setup: readySetup, providers: [] }),
    authTokens: vi.fn().mockResolvedValue({
      legacy_key_enabled: false,
      authenticated_with_legacy_key: false,
      current_user_id: "default",
      data: [
        {
          token_id: "abcdefabcdefabcd",
          name: "我的手机",
          user_id: "default",
          role: "chat",
          memory_access: "read-write",
          created_at: "2026-09-03T00:00:00Z",
          last_used_at: null,
          revoked_at: null
        }
      ]
    }),
    memoryNetwork: vi.fn(),
    reviewMemories: vi.fn(),
    ...overrides
  } as unknown as MemoryApi;
}

function renderDashboard(api: MemoryApi) {
  return render(
    <DashboardPage
      api={api}
      settings={{ apiBaseUrl: "http://127.0.0.1:2026", apiKey: "key", userId: "default" }}
      setPage={vi.fn()}
      openMemory={vi.fn()}
      notify={vi.fn()}
      confirm={vi.fn().mockResolvedValue(true)}
      refreshKey={0}
    />
  );
}

describe("first chat feedback loop", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn()
      })
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the try-it card with the test sentence when a chat key exists but memory is empty", async () => {
    renderDashboard(apiDouble());
    expect(await screen.findByText("试一下：让 AI 记住第一件事")).toBeInTheDocument();
    expect(screen.getByText(/我喜欢黑咖啡，不加糖/)).toBeInTheDocument();
    expect(screen.getByText("正在等第一条记忆出现…")).toBeInTheDocument();
  });

  it("turns green once the report shows the first memory", async () => {
    const memoryReport = vi
      .fn()
      .mockResolvedValueOnce(report(0)) // 工作台首次加载
      .mockResolvedValueOnce(report(0)) // 探针第一次轮询
      .mockResolvedValue(report(1));
    const api = apiDouble({ memoryReport });
    renderDashboard(api);
    expect(await screen.findByText("正在等第一条记忆出现…")).toBeInTheDocument();
    // 探针每 6 秒轮询一次；第二次轮询看到计数变为 1 后卡片要停留在完成态，
    // 即使工作台随后重新加载并发现记忆库不再为空。
    await waitFor(
      () => expect(screen.getByText(/第一条记忆已经保存/)).toBeInTheDocument(),
      { timeout: 9000 }
    );
  }, 12000);

  it("explains why the last turn was skipped instead of a bare spinner", async () => {
    const api = apiDouble({
      decisionLogs: vi.fn().mockResolvedValue([
        {
          id: "log-1",
          candidate_json: "{}",
          decision: "ignore",
          reason: "本地预过滤：寒暄致谢",
          created_at: "2026-09-03T00:00:00Z"
        }
      ])
    });
    renderDashboard(api);
    expect(await screen.findByText(/收到过一轮对话，但没有保存/)).toBeInTheDocument();
  });

  it("stays hidden when memory already exists", async () => {
    renderDashboard(apiDouble({ memoryReport: vi.fn().mockResolvedValue(report(3)) }));
    await screen.findByText("浮现记忆");
    expect(screen.queryByText("试一下：让 AI 记住第一件事")).not.toBeInTheDocument();
  });
});
