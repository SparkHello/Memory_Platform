import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { EvaluationPage } from "../src/pages/memory/EvaluationPage";
import type { MechanismDiagnosisResult, RecallEvalWorkbench } from "../src/types";

function diagnosis(snapshotInitialized: boolean): MechanismDiagnosisResult {
  return {
    snapshot_initialized: snapshotInitialized,
    database: "synthetic",
    memory_count: 0,
    metrics: {},
    verdicts: []
  };
}

const EMPTY_WORKBENCH = {
  snapshot: "synthetic",
  labels_path: "synthetic",
  user_id: "tester",
  target_label_min: 20,
  target_label_max: 30,
  labels: [],
  summary: { queries_total: 0, queries_graded: 0 },
  validation_issues: [],
  candidates: [],
  last_results: {}
} as unknown as RecallEvalWorkbench;

describe("EvaluationPage 按 snapshot_initialized 渲染", () => {
  it("snapshot_initialized=false 时不请求 workbench，渲染初始化空态且无错误条", async () => {
    const recallEvaluationWorkbench = vi.fn();
    const api = {
      evaluationDiagnosis: vi.fn().mockResolvedValue(diagnosis(false)),
      recallEvaluationWorkbench
    } as unknown as MemoryApi;

    render(<EvaluationPage api={api} notify={vi.fn()} />);

    expect(await screen.findByText("尚未初始化召回评测快照")).toBeInTheDocument();
    expect(recallEvaluationWorkbench).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("snapshot_initialized=true 时正常请求一次 workbench 并渲染标注台", async () => {
    const recallEvaluationWorkbench = vi.fn().mockResolvedValue(EMPTY_WORKBENCH);
    const api = {
      evaluationDiagnosis: vi.fn().mockResolvedValue(diagnosis(true)),
      recallEvaluationWorkbench
    } as unknown as MemoryApi;

    render(<EvaluationPage api={api} notify={vi.fn()} />);

    expect(await screen.findByText("Query 标注")).toBeInTheDocument();
    expect(recallEvaluationWorkbench).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("诊断请求失败时保持旧行为：仍请求 workbench，由 404 静默逻辑兜底", async () => {
    const recallEvaluationWorkbench = vi.fn().mockRejectedValue(new Error("404: 快照不存在"));
    const api = {
      evaluationDiagnosis: vi.fn().mockRejectedValue(new Error("503: 合成诊断故障")),
      recallEvaluationWorkbench
    } as unknown as MemoryApi;

    render(<EvaluationPage api={api} notify={vi.fn()} />);

    expect(await screen.findByText("尚未初始化召回评测快照")).toBeInTheDocument();
    expect(recallEvaluationWorkbench).toHaveBeenCalledTimes(1);
    // 诊断失败只显示诊断错误条，整页 error 不应出现。
    expect(screen.getByText(/^机制诊断加载失败/)).toBeInTheDocument();
  });
});
