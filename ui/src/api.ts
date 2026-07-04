import type {
  ConnectionSettings,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  DecisionLog,
  MemoryExport,
  DatabaseHealthResult,
  MechanismDiagnosisResult,
  MemoryContextExplainResult,
  MemoryNetwork,
  MemoryStatus,
  MemoryRecord,
  MemoryReport,
  MemoryPurgeResult,
  MemorySearchRecord,
  MemorySourceExplanation,
  MemorySpace,
  MemorySpaceDetail,
  MemorySpacesUpdatePayload,
  MemorySurfaceRecord,
  MemoryUpdatePayload,
  MemoryUpdateResult,
  RecentContextSummary,
  RecallEvalLabel,
  RecallEvalRunResult,
  RecallEvalWorkbench,
  ReviewRelatedCandidate,
  ReviewActionApplyResult,
  ReviewGovernanceAction,
  ReviewRecommendation,
  ReviewRevisionApplyResult,
  ReviewRevisionOperation,
  ReviewRevisionPreview,
  ReviewRiskTag,
  ReviewSeverity,
  RestoreResult,
  ReviewResult,
  SearchFeedbackValue,
  SurfaceMode,
  TraversalResponse
} from "./types";
import { normalizeBaseUrl } from "./storage";

type RequestOptions = {
  method?: string;
  body?: unknown;
  auth?: boolean;
  text?: boolean;
  blob?: boolean;
};

type RedactionOptions = {
  redactSensitive?: boolean;
};

type MemoryListOptions = RedactionOptions & {
  status?: MemoryStatus | "all";
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

  async listMemories(options: MemoryListOptions = {}): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>(
      `/memories${memoryListQuery(options)}`
    );
    return payload.data || [];
  }

  async listDeletedMemories(options: RedactionOptions = {}): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>(
      `/memories/deleted?limit=1000${redactionSuffix(options.redactSensitive)}`
    );
    return payload.data || [];
  }

  async listMemorySpaces(): Promise<MemorySpace[]> {
    const payload = await this.request<{ data: MemorySpace[] }>("/memories/spaces");
    return payload.data || [];
  }

  async memorySpace(
    spaceId: string,
    options: RedactionOptions = {}
  ): Promise<MemorySpaceDetail> {
    return this.request(
      `/memories/spaces/${encodeURIComponent(spaceId)}?limit=1000${redactionSuffix(
        options.redactSensitive
      )}`
    );
  }

  async searchMemories(
    query: string,
    limit = 20,
    options: RedactionOptions = {}
  ): Promise<MemorySearchRecord[]> {
    const payload = await this.request<{ data: MemorySearchRecord[] }>("/memories/search", {
      method: "POST",
      body: { query, limit, redact_sensitive: options.redactSensitive ?? false }
    });
    return payload.data || [];
  }

  async explainContext(options: {
    query: string;
    limit?: number;
    includeCoreMemory?: boolean;
    includeRecentContext?: boolean;
    conversationId?: string | null;
    redactSensitive?: boolean;
  }): Promise<MemoryContextExplainResult> {
    return this.request("/memories/context/explain", {
      method: "POST",
      body: {
        query: options.query,
        limit: options.limit ?? 5,
        include_core_memory: options.includeCoreMemory ?? true,
        include_recent_context: options.includeRecentContext ?? true,
        conversation_id: options.conversationId || undefined,
        redact_sensitive: options.redactSensitive ?? false
      }
    });
  }

  async submitSearchFeedback(options: {
    query: string;
    memoryId?: string | null;
    feedback: SearchFeedbackValue;
    note?: string | null;
  }): Promise<{ recorded: boolean; log: DecisionLog }> {
    return this.request("/memories/search-feedback", {
      method: "POST",
      body: {
        query: options.query,
        memory_id: options.memoryId || undefined,
        feedback: options.feedback,
        note: options.note || undefined
      }
    });
  }

  async surfaceMemories(
    limit = 8,
    mode: SurfaceMode = "balanced",
    options: RedactionOptions = {}
  ): Promise<MemorySurfaceRecord[]> {
    const payload = await this.request<{ data: MemorySurfaceRecord[] }>("/memories/surface", {
      method: "POST",
      body: { limit, mode, redact_sensitive: options.redactSensitive ?? false }
    });
    return payload.data || [];
  }

  async memoryNetwork(
    options: {
      limit?: number;
      similarityThreshold?: number;
      maxSimilarityEdges?: number;
      spaceId?: string;
      type?: string;
      sensitivity?: string;
      valenceMin?: number;
      valenceMax?: number;
      arousalMin?: number;
      arousalMax?: number;
      redactSensitive?: boolean;
    } = {}
  ): Promise<MemoryNetwork> {
    return this.request("/memories/network", {
      method: "POST",
      body: {
        limit: options.limit ?? 80,
        similarity_threshold: options.similarityThreshold ?? 0.42,
        max_similarity_edges: options.maxSimilarityEdges ?? 80,
        space_id: options.spaceId || undefined,
        type: options.type || undefined,
        sensitivity: options.sensitivity || undefined,
        valence_min: options.valenceMin,
        valence_max: options.valenceMax,
        arousal_min: options.arousalMin,
        arousal_max: options.arousalMax,
        redact_sensitive: options.redactSensitive ?? false
      }
    });
  }

  async traverseMemoryNetwork(
    seedId: string,
    options: {
      depth?: number;
      limit?: number;
      similarityThreshold?: number;
      maxCandidates?: number;
      maxEdges?: number;
      redactSensitive?: boolean;
    } = {}
  ): Promise<TraversalResponse> {
    return this.request("/memories/network/traverse", {
      method: "POST",
      body: {
        seed_id: seedId,
        depth: options.depth ?? 2,
        limit: options.limit ?? 10,
        similarity_threshold: options.similarityThreshold ?? 0.42,
        max_candidates: options.maxCandidates ?? 500,
        max_edges: options.maxEdges ?? 1500,
        redact_sensitive: options.redactSensitive ?? false
      }
    });
  }

  async getMemory(memoryId: string, options: RedactionOptions = {}): Promise<MemoryRecord> {
    const payload = await this.request<{ memory: MemoryRecord }>(
      `/memories/${encodeURIComponent(memoryId)}${redactionQuery(options.redactSensitive)}`
    );
    return payload.memory;
  }

  async whyRemember(
    memoryId: string,
    options: RedactionOptions = {}
  ): Promise<MemorySourceExplanation> {
    return this.request(
      `/memories/${encodeURIComponent(memoryId)}/why${redactionQuery(options.redactSensitive)}`
    );
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

  async purgeDeletedMemory(memoryId: string): Promise<MemoryPurgeResult> {
    return this.request(`/memories/deleted/${encodeURIComponent(memoryId)}/purge`, {
      method: "DELETE",
      body: { confirm_memory_id: memoryId }
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

  async updateMemorySpaces(
    memoryId: string,
    payload: MemorySpacesUpdatePayload
  ): Promise<MemoryUpdateResult> {
    return this.request(`/memories/${encodeURIComponent(memoryId)}/spaces`, {
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

  async exportObsidianZip(): Promise<Blob> {
    return this.request("/memories/export?format=obsidian_markdown&include_deleted=true", {
      blob: true
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

  async memoryHealth(): Promise<DatabaseHealthResult> {
    return this.request("/memories/health");
  }

  async evaluationDiagnosis(): Promise<MechanismDiagnosisResult> {
    return this.request("/memories/evaluation/diagnosis");
  }

  async initRecallEvaluation(): Promise<Record<string, unknown>> {
    return this.request("/memories/evaluation/recall/init", {
      method: "POST"
    });
  }

  async recallEvaluationWorkbench(options: { redactSensitive?: boolean } = {}): Promise<RecallEvalWorkbench> {
    const suffix = options.redactSensitive === false ? "?redact_sensitive=false" : "?redact_sensitive=true";
    return this.request(`/memories/evaluation/recall/workbench${suffix}`);
  }

  async saveRecallEvaluationLabels(labels: RecallEvalLabel[]): Promise<{
    labels: RecallEvalLabel[];
    summary: RecallEvalWorkbench["summary"];
    validation_issues: RecallEvalWorkbench["validation_issues"];
  }> {
    return this.request("/memories/evaluation/recall/labels", {
      method: "PUT",
      body: {
        labels: labels.map((label) => ({
          id: label.id,
          query: label.query,
          relevant_ids: label.relevant_ids,
          note: label.note || undefined
        }))
      }
    });
  }

  async runRecallEvaluation(options: {
    mode: "keyword" | "embedding";
    k?: number;
  }): Promise<RecallEvalRunResult> {
    return this.request("/memories/evaluation/recall/run", {
      method: "POST",
      body: {
        mode: options.mode,
        k: options.k ?? 8
      }
    });
  }

  async previewReviewRevision(options: {
    memoryIds: string[];
    userNote: string;
    recommendationReason?: string | null;
    relation?: ReviewRecommendation["relation"] | null;
    suggestedContent?: string | null;
    riskTags?: ReviewRiskTag[];
    severity?: ReviewSeverity | null;
  }): Promise<ReviewRevisionPreview> {
    return this.request("/memories/review/revise/preview", {
      method: "POST",
      body: {
        memory_ids: options.memoryIds,
        user_note: options.userNote,
        recommendation_reason: options.recommendationReason || undefined,
        relation: options.relation || undefined,
        suggested_content: options.suggestedContent || undefined,
        risk_tags: options.riskTags || [],
        severity: options.severity || undefined
      }
    });
  }

  async findReviewRevisionRelated(options: {
    memoryIds: string[];
    userNote: string;
    recommendationReason?: string | null;
    suggestedContent?: string | null;
    limit?: number;
  }): Promise<ReviewRelatedCandidate[]> {
    const payload = await this.request<{ data: ReviewRelatedCandidate[] }>(
      "/memories/review/revise/related",
      {
        method: "POST",
        body: {
          memory_ids: options.memoryIds,
          user_note: options.userNote,
          recommendation_reason: options.recommendationReason || undefined,
          suggested_content: options.suggestedContent || undefined,
          limit: options.limit ?? 8
        }
      }
    );
    return payload.data || [];
  }

  async applyReviewRevision(options: {
    memoryIds: string[];
    operations: ReviewRevisionOperation[];
    previewToken: string;
    riskTags?: ReviewRiskTag[];
    severity?: ReviewSeverity | null;
  }): Promise<ReviewRevisionApplyResult> {
    return this.request("/memories/review/revise/apply", {
      method: "POST",
      body: {
        memory_ids: options.memoryIds,
        operations: options.operations,
        preview_token: options.previewToken,
        risk_tags: options.riskTags || [],
        severity: options.severity || undefined
      }
    });
  }

  async applyReviewAction(options: {
    action: ReviewGovernanceAction;
    memoryIds: string[];
    reason?: string | null;
    riskTags?: ReviewRiskTag[];
    severity?: ReviewSeverity | null;
    reviewAfter?: string | null;
    content?: string | null;
  }): Promise<ReviewActionApplyResult> {
    return this.request("/memories/review/actions", {
      method: "POST",
      body: {
        action: options.action,
        memory_ids: options.memoryIds,
        reason: options.reason || undefined,
        risk_tags: options.riskTags || [],
        severity: options.severity || undefined,
        review_after: options.reviewAfter || undefined,
        content: options.content || undefined
      }
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
    if (options.blob) {
      return (await response.blob()) as T;
    }
    return (await response.json()) as T;
  }
}

function redactionQuery(redactSensitive?: boolean): string {
  return redactSensitive ? "?redact_sensitive=true" : "";
}

function memoryListQuery(options: MemoryListOptions): string {
  const params = new URLSearchParams();
  if (options.redactSensitive) {
    params.set("redact_sensitive", "true");
  }
  if (options.status) {
    params.set("status", options.status);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

function redactionSuffix(redactSensitive?: boolean): string {
  return redactSensitive ? "&redact_sensitive=true" : "";
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
