import type { ProvidersStatus } from "../types";
import { isProviderSetupReady } from "./providerSetup";

type ProviderSetup = ProvidersStatus["setup"];

export type NextStep = {
  label: string;
  hash: `#/${string}`;
};

/**
 * 由 providers setup 状态推导空库页面的全局「下一步」引导目标。
 * 完全就绪（含 embedding 契约校验，见 providerSetup）返回 null，页面不再引导。
 *
 * 「模型就绪但尚未生成 chat token」的中间态刻意不推导：providers setup
 * 契约里没有独立的 token 信号，Dashboard 是结合 DashboardData.hasChatToken
 * 才确认该状态（见 DashboardPage ConnectClientCard），列表页拿不到该字段。
 * 不为这条引导新增后端接口，因此中间态返回 null 保持安静。
 */
export function nextStepForSetup(setup: ProviderSetup | null | undefined): NextStep | null {
  // 状态尚未拉取到时不引导，避免误报。
  if (!setup) return null;
  if (isProviderSetupReady(setup)) return null;
  // 与 DashboardPage SetupNextStepCard 的 isRepair 判定保持一致。
  if (setup.state === "configuration_error" || setup.next_action === "repair_model_gateway") {
    return { label: "去修复模型网关配置", hash: "#/providers" };
  }
  if (setup.state === "needs_model") {
    return { label: "去配置模型渠道", hash: "#/providers" };
  }
  return null;
}
