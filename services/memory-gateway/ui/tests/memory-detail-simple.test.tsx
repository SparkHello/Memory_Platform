import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { MemoryDetailDrawer } from "../src/components/MemoryDetailDrawer";
import type { MemoryRecord } from "../src/types";

const SPACE_ID = "dashscope.qwen3.7-text-embedding:1024";

const memory = {
  id: "memory-leak-1",
  content: "用户只喝美式咖啡。",
  type: "semantic",
  status: "dynamic",
  importance: 5,
  confidence: 0.9,
  valence: 0.5,
  arousal: 0.2,
  stability: "stable",
  sensitivity: "normal",
  usage_count: 0,
  topics: [],
  entities: [],
  space_ids: [],
  evidence_memory_ids: [],
  digested: false,
  embedding_space_id: SPACE_ID,
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
    reviewMemories: vi.fn().mockResolvedValue({ total: 0, recommendations: [] })
  };
}

function renderDrawer(expertMode: boolean) {
  render(
    <MemoryDetailDrawer
      api={apiDouble() as unknown as MemoryApi}
      memoryId={memory.id}
      notify={vi.fn()}
      confirm={vi.fn().mockResolvedValue(true)}
      onClose={vi.fn()}
      onOpenMemory={vi.fn()}
      onChanged={vi.fn()}
      expertMode={expertMode}
    />
  );
}

describe("memory detail simple-mode leakage", () => {
  it("在简洁模式隐藏内部字段但保留语义检索状态", async () => {
    renderDrawer(false);

    expect(await screen.findByRole("dialog", { name: "记忆档案" })).toBeVisible();
    expect(await screen.findByText(memory.content)).toBeVisible();
    expect(screen.getByText("语义检索")).toBeVisible();
    expect(screen.getByText("已启用")).toBeVisible();
    expect(screen.queryByText(SPACE_ID)).not.toBeInTheDocument();
    expect(screen.queryByText("向量空间")).not.toBeInTheDocument();
    expect(screen.queryByText(/embedding/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看全部字段" })).not.toBeInTheDocument();
    expect(screen.queryByText("memory-leak-1")).not.toBeInTheDocument();
  });

  it("在专家模式保留向量空间和全部字段", async () => {
    const user = userEvent.setup();
    renderDrawer(true);

    expect(await screen.findByText(memory.content)).toBeVisible();
    expect(screen.getByText("向量空间")).toBeVisible();
    expect(screen.getByText(SPACE_ID)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "查看全部字段" }));
    expect(screen.getByText("memory-leak-1")).toBeVisible();
    expect(screen.getByText("衰减 λ")).toBeVisible();
  });
});
