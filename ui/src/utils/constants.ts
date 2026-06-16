import type {
  CoreSectionName,
  MemoryAction,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  ReviewAction
} from "../types";

export const MEMORY_TYPES: MemoryType[] = [
  "project",
  "preference",
  "fact",
  "learning",
  "style",
  "person",
  "relationship"
];

export const STABILITIES: MemoryStability[] = ["temporary", "medium", "stable"];
export const SENSITIVITIES: MemorySensitivity[] = ["normal", "private", "sensitive"];
export const REVIEW_ACTIONS: ReviewAction[] = ["merge", "delete", "lower", "review", "keep"];
export const DECISIONS: MemoryAction[] = ["create", "update", "ignore"];

export const CORE_SECTIONS: Array<{ key: CoreSectionName; title: string }> = [
  { key: "profile", title: "个人背景" },
  { key: "preferences", title: "偏好" },
  { key: "relationships", title: "关系" },
  { key: "routines", title: "日常习惯" },
  { key: "goals", title: "目标计划" },
  { key: "communication", title: "沟通方式" }
];

export const CONFIG_KEYS = [
  "GATEWAY_API_KEY",
  "UPSTREAM_BASE_URL",
  "UPSTREAM_API_KEY",
  "UPSTREAM_MODEL",
  "EMBEDDING_BASE_URL",
  "EMBEDDING_API_KEY",
  "EMBEDDING_MODEL",
  "EMBEDDING_DIMENSIONS",
  "DATABASE_PATH",
  "REQUEST_TIMEOUT_SECONDS"
];

export const DISPLAY_TEXT: Record<string, string> = {
  all: "全部",
  project: "项目",
  preference: "偏好",
  fact: "事实",
  learning: "学习",
  style: "风格",
  person: "人物",
  relationship: "关系",
  temporary: "临时",
  medium: "中期",
  stable: "稳定",
  normal: "普通",
  private: "私密",
  sensitive: "敏感",
  create: "创建",
  update: "更新",
  ignore: "忽略",
  keep: "保留",
  merge: "合并",
  lower: "降权",
  delete: "移入回收站",
  review: "复核",
  none: "无",
  same: "重复",
  supplement: "补充",
  conflict: "冲突",
  supersede: "替代",
  profile: "个人背景",
  preferences: "偏好",
  relationships: "关系",
  routines: "日常习惯",
  goals: "目标计划",
  communication: "沟通方式",
  other: "其他记忆",
  success: "成功",
  error: "错误"
};
