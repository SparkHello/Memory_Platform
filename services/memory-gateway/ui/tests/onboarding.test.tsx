import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { NewChannelWizard } from "../src/pages/system/NewChannelWizard";
import { SettingsPage } from "../src/pages/system/SettingsPage";
import type {
  ModelGatewayChannelDiscoverResult,
  ModelGatewayConnectionCheck,
  ModelGatewayControlSnapshot
} from "../src/types";

const emptyControl: ModelGatewayControlSnapshot = {
  revision: "revision-1",
  admin_required: true,
  connections: [],
  deployments: [],
  routes: []
};

function connectionCheck(
  level: "ok" | "error",
  detail: string,
  models: string[] = []
): ModelGatewayConnectionCheck {
  return {
    mode: "discovery",
    summary: { [level]: 1 },
    connections: [
      {
        connection_id: "deepseek",
        status: level,
        level,
        detail,
        discovered_model_count: models.length,
        discovered_models: models
      }
    ]
  };
}

function discovery(models: string[]): ModelGatewayChannelDiscoverResult {
  return {
    valid: true,
    persisted: false,
    revision: "revision-1",
    candidate: {
      connection_id: "",
      channel_operator: "deepseek",
      base_url: "https://api.deepseek.com",
      adapter: "deepseek",
      auth_type: "bearer",
      allowed_private_networks: [],
      models_endpoint: "/models"
    },
    models: models.map((id) => ({ id, model_author: "unknown", aliases: [] })),
    report: connectionCheck("ok", "连接正常", models)
  };
}

describe("first-run setup", () => {
  it("only asks a new user for the gateway key", async () => {
    const user = userEvent.setup();
    render(
      <SettingsPage
        settings={{ apiBaseUrl: "http://localhost:2026", apiKey: "", userId: "default" }}
        onSave={vi.fn()}
        notify={vi.fn()}
      />
    );

    expect(screen.getByText(/已检测到本地服务/)).toBeInTheDocument();
    expect(screen.queryByLabelText("服务地址")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("用户 ID")).not.toBeInTheDocument();

    const continueButton = screen.getByRole("button", { name: "验证并继续" });
    expect(continueButton).toBeDisabled();
    await user.type(screen.getByLabelText("访问密钥"), "valid-gateway-key");
    expect(continueButton).toBeEnabled();
  });

  it("keeps rejected candidate discovery entirely in memory and lets the user retry", async () => {
    const user = userEvent.setup();
    const discoverProviderChannel = vi
      .fn()
      .mockRejectedValueOnce(new Error("API Key 无效"))
      .mockResolvedValueOnce(discovery(["deepseek-chat"]));
    const validateProviderChannelBundle = vi.fn();
    const applyProviderChannelBundle = vi.fn();
    const api = {
      discoverProviderChannel,
      validateProviderChannelBundle,
      applyProviderChannelBundle
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard
        api={api}
        adminKey="valid-admin-key"
        control={emptyControl}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />
    );

    await user.type(screen.getByPlaceholderText("sk-..."), "rejected-key");
    await user.click(screen.getByRole("button", { name: "只读发现模型" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("均未保存");
    expect(screen.queryByLabelText("聊天模型")).not.toBeInTheDocument();
    expect(validateProviderChannelBundle).not.toHaveBeenCalled();
    expect(applyProviderChannelBundle).not.toHaveBeenCalled();

    const keyInput = screen.getByPlaceholderText("sk-...");
    await user.clear(keyInput);
    await user.type(keyInput, "corrected-key");
    await user.click(screen.getByRole("button", { name: "只读发现模型" }));

    const model = await screen.findByLabelText("聊天模型");
    expect(model).toHaveValue("deepseek-chat");
    expect(discoverProviderChannel).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        revision: "revision-1",
        candidate_key: "corrected-key",
        channel_operator: "deepseek"
      }),
      "valid-admin-key"
    );
  });

  it("validates then atomically applies one bundle while existing routes default to keep", async () => {
    const user = userEvent.setup();
    const control: ModelGatewayControlSnapshot = {
      ...emptyControl,
      routes: [
        {
          id: "memory.chat",
          kind: "chat",
          targets: ["existing-chat"],
          required_capabilities: [],
          max_attempts: 2,
          fallback_scope: "any_channel",
          enabled: true
        }
      ]
    };
    const validateProviderChannelBundle = vi.fn().mockResolvedValue({
      valid: true,
      applied: false,
      connection_id: "deepseek-account",
      deployment_ids: ["deepseek-chat"],
      changed_routes: ["memory.extract"],
      revision: "revision-1",
      discovery: connectionCheck("ok", "连接正常", ["deepseek-chat"])
    });
    const applyProviderChannelBundle = vi.fn().mockResolvedValue({
      valid: true,
      applied: true,
      connection_id: "deepseek-account",
      deployment_ids: ["deepseek-chat"],
      changed_routes: ["memory.extract"],
      revision: "revision-2",
      discovery: connectionCheck("ok", "连接正常", ["deepseek-chat"])
    });
    const api = {
      discoverProviderChannel: vi.fn().mockResolvedValue(discovery(["deepseek-chat"])),
      validateProviderChannelBundle,
      applyProviderChannelBundle
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard
        api={api}
        adminKey="valid-admin-key"
        control={control}
        onClose={vi.fn()}
        onCompleted={vi.fn()}
      />
    );

    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "只读发现模型" }));
    await screen.findByLabelText("聊天模型");
    expect(screen.getByLabelText("现有文本路由")).toHaveValue("keep");

    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    expect(await screen.findByText(/完整配置校验通过/)).toBeInTheDocument();
    const bundle = validateProviderChannelBundle.mock.calls[0][0];
    expect(bundle.connection.secret).toBe("candidate-key");
    expect(bundle.deployments[0].model_author).toBe("unknown");
    expect(bundle.routes.find((route: { id: string }) => route.id === "memory.chat")).toMatchObject({
      operation: "keep",
      targets: [],
      fallback_scope: "any_channel",
      max_attempts: 2
    });
    expect(bundle.routes.find((route: { id: string }) => route.id === "memory.extract")).toMatchObject({
      operation: "replace",
      targets: ["$0"],
      fallback_scope: "none",
      max_attempts: 1
    });

    await user.click(screen.getByRole("button", { name: "确认并原子应用" }));
    expect(await screen.findByText("模型配置完成")).toBeInTheDocument();
    expect(applyProviderChannelBundle).toHaveBeenCalledTimes(1);
  });

  it("explains reindexing before replacing an embedding space", async () => {
    const user = userEvent.setup();
    const control: ModelGatewayControlSnapshot = {
      ...emptyControl,
      deployments: [
        {
          id: "old-embedding",
          connection: "old-channel",
          upstream_model: "embed-v3",
          model_author: "unknown",
          model_family: "",
          kind: "embedding",
          adapter_profile: "inherit",
          capabilities: {},
          dimensions: 768,
          embedding_space: "old.space:768",
          enabled: true
        }
      ],
      routes: [
        {
          id: "memory.embedding",
          kind: "embedding",
          targets: ["old-embedding"],
          required_capabilities: [],
          max_attempts: 1,
          fallback_scope: "none",
          enabled: true
        }
      ]
    };
    const api = {
      discoverProviderChannel: vi.fn().mockResolvedValue(discovery(["chat-v1", "embed-v4"])),
      validateProviderChannelBundle: vi.fn(),
      applyProviderChannelBundle: vi.fn()
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard api={api} adminKey="admin" control={control} onClose={vi.fn()} onCompleted={vi.fn()} />
    );
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "只读发现模型" }));
    await user.selectOptions(await screen.findByLabelText("聊天模型"), "chat-v1");
    await user.click(screen.getByLabelText("同时保存一个向量模型（可选）"));
    await user.type(screen.getByPlaceholderText("精确 embedding 模型 ID"), "embed-v4");
    await user.type(screen.getByPlaceholderText("例如 1024"), "1024");
    await user.selectOptions(screen.getByLabelText("现有向量路由"), "replace");

    expect(screen.getByRole("alert")).toHaveTextContent("已有记忆和知识向量必须完整重索引");
    expect(screen.getByRole("alert")).toHaveTextContent("old.space:768 / 768 维");
  });

  it("treats an apply timeout as ambiguous and tells the user to refresh before retrying", async () => {
    const user = userEvent.setup();
    const api = {
      discoverProviderChannel: vi.fn().mockResolvedValue(discovery(["deepseek-chat"])),
      validateProviderChannelBundle: vi.fn().mockResolvedValue({
        valid: true,
        applied: false,
        connection_id: "deepseek-account",
        deployment_ids: ["deepseek-chat"],
        changed_routes: ["memory.chat"],
        revision: "revision-1",
        discovery: connectionCheck("ok", "连接正常", ["deepseek-chat"])
      }),
      applyProviderChannelBundle: vi.fn().mockRejectedValue(new Error("请求超时"))
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard api={api} adminKey="admin" control={emptyControl} onClose={vi.fn()} onCompleted={vi.fn()} />
    );
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "只读发现模型" }));
    await screen.findByLabelText("聊天模型");
    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    await screen.findByText(/完整配置校验通过/);
    await user.click(screen.getByRole("button", { name: "确认并原子应用" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("整套提交可能已经生效");
    expect(screen.getByRole("alert")).toHaveTextContent("请先刷新配置确认，勿直接重试");
    expect(screen.queryByText("模型配置完成")).not.toBeInTheDocument();
  });
});
