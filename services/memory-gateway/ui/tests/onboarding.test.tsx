import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MemoryApi } from "../src/api";
import { NewChannelWizard } from "../src/pages/system/NewChannelWizard";
import { SettingsPage } from "../src/pages/system/SettingsPage";
import type {
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

  it("lets the user replace a rejected provider key before choosing a model", async () => {
    const user = userEvent.setup();
    const updateProviderSecret = vi.fn().mockResolvedValue({
      connection_id: "deepseek",
      configured: true
    });
    const checkProviderConnection = vi
      .fn()
      .mockResolvedValueOnce(connectionCheck("error", "API Key 无效"))
      .mockResolvedValueOnce(connectionCheck("ok", "连接正常", ["deepseek-chat"]));
    const api = {
      createProviderConnection: vi
        .fn()
        .mockResolvedValueOnce({
          valid: true,
          applied: false,
          connection_id: "deepseek",
          revision: "revision-1"
        })
        .mockResolvedValueOnce({
          valid: true,
          applied: true,
          connection_id: "deepseek",
          revision: "revision-2"
        }),
      updateProviderSecret,
      checkProviderConnection,
      applyProviderDeployments: vi.fn()
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
    await user.click(screen.getByRole("button", { name: "保存并检查" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("连接检查失败");
    expect(screen.queryByLabelText("聊天模型")).not.toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText("仅在需要替换时重新填写"),
      "corrected-key"
    );
    await user.click(screen.getByRole("button", { name: "保存新密钥并重新检查" }));

    const model = await screen.findByLabelText("聊天模型");
    expect(model).toHaveValue("deepseek-chat");
    expect(updateProviderSecret).toHaveBeenNthCalledWith(
      1,
      "deepseek",
      "rejected-key",
      "valid-admin-key"
    );
    expect(updateProviderSecret).toHaveBeenNthCalledWith(
      2,
      "deepseek",
      "corrected-key",
      "valid-admin-key"
    );
    expect(checkProviderConnection).toHaveBeenCalledTimes(2);
  });
});
