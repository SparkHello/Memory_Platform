import { describe, expect, it } from "vitest";
import type { ProvidersStatus } from "../src/types";
import { nextStepForSetup } from "../src/utils/nextStep";

type ProviderSetup = ProvidersStatus["setup"];

function setup(partial: Partial<ProviderSetup>): ProviderSetup {
  return {
    state: "ready",
    service_ready: true,
    model_gateway_connected: true,
    chat_ready: true,
    required_chat_routes: ["chat"],
    usable_chat_routes: ["chat"],
    missing_chat_routes: [],
    next_action: "connect_client",
    ...partial
  };
}

describe("nextStepForSetup", () => {
  it("returns null while setup status is unknown", () => {
    expect(nextStepForSetup(null)).toBeNull();
    expect(nextStepForSetup(undefined)).toBeNull();
  });

  it("returns null when setup is fully ready", () => {
    expect(nextStepForSetup(setup({}))).toBeNull();
  });

  it("guides to model channels when no model is configured", () => {
    expect(
      nextStepForSetup(
        setup({
          state: "needs_model",
          service_ready: false,
          chat_ready: false,
          usable_chat_routes: [],
          missing_chat_routes: ["chat"],
          next_action: "configure_model"
        })
      )
    ).toEqual({ label: "去配置模型渠道", hash: "#/providers" });
  });

  it("guides to repair when the model gateway configuration is broken", () => {
    expect(
      nextStepForSetup(
        setup({
          state: "configuration_error",
          service_ready: false,
          chat_ready: false,
          next_action: "repair_model_gateway"
        })
      )
    ).toEqual({ label: "去修复模型网关配置", hash: "#/providers" });
  });

  it("prefers repair guidance when next_action says repair even if state is needs_model", () => {
    expect(
      nextStepForSetup(
        setup({ state: "needs_model", chat_ready: false, next_action: "repair_model_gateway" })
      )
    ).toEqual({ label: "去修复模型网关配置", hash: "#/providers" });
  });

  it("stays quiet for the intermediate state (model ready but chat token missing)", () => {
    // chat_ready 还差一步但没有可靠的「未生成 chat token」信号，不引导。
    expect(
      nextStepForSetup(setup({ chat_ready: false, next_action: "connect_client" }))
    ).toBeNull();
  });

  it("stays quiet when the service is not ready but state is not an error", () => {
    expect(nextStepForSetup(setup({ service_ready: false }))).toBeNull();
  });
});
