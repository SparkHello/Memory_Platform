import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { MemoryDetailDrawer } from "../src/components/MemoryDetailDrawer";
import { ReviewPage } from "../src/pages/memory/ReviewPage";
import type { DatabaseHealthIssue, DatabaseHealthResult, MemoryRecord } from "../src/types";

const EMBEDDING_SPACE_ID = "synthetic-embedding-space-1536-v1";

const embeddingHealthIssues: DatabaseHealthIssue[] = [
  {
    type: "embedding_missing",
    severity: "warning",
    object_id: "memory-1",
    related_id: null,
    message: "Active memory has no embedding vector.",
    recommended_action: "Regenerate embeddings if semantic search quality matters."
  },
  {
    type: "embedding_invalid",
    severity: "error",
    object_id: "memory-2",
    related_id: null,
    message: "Active memory embedding is not valid numeric JSON.",
    recommended_action: "Regenerate the memory embedding."
  },
  {
    type: "embedding_dimension_mismatch",
    severity: "warning",
    object_id: "memory-3",
    related_id: null,
    message: "Active memory embedding dimension does not match current configuration.",
    recommended_action: "Regenerate embeddings with the configured embedding dimension."
  }
];

const healthResult: DatabaseHealthResult = {
  status: "warning",
  checked_at: "2026-08-15T00:00:00Z",
  summary: { errors: 1, warnings: 2, info: 0 },
  issues: embeddingHealthIssues
} as DatabaseHealthResult;

function reviewApiDouble() {
  return {
    reviewMemories: vi.fn().mockResolvedValue({ total: 0, recommendations: [] }),
    memoryHealth: vi.fn().mockResolvedValue(healthResult),
    listMemories: vi.fn().mockResolvedValue([]),
    decisionLogs: vi.fn().mockResolvedValue([])
  };
}

function renderReviewPage(api: ReturnType<typeof reviewApiDouble>, expertMode: boolean) {
  return render(
    <ReviewPage
      api={api as unknown as MemoryApi}
      notify={vi.fn()}
      confirm={vi.fn().mockResolvedValue(true)}
      openMemory={vi.fn()}
      expertMode={expertMode}
    />
  );
}

describe("简洁模式数据库健康", () => {
  it("不暴露 embedding 术语和后端英文诊断原文", async () => {
    const api = reviewApiDouble();
    renderReviewPage(api, false);

    await screen.findByText("数据库健康");

    // 三类 embedding 检查都以用户可理解的语义索引文案呈现。
    expect(screen.getByText("语义索引缺失")).toBeInTheDocument();
    expect(screen.getByText("语义索引损坏")).toBeInTheDocument();
    expect(screen.getByText("语义索引过期")).toBeInTheDocument();
    // 页面任何位置都不得出现 embedding 内部术语（含后端英文 message / recommended_action）。
    expect(screen.queryByText(/embedding/i)).not.toBeInTheDocument();
    expect(screen.queryByText("缺少 embedding")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Active memory has no embedding vector.")
    ).not.toBeInTheDocument();
  });

  it("专家模式保留完整技术诊断信息", async () => {
    const api = reviewApiDouble();
    renderReviewPage(api, true);

    await screen.findByText("数据库健康");

    expect(screen.getByText("缺少 embedding")).toBeInTheDocument();
    expect(screen.getByText("embedding 无效")).toBeInTheDocument();
    expect(screen.getByText("embedding 维度不匹配")).toBeInTheDocument();
    expect(screen.getByText("Active memory has no embedding vector.")).toBeInTheDocument();
    expect(
      screen.getByText("Regenerate embeddings with the configured embedding dimension.")
    ).toBeInTheDocument();
  });
});

const drawerMemory = {
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
  embedding_space_id: EMBEDDING_SPACE_ID,
  updated_at: "2026-08-15T00:00:00Z",
  created_at: "2026-08-15T00:00:00Z",
  revision: 1
} as MemoryRecord;

function drawerApiDouble() {
  return {
    getMemory: vi.fn().mockResolvedValue(drawerMemory),
    listMemorySpaces: vi.fn().mockResolvedValue([]),
    decisionLogs: vi.fn().mockResolvedValue([]),
    whyRemember: vi.fn().mockResolvedValue(null),
    traverseMemoryNetwork: vi.fn().mockResolvedValue({ results: [], edges: [], meta: {} }),
    reviewMemories: vi.fn().mockResolvedValue({ total: 1, recommendations: [] })
  };
}

function renderDrawer(api: ReturnType<typeof drawerApiDouble>, expertMode: boolean) {
  return render(
    <MemoryDetailDrawer
      api={api as unknown as MemoryApi}
      memoryId={drawerMemory.id}
      notify={vi.fn()}
      confirm={vi.fn().mockResolvedValue(true)}
      onClose={vi.fn()}
      onOpenMemory={vi.fn()}
      onChanged={vi.fn()}
      expertMode={expertMode}
    />
  );
}

describe("简洁模式记忆档案抽屉", () => {
  it("打开抽屉后不显示原始 embedding_space_id 或完整内部字段", async () => {
    const api = drawerApiDouble();
    renderDrawer(api, false);

    // 抽屉真正加载完成后才断言，而不是只检查初始渲染。
    await screen.findByText(drawerMemory.content);

    expect(screen.queryByText(EMBEDDING_SPACE_ID)).not.toBeInTheDocument();
    expect(screen.queryByText("向量空间")).not.toBeInTheDocument();
    expect(screen.queryByText(/embedding/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "查看全部字段" })
    ).not.toBeInTheDocument();
  });

  it("专家模式仍显示向量空间与全部字段", async () => {
    const api = drawerApiDouble();
    const user = userEvent.setup();
    renderDrawer(api, true);

    await screen.findByText(drawerMemory.content);

    expect(screen.getByText("向量空间")).toBeInTheDocument();
    expect(screen.getByText(EMBEDDING_SPACE_ID)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看全部字段" }));
    await waitFor(() => expect(screen.getByText("衰减 λ")).toBeInTheDocument());
  });
});
