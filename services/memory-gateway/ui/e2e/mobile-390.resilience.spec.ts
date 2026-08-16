import { expect, test } from "@playwright/test";
import { installFakeApi, seedConsoleSettings } from "./fakeApi";

declare global {
  interface Window {
    __CONSOLE_CRASH_PAGE__?: boolean;
  }
}

const ORIGIN = "http://127.0.0.1:4173";

test("移动端 390：错误页与「页面不存在」面板无横向溢出，底部导航可用", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN);
  const api = await installFakeApi(page);
  expect(page.viewportSize()).toEqual({ width: 390, height: 844 });

  // 「页面不存在」面板在移动端可读可点。
  await page.goto(`${ORIGIN}/ui/#/definitely-not-a-page`);
  await expect(page.getByText("页面不存在")).toBeVisible();
  expect(await horizontalOverflow(page)).toEqual([]);
  await page.getByRole("button", { name: "返回工作室" }).click();
  await expect(page.locator(".workspace").getByText("记忆工作室", { exact: true })).toBeVisible();

  // 渲染崩溃后错误页适配移动端，底部导航仍在。
  await page.evaluate(() => {
    window.__CONSOLE_CRASH_PAGE__ = true;
    window.location.hash = "#/usage";
  });
  await expect(page.getByText("页面出现错误")).toBeVisible();
  expect(await horizontalOverflow(page)).toEqual([]);
  await expect(page.getByRole("button", { name: "更多" })).toBeVisible();

  await page.evaluate(() => {
    window.__CONSOLE_CRASH_PAGE__ = false;
  });
  await page.getByRole("button", { name: "返回工作室" }).click();
  await expect(page.locator(".workspace").getByText("记忆工作室", { exact: true })).toBeVisible();

  expect(api.unknownApiPaths).toEqual([]);
  expect(api.blockedExternalUrls).toEqual([]);
});

async function horizontalOverflow(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    if (document.documentElement.scrollWidth <= document.documentElement.clientWidth) return [];
    return Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((element) => !element.closest(".ambient-field"))
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        className: element.className,
        right: Math.round(element.getBoundingClientRect().right),
        width: Math.round(element.getBoundingClientRect().width)
      }))
      .filter((item) => item.right > window.innerWidth + 1 && item.width > 0)
      .slice(0, 12);
  });
}
