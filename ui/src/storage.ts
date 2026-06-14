import type { ConnectionSettings } from "./types";

const API_BASE_URL_KEY = "memory-console.apiBaseUrl";
const API_KEY_KEY = "memory-console.gatewayApiKey";
const USER_ID_KEY = "memory-console.userId";

export function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    return window.location.origin;
  }
  return trimmed.replace(/\/+$/, "");
}

export function loadSettings(): ConnectionSettings {
  return {
    apiBaseUrl: normalizeBaseUrl(localStorage.getItem(API_BASE_URL_KEY) || window.location.origin),
    apiKey: localStorage.getItem(API_KEY_KEY) || "",
    userId: localStorage.getItem(USER_ID_KEY) || "default"
  };
}

export function saveSettings(settings: ConnectionSettings): ConnectionSettings {
  const normalized = {
    apiBaseUrl: normalizeBaseUrl(settings.apiBaseUrl),
    apiKey: settings.apiKey.trim(),
    userId: settings.userId.trim() || "default"
  };
  localStorage.setItem(API_BASE_URL_KEY, normalized.apiBaseUrl);
  localStorage.setItem(API_KEY_KEY, normalized.apiKey);
  localStorage.setItem(USER_ID_KEY, normalized.userId);
  return normalized;
}
