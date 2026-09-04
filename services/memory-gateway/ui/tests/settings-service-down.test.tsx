import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsPage, serviceDownMessage } from "../src/pages/system/SettingsPage";

// jsdom serves tests from http://localhost:3000, i.e. a loopback origin, exactly
// like the console on a phone (http://127.0.0.1:2026) or a desktop install.
const failedToFetch = () => new TypeError("Failed to fetch");

describe("service unreachable on the login page", () => {
  const originalFetch = globalThis.fetch;
  const originalUserAgent = navigator.userAgent;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    Object.defineProperty(navigator, "userAgent", { value: originalUserAgent, configurable: true });
  });

  it("tells a phone user the embedded service stopped instead of asking about the address", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(failedToFetch()) as unknown as typeof fetch;
    const user = userEvent.setup();
    render(
      <SettingsPage
        settings={{ apiBaseUrl: "http://localhost:3000", apiKey: "", userId: "default" }}
        onSave={vi.fn()}
        notify={vi.fn()}
        embedded
      />
    );
    await user.type(screen.getByLabelText("登录密钥"), "mgw_anything");
    await user.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() => expect(screen.getByText(/手机上的记忆服务没有响应/)).toBeInTheDocument());
    expect(screen.queryByText(/请确认服务地址与端口/)).not.toBeInTheDocument();
  });

  it("recognises an Android browser even before /health ever reported the embedded profile", () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Linux; Android 15; V2309A) AppleWebKit/537.36 Chrome/128 Mobile Safari/537.36",
      configurable: true
    });
    expect(serviceDownMessage(failedToFetch(), false)).toMatch(/回到 Memory Platform App/);
  });

  it("points desktop loopback installs at the service process", () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 Chrome/128 Safari/537.36",
      configurable: true
    });
    expect(serviceDownMessage(failedToFetch(), false)).toMatch(/memgw stack start/);
  });

  it("keeps the generic wording for non-network failures", () => {
    expect(serviceDownMessage(new Error("boom"), true)).toBe("boom");
  });
});
