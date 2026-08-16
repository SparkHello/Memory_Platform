import { expect, test } from "@playwright/test";
import { installFakeApi, seedConsoleSettings } from "./fakeApi";

declare global {
  interface Window {
    __CONSOLE_CRASH_PAGE__?: boolean;
  }
}

const ORIGIN = "http://127.0.0.1:4173";

test("渲染崩溃不白屏：错误页可返回工作室且侧栏可用", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN);
  await installFakeApi(page);
  // 标记存活性：若发生整页刷新，该标记会被清掉。
  await page.addInitScript(() => {
    (window as unknown as { __survived?: boolean }).__survived = true;
  });

  await page.goto(`${ORIGIN}/ui/#/memories`);
  await expect(page.getByRole("heading", { name: "记忆库" })).toBeVisible();

  // 开启崩溃探针后切页触发渲染异常（探针在页面边界内抛错）。
  await page.evaluate(() => {
    window.__CONSOLE_CRASH_PAGE__ = true;
    window.location.hash = "#/usage";
  });
  await expect(page.getByText("页面出现错误")).toBeVisible();
  // 侧栏在边界外，仍然可用。
  await expect(page.getByRole("button", { name: /记忆库/ }).first()).toBeVisible();

  await page.evaluate(() => {
    window.__CONSOLE_CRASH_PAGE__ = false;
  });
  await page.getByRole("button", { name: "返回工作室" }).click();
  await expect(page.locator(".workspace").getByText("记忆工作室", { exact: true })).toBeVisible();
  expect(page.url()).toContain("#/studio");
  expect(await page.evaluate(() => (window as unknown as { __survived?: boolean }).__survived)).toBe(true);
});

test("未知 hash 显示「页面不存在」并引导回工作室", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN);
  await installFakeApi(page);

  await page.goto(`${ORIGIN}/ui/#/definitely-not-a-page`);
  await expect(page.getByText("页面不存在")).toBeVisible();
  await expect(page.getByText("#/definitely-not-a-page")).toBeVisible();

  await page.getByRole("button", { name: "返回工作室" }).click();
  await expect(page.locator(".workspace").getByText("记忆工作室", { exact: true })).toBeVisible();
  expect(page.url()).toContain("#/studio");
});

// 简洁模式（含直达 hash 打开的高级页面）一律不泄露内部实现术语。
const FORBIDDEN_SIMPLE_TERMS = ["memory.chat", "memory.extract", "deployment", "逐字片段", "分叉点"];

test("简洁模式：遍历全部入口与直达高级页均不出现内部术语", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN); // uiMode=simple
  await installFakeApi(page);

  await page.goto(`${ORIGIN}/ui/#/studio`);
  await expect(
    page.locator(".sidebar").getByRole("button", { name: "切换到专家模式" })
  ).toBeVisible();

  // SIMPLE_NAV 的六个入口通过侧栏真实点击遍历。
  const sidebarTour: Array<{ nav: string; hash: string }> = [
    { nav: "记忆工作室", hash: "#/studio" },
    { nav: "记忆库", hash: "#/memories" },
    { nav: "知识库", hash: "#/knowledge" },
    { nav: "模型与路由", hash: "#/providers" },
    { nav: "报告与备份", hash: "#/reports" },
    { nav: "接入信息", hash: "#/integration" }
  ];
  // 侧栏不展示的高级页面用 hash 直达，同样受简洁模式约束。
  const directHashes = ["#/knowledge-search", "#/recent"];

  const assertNoForbiddenTerms = async (where: string) => {
    // 等页面异步内容落定（LoadingBlock 的旋转图标消失）再取全文。
    await expect(page.locator(".content-area .state-block .spin")).toHaveCount(0);
    const bodyText = await page.locator("body").innerText();
    for (const term of FORBIDDEN_SIMPLE_TERMS) {
      expect(bodyText, `${where} 不应出现内部术语「${term}」`).not.toContain(term);
    }
  };

  for (const target of sidebarTour) {
    await page.locator(".sidebar").getByRole("button", { name: target.nav }).first().click();
    await expect(page).toHaveURL(new RegExp(`${target.hash.replace("/", "\\/")}$`));
    await expect(page.locator(".topbar-title strong")).toHaveText(target.nav);
    await assertNoForbiddenTerms(target.hash);
  }

  for (const hash of directHashes) {
    await page.evaluate((next) => {
      window.location.hash = next;
    }, hash);
    await expect(page).toHaveURL(new RegExp(`${hash.replace("/", "\\/")}$`));
    await assertNoForbiddenTerms(hash);
  }
});

test("warning 横幅关闭后写入 localStorage，切换页面与重新载入都不再出现", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN);
  const api = await installFakeApi(page);

  await page.goto(`${ORIGIN}/ui/#/studio`);
  const pill = page.locator(".status-pill");
  await expect(pill).toContainText("服务在线 · 待配置模型");

  await page.getByRole("button", { name: "关闭状态提示" }).click();
  await expect(pill).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("memory-console.dismissedStatus"))).toBe(
    "服务在线 · 待配置模型"
  );

  // 切到另一个 warning 可操作页面：同消息横幅保持关闭。
  await page.locator(".sidebar").getByRole("button", { name: "模型与路由" }).click();
  await expect(page).toHaveURL(/#\/providers$/);
  await expect(page.locator(".topbar-title strong")).toHaveText("模型与路由");
  await expect(pill).toHaveCount(0);

  // 无关页面本就不显示 warning 横幅（侧栏「记忆库」带回收站角标，可访问名称含角标数字，不能 exact 匹配）。
  await page.locator(".sidebar").getByRole("button", { name: "记忆库" }).click();
  await expect(page).toHaveURL(/#\/memories$/);
  await expect(pill).toHaveCount(0);

  // 重新载入整页：dismissedStatus 持久生效（先等服务检查完成，避免「检查中」阶段误判）。
  const statusCalls = api.calls.filter((call) => call.pathname === "/providers/status").length;
  await page.reload();
  await expect(page.locator(".topbar-title strong")).toHaveText("记忆库");
  await expect
    .poll(() => api.calls.filter((call) => call.pathname === "/providers/status").length)
    .toBeGreaterThan(statusCalls);
  await expect(pill).toHaveCount(0);
});

test("角标刷新失败：侧栏提示并可重试恢复", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN);
  const api = await installFakeApi(page);
  api.failMemoryReport = true;

  await page.goto(`${ORIGIN}/ui/#/studio`);
  await expect(page.getByText("待办角标暂时无法更新")).toBeVisible();

  api.failMemoryReport = false;
  await page.getByRole("button", { name: "重试", exact: true }).first().click();
  await expect(page.getByText("待办角标暂时无法更新")).toHaveCount(0);
});
