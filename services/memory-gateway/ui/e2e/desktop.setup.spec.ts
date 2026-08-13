import { expect, test } from "@playwright/test";
import { installFakeApi } from "./fakeApi";

test("first Console token setup, expert mode, and interrupted channel draft stay safe", async ({ page }) => {
  const api = await installFakeApi(page);
  const viewport = page.viewportSize();
  expect(viewport).toEqual({ width: 1440, height: 900 });

  await page.goto("/ui/");
  await expect(page.getByRole("heading", { name: "输入访问密钥" })).toBeVisible();
  await expect(page.getByLabel("服务地址")).toHaveCount(0);
  await expect(page.getByLabel("用户 ID")).toHaveCount(0);

  const consoleToken = "mgw_firstconsole_synthetic_console_secret_000000000000";
  await page.getByLabel("访问密钥").fill(consoleToken);
  await page.getByRole("button", { name: "验证并继续" }).click();
  await expect(page.getByRole("heading", { name: "连接一个模型渠道" })).toBeVisible();
  expect(
    await page.evaluate(() => localStorage.getItem("memory-console.gatewayApiKey"))
  ).toBe(consoleToken);
  expect(
    api.calls.some(
      (call) => call.pathname === "/memories/report" && call.authorizationPresent
    )
  ).toBe(true);

  await page.getByLabel("Model Gateway admin 密钥").fill(
    "synthetic-admin-secret-that-is-never-persisted"
  );
  await page.getByRole("button", { name: "验证管理密钥" }).click();
  await expect(page.getByRole("heading", { name: "新建渠道" })).toBeVisible();

  const candidateKey = "synthetic-provider-candidate-key-never-persisted";
  await page.getByRole("radio", { name: /DeepSeek 官方/ }).click();
  await page.getByPlaceholder("sk-...").fill(candidateKey);
  await page.getByRole("button", { name: "只读发现模型" }).click();
  await expect(page.getByText(/候选渠道和密钥尚未保存/)).toBeVisible();
  // 已填写供应商 API Key 时关闭必须先确认，误触不会直接丢弃草稿。
  await page.getByRole("button", { name: "关闭新建渠道" }).click();
  const discardDialog = page
    .getByRole("dialog")
    .filter({ hasText: "丢弃未保存的渠道配置" });
  await expect(discardDialog).toBeVisible();
  await discardDialog.getByRole("button", { name: "继续编辑" }).click();
  await expect(page.getByRole("heading", { name: "新建渠道" })).toBeVisible();
  await page.getByRole("button", { name: "关闭新建渠道" }).click();
  await discardDialog.getByRole("button", { name: "丢弃并关闭" }).click();
  await expect(page.getByRole("heading", { name: "新建渠道" })).toHaveCount(0);

  expect(
    api.calls.filter((call) => call.pathname === "/providers/channels/discover")
  ).toHaveLength(1);
  expect(
    api.calls.filter((call) => call.pathname === "/providers/channel-bundles/validate")
  ).toHaveLength(0);
  expect(
    api.calls.filter((call) => call.pathname === "/providers/channel-bundles/apply")
  ).toHaveLength(0);
  const storedValues = await page.evaluate(() =>
    Array.from({ length: localStorage.length }, (_, index) => {
      const key = localStorage.key(index);
      return key ? localStorage.getItem(key) : null;
    })
  );
  expect(storedValues).not.toContain(candidateKey);
  expect(storedValues).not.toContain("synthetic-admin-secret-that-is-never-persisted");

  await expect(page.getByRole("button", { name: "评测闭环" })).toHaveCount(0);
  await page.getByRole("button", { name: "切换到专家模式" }).click();
  await expect(page.getByRole("button", { name: "评测闭环" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "返回简洁模式" })).toBeVisible();
  await expect(page.getByRole("button", { name: "评测闭环" })).toBeVisible();
  await page.getByRole("button", { name: "返回简洁模式" }).click();
  await expect(page.getByRole("button", { name: "评测闭环" })).toHaveCount(0);

  expect(api.blockedExternalUrls).toEqual([]);
  expect(api.unknownApiPaths).toEqual([]);
});
