import type { ProviderModelSummary, ProviderSummary, RouteSummary } from "../types";
import { clampNumber, isRecord } from "./format";

export type ProviderDraft = {
  mode: "create" | "edit";
  provider: string;
  name: string;
  base_url: string;
  api_key: string;
  enabled: boolean;
  timeout_seconds: number;
};

export type ProviderModelDraft = {
  mode: "create" | "edit";
  id: string;
  provider: string;
  upstream_model: string;
  display_name: string;
  api_format: "openai_compatible" | "claude_sdk";
  pricing_mode: "flat" | "tiered";
  pricing_tiers_json: string;
  pricing_tiers: PriceTierDraft[];
  input_price_per_million: string;
  output_price_per_million: string;
  cache_hit_price_per_million: string;
  currency: string;
  enabled: boolean;
};

export type PriceTierDraft = {
  up_to_tokens: string;
  input_price_per_million: string;
  output_price_per_million: string;
  cache_hit_price_per_million: string;
};

export type RouteDraft = {
  mode: "create" | "edit";
  id: string;
  virtual_model: string;
  provider_model_id: string;
  provider: string;
  upstream_model: string;
  priority: number;
  enabled: boolean;
};

export const EMPTY_PROVIDER_DRAFT: ProviderDraft = {
  mode: "create",
  provider: "",
  name: "",
  base_url: "",
  api_key: "",
  enabled: true,
  timeout_seconds: 60
};

export const EMPTY_PROVIDER_MODEL_DRAFT: ProviderModelDraft = {
  mode: "create",
  id: "",
  provider: "",
  upstream_model: "",
  display_name: "",
  api_format: "openai_compatible",
  pricing_mode: "flat",
  pricing_tiers_json: "",
  pricing_tiers: createEmptyPriceTierDrafts(),
  input_price_per_million: "0",
  output_price_per_million: "0",
  cache_hit_price_per_million: "0",
  currency: "CNY",
  enabled: true
};

export const EMPTY_ROUTE_DRAFT: RouteDraft = {
  mode: "create",
  id: "",
  virtual_model: "",
  provider_model_id: "",
  provider: "",
  upstream_model: "",
  priority: 50,
  enabled: true
};

export function sourceText(source: string): string {
  if (source === "sqlite") return "SQLite UI 配置";
  if (source === "toml") return "providers.toml";
  return "旧版 .env 单模型配置";
}

export function providerToDraft(provider: ProviderSummary): ProviderDraft {
  return {
    mode: "edit",
    provider: provider.provider || provider.id,
    name: provider.name,
    base_url: provider.base_url,
    api_key: "",
    enabled: provider.enabled,
    timeout_seconds: provider.timeout_seconds
  };
}

export function providerModelToDraft(model: ProviderModelSummary): ProviderModelDraft {
  return {
    mode: "edit",
    id: model.id,
    provider: model.provider,
    upstream_model: model.upstream_model,
    display_name: model.display_name,
    api_format: model.api_format || "openai_compatible",
    pricing_mode: model.pricing_mode || "flat",
    pricing_tiers_json: model.pricing_tiers_json || "",
    pricing_tiers: priceTierDraftsFromJson(model.pricing_tiers_json),
    input_price_per_million: decimalInputText(model.input_price_per_million),
    output_price_per_million: decimalInputText(model.output_price_per_million),
    cache_hit_price_per_million: decimalInputText(model.cache_hit_price_per_million),
    currency: model.currency,
    enabled: model.enabled !== false
  };
}

export function routeToDraft(route: RouteSummary): RouteDraft {
  return {
    mode: "edit",
    id: route.id || "",
    virtual_model: route.virtual_model,
    provider_model_id: route.provider_model_id || "",
    provider: route.provider,
    upstream_model: route.upstream_model,
    priority: route.priority,
    enabled: route.enabled !== false
  };
}

export function providerModelLabel(model: ProviderModelSummary): string {
  const name = model.display_name || model.upstream_model;
  return `${model.provider} / ${name} (${model.upstream_model}, ${apiFormatText(model.api_format)})`;
}

export function apiFormatText(value?: string | null): string {
  if (value === "claude_sdk") return "Claude SDK";
  return "OpenAI-compatible";
}

export function pricingModeText(value?: string | null): string {
  if (value === "tiered") return "分级价格";
  return "固定价格";
}

export function createEmptyPriceTierDrafts(): PriceTierDraft[] {
  return [
    {
      up_to_tokens: "1000000",
      input_price_per_million: "0",
      output_price_per_million: "0",
      cache_hit_price_per_million: "0"
    },
    {
      up_to_tokens: "",
      input_price_per_million: "0",
      output_price_per_million: "0",
      cache_hit_price_per_million: "0"
    }
  ];
}

export function ensureTwoPriceTierDrafts(tiers?: PriceTierDraft[] | null): PriceTierDraft[] {
  const defaults = createEmptyPriceTierDrafts();
  return defaults.map((fallback, index) => ({
    ...fallback,
    ...(tiers?.[index] || {})
  }));
}

export function priceTierDraftsFromJson(raw?: string | null): PriceTierDraft[] {
  if (!raw?.trim()) {
    return createEmptyPriceTierDrafts();
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) {
      return createEmptyPriceTierDrafts();
    }
    const defaults = createEmptyPriceTierDrafts();
    return defaults.map((fallback, index) => {
      const tier = parsed[index];
      if (!isRecord(tier)) {
        return fallback;
      }
      return {
        up_to_tokens:
          tier.up_to_tokens === null || tier.up_to_tokens === undefined
            ? fallback.up_to_tokens
            : decimalInputText(tier.up_to_tokens, fallback.up_to_tokens),
        input_price_per_million: decimalInputText(
          tier.input_price_per_million ?? tier.input,
          fallback.input_price_per_million
        ),
        output_price_per_million: decimalInputText(
          tier.output_price_per_million ?? tier.output,
          fallback.output_price_per_million
        ),
        cache_hit_price_per_million: decimalInputText(
          tier.cache_hit_price_per_million ?? tier.cache_hit,
          fallback.cache_hit_price_per_million
        )
      };
    });
  } catch {
    return createEmptyPriceTierDrafts();
  }
}

export function priceTierDraftsToJson(tiers?: PriceTierDraft[] | null): string {
  return JSON.stringify(
    ensureTwoPriceTierDrafts(tiers).map((tier) => ({
      up_to_tokens: tier.up_to_tokens.trim()
        ? Math.round(clampNumber(decimalInputValue(tier.up_to_tokens), 0, Number.MAX_SAFE_INTEGER))
        : null,
      input: clampNumber(decimalInputValue(tier.input_price_per_million), 0, 1_000_000),
      output: clampNumber(decimalInputValue(tier.output_price_per_million), 0, 1_000_000),
      cache_hit: clampNumber(decimalInputValue(tier.cache_hit_price_per_million), 0, 1_000_000)
    }))
  );
}

export function normalizeDecimalInput(raw: string): string {
  const value = raw.trim().replace(",", ".");
  if (!value) {
    return "";
  }
  const clean = value.replace(/[^\d.]/g, "");
  if (!clean) {
    return "";
  }
  const dotIndex = clean.indexOf(".");
  const hasDecimal = dotIndex !== -1;
  const wholeRaw = hasDecimal ? clean.slice(0, dotIndex) : clean;
  const fraction = hasDecimal ? clean.slice(dotIndex + 1).replace(/\./g, "") : "";
  const whole = wholeRaw.replace(/^0+(?=\d)/, "") || "0";
  return hasDecimal ? `${whole}.${fraction}` : whole;
}

export function normalizeDecimalInputOnBlur(raw: string, emptyValue = "0"): string {
  const normalized = normalizeDecimalInput(raw);
  if (!normalized) {
    return emptyValue;
  }
  if (normalized === "0.") {
    return "0";
  }
  if (normalized.endsWith(".")) {
    return normalized.slice(0, -1) || "0";
  }
  return normalized;
}

export function decimalInputValue(value: string): number {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

export function decimalInputText(value: unknown, fallback = "0"): string {
  if (typeof value === "string" && value.trim() === "") {
    return fallback;
  }
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    return fallback;
  }
  return number.toLocaleString("en-US", {
    useGrouping: false,
    maximumFractionDigits: 12
  });
}
