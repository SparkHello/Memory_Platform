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

const STATUS_MESSAGES: Record<number, string> = {
  400: "请求参数有误",
  401: "访问凭证无效，请在设置中核对当前设备的 Console token",
  403: "没有权限执行此操作",
  404: "请求的内容不存在",
  409: "操作冲突，请刷新后重试",
  422: "请求格式有误",
  429: "请求过于频繁，请稍后再试",
  500: "服务内部错误，请稍后重试",
  502: "上游服务暂时不可用，请稍后重试",
  503: "服务暂时不可用，请稍后重试",
  504: "上游服务响应超时，请稍后重试"
};

// 4xx 校验类错误保留服务端细节，帮助定位具体字段
const DETAIL_STATUSES = new Set([400, 409, 422]);

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const base = STATUS_MESSAGES[error.status];
    if (!base) return `${error.status}: ${error.detail}`;
    if (DETAIL_STATUSES.has(error.status) && error.detail) {
      return `${base}：${error.detail}`;
    }
    return base;
  }
  if (error instanceof Error) {
    if (error.message === "Failed to fetch") {
      return "无法连接到记忆服务，请确认服务地址与端口";
    }
    return error.message;
  }
  return "操作失败";
}
