import type { ProvidersStatus } from "../types";

type ProviderSetup = ProvidersStatus["setup"];

/**
 * Overall Console readiness is stricter than chat-route availability.
 * `chat_ready` remains useful for live probes, but an invalid embedding
 * contract must keep the setup and repair surfaces visible.
 */
export function isProviderSetupReady(
  setup: ProviderSetup | null | undefined
): boolean {
  return Boolean(
    setup &&
      setup.state === "ready" &&
      setup.service_ready &&
      setup.chat_ready
  );
}
