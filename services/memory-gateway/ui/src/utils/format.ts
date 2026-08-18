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
  401: "访问凭证无效",
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

// 4xx 校验/权限类错误保留服务端细节，帮助定位具体字段或缺失的权限
const DETAIL_STATUSES = new Set([400, 403, 409, 422]);

/** Which credential the failing request was most likely using. */
export type ErrorCredentialHint =
  | "console"
  | "admin"
  | "provider"
  | "chat"
  | "mcp"
  | "auto";

export type ErrorMessageOptions = {
  credential?: ErrorCredentialHint;
};

const CREDENTIAL_401_MESSAGES: Record<Exclude<ErrorCredentialHint, "auto">, string> = {
  console:
    "访问凭证无效，请在设置中核对当前设备的 Console token（credentials/gateway.txt 或旧版 gateway.key）",
  admin:
    "Model Gateway admin 密钥无效。它与登录网页用的 Console token（gateway.txt）不是同一把钥匙；请使用 credentials/admin.txt",
  provider: "供应商 API Key 或渠道凭证无效，请核对渠道密钥后重试",
  chat: "chat token 无效或已撤销，请到「客户端接入」重新创建",
  mcp: "MCP token 无效或已撤销，请到「客户端接入」重新创建"
};

const GENERIC_UNAUTHORIZED = new Set([
  "unauthorized",
  "not authenticated",
  "authentication required",
  "invalid token",
  "invalid api key",
  "invalid credentials"
]);

function isGenericUnauthorizedDetail(detail: string): boolean {
  const normalized = detail.trim().toLowerCase();
  if (!normalized || normalized.length < 4) return true;
  if (GENERIC_UNAUTHORIZED.has(normalized)) return true;
  // 后端鉴权中间件的机制性描述（如 "Authorization Bearer token 无效"）对用户
  // 没有行动指引，视为 generic，换成对应凭证的操作建议。
  if (/^authorization\b/.test(normalized)) return true;
  if (/bearer token\s*(无效|缺失|invalid|missing|expired)/.test(normalized)) return true;
  return /^(http\s*)?401(\s|$)/i.test(normalized);
}

function pathSuggestsAdmin(path: string | undefined): boolean {
  if (!path) return false;
  if (path.includes("/providers/admin")) return true;
  if (!path.startsWith("/providers/")) return false;
  return (
    path.includes("/connections") ||
    path.includes("/deployments") ||
    path.includes("/routes") ||
    path.includes("/channel") ||
    path.includes("/bundle") ||
    path.includes("/secret") ||
    path.includes("/check")
  );
}

function detailSuggestsAdmin(detail: string, code?: string): boolean {
  const haystack = `${code || ""} ${detail}`.toLowerCase();
  return (
    haystack.includes("admin") ||
    haystack.includes("model gateway") ||
    detail.includes("管理密钥") ||
    detail.includes("admin key") ||
    detail.includes("admin.key") ||
    detail.includes("admin.txt")
  );
}

function detailSuggestsProvider(detail: string, code?: string): boolean {
  const haystack = `${code || ""} ${detail}`.toLowerCase();
  return (
    haystack.includes("api key") ||
    haystack.includes("api_key") ||
    detail.includes("供应商") ||
    detail.includes("渠道密钥") ||
    detail.includes("候选密钥")
  );
}

export function inferErrorCredential(
  error: ApiError,
  hint: ErrorCredentialHint = "auto"
): Exclude<ErrorCredentialHint, "auto"> {
  if (hint !== "auto") return hint;
  if (error.code === "admin_key_required" || error.code === "admin_auth_failed") {
    return "admin";
  }
  if (error.code === "provider_auth_failed") return "provider";
  if (detailSuggestsAdmin(error.detail, error.code) || pathSuggestsAdmin(error.path)) {
    return "admin";
  }
  if (detailSuggestsProvider(error.detail, error.code)) return "provider";
  if (error.path?.startsWith("/auth/tokens")) return "console";
  return "console";
}

export function errorMessage(error: unknown, options?: ErrorMessageOptions): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      const credential = inferErrorCredential(error, options?.credential ?? "auto");
      const fallback = CREDENTIAL_401_MESSAGES[credential];
      if (error.detail && !isGenericUnauthorizedDetail(error.detail)) {
        // Server already explained; still append a short cross-key hint for admin 401s
        // when the detail never mentions the Console vs admin distinction.
        if (
          credential === "admin" &&
          !error.detail.includes("gateway.key") &&
          !error.detail.includes("gateway.txt") &&
          !error.detail.includes("Console") &&
          !error.detail.includes("不是同一")
        ) {
          return `${error.detail}（admin.txt 与登录用的 Console token 不是同一把钥匙）`;
        }
        return error.detail;
      }
      return fallback;
    }

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
