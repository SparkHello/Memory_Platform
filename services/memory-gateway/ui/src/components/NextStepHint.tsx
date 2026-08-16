import { ArrowRight } from "lucide-react";
import type { ProvidersStatus } from "../types";
import { nextStepForSetup } from "../utils/nextStep";

/**
 * 空库页面空态上方的全局「下一步」引导：setup 未就绪时给出跳转入口；
 * 推导结果为 null（已就绪或状态未知）时不渲染。
 */
export function NextStepHint({
  setup
}: {
  setup: ProvidersStatus["setup"] | null | undefined;
}) {
  const step = nextStepForSetup(setup);
  if (!step) return null;
  return (
    <div className="notice warning next-step-hint">
      <span className="notice-text">这里还没有数据。先完成模型配置，内容才会开始积累。</span>
      <a className="secondary-button compact" href={step.hash}>
        {step.label}
        <ArrowRight size={14} aria-hidden />
      </a>
    </div>
  );
}
