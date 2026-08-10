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
    configured: false
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
    await user.type(screen.getByLabelText("Model Gateway admin 密钥"), "admin-key");
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
});
