import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { AddChannelModelPanel } from "../src/pages/system/AddChannelModelPanel";
import type { ModelGatewayControlSnapshot } from "../src/types";

const control: ModelGatewayControlSnapshot = {
  revision: "a".repeat(64),
  admin_required: true,
  connections: [
    {
      id: "dashscope-account",
      channel_operator: "dashscope",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      adapter: "dashscope_openai",
      usage_scope: "backend_allowed",
      allowed_private_networks: ["198.18.0.0/15"],
      connect_timeout_seconds: 30,
      read_timeout_seconds: 300,
      write_timeout_seconds: 300,
      pool_timeout_seconds: 300,
      enabled: true,
      configured: true
    }
  ],
  deployments: [
    {
      id: "dashscope-account-deepseek-v4-flash-0731",
      connection: "dashscope-account",
      upstream_model: "deepseek-v4-flash-0731",
      model_author: "unknown",
      model_family: "",
      kind: "chat",
      adapter_profile: "dashscope_deepseek_v4",
      capabilities: { tools: true },
      dimensions: null,
      embedding_space: "",
      enabled: true
    }
  ],
  routes: [
    {
      id: "memory.chat",
      kind: "chat",
      targets: ["dashscope-account-deepseek-v4-flash-0731"],
      required_capabilities: [],
      max_attempts: 1,
      fallback_scope: "none",
      enabled: true
    }
  ]
};

describe("add model to existing channel", () => {
  it("lists unused chat models and appends the chosen one as a same-channel fallback", async () => {
    const user = userEvent.setup();
    const applyProviderDeployments = vi
      .fn()
      .mockResolvedValueOnce({
        valid: true,
        applied: false,
        deployments: [{ id: "new", upstream_model: "qwen3.8-max", kind: "chat" }],
        changed_routes: ["memory.chat"],
        warnings: [],
        revision: "a".repeat(64)
      })
      .mockResolvedValueOnce({
        valid: true,
        applied: true,
        deployments: [{ id: "new", upstream_model: "qwen3.8-max", kind: "chat" }],
        changed_routes: ["memory.chat"],
        warnings: [],
        revision: "b".repeat(64)
      });
    const api = {
      checkProviderConnection: vi.fn().mockResolvedValue({
        mode: "discovery",
        summary: { ok: 1 },
        connections: [
          {
            connection_id: "dashscope-account",
            status: "ok",
            level: "ok",
            detail: "models endpoint 连接与鉴权正常",
            discovered_models: [
              "deepseek-v4-flash-0731",
              "qwen3.8-max",
              "qwen3.7-text-embedding"
            ]
          }
        ]
      }),
      applyProviderDeployments
    } as unknown as MemoryApi;

    render(
      <AddChannelModelPanel
        api={api}
        adminKey="admin-key"
        control={control}
        connection={control.connections[0]}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />
    );

    expect(await screen.findByText(/已用该渠道保存的密钥列出/)).toBeInTheDocument();
    const select = await screen.findByLabelText("聊天模型");
    expect(select).toHaveTextContent("qwen3.8-max");
    expect(select).not.toHaveTextContent("deepseek-v4-flash-0731");
    expect(select).not.toHaveTextContent("qwen3.7-text-embedding");

    await user.selectOptions(select, "qwen3.8-max");
    await user.click(screen.getByRole("button", { name: "检查配置" }));
    expect(await screen.findByText(/检查通过/)).toBeInTheDocument();
    const preview = applyProviderDeployments.mock.calls[0][0];
    expect(preview.dry_run).toBe(true);
    expect(preview.connection).toBe("dashscope-account");
    expect(preview.deployments[0].upstream_model).toBe("qwen3.8-max");
    expect(preview.routes[0]).toMatchObject({
      id: "memory.chat",
      targets: ["dashscope-account-deepseek-v4-flash-0731", "$0"],
      fallback_scope: "same_channel"
    });

    await user.click(screen.getByRole("button", { name: "确认添加" }));
    expect(await screen.findByText(/已添加 qwen3.8-max/)).toBeInTheDocument();
    expect(applyProviderDeployments.mock.calls[1][0].dry_run).toBe(false);
  });
});
