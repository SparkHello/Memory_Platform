import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryApi } from "../src/api";
import { DeveloperPage } from "../src/pages/system/DeveloperPage";
import type {
  AuthTokenCreateResult,
  AuthTokenListResult,
  AuthTokenRecord
} from "../src/types";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(document, "execCommand");

function restoreProperty(target: object, key: PropertyKey, descriptor?: PropertyDescriptor) {
  if (descriptor) Object.defineProperty(target, key, descriptor);
  else Reflect.deleteProperty(target, key);
}

afterEach(() => {
  vi.unstubAllGlobals();
  restoreProperty(navigator, "clipboard", clipboardDescriptor);
  restoreProperty(document, "execCommand", execCommandDescriptor);
});

const baseList: AuthTokenListResult = {
  data: [],
  current_user_id: "alice",
  legacy_key_enabled: true,
  authenticated_with_legacy_key: true,
  allowed_create_roles: ["chat", "mcp"]
};

function tokenRecord(overrides: Partial<AuthTokenRecord> = {}): AuthTokenRecord {
  return {
    token_id: "0123456789abcdef",
    name: "Alice phone",
    user_id: "alice",
    role: "chat",
    created_at: "2026-08-09T10:00:00+00:00",
    last_used_at: "2026-08-09T10:05:00+00:00",
    revoked_at: null,
    is_current: false,
    can_revoke: true,
    revoke_block_reason: null,
    ...overrides
  };
}

function renderPage(
  apiOverrides: Partial<MemoryApi> = {},
  confirm = vi.fn().mockResolvedValue(true)
) {
  const api = {
    authTokens: vi.fn().mockResolvedValue(baseList),
    createAuthToken: vi.fn(),
    revokeAuthToken: vi.fn(),
    ...apiOverrides
  } as unknown as MemoryApi;
  const notify = vi.fn();
  render(
    <DeveloperPage
      api={api}
      settings={{
        apiBaseUrl: "http://memory.lan:2026",
        apiKey: "console-login-secret-must-never-render",
        userId: "alice"
      }}
      notify={notify}
      confirm={confirm}
    />
  );
  return { api, notify, confirm };
}

describe("device-token integration page", () => {
  it("shows a newly created token once and copies through the LAN HTTP fallback", async () => {
    const user = userEvent.setup();
    const rawToken = "mgw_0123456789abcdef_one_time_secret_value_123456789";
    const created: AuthTokenCreateResult = {
      token: rawToken,
      record: tokenRecord()
    };
    const createAuthToken = vi.fn().mockResolvedValue(created);
    const { notify } = renderPage({ createAuthToken } as Partial<MemoryApi>);

    expect(
      await screen.findByText("当前浏览器仍在使用旧共享访问密钥")
    ).toBeInTheDocument();
    expect(screen.queryByText("console-login-secret-must-never-render")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("设备或客户端名称"), "Alice phone");
    await user.click(screen.getByRole("button", { name: "创建 chat token" }));

    expect(createAuthToken).toHaveBeenCalledWith("Alice phone", "chat", {
      memoryAccess: "read-write"
    });
    // 一次性 token 默认掩码，点击「显示 token」后才出现明文。
    expect(await screen.findByText("只显示这一次")).toBeInTheDocument();
    expect(screen.queryByText(rawToken)).not.toBeInTheDocument();
    expect(
      screen.getByText(`${rawToken.slice(0, 12)}…${rawToken.slice(-4)}`)
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "显示 token" }));
    expect(screen.getByText(rawToken)).toBeInTheDocument();

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new DOMException("NotAllowedError")) }
    });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand
    });

    await user.click(screen.getByRole("button", { name: "复制完整接入配置" }));
    await waitFor(() => expect(execCommand).toHaveBeenCalledWith("copy"));
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("包含一次性 token"),
      "success"
    );

    await user.click(screen.getByRole("button", { name: "我已保存" }));
    expect(screen.queryByText(rawToken)).not.toBeInTheDocument();
    expect(screen.getByText("Alice phone")).toBeInTheDocument();
  });

  it("guides MCP creation and revokes one device independently", async () => {
    const user = userEvent.setup();
    const active = tokenRecord({ role: "mcp", name: "Work MCP" });
    const revoked = { ...active, revoked_at: "2026-08-09T11:00:00+00:00" };
    const authTokens = vi.fn().mockResolvedValue({
      ...baseList,
      authenticated_with_legacy_key: false,
      data: [active]
    });
    const revokeAuthToken = vi.fn().mockResolvedValue({
      revoked: true,
      already_revoked: false,
      record: revoked
    });
    const confirm = vi.fn().mockResolvedValue(true);
    renderPage(
      { authTokens, revokeAuthToken } as Partial<MemoryApi>,
      confirm
    );

    expect(await screen.findByText("Work MCP")).toBeInTheDocument();
    expect(screen.getAllByText("MCP").length).toBeGreaterThan(0);
    expect(screen.getByText("MCP 工具接入")).toBeInTheDocument();
    expect(screen.getByText("最近使用")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "撤销" }));

    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "danger", confirmLabel: "撤销 token" })
    );
    expect(revokeAuthToken).toHaveBeenCalledWith(active.token_id);
    expect(await screen.findAllByText("已撤销")).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "已撤销" })).toBeDisabled();
  });

  it("disables revocation for the last active Console credential", async () => {
    const lastConsole = tokenRecord({
      role: "console",
      name: "Current browser",
      is_current: true,
      can_revoke: false,
      revoke_block_reason: "last_active_console_token"
    });
    const authTokens = vi.fn().mockResolvedValue({
      ...baseList,
      authenticated_with_legacy_key: false,
      data: [lastConsole]
    });
    const revokeAuthToken = vi.fn();
    renderPage({ authTokens, revokeAuthToken } as Partial<MemoryApi>);

    expect(await screen.findByText("Current browser")).toBeInTheDocument();
    expect(screen.getByText(/最后一个可用 Console token/)).toBeInTheDocument();
    expect(screen.getByText("memgw token create --role console")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "需保留" })).toBeDisabled();
    expect(revokeAuthToken).not.toHaveBeenCalled();
  });
});

describe("device-token API client", () => {
  it("uses the console endpoints without accepting a user or console role argument", async () => {
    const created: AuthTokenCreateResult = {
      token: "mgw_0123456789abcdef_one_time_secret_value_123456789",
      record: tokenRecord()
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(baseList), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(created), {
          status: 201,
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ revoked: true, already_revoked: false, record: tokenRecord() }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      );
    vi.stubGlobal("fetch", fetchMock);
    const api = new MemoryApi({
      apiBaseUrl: "http://memory.lan:2026",
      apiKey: "console-token",
      userId: "alice"
    });

    await api.authTokens();
    await api.createAuthToken("Alice phone", "chat");
    await api.revokeAuthToken("0123456789abcdef");

    const [listUrl, listInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(listUrl).toBe("http://memory.lan:2026/auth/tokens");
    expect(listInit.method).toBe("GET");
    const [createUrl, createInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(createUrl).toBe("http://memory.lan:2026/auth/tokens");
    expect(createInit.method).toBe("POST");
    expect(JSON.parse(String(createInit.body))).toEqual({
      name: "Alice phone",
      role: "chat"
    });
    const [deleteUrl, deleteInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(deleteUrl).toBe(
      "http://memory.lan:2026/auth/tokens/0123456789abcdef"
    );
    expect(deleteInit.method).toBe("DELETE");
  });
});
