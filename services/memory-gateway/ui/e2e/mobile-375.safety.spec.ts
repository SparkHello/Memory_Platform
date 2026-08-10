import { expect, test } from "@playwright/test";
import { installFakeApi, seedConsoleSettings } from "./fakeApi";

declare global {
  interface Window {
    __clipboardWriteAttempts?: number;
    __execCommandCopies?: number;
  }
}

test("LAN clipboard fallback, selection export, and purge preview are safe on compact mobile", async ({ page }) => {
  const lanOrigin = "http://memory-platform.test:4173";
  await seedConsoleSettings(page, lanOrigin);
  await page.addInitScript(() => {
    window.__clipboardWriteAttempts = 0;
    window.__execCommandCopies = 0;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async () => {
          window.__clipboardWriteAttempts = (window.__clipboardWriteAttempts || 0) + 1;
          throw new DOMException("Clipboard unavailable on LAN HTTP", "NotAllowedError");
        }
      }
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: (command: string) => {
        if (command === "copy") window.__execCommandCopies = (window.__execCommandCopies || 0) + 1;
        return command === "copy";
      }
    });
  });
  const api = await installFakeApi(page);
  expect(page.viewportSize()).toEqual({ width: 375, height: 667 });

  await page.goto(`${lanOrigin}/ui/#/integration`);
  expect(await page.evaluate(() => window.isSecureContext)).toBe(false);
  await expect(page.getByRole("heading", { name: "客户端接入" })).toBeVisible();
  await page.getByRole("button", { name: "复制端点" }).click();
  await expect(page.getByText("常用 Console REST 端点已复制")).toBeVisible();
  expect(
    await page.evaluate(() => ({
      clipboard: window.__clipboardWriteAttempts,
      fallback: window.__execCommandCopies
    }))
  ).toEqual({ clipboard: 1, fallback: 1 });

  await page.goto(`${lanOrigin}/ui/#/memories`);
  await expect(page.getByRole("heading", { name: "记忆库" })).toBeVisible();
  expect(await horizontalOverflow(page)).toEqual([]);
  await page.keyboard.press("/");
  await expect(page.getByLabel("搜索记忆")).toBeFocused();

  await page.getByRole("checkbox", { name: /选择记忆：合成记忆 01/ }).click();
  await expect(page.getByRole("region", { name: "批量操作" })).toContainText("已选 1 条");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出所选" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^memory-selected-\d{4}-\d{2}-\d{2}\.json$/);
  expect(api.exportBodies).toEqual([{ memory_ids: ["active-memory-01"] }]);
  expect(
    api.calls.filter(
      (call) => call.pathname === "/memories/export" && call.pathname !== "/memories/export/selection"
    )
  ).toHaveLength(0);

  await page.getByRole("button", { name: "回收站", exact: true }).click();
  await expect(
    page.locator(".memory-card-list").getByText("合成回收站记忆 01", { exact: false })
  ).toBeVisible();
  await page.getByRole("checkbox", { name: /选择记忆：合成回收站记忆 01/ }).click();
  await page.getByRole("button", { name: "永久删除" }).click();
  const cancel = page.getByRole("button", { name: "取消，保留记忆" });
  await expect(page.getByRole("dialog", { name: "核对永久删除影响" })).toContainText(
    "实际将永久删除 2 条"
  );
  await expect(page.getByRole("dialog", { name: "核对永久删除影响" })).toContainText("Core 影响");
  await expect(cancel).toBeFocused();
  expect(api.purgePreviewBodies).toEqual([{ memory_ids: ["deleted-memory-01"] }]);
  expect(api.purgeCommitBodies).toEqual([]);

  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "核对永久删除影响" })).toHaveCount(0);
  expect(api.purgeCommitBodies).toEqual([]);

  await page.getByRole("button", { name: "永久删除" }).click();
  await expect(cancel).toBeFocused();
  await page.getByRole("button", { name: "按以上范围永久删除" }).click();
  await expect(page.getByText("已永久删除 2 条记忆")).toBeVisible();
  expect(api.purgeCommitBodies).toEqual([
    {
      memory_ids: ["deleted-memory-01"],
      fingerprint: "b".repeat(64),
      preview_token: "synthetic-signed-preview-token"
    }
  ]);
  expect(
    api.calls.filter((call) => /^\/memories\/deleted\/[^/]+\/purge$/.test(call.pathname))
  ).toHaveLength(0);

  expect(api.blockedExternalUrls).toEqual([]);
  expect(api.unknownApiPaths).toEqual([]);
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
