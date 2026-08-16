import { expect, test } from "@playwright/test";
import {
  installFakeApi,
  LOGIN_EXCHANGED_TOKEN,
  VALID_LOGIN_CODE
} from "./fakeApi";

test("one-time login link exchanges the code and enters the console", async ({ page }) => {
  const api = await installFakeApi(page);

  // 首启：不预置任何凭据，直接带 #login=<code> 打开。
  await page.goto(`/ui/#login=${VALID_LOGIN_CODE}`);

  // 成功：直接进入主站（模型配置引导），而不是「输入访问密钥」门槛或「页面不存在」。
  await expect(page.getByRole("heading", { name: "连接一个模型渠道" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "输入访问密钥" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "页面不存在" })).toHaveCount(0);

  // 交换来的 console token 已写入本地存储。
  expect(
    await page.evaluate(() => localStorage.getItem("memory-console.gatewayApiKey"))
  ).toBe(LOGIN_EXCHANGED_TOKEN);

  // code 被立即从 URL 抹掉，不留在地址栏或历史记录里。
  expect(page.url()).not.toContain("login=");
  expect(page.url()).not.toContain(VALID_LOGIN_CODE);

  // exchange 只发一次（StrictMode 双跑被拦截）、body 为 {code} 且不带任何凭据头。
  const exchanges = api.calls.filter(
    (call) => call.pathname === "/auth/console-login-exchange"
  );
  expect(exchanges).toHaveLength(1);
  expect(exchanges[0].method).toBe("POST");
  expect(exchanges[0].body).toEqual({ code: VALID_LOGIN_CODE });
  expect(exchanges[0].authorizationPresent).toBe(false);

  expect(api.unknownApiPaths).toEqual([]);
});

test("expired login link falls back to manual token entry with a notice", async ({ page }) => {
  const api = await installFakeApi(page);

  await page.goto("/ui/#login=mgc_expired_or_reused_code_0000000000");

  // 失败：落回既有首启填 key 页，并显示失效提示；不会显示「页面不存在」。
  await expect(page.getByRole("heading", { name: "输入访问密钥" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(
    "登录链接已失效，请重新运行 memgw open"
  );
  await expect(page.getByRole("heading", { name: "页面不存在" })).toHaveCount(0);

  // 失效的 code 同样被从 URL 抹掉。
  expect(page.url()).not.toContain("login=");

  // 没有写入任何密钥。
  expect(
    await page.evaluate(() => localStorage.getItem("memory-console.gatewayApiKey"))
  ).toBeNull();

  // 手动粘贴 gateway.txt 的兜底流程原样可用。
  const consoleToken = "mgw_firstconsole_synthetic_console_secret_000000000000";
  await page.getByLabel("访问密钥").fill(consoleToken);
  await page.getByRole("button", { name: "验证并继续" }).click();
  await expect(page.getByRole("heading", { name: "连接一个模型渠道" })).toBeVisible();
  expect(
    await page.evaluate(() => localStorage.getItem("memory-console.gatewayApiKey"))
  ).toBe(consoleToken);

  expect(api.unknownApiPaths).toEqual([]);
});
