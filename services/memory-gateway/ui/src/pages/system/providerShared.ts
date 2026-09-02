import type { ModelGatewayCapabilities } from "../../types";

/** 供应商配置页共享的操作反馈：只在组件内存，不持久化任何密钥。 */
export type ProviderFeedback = { tone: "success" | "warning" | "error"; message: string };

export const ROUTE_LABELS: Record<string, string> = {
  "memory.chat": "日常聊天",
  "memory.extract": "提取长期记忆",
  "memory.compact": "压缩对话上下文",
  "memory.core": "整理核心记忆",
  "memory.review": "记忆体检",
  "knowledge.fast": "快速知识检索",
  "knowledge.pro": "深度知识检索",
  "memory.embedding": "语义搜索",
  "pricing.research": "价格信息研究"
};

export const CHAT_ROUTE_IDS = [
  "memory.chat",
  "memory.extract",
  "memory.compact",
  "memory.core",
  "memory.review",
  "knowledge.fast",
  "knowledge.pro"
] as const;

export const CAPABILITY_OPTIONS: Array<{
  key: keyof ModelGatewayCapabilities;
  label: string;
}> = [
  { key: "tools", label: "工具调用 tools" },
  { key: "parallel_tools", label: "并行工具 parallel_tools" },
  { key: "reasoning", label: "推理 reasoning" },
  { key: "multimodal_input", label: "多模态输入 multimodal_input" },
  { key: "json_object", label: "JSON 对象 json_object" },
  { key: "json_schema", label: "JSON Schema json_schema" }
];


const CHANNEL_OPERATOR_LABELS: Record<string, string> = {
  dashscope: "阿里云百炼",
  google: "Google Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  deepseek: "DeepSeek",
  moonshot: "Kimi（月之暗面）",
  zhipu: "智谱 AI",
  siliconflow: "硅基流动",
  volcengine: "火山引擎",
  minimax: "MiniMax",
  openrouter: "OpenRouter",
  ollama: "Ollama",
  xai: "xAI",
  mistral: "Mistral"
};

/** Human name for a channel operator id; ids stay as stored. */
export function channelOperatorLabel(operator: string): string {
  return CHANNEL_OPERATOR_LABELS[operator.trim().toLowerCase()] || operator;
}
