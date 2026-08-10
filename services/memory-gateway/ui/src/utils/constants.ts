import type {
  CoreSectionName,
  DecisionLogAction,
  MemoryStatus,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  ReviewAction,
  ReviewRiskTag,
  ReviewSeverity,
  SurfaceMode
} from "../types";

export const MEMORY_TYPES: MemoryType[] = [
  "episodic",
  "semantic",
  "procedural",
  "emotional",
  "reflective"
];

export const MEMORY_TYPE_COLOR_VAR: Record<MemoryType, string> = {
  episodic: "var(--type-episodic)",
  semantic: "var(--type-semantic)",
  procedural: "var(--type-procedural)",
  emotional: "var(--type-emotional)",
  reflective: "var(--type-reflective)"
};

export const MEMORY_STATUSES: MemoryStatus[] = ["dynamic", "resolved", "archived", "pinned"];
export const STABILITIES: MemoryStability[] = ["temporary", "medium", "stable"];
export const SENSITIVITIES: MemorySensitivity[] = ["normal", "private", "sensitive"];
export const REVIEW_ACTIONS: ReviewAction[] = ["merge", "delete", "lower", "review", "keep"];
export const REVIEW_RISK_TAGS: ReviewRiskTag[] = [
  "duplicate",
  "conflict",
  "expired",
  "time_uncertain",
  "sensitive",
  "low_value",
  "core_evidence",
  "stale",
  "emotion_uncertain"
];
export const REVIEW_SEVERITIES: ReviewSeverity[] = ["high", "medium", "low"];
export const DECISIONS: DecisionLogAction[] = ["create", "update", "ignore", "purge"];
export const SURFACE_MODES: SurfaceMode[] = [
  "balanced",
  "important",
  "emotional",
  "stale",
  "review_due"
];

export const CORE_SECTIONS: Array<{ key: CoreSectionName; title: string }> = [
  { key: "profile", title: "个人背景" },
  { key: "preferences", title: "偏好" },
  { key: "relationships", title: "关系" },
  { key: "routines", title: "日常习惯" },
  { key: "goals", title: "目标计划" },
  { key: "communication", title: "沟通方式" }
];

export const CORE_SECTION_COLOR_VAR: Record<CoreSectionName, string> = {
  profile: "var(--type-semantic)",
  preferences: "var(--type-emotional)",
  relationships: "var(--type-episodic)",
  routines: "var(--type-procedural)",
  goals: "var(--emo-mid)",
  communication: "var(--type-reflective)"
};

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
  "EVAL_DIR",
  "REQUEST_TIMEOUT_SECONDS"
];

export const CONFIG_KEY_HINTS: Record<string, string> = {
  GATEWAY_API_KEY: "仅供旧安装迁移的 legacy 全权限密钥；全新安装应使用按设备、按用途的 scoped token",
  UPSTREAM_BASE_URL: "上游聊天模型的 OpenAI 兼容接口地址",
  UPSTREAM_API_KEY: "服务端调用上游模型的密钥，只在服务端使用，不会透传给客户端",
  UPSTREAM_MODEL: "记忆提取与整理所使用的模型名称",
  EMBEDDING_BASE_URL: "向量化（embedding）服务的接口地址",
  EMBEDDING_API_KEY: "服务端调用向量化服务的密钥，不会透传给客户端",
  EMBEDDING_MODEL: "搜索召回所使用的向量模型名称",
  EMBEDDING_DIMENSIONS: "向量维度，需与模型输出保持一致",
  DATABASE_PATH: "SQLite 记忆数据库文件路径",
  EVAL_DIR: "召回评测的快照与标注工作目录",
  REQUEST_TIMEOUT_SECONDS: "服务端调用上游模型的超时秒数"
};

export const DISPLAY_TEXT: Record<string, string> = {
  all: "全部",
  episodic: "事件",
  semantic: "语义",
  procedural: "流程",
  emotional: "情绪",
  reflective: "反思",
  dynamic: "活跃",
  resolved: "已解决",
  archived: "归档",
  pinned: "钉选",
  temporary: "临时",
  medium: "中期",
  stable: "稳定",
  normal: "普通",
  private: "私密",
  sensitive: "敏感",
  create: "创建",
  update: "更新",
  ignore: "忽略",
  purge: "永久删除",
  keep: "保留",
  merge: "合并",
  lower: "降权",
  delete: "移入回收站",
  review: "复核",
  duplicate: "重复",
  conflict: "冲突",
  expired: "过期",
  time_uncertain: "时间不确定",
  low_value: "低价值",
  core_evidence: "核心证据",
  stale: "长期未活跃",
  emotion_uncertain: "高唤起低置信",
  balanced: "均衡",
  important: "重要",
  review_due: "待复核",
  expired_review: "过期复核",
  near_expiry: "即将过期",
  sensitive_review: "敏感复核",
  stale_review: "长期未活跃",
  low_life: "低生命力",
  ok: "正常",
  warning: "警告",
  info: "提示",
  active: "已激活",
  degenerate: "退化",
  dormant: "休眠",
  sparse: "稀疏",
  insufficient_data: "样本不足",
  sector_typing: "扇区分化",
  lifecycle_status: "生命周期",
  temporal_kg: "时序知识图谱",
  graph_structure: "图结构",
  emotion_affect: "情绪通道",
  recall_health: "召回健康",
  keyword: "关键词",
  embedding: "语义",
  orphan_core_evidence: "核心证据缺失",
  archived_core_evidence: "核心证据在回收站",
  orphan_space_link_memory: "空间链接缺失记忆",
  orphan_space_link_space: "空间链接缺失空间",
  embedding_missing: "缺少 embedding",
  embedding_invalid: "embedding 无效",
  embedding_dimension_mismatch: "embedding 维度不匹配",
  export_consistency_error: "导出一致性错误",
  export_space_reference_missing: "导出空间引用缺失",
  stale_search_cache_reference: "搜索缓存旧引用",
  orphan_core_history_evidence: "核心历史证据缺失",
  orphan_decision_log_reference: "决策日志旧引用",
  invalid_decision_log_json: "决策日志 JSON 无效",
  fresh_high_importance: "新近且重要",
  high_importance: "高重要度",
  high_score: "活跃度高",
  important_memory: "重要记忆",
  emotional_signal: "情绪信号",
  stale_important: "长期未活跃",
  high: "高",
  medium_severity: "中",
  low: "低",
  ai_modify: "AI 修改",
  confirm_valid: "确认有效",
  move_to_trash: "移入回收站",
  lower_importance: "降低重要度",
  review_core_memory: "整理核心记忆",
  snooze: "稍后提醒",
  has_core_evidence: "影响核心记忆",
  no_core_evidence: "不影响核心记忆",
  severity_desc: "严重程度",
  updated_desc: "最近更新",
  rank_below_limit: "排名靠后未入选",
  none: "无",
  same: "重复",
  supplement: "补充",
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
