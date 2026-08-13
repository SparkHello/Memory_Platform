/**
 * 决策日志原因的显示层翻译。
 *
 * 后端 ingest/extractor 写入的 reason 是精确的审计文本（含 source_quote、
 * candidate.* 等机制术语），决策日志页原样展示以便排障；但工作室的
 * 「最近未写入记忆」面板面向普通用户，这里把已知模式翻译成白话。
 * 未识别的文本原样返回，不做有损改写。
 */

const MODEL_REASON_TEXT: Record<string, string> = {
  no_long_term_value: "模型判断这段对话没有需要长期记住的信息",
  temporary_or_one_off: "内容只是临时状态或一次性事项，不值得长期记住",
  hypothetical_or_uncertain: "内容是假设或不确定的说法，暂不记录",
  not_user_asserted: "内容不是你亲口确认的事实，暂不记录",
  sensitive_without_explicit_request: "内容涉及敏感信息，且你没有明确要求记住",
  insufficient_context: "上下文不足，模型无法确定要记什么",
  other: "模型判断这轮对话不需要保存记忆",
  unclassified: "模型没有说明原因，这轮未保存记忆",
  invalid_model_output: "提取模型输出格式异常，这轮未保存记忆",
  upstream_unavailable: "提取模型暂时不可用，这轮未保存记忆"
};

// 顺序即优先级：具体模式在前，宽泛的防编造兜底在最后。
const REASON_RULES: Array<[RegExp, string]> = [
  [
    /敏感原文未发送给远程提取模型/,
    "涉及隐私的内容不会发送给提取模型，这轮没有保存记忆（如确需处理，可在服务端启用 ALLOW_SENSITIVE_EGRESS）"
  ],
  [/敏感候选未保存/, "候选内容涉及敏感信息，已按隐私策略放弃保存"],
  [/敏感事实缺少 source_quote 支撑/, "提取结果包含你的原话中没有的敏感信息，为防误记已放弃保存"],
  [/否定含义不一致/, "提取结果与你的原话意思相反（肯定/否定不一致），为防误记已放弃保存"],
  [
    /(疑似模型(自行)?编造|共同事实锚点|缺少 source_quote|缺少 context_quote|context_quote|未绑定到 candidate\.memory)/,
    "提取结果没有通过「原话核对」防编造校验，已放弃保存"
  ]
];

export function friendlyIngestSkipReason(reason: string): string {
  const text = reason.trim();
  if (!text) return "未记录原因";

  const modelReason = text.match(/提取模型未返回候选记忆；原因码=([a-z_]+)/);
  if (modelReason) {
    return MODEL_REASON_TEXT[modelReason[1]] || MODEL_REASON_TEXT.unclassified;
  }

  for (const [pattern, friendly] of REASON_RULES) {
    if (pattern.test(text)) return friendly;
  }
  return text;
}
