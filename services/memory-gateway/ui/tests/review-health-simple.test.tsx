import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { ReviewPage } from "../src/pages/memory/ReviewPage";
import type { DatabaseHealthIssue } from "../src/types";

const issues: DatabaseHealthIssue[] = [
  {
    type: "embedding_missing",
    severity: "warning",
    object_id: "memory:mem-1",
    related_id: "mem-1",
    message: "Active memory has no embedding vector.",
    recommended_action: "Regenerate embeddings"
  },
  {
    type: "embedding_invalid",
    severity: "warning",
    object_id: "memory:mem-2",
    related_id: "mem-2",
    message: "Stored embedding is invalid.",
    recommended_action: "Regenerate embeddings"
  },
  {
    type: "embedding_dimension_mismatch",
    severity: "warning",
    object_id: "memory:mem-3",
    related_id: "mem-3",
    message: "embedding dimension mismatch against current space.",
    recommended_action: "Regenerate embeddings"
  }
];

function apiDouble() {
  return {
    reviewMemories: vi.fn().mockResolvedValue({ total: 0, recommendations: [] }),
    memoryHealth: vi.fn().mockResolvedValue({
      status: "warning",
      checked_at: "2026-08-09T10:00:00Z",
      summary: { errors: 0, warnings: 3, info: 0 },
      issues
    }),
    listMemories: vi.fn().mockResolvedValue([]),
    decisionLogs: vi.fn().mockResolvedValue([])
  } as unknown as MemoryApi;
}

function renderReview(expertMode: boolean) {
  render(
    <ReviewPage
      api={apiDouble()}
      notify={vi.fn()}
      confirm={vi.fn().mockResolvedValue(true)}
      openMemory={vi.fn()}
      expertMode={expertMode}
    />
  );
}

const INTERNAL_LABELS = ["缺少 embedding", "embedding 无效", "embedding 维度不匹配"];

describe("review health simple-mode leakage", () => {
  it("does not show internal embedding diagnostics in simple mode", async () => {
    renderReview(false);

    expect(await screen.findByText("语义索引缺失")).toBeVisible();
    expect(screen.getByText("语义索引无效")).toBeVisible();
    expect(screen.getByText("语义索引规格不一致")).toBeVisible();
    expect(screen.getByText("部分记忆还不能用于语义搜索。")).toBeVisible();

    for (const label of INTERNAL_LABELS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
    expect(screen.queryByText(/embedding/i)).not.toBeInTheDocument();
    expect(screen.queryByText("memory:mem-1")).not.toBeInTheDocument();
    expect(screen.queryByText("Regenerate embeddings")).not.toBeInTheDocument();
  });

  it("keeps technical health diagnostics in expert mode", async () => {
    renderReview(true);

    expect(await screen.findByText("缺少 embedding")).toBeVisible();
    expect(screen.getByText("embedding 无效")).toBeVisible();
    expect(screen.getByText("embedding 维度不匹配")).toBeVisible();
    expect(screen.getByText("Active memory has no embedding vector.")).toBeVisible();
    expect(screen.getByText("memory:mem-1")).toBeVisible();
    expect(screen.getAllByText("Regenerate embeddings").length).toBeGreaterThan(0);
  });
});
