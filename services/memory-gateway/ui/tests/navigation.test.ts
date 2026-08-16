import { describe, expect, it } from "vitest";
import {
  hashForPage,
  hashForRoute,
  PAGE_META,
  parseHash,
  pageFromHash
} from "../src/navigation";
import type { PageKey } from "../src/types";

describe("navigation round-trip", () => {
  it("每个页面的规范 hash 都能被 parseHash 解析回原页面", () => {
    for (const key of Object.keys(PAGE_META) as PageKey[]) {
      const hash = hashForPage(key);
      const route = parseHash(hash);
      expect(route, `page ${key} hash ${hash}`).not.toBeNull();
      expect(route?.page).toBe(key);
      expect(route?.memoryId).toBeNull();
      expect(route?.knowledgeId).toBeNull();
      expect(pageFromHash(hash)).toBe(key);
    }
  });

  it("#/memories/<id> 解析为记忆库页 + 档案抽屉，支持编码字符", () => {
    const id = "记忆 abc/01";
    const route = parseHash(hashForRoute("memories", id));
    expect(route).toEqual({ page: "memories", memoryId: id, knowledgeId: null });
  });

  it("#/knowledge/<id> 解析为知识文档全页地址", () => {
    const route = parseHash(hashForRoute("knowledge", null, "doc-01"));
    expect(route).toEqual({ page: "knowledge", memoryId: null, knowledgeId: "doc-01" });
  });

  it("#/<page>?memory=<id> 在任意页面叠加档案抽屉", () => {
    const route = parseHash(hashForRoute("usage", "mem-42"));
    expect(route).toEqual({ page: "usage", memoryId: "mem-42", knowledgeId: null });
  });

  it("尾部斜杠与空 hash 的既有行为不变", () => {
    expect(parseHash("#/usage/")?.page).toBe("usage");
    expect(parseHash("")).toBeNull();
    expect(parseHash("#/")).toBeNull();
  });

  it("未知 hash 返回 null，由 App 显示「页面不存在」", () => {
    expect(parseHash("#/foo")).toBeNull();
    expect(parseHash("#/definitely-not-a-page")).toBeNull();
  });
});
