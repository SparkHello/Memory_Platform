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

async function pickDeepseek(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("radio", { name: /DeepSeek 官方/ }));
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

    expect(screen.getByRole("heading", { name: "输入登录密钥" })).toBeInTheDocument();
    expect(screen.getByText(/已检测到本地服务/)).toBeInTheDocument();
    expect(screen.getAllByText(/credentials\/gateway\.txt/).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("服务地址")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("用户 ID")).not.toBeInTheDocument();

    const continueButton = screen.getByRole("button", { name: "验证并继续" });
    expect(continueButton).toBeDisabled();
    await user.type(screen.getByLabelText("登录密钥"), "valid-gateway-key");
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

    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "rejected-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "密钥和渠道都还没保存"
    );
    expect(screen.queryByLabelText("聊天模型")).not.toBeInTheDocument();
    expect(validateProviderChannelBundle).not.toHaveBeenCalled();
    expect(applyProviderChannelBundle).not.toHaveBeenCalled();

    const keyInput = screen.getByPlaceholderText("sk-...");
    await user.clear(keyInput);
    await user.type(keyInput, "corrected-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));

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

    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));
    await screen.findByLabelText("聊天模型");
    expect(screen.getByLabelText("现有文本路由")).toHaveValue("keep");

    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    expect(await screen.findByText(/配置检查通过/)).toBeInTheDocument();
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

    await user.click(screen.getByRole("button", { name: "确认并保存" }));
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
    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));
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
    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));
    await screen.findByLabelText("聊天模型");
    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    await screen.findByText(/配置检查通过/);
    await user.click(screen.getByRole("button", { name: "确认并保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("整套提交可能已经生效");
    expect(screen.getByRole("alert")).toHaveTextContent("请先刷新配置确认，勿直接重试");
    expect(screen.queryByText("模型配置完成")).not.toBeInTheDocument();
  });

  it("surfaces the TUN fake-ip opt-in beside the discovery error", async () => {
    const user = userEvent.setup();
    const api = {
      discoverProviderChannel: vi.fn().mockRejectedValue(
        new Error("base_url hostname 解析到未显式允许的本地或私有地址（dashscope.aliyuncs.com → 198.18.4.17）")
      )
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard api={api} adminKey="admin" control={emptyControl} onClose={vi.fn()} onCompleted={vi.fn()} />
    );
    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("使用 Clash/Surge 等 TUN fake-ip");
    const boxes = screen.getAllByRole("checkbox", { name: /使用 Clash\/Surge 等 TUN fake-ip/ });
    expect(boxes.length).toBeGreaterThanOrEqual(1);
    expect(boxes[0]).toBeVisible();
  });

  it("hides embedding model IDs from the chat dropdown", async () => {
    const user = userEvent.setup();
    const api = {
      discoverProviderChannel: vi.fn().mockResolvedValue(
        discovery(["deepseek-chat", "qwen3.7-text-embedding", "qwen-tts-flash"])
      )
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard api={api} adminKey="admin" control={emptyControl} onClose={vi.fn()} onCompleted={vi.fn()} />
    );
    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));

    const chatSelect = await screen.findByLabelText("聊天模型");
    expect(chatSelect).toHaveTextContent("deepseek-chat");
    expect(chatSelect).not.toHaveTextContent("qwen3.7-text-embedding");
    expect(chatSelect).not.toHaveTextContent("qwen-tts-flash");
    expect(screen.getByText(/已隐藏 2 个嵌入\/语音\/图像模型/)).toBeInTheDocument();
  });

  it("reuses an existing chat token and re-embeds after enabling vectors", async () => {
    const user = userEvent.setup();
    const createAuthToken = vi.fn();
    const reEmbedMemories = vi.fn().mockResolvedValue({
      re_embedded: 2,
      memory_ids: ["a", "b"],
      failed_ids: []
    });
    const api = {
      discoverProviderChannel: vi.fn().mockResolvedValue(discovery(["deepseek-chat", "text-embedding-v3"])),
      validateProviderChannelBundle: vi.fn().mockResolvedValue({
        valid: true,
        applied: false,
        connection_id: "deepseek-account",
        deployment_ids: ["deepseek-chat"],
        changed_routes: ["memory.chat", "memory.embedding"],
        revision: "revision-1",
        discovery: connectionCheck("ok", "连接正常", ["deepseek-chat"])
      }),
      applyProviderChannelBundle: vi.fn().mockResolvedValue({
        valid: true,
        applied: true,
        connection_id: "deepseek-account",
        deployment_ids: ["deepseek-chat"],
        changed_routes: ["memory.chat", "memory.embedding"],
        revision: "revision-2",
        discovery: connectionCheck("ok", "连接正常", ["deepseek-chat"])
      }),
      authTokens: vi.fn().mockResolvedValue({
        data: [
          {
            token_id: "existing",
            name: "laptop",
            user_id: "default",
            role: "chat",
            created_at: "2026-01-01T00:00:00Z",
            revoked_at: null,
            is_current: false,
            can_revoke: true
          }
        ],
        current_user_id: "default",
        legacy_key_enabled: false
      }),
      createAuthToken,
      reEmbedMemories
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard api={api} adminKey="admin" control={emptyControl} onClose={vi.fn()} onCompleted={vi.fn()} />
    );
    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));
    await user.click(screen.getByLabelText("同时保存一个向量模型（可选）"));
    await user.type(screen.getByPlaceholderText("精确 embedding 模型 ID"), "text-embedding-v3");
    await user.type(screen.getByPlaceholderText("例如 1024"), "1024");
    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    await screen.findByText(/配置检查通过/);
    await user.click(screen.getByRole("button", { name: "确认并保存" }));

    expect(await screen.findByText("模型配置完成")).toBeInTheDocument();
    expect(createAuthToken).not.toHaveBeenCalled();
    expect(reEmbedMemories).toHaveBeenCalledWith({ scan: true });
    expect(screen.getByText(/已为 2 条缺少当前空间向量的记忆补齐向量/)).toBeInTheDocument();
    expect(screen.getByText(/请到「客户端接入」使用已有聊天密钥/)).toBeInTheDocument();
  });

  it("defaults the embedding access point to the chat URL and only sends it when different", async () => {
    const user = userEvent.setup();
    const validateProviderChannelBundle = vi.fn().mockResolvedValue({
      valid: true,
      applied: false,
      connection_id: "deepseek-account",
      embedding_connection_id: "deepseek-embedding-account",
      deployment_ids: ["deepseek-chat", "deepseek-embed"],
      changed_routes: ["memory.chat", "memory.embedding"],
      revision: "revision-1",
      discovery: connectionCheck("ok", "连接正常", ["deepseek-chat"])
    });
    const api = {
      discoverProviderChannel: vi.fn().mockResolvedValue(discovery(["deepseek-chat"])),
      validateProviderChannelBundle,
      applyProviderChannelBundle: vi.fn()
    } as unknown as MemoryApi;

    render(
      <NewChannelWizard api={api} adminKey="admin" control={emptyControl} onClose={vi.fn()} onCompleted={vi.fn()} />
    );
    await pickDeepseek(user);
    await user.type(screen.getByPlaceholderText("sk-..."), "candidate-key");
    await user.click(screen.getByRole("button", { name: "列出可用模型" }));
    await user.click(screen.getByLabelText("同时保存一个向量模型（可选）"));
    await user.type(screen.getByPlaceholderText("精确 embedding 模型 ID"), "text-embedding-v3");
    await user.type(screen.getByPlaceholderText("例如 1024"), "1024");

    const embedUrl = screen.getByLabelText("向量接入点");
    expect(embedUrl).toHaveValue("https://api.deepseek.com");

    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    expect(await screen.findByText(/配置检查通过/)).toBeInTheDocument();
    expect(validateProviderChannelBundle.mock.calls[0][0].embedding_base_url).toBeUndefined();

    await user.clear(embedUrl);
    await user.type(embedUrl, "https://embed.deepseek.com/v1");
    await user.click(screen.getByRole("button", { name: "校验完整配置" }));
    expect(await screen.findByText(/向量模型会走单独接入点/)).toBeInTheDocument();
    expect(validateProviderChannelBundle.mock.calls[1][0].embedding_base_url).toBe(
      "https://embed.deepseek.com/v1"
    );
    expect(validateProviderChannelBundle.mock.calls[1][0].deployments[1].embedding_space).toContain(
      "embed.deepseek.com"
    );
  });
});
