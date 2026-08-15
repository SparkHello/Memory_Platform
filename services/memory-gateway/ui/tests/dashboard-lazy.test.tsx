import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { DashboardPage } from "../src/pages/DashboardPage";

describe("dashboard expensive analysis", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("does not run full review or build the network before exploration is opened", async () => {
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
    const memoryNetwork = vi.fn().mockResolvedValue({
      nodes: [],
      edges: [],
      meta: {
        memory_count: 0,
        core_count: 0,
        similarity_threshold: 0.42,
        max_similarity_edges: 40
      }
    });
    const reviewMemories = vi.fn();
    const api = {
      health: vi.fn().mockResolvedValue({ status: "ok" }),
      memoryReport: vi.fn().mockResolvedValue({
        user_id: "default",
        generated_at: "2026-08-15T00:00:00Z",
        counts: { active_memories: 0, deleted_memories: 0, core_sections: 1 },
        sections: []
      }),
      decisionLogs: vi.fn().mockResolvedValue([]),
      surfaceMemories: vi.fn().mockResolvedValue([]),
      listMemorySpaces: vi.fn().mockResolvedValue([]),
      providersStatus: vi.fn().mockResolvedValue(null),
      authTokens: vi.fn().mockResolvedValue(null),
      memoryNetwork,
      reviewMemories
    };
    const user = userEvent.setup();

    render(
      <DashboardPage
        api={api as unknown as MemoryApi}
        settings={{ apiBaseUrl: "http://localhost:2026", apiKey: "key", userId: "default" }}
        setPage={vi.fn()}
        openMemory={vi.fn()}
        notify={vi.fn()}
        confirm={vi.fn().mockResolvedValue(true)}
        refreshKey={0}
      />
    );

    await screen.findByText("浮现记忆");
    expect(reviewMemories).not.toHaveBeenCalled();
    expect(memoryNetwork).not.toHaveBeenCalled();

    await user.click(screen.getByText("探索情绪、网络与计数"));
    await waitFor(() => expect(memoryNetwork).toHaveBeenCalledTimes(1));
    expect(reviewMemories).not.toHaveBeenCalled();
  });
});
