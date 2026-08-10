import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { UsagePage } from "../src/pages/system/UsagePage";
import type { CentralModelUsageSummary } from "../src/types";

const centralSummary: CentralModelUsageSummary = {
  days: 30,
  filters: { client_id: "memory-gateway", operation: "", user_tag: "usr_opaque" },
  calls: 3,
  complete_calls: 2,
  input_tokens: 120,
  output_tokens: 45,
  total_tokens: 165,
  estimated_costs: { CNY: "0.012345" },
  incomplete_cost_calls: 1,
  attempts: {
    recorded: 4,
    known_cost_attempts: 3,
    unknown_cost_attempts: 1,
    not_sent_attempts: 1,
    legacy_logical_events_without_attempts: 0,
    by_currency: {
      CNY: { known_attempts: 3, unknown_attempts: 1, estimated_cost: "0.012345" }
    }
  },
  deployments: [
    {
      deployment_id: "dashscope-flash",
      connection_id: "dashscope-payg",
      channel_operator: "dashscope",
      model_author: "deepseek",
      upstream_model: "deepseek-v4-flash-0731",
      calls: 3,
      total_tokens: 165
    }
  ],
  retention: { raw_days: 90, daily_days: 365 }
};

describe("central model usage view", () => {
  it("renders the attempt ledger without assuming the legacy local schema", async () => {
    const api = {
      modelUsage: vi.fn().mockResolvedValue(centralSummary)
    } as unknown as MemoryApi;

    render(<UsagePage api={api} />);

    expect(await screen.findByText("逐 Attempt 账本")).toBeInTheDocument();
    expect(screen.getByText("deepseek-v4-flash-0731")).toBeInTheDocument();
    expect(screen.getByText("可能已计费但未知")).toBeInTheDocument();
    expect(screen.getByText(/原始逐请求与逐 attempt 事件保留 90 天/)).toBeInTheDocument();
    expect(api.modelUsage).toHaveBeenCalledWith("30", expect.any(AbortSignal));
  });
});
