import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { ProvidersPage } from "../src/pages/system/ProvidersPage";
import type { ModelGatewayControlSnapshot, ProvidersStatus } from "../src/types";

const baseControl: ModelGatewayControlSnapshot = {
  revision: "a".repeat(64),
  admin_required: true,
  connections: [
    {
      id: "active-channel",
      channel_operator: "official",
      base_url: "https://official.example/v1",
      adapter: "generic",
      usage_scope: "backend_allowed",
      allowed_private_networks: [],
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
      id: "active-chat",
      connection: "active-channel",
      upstream_model: "author/chat-v1",
      model_author: "author",
      model_family: "chat",
      kind: "chat",
      adapter_profile: "inherit",
      capabilities: { tools: true },
      dimensions: null,
      embedding_space: "",
      pricing: "used-pricing",
      enabled: true
    }
  ],
  routes: [
    {
      id: "memory.chat",
      kind: "chat",
      targets: ["active-chat"],
      required_capabilities: [],
      max_attempts: 1,
      fallback_scope: "none",
      enabled: true
    }
  ]
};

const adminControl: ModelGatewayControlSnapshot = {
  ...baseControl,
  connections: [
    ...baseControl.connections,
    {
      ...baseControl.connections[0],
      id: "orphan-channel",
      channel_operator: "orphan"
    }
  ],
  deployments: [
    ...baseControl.deployments,
    {
      ...baseControl.deployments[0],
      id: "orphan-chat",
      upstream_model: "author/orphan-v1",
      pricing: null
    }
  ],
  pricing: [
    {
      id: "used-pricing",
      mode: "unknown",
      currency: "USD",
      unit_tokens: 1_000_000,
      tiers: [],
      source_url: "",
      effective_from: "",
      checked_at: "",
      notes: ""
    },
    {
      id: "orphan-pricing",
      mode: "unknown",
      currency: "CNY",
      unit_tokens: 1_000_000,
      tiers: [],
      source_url: "",
      effective_from: "",
      checked_at: "",
      notes: ""
    }
  ]
};

const status: ProvidersStatus = {
  runtime: {
    model_gateway_enabled: true,
    model_gateway_base_url: "http://model-gateway:2030/v1",
    chat_source: "model_gateway",
    knowledge_source: "model_gateway",
    providers_path: "",
    routes_path: ""
  },
  embedding: {
    model: "memory.embedding",
    base_url: "http://model-gateway:2030/v1",
    dimensions: 1024,
    configured: false,
    mode: "auto",
    state: "off",
    code: "embedding_route_off"
  },
  providers: [],
  routes: [],
  control: baseControl,
  config_error: "",
  setup: {
    state: "needs_model",
    service_ready: true,
    model_gateway_connected: true,
    chat_ready: false,
    required_chat_routes: ["memory.chat"],
    usable_chat_routes: [],
    missing_chat_routes: ["memory.chat"],
    next_action: "configure_model"
  }
};

describe("provider object management", () => {
  it("shows an intentionally disabled automatic embedding route as keyword mode", async () => {
    const api = {
      providersStatus: vi.fn().mockResolvedValue(status)
    } as unknown as MemoryApi;

    render(<ProvidersPage api={api} expertMode />);

    expect(await screen.findByText(/自动契约 · 已关闭（关键词检索）/)).toBeInTheDocument();
    expect(screen.getByText(/embedding_route_off/)).toBeInTheDocument();
  });

  it("keeps the repair surface visible when chat works but the embedding contract is invalid", async () => {
    const invalidStatus: ProvidersStatus = {
      ...status,
      embedding: {
        ...status.embedding,
        configured: false,
        dimensions: 0,
        state: "invalid",
        code: "model_gateway_embedding_contract_mismatch"
      },
      setup: {
        ...status.setup,
        state: "configuration_error",
        service_ready: false,
        chat_ready: true,
        next_action: "repair_model_gateway"
      }
    };
    const api = {
      providersStatus: vi.fn().mockResolvedValue(invalidStatus)
    } as unknown as MemoryApi;

    render(<ProvidersPage api={api} initialSetup expertMode />);

    expect(
      await screen.findByRole("heading", { name: "修复模型与向量配置" })
    ).toBeInTheDocument();
    expect(screen.getByText(/自动契约 · 契约无效/)).toBeInTheDocument();
    expect(screen.getByText(/model_gateway_embedding_contract_mismatch/)).toBeInTheDocument();
    expect(screen.queryByText("完成下面三步即可开始聊天")).not.toBeInTheDocument();
  });

  it("loads the full admin graph and only enables delete for unreferenced objects", async () => {
    const user = userEvent.setup();
    const deleteProviderObject = vi.fn().mockResolvedValue({
      deleted: true,
      id: "orphan-pricing",
      collection: "pricing",
      revision: "b".repeat(64)
    });
    const api = {
      providersStatus: vi.fn().mockResolvedValue(status),
      checkProviderAdminKey: vi.fn().mockResolvedValue({ valid: true }),
      providerAdminConfiguration: vi.fn().mockResolvedValue(adminControl),
      deleteProviderObject,
      setProviderObjectEnabled: vi.fn()
    } as unknown as MemoryApi;

    render(<ProvidersPage api={api} expertMode />);
    await screen.findByText("official");
    await user.type(screen.getByLabelText("管理密钥"), "admin-key");
    await user.click(screen.getByRole("button", { name: "验证管理密钥" }));

    const orphanRow = (await screen.findByText("orphan-pricing")).closest("article");
    const usedRow = screen.getByText("used-pricing").closest("article");
    expect(orphanRow).not.toBeNull();
    expect(usedRow).not.toBeNull();
    expect(api.providerAdminConfiguration).toHaveBeenCalledWith("admin-key");
    expect(within(usedRow!).getByRole("button", { name: "删除" })).toBeDisabled();
    expect(within(orphanRow!).getByRole("button", { name: "删除" })).toBeEnabled();

    await user.click(within(orphanRow!).getByRole("button", { name: "删除" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("服务端会再次检查引用关系和 revision");
    await user.click(screen.getByRole("button", { name: "删除对象" }));

    expect(deleteProviderObject).toHaveBeenCalledWith(
      "pricing",
      "orphan-pricing",
      "a".repeat(64),
      "admin-key"
    );
  });

  it("lets an admin edit the capabilities of an existing deployment", async () => {
    const user = userEvent.setup();
    const updateProviderDeploymentCapabilities = vi.fn().mockResolvedValue({
      updated: true,
      id: "orphan-chat",
      capabilities: { tools: true },
      revision: "b".repeat(64)
    });
    const probeProviderChannelCapabilities = vi.fn().mockResolvedValue({
      persisted: false,
      revision: "a".repeat(64),
      upstream_model: "author/orphan-v1",
      ok: true,
      capabilities: { streaming: true, tools: true, reasoning: true },
      details: { chat: { ok: true } }
    });
    const api = {
      providersStatus: vi.fn().mockResolvedValue(status),
      checkProviderAdminKey: vi.fn().mockResolvedValue({ valid: true }),
      providerAdminConfiguration: vi.fn().mockResolvedValue({
        ...adminControl,
        deployments: adminControl.deployments.map((deployment) =>
          deployment.id === "orphan-chat" ? { ...deployment, capabilities: {} } : deployment
        )
      }),
      updateProviderDeploymentCapabilities,
      probeProviderChannelCapabilities,
      setProviderObjectEnabled: vi.fn()
    } as unknown as MemoryApi;

    // an earlier test in this file may have left a validated key in session storage
    sessionStorage.clear();
    render(<ProvidersPage api={api} expertMode />);
    await screen.findByText("official");
    await user.type(screen.getByLabelText("管理密钥"), "admin-key");
    await user.click(screen.getByRole("button", { name: "验证管理密钥" }));

    const chip = await screen.findByRole("button", { name: "修改模型 orphan-chat 的能力" });
    expect(chip).toHaveTextContent("无工具调用");
    await user.click(chip);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("模型能力：author/orphan-v1");
    // auto-detect reuses the saved channel secret: no key is typed here
    await user.click(within(dialog).getByRole("button", { name: "自动检测能力" }));
    expect(probeProviderChannelCapabilities).toHaveBeenCalledWith(
      expect.objectContaining({ connection_id: "active-channel", upstream_model: "author/orphan-v1" }),
      "admin-key"
    );
    expect(await within(dialog).findByLabelText("工具调用 tools")).toBeChecked();
    expect(within(dialog).getByLabelText("推理 reasoning")).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "保存能力" }));

    expect(updateProviderDeploymentCapabilities).toHaveBeenCalledWith(
      "orphan-chat",
      "a".repeat(64),
      expect.objectContaining({ tools: true, reasoning: true }),
      "admin-key"
    );
  });
});
