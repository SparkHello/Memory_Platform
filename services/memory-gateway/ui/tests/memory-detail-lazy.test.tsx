import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { MemoryDetailDrawer } from "../src/components/MemoryDetailDrawer";
import type { MemoryRecord } from "../src/types";

const memory = {
  id: "memory-1",
  content: "A remembered preference",
  type: "semantic",
  status: "active",
  importance: 5,
  confidence: 0.9,
  stability: "stable",
  sensitivity: "normal",
  usage_count: 0,
  topics: [],
  entities: [],
  space_ids: [],
  updated_at: "2026-08-15T00:00:00Z",
  created_at: "2026-08-15T00:00:00Z",
  revision: 1
} as MemoryRecord;

function apiDouble() {
  return {
    getMemory: vi.fn().mockResolvedValue(memory),
    listMemorySpaces: vi.fn().mockResolvedValue([]),
    decisionLogs: vi.fn().mockResolvedValue([]),
    whyRemember: vi.fn().mockResolvedValue(null),
    traverseMemoryNetwork: vi.fn().mockResolvedValue({ results: [], edges: [], meta: {} }),
    reviewMemories: vi.fn().mockResolvedValue({ total: 1, recommendations: [] })
  };
}

describe("memory detail expensive analysis", () => {
  it("loads graph traversal and full review only after explicit user actions", async () => {
    const api = apiDouble();
    const user = userEvent.setup();

    render(
      <MemoryDetailDrawer
        api={api as unknown as MemoryApi}
        memoryId={memory.id}
        notify={vi.fn()}
        confirm={vi.fn().mockResolvedValue(true)}
        onClose={vi.fn()}
        onOpenMemory={vi.fn()}
        onChanged={vi.fn()}
      />
    );

    await screen.findByText(memory.content);
    expect(api.traverseMemoryNetwork).not.toHaveBeenCalled();
    expect(api.reviewMemories).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "加载关联分析（实验）" }));
    await waitFor(() => expect(api.traverseMemoryNetwork).toHaveBeenCalledTimes(1));
    expect(api.reviewMemories).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "加载治理建议" }));
    await waitFor(() => expect(api.reviewMemories).toHaveBeenCalledTimes(1));
  });
});
