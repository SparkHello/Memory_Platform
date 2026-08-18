import type { PageKey } from "./types";

export type PageMeta = {
  label: string;
  shortLabel: string;
  hash: `#/${string}`;
};

export const PAGE_META: Record<PageKey, PageMeta> = {
  dashboard: { label: "记忆工作室", shortLabel: "工作室", hash: "#/studio" },
  memories: { label: "记忆库", shortLabel: "记忆库", hash: "#/memories" },
  knowledge: { label: "知识库", shortLabel: "知识库", hash: "#/knowledge" },
  knowledgeSearch: { label: "检索调试", shortLabel: "检索", hash: "#/knowledge-search" },
  core: { label: "核心记忆", shortLabel: "核心", hash: "#/core" },
  recent: { label: "对话上下文", shortLabel: "上下文", hash: "#/recent" },
  review: { label: "记忆体检", shortLabel: "体检", hash: "#/review" },
  recall: { label: "召回解释", shortLabel: "召回", hash: "#/recall" },
  evaluation: { label: "评测闭环", shortLabel: "评测", hash: "#/evaluation" },
  reports: { label: "报告与备份", shortLabel: "备份", hash: "#/reports" },
  logs: { label: "决策日志", shortLabel: "日志", hash: "#/logs" },
  usage: { label: "用量与费用", shortLabel: "用量", hash: "#/usage" },
  providers: { label: "模型与路由", shortLabel: "模型", hash: "#/providers" },
  settings: { label: "连接设置", shortLabel: "设置", hash: "#/settings" },
  developer: { label: "客户端接入", shortLabel: "接入", hash: "#/integration" }
};

/**
 * 导航层级：分区（侧栏 caption）→ 页面（侧栏条目，带可选待办角标）→ 记忆档案 / 知识文档。
 * 工作室是单页分区，作为汇总各分区待办信号的枢纽首页。
 */
export type SectionKey = "studio" | "memory" | "knowledge" | "governance" | "data" | "system";

export type NavSection = {
  key: SectionKey;
  label: string;
  items: PageKey[];
};

export const NAV_SECTIONS: NavSection[] = [
  { key: "studio", label: "工作室", items: ["dashboard"] },
  { key: "memory", label: "记忆", items: ["memories", "core", "recent"] },
  { key: "knowledge", label: "知识", items: ["knowledge", "knowledgeSearch"] },
  { key: "governance", label: "治理", items: ["review", "evaluation", "recall"] },
  { key: "data", label: "数据", items: ["usage", "reports", "logs"] },
  { key: "system", label: "系统", items: ["providers", "settings", "developer"] }
];

/**
 * 默认只展示完成日常任务所需的入口。高级页面继续保留完整 hash 路由，
 * 因此旧书签和页面内跳转不会因为简洁模式而失效。
 */
export const SIMPLE_NAV_SECTIONS: NavSection[] = [
  { key: "studio", label: "开始", items: ["dashboard"] },
  { key: "memory", label: "内容", items: ["memories", "knowledge"] },
  { key: "system", label: "使用", items: ["review", "usage", "providers", "reports", "developer"] }
];

export function sectionForPage(page: PageKey): NavSection {
  return NAV_SECTIONS.find((section) => section.items.includes(page)) || NAV_SECTIONS[0];
}

export type Route = {
  page: PageKey;
  memoryId: string | null;
  knowledgeId: string | null;
};

/**
 * 支持的 hash 形态：
 *   #/memories/<id>        —— 记忆档案的规范地址（记忆库页 + 档案抽屉）
 *   #/knowledge/<id>       —— 知识文档的独立全页地址
 *   #/<page>?memory=<id>   —— 在任意页面上叠加档案抽屉，不丢失上下文
 *   #/<page>               —— 普通页面
 */
export function parseHash(hash: string): Route | null {
  const [path, queryText = ""] = hash.split("?");
  const normalized = path.replace(/\/$/, "");

  const memoryMatch = normalized.match(/^#\/memories\/([^/]+)$/);
  if (memoryMatch) {
    return { page: "memories", memoryId: decodeURIComponent(memoryMatch[1]), knowledgeId: null };
  }

  const knowledgeMatch = normalized.match(/^#\/knowledge\/([^/]+)$/);
  if (knowledgeMatch) {
    return { page: "knowledge", memoryId: null, knowledgeId: decodeURIComponent(knowledgeMatch[1]) };
  }

  const entry = (Object.entries(PAGE_META) as Array<[PageKey, PageMeta]>).find(
    ([, meta]) => meta.hash === normalized
  );
  if (!entry) return null;

  const memoryId = new URLSearchParams(queryText).get("memory");
  return { page: entry[0], memoryId: memoryId || null, knowledgeId: null };
}

export function hashForRoute(page: PageKey, memoryId?: string | null, knowledgeId?: string | null): string {
  if (page === "knowledge" && knowledgeId) {
    return `#/knowledge/${encodeURIComponent(knowledgeId)}`;
  }
  if (!memoryId) return PAGE_META[page].hash;
  if (page === "memories") return `#/memories/${encodeURIComponent(memoryId)}`;
  return `${PAGE_META[page].hash}?memory=${encodeURIComponent(memoryId)}`;
}

export function pageFromHash(hash: string): PageKey | null {
  return parseHash(hash)?.page ?? null;
}

export function hashForPage(page: PageKey): string {
  return hashForRoute(page);
}
