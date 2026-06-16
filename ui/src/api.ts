import type {
  ConnectionSettings,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  DecisionLog,
  MemoryExport,
  MemoryRecord,
  MemoryReport,
  MemorySourceExplanation,
  MemoryUpdatePayload,
  MemoryUpdateResult,
  RecentContextSummary,
  RestoreResult,
  ReviewResult
} from "./types";
import { normalizeBaseUrl } from "./storage";

type RequestOptions = {
  method?: string;
  body?: unknown;
  auth?: boolean;
  text?: boolean;
};

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

export class MemoryApi {
  private settings: ConnectionSettings;

  constructor(settings: ConnectionSettings) {
    this.settings = {
      ...settings,
      apiBaseUrl: normalizeBaseUrl(settings.apiBaseUrl)
    };
  }

  base(path: string): string {
    return `${this.settings.apiBaseUrl}${path}`;
  }

  async health(): Promise<{ status: string }> {
    return this.request("/health", { auth: false });
  }

  async listMemories(): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>("/memories");
    return payload.data || [];
  }

  async listDeletedMemories(): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>("/memories/deleted?limit=1000");
    return payload.data || [];
  }

  async searchMemories(query: string, limit = 20): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>("/memories/search", {
      method: "POST",
      body: { query, limit }
    });
    return payload.data || [];
  }

  async whyRemember(memoryId: string): Promise<MemorySourceExplanation> {
    return this.request(`/memories/${encodeURIComponent(memoryId)}/why`);
  }

  async deleteMemory(memoryId: string): Promise<void> {
    await this.request(`/memories/${encodeURIComponent(memoryId)}`, {
      method: "DELETE"
    });
  }

  async restoreMemory(memoryId: string): Promise<void> {
    await this.request(`/memories/${encodeURIComponent(memoryId)}/restore`, {
      method: "POST"
    });
  }

  async updateMemory(
    memoryId: string,
    payload: MemoryUpdatePayload
  ): Promise<MemoryUpdateResult> {
    return this.request(`/memories/${encodeURIComponent(memoryId)}`, {
      method: "PATCH",
      body: payload
    });
  }

  async memoryReport(): Promise<MemoryReport> {
    return this.request("/memories/report?format=json");
  }

  async memoryReportMarkdown(): Promise<string> {
    return this.request("/memories/report?format=markdown", { text: true });
  }

  async exportMemories(format: "json" | "markdown"): Promise<MemoryExport | string> {
    return this.request(`/memories/export?format=${format}&include_deleted=true`, {
      text: format === "markdown"
    });
  }

  async restoreFromExport(
    data: MemoryExport,
    overwrite: boolean,
    includeDeleted: boolean
  ): Promise<RestoreResult> {
    return this.request("/memories/restore", {
      method: "POST",
      body: {
        data,
        overwrite,
        include_deleted: includeDeleted
      }
    });
  }

  async decisionLogs(limit = 100): Promise<DecisionLog[]> {
    const payload = await this.request<{ data: DecisionLog[] }>(
      `/memories/decision-logs?limit=${limit}`
    );
    return payload.data || [];
  }

  async recentContext(): Promise<RecentContextSummary[]> {
    const payload = await this.request<{ data: RecentContextSummary[] }>(
      "/memories/recent-context"
    );
    return payload.data || [];
  }

  async coreMemory(): Promise<CoreMemorySection[]> {
    const payload = await this.request<{ data: CoreMemorySection[] }>("/memories/core");
    return payload.data || [];
  }

  async coreHistory(): Promise<CoreMemoryHistoryItem[]> {
    const payload = await this.request<{ data: CoreMemoryHistoryItem[] }>(
      "/memories/core/history?limit=200"
    );
    return payload.data || [];
  }

  async consolidateCoreMemory(): Promise<unknown> {
    return this.request("/memories/core/consolidate", {
      method: "POST"
    });
  }

  async reviewMemories(): Promise<ReviewResult> {
    return this.request("/memories/review", {
      method: "POST"
    });
  }

  async mergeMemories(memoryIds: string[], content?: string | null): Promise<unknown> {
    return this.request("/memories/merge", {
      method: "POST",
      body: {
        memory_ids: memoryIds,
        content: content || undefined
      }
    });
  }

  private async request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers = new Headers();
    const auth = options.auth !== false;
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json; charset=utf-8");
    }
    if (auth && this.settings.apiKey) {
      headers.set("Authorization", `Bearer ${this.settings.apiKey}`);
    }
    if (auth) {
      headers.set("X-User-Id", this.settings.userId || "default");
    }

    const response = await fetch(this.base(path), {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });

    if (!response.ok) {
      throw new ApiError(response.status, await readError(response));
    }

    if (options.text) {
      return (await response.text()) as T;
    }
    return (await response.json()) as T;
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    if (typeof payload.detail === "string") {
      return payload.detail;
    }
    return JSON.stringify(payload);
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}
