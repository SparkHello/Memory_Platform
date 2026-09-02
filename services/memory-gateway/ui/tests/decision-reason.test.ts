import { describe, expect, it } from "vitest";
import { friendlyIngestSkipReason } from "../src/utils/decisionReason";

// 输入样本取自后端 app/memory/ingest.py 与 app/memory/extractor.py 的真实审计文本。
describe("friendlyIngestSkipReason", () => {
  it("translates grounding-gate rejections into plain language", () => {
    const samples = [
      "candidate.memory 的每个事实必须在 source_quote 中有共同事实锚点",
      "candidate.entities 中有值未出现在 source_quote，疑似模型编造",
      "candidate.memory 中的结构化日期未出现在 source_quote，疑似模型编造",
      "source_quote 不是用户原话，疑似模型自行编造",
      "缺少 source_quote（必须提供用户原话的逐字片段）",
      "candidate.entities 中有值未绑定到 candidate.memory 命题",
      "context_quote 不是较早对话原文，疑似模型自行编造"
    ];
    for (const raw of samples) {
      const friendly = friendlyIngestSkipReason(raw);
      expect(friendly).toBe("提取结果没有通过「原话核对」防编造校验，已放弃保存");
    }
  });

  it("keeps sensitive and negation rejections distinct", () => {
    expect(
      friendlyIngestSkipReason("candidate.memory 的敏感事实缺少 source_quote 支撑（类别: id_number）")
    ).toMatch(/敏感信息/);
    expect(friendlyIngestSkipReason("candidate.memory 与 source_quote 的否定含义不一致")).toMatch(
      /意思相反/
    );
    expect(
      friendlyIngestSkipReason("敏感原文未发送给远程提取模型；如确需处理，请显式启用 ALLOW_SENSITIVE_EGRESS")
    ).toMatch(/涉及隐私/);
    expect(friendlyIngestSkipReason("敏感候选未保存；详细理由已脱敏")).toMatch(/隐私策略/);
  });

  it("maps model reason codes to their explanations", () => {
    expect(
      friendlyIngestSkipReason(
        "提取模型未返回候选记忆；原因码=no_long_term_value（没有未来长期价值）；模型理由已脱敏"
      )
    ).toBe("模型判断这段对话没有需要长期记住的信息");
    expect(
      friendlyIngestSkipReason("提取模型未返回候选记忆；原因码=insufficient_context（上下文不足）；模型理由已脱敏")
    ).toBe("上下文不足，模型无法确定要记什么");
    // 未知原因码回退到 unclassified 文案而不是抛错
    expect(
      friendlyIngestSkipReason("提取模型未返回候选记忆；原因码=some_future_code（新枚举）；模型理由已脱敏")
    ).toBe("模型没有说明原因，这轮未保存记忆");
  });

  it("explains local prefilter skips without exposing the mechanism", () => {
    expect(friendlyIngestSkipReason("本地预过滤：本轮仅为寒暄或确认，未调用提取模型")).toMatch(
      /寒暄、提问或代码片段/
    );
    expect(friendlyIngestSkipReason("本地预过滤：本轮仅为提问，未调用提取模型")).toMatch(
      /没有调用提取模型/
    );
    expect(friendlyIngestSkipReason("本地预过滤：用户文本超过 64 KiB，未调用提取模型")).toMatch(
      /64 KiB/
    );
  });

  it("passes through already-plain reasons and empty input", () => {
    expect(friendlyIngestSkipReason("已有相同记忆")).toBe("已有相同记忆");
    expect(friendlyIngestSkipReason("提交文本为空")).toBe("提交文本为空");
    expect(friendlyIngestSkipReason("")).toBe("未记录原因");
    expect(friendlyIngestSkipReason("   ")).toBe("未记录原因");
  });
});
