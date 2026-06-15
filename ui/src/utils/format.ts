import { ApiError } from "../api";
import { normalizeBaseUrl } from "../storage";
import type { CoreSectionName, ReviewAction } from "../types";
import { CORE_SECTIONS, DISPLAY_TEXT } from "./constants";

export function displayText(value: string): string {
  return DISPLAY_TEXT[value] || value;
}

export function reviewActionText(action: ReviewAction): string {
  return `${displayText(action)}建议`;
}

export function reportSectionTitle(section: string, fallback: string): string {
  return DISPLAY_TEXT[section] || fallback;
}

export function percent(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${Math.round(value * 100)}%`;
}

export function dateText(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function numberText(value?: number | null) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 6
  });
}

export function moneyText(value?: number | null, currency?: string | null) {
  const amount = numberText(value);
  return `${amount} ${currency || ""}`.trim();
}

export function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

export function nullableText(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function clampNumber(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}

export function sectionTitle(section: CoreSectionName): string {
  return CORE_SECTIONS.find((item) => item.key === section)?.title || section;
}

export function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

export function joinUrl(baseUrl: string, path: string): string {
  return `${normalizeBaseUrl(baseUrl)}${path}`;
}

export function maskSecret(secret: string): string {
  if (!secret) return "未设置";
  if (secret.length <= 6) return "......";
  return `${secret.slice(0, 3)}....${secret.slice(-3)}`;
}

export function candidateSummary(raw: string): string {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (isRecord(parsed)) {
      const memory = parsed.memory || parsed.content || parsed.reason || parsed.source_quote;
      if (memory) return String(memory);
    }
    return JSON.stringify(parsed).slice(0, 160);
  } catch {
    return raw.slice(0, 160);
  }
}

export function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.status}: ${error.detail}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "操作失败";
}
