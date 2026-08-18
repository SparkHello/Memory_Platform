import { expect, test } from "@playwright/test";
import { installFakeApi, seedConsoleSettings, type FakeHealthIssue } from "./fakeApi";

const ORIGIN = "http://127.0.0.1:4173";
const EMBEDDING_SPACE_ID = "synthetic-embedding-space-v1";

const embeddingHealthIssues: FakeHealthIssue[] = [
  {
    type: "embedding_missing",
    severity: "warning",
    object_id: "active-memory-01",
    related_id: null,
    message: "Active memory has no embedding vector.",
    recommended_action: "Regenerate embeddings if semantic search quality matters."
  },
  {
    type: "embedding_dimension_mismatch",
    severity: "error",
    object_id: "active-memory-02",
    related_id: null,
    message: "Active memory embedding dimension does not match current configuration.",
    recommended_action: "Regenerate embeddings with the configured embedding dimension."
  }
];

test("简洁模式：体检页与记忆档案抽屉不泄露 embedding 内部信息", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN); // uiMode=simple
  const api = await installFakeApi(page, { healthIssues: embeddingHealthIssues });

  // 数据库健康：内部 embedding 文案必须被翻译成用户可理解的语义索引文案。
  await page.goto("/ui/#/review");
  await expect(page.getByRole("heading", { name: "数据库健康" })).toBeVisible();
  await expect(page.getByText("语义索引缺失")).toBeVisible();
  await expect(page.getByText("语义索引过期")).toBeVisible();
  await expect(page.getByText(/embedding/i)).toHaveCount(0);

  // 真正打开记忆档案抽屉：简洁模式不得显示原始 embedding_space_id 或完整内部字段。
  await page.goto("/ui/#/memories");
  await page.locator(".memory-card-open").first().click();
  const drawer = page.getByRole("dialog", { name: "记忆档案" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("合成记忆 01", { exact: false })).toBeVisible();
  await expect(drawer.getByText(EMBEDDING_SPACE_ID)).toHaveCount(0);
  await expect(drawer.getByText(/embedding/i)).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: "查看全部字段" })).toHaveCount(0);

  expect(api.blockedExternalUrls).toEqual([]);
});

test("专家模式：体检页与记忆档案抽屉保留完整技术诊断", async ({ page }) => {
  await seedConsoleSettings(page, ORIGIN);
  await page.addInitScript(() => {
    localStorage.setItem("memory-console.uiMode", "expert");
  });
  const api = await installFakeApi(page, { healthIssues: embeddingHealthIssues });

  await page.goto("/ui/#/review");
  await expect(page.getByRole("heading", { name: "数据库健康" })).toBeVisible();
  await expect(page.getByText("缺少 embedding")).toBeVisible();
  await expect(
    page.getByText("Active memory embedding dimension does not match current configuration.")
  ).toBeVisible();

  await page.goto("/ui/#/memories");
  await page.locator(".memory-card-open").first().click();
  const drawer = page.getByRole("dialog", { name: "记忆档案" });
  await expect(drawer.getByText(EMBEDDING_SPACE_ID)).toBeVisible();
  await drawer.getByRole("button", { name: "查看全部字段" }).click();
  await expect(drawer.getByText("衰减 λ")).toBeVisible();

  expect(api.blockedExternalUrls).toEqual([]);
});
