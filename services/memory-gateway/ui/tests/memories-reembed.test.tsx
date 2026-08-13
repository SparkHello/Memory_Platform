import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { MemoriesPage } from "../src/pages/memory/MemoriesPage";
import type { MemoryRecord, ProvidersStatus } from "../src/types";

function memory(partial: Partial<MemoryRecord> = {}): MemoryRecord {
  return {
    id: "mem-1",
    content: "用户只喝美式咖啡，不加糖不加奶。",
    type: "semantic",
    importance: 8,
    confidence: 0.9,
    valence: 0.5,
    arousal: 0.3,
    usage_count: 0,
    stability: "stable",
    sensitivity: "normal",
    evidence_memory_ids: [],
    topics: ["偏好"],
    entities: [],
    space_ids: [],
    status: "dynamic",
    digested: false,
    created_at: "2026-08-13T00:00:00Z",
    updated_at: "2026-08-13T00:00:00Z",
    revision: 1,
    embedding_space_id: null,
    ...partial
  };
}

const embeddingReady: ProvidersStatus["embedding"] = {
  model: "memory.embedding",
  base_url: "http://model-gateway:2030/v1",
  dimensions: 1024,
  configured: true,
  mode: "auto",
  state: "ready",
  code: "ok",
  space_id: "dashscope.qwen3.7-text-embedding:1024"
};

describe("memories re-embed banner", () => {
  it("offers to backfill memories that have no current-space vectors", async () => {
    const user = userEvent.setup();
    const reEmbedMemories = vi.fn().mockResolvedValue({
      re_embedded: 1,
      memory_ids: ["mem-1"],
      failed_ids: []
    });
    const notify = vi.fn();
    const api = {
      listMemories: vi.fn().mockResolvedValue([memory()]),
      listMemorySpaces: vi.fn().mockResolvedValue([]),
      memoryHealth: vi.fn().mockResolvedValue({
        status: "warning",
        issues: [
          {
            type: "embedding_missing",
            severity: "warning",
            object_id: "memory:mem-1",
            related_id: "mem-1",
            message: "Active memory has no embedding vector.",
            recommended_action: "Regenerate embeddings"
          }
        ]
      }),
      providersStatus: vi.fn().mockResolvedValue({
        embedding: embeddingReady
      }),
      reEmbedMemories
    } as unknown as MemoryApi;

    render(
      <MemoriesPage api={api} notify={notify} openMemory={vi.fn()} refreshKey={0} />
    );

    expect(await screen.findByText(/没有当前空间的向量/)).toBeInTheDocument();
    expect(screen.getAllByText(/无向量/).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "补齐向量" }));
    expect(reEmbedMemories).toHaveBeenCalledWith({ scan: true });
    expect(notify).toHaveBeenCalledWith("已为 1 条记忆补齐向量", "success");
  });
});
