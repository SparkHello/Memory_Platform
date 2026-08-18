import { expect, test } from "@playwright/test";
import { installFakeApi, seedConsoleSettings } from "./fakeApi";

test("mobile routes and repeated current destination reset real page scrolling", async ({ page }) => {
  await seedConsoleSettings(page, "http://127.0.0.1:4173");
  const api = await installFakeApi(page);
  expect(page.viewportSize()).toEqual({ width: 390, height: 844 });

  await page.goto("/ui/#/memories");
  await expect(page.getByRole("heading", { name: "记忆库" })).toBeVisible();
  await expect(
    page.locator(".memory-card-list").getByText("合成记忆 25", { exact: false })
  ).toBeVisible();
  expect(await horizontalOverflow(page)).toEqual([]);

  await page.keyboard.press("/");
  await expect(page.getByLabel("搜索记忆")).toBeFocused();
  await page.getByLabel("搜索记忆").press("Escape");
  await page.locator("body").click({ position: { x: 2, y: 2 } });

  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(300);
  const mobileNav = page.getByRole("navigation", { name: "移动端导航" });
  await mobileNav.getByRole("button", { name: "记忆库", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);

  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(300);
  // 移动底栏主导航现在是 工作室/记忆库/知识库/模型/接入（含首配需要的模型入口）。
  await mobileNav.getByRole("button", { name: "接入", exact: true }).click();
  await expect(page).toHaveURL(/#\/integration$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  expect(
    await page.evaluate(() => document.querySelector<HTMLElement>(".content-area")?.scrollTop || 0)
  ).toBe(0);

  expect(api.blockedExternalUrls).toEqual([]);
  expect(api.unknownApiPaths).toEqual([]);
});

test("390px 底栏五个主入口与更多保持单行，换行即失败", async ({ page }) => {
  await seedConsoleSettings(page, "http://127.0.0.1:4173");
  await installFakeApi(page);
  expect(page.viewportSize()).toEqual({ width: 390, height: 844 });

  await page.goto("/ui/#/memories");
  const mobileNav = page.getByRole("navigation", { name: "移动端导航" });
  await expect(mobileNav.getByRole("button", { name: "更多" })).toBeVisible();

  const labels = (await mobileNav.getByRole("button").allTextContents()).map((text) =>
    text.replace(/\s+/g, "")
  );
  expect(labels).toEqual(["工作室", "记忆库", "知识库", "模型", "接入", "更多"]);

  const boxes = await mobileNav.getByRole("button").evaluateAll((buttons) =>
    buttons.map((button) => {
      const rect = button.getBoundingClientRect();
      return {
        label: (button.textContent || "").replace(/\s+/g, ""),
        top: Math.round(rect.top),
        left: Math.round(rect.left)
      };
    })
  );
  expect(new Set(boxes.map((box) => box.top)).size, `底栏出现换行：${JSON.stringify(boxes)}`).toBe(1);

  const lefts = boxes.map((box) => box.left);
  expect(lefts, "底栏按钮应按从左到右排在同一行").toEqual(
    [...lefts].sort((left, right) => left - right)
  );
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
