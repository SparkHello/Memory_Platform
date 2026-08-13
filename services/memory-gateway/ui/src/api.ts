import type {
  AuthTokenCreateResult,
  AuthTokenListResult,
  AuthTokenRevokeResult,
  ConnectionSettings,
  ConversationBranchArchiveResult,
  ConversationBranchList,
  ConversationBranchRestoreResult,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreMemoryUpdatePayload,
  CoreMemoryUpdateResult,
  DecisionLog,
  MemoryExport,
  ModelUsageSummary,
  ModelGatewayBundleResult,
  ModelGatewayChannelDiscoverBody,
  ModelGatewayChannelDiscoverResult,
  ModelGatewayCapabilityProbeBody,
  ModelGatewayCapabilityProbeResult,
  ModelGatewayChannelBundleBody,
  ModelGatewayConnectionCheck,
  ModelGatewayConnectionCreateBody,
  ModelGatewayConnectionCreateResult,
  ModelGatewayControlSnapshot,
  ModelGatewayDeploymentApplyBody,
  ModelGatewayDeploymentApplyResult,
  ModelGatewayObjectMutationResult,
  ModelGatewayRouteChangeResult,
  ModelGatewayRouteDraft,
  ProvidersStatus,
  DatabaseHealthResult,
  MechanismDiagnosisResult,
  MemoryContextExplainResult,
  MemoryNetwork,
  MemoryStatus,
  MemoryRecord,
  MemoryReport,
  MemoryBatchPurgeResult,
  MemoryPurgeResult,
  MemoryPurgePreviewResult,
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
  StackBackupValidationResult,
  ConversationImportPreviewResult,
  ConversationImportCommitResult,
  ReviewResult,
  SearchFeedbackValue,
  SurfaceMode,
  TraversalResponse
} from "./types";
import type {
  KnowledgeDocument,
  KnowledgeDocumentDetail,
  KnowledgeDocumentStatus,
  KnowledgeExport,
  KnowledgeReadResponse,
  KnowledgeRestoreResult,
  KnowledgeSearchQuality,
  KnowledgeSearchResponse,
  KnowledgeStatus,
  KnowledgeUploadCommitResult,
  KnowledgeUploadSession
} from "./types";
import { normalizeBaseUrl } from "./storage";

const DEFAULT_TIMEOUT_MS = 30000;
const REVIEW_RELATED_TIMEOUT_MS = 90000;
const REVIEW_PREVIEW_TIMEOUT_MS = 120000;

type RequestOptions = {
  method?: string;
  body?: unknown;
  rawBody?: BodyInit;
  contentType?: string;
  auth?: boolean;
  text?: boolean;
  blob?: boolean;
  signal?: AbortSignal;
  timeoutMs?: number;
  timeoutMessage?: string;
  headers?: Record<string, string>;
};

type RedactionOptions = {
  redactSensitive?: boolean;
  includeSensitive?: boolean;
};

type MemoryListOptions = RedactionOptions & {
  status?: MemoryStatus | "all";
};

export class ApiError extends Error {
  status: number;
  detail: string;
  code?: string;
  data?: Record<string, unknown>;
  /** Request path (no query), used by errorMessage for credential-aware copy. */
  path?: string;

  constructor(
    status: number,
    detail: string,
    code?: string,
    data?: Record<string, unknown>,
    path?: string
  ) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.code = code;
    this.data = data;
    this.path = path;
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
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

  async health(signal?: AbortSignal): Promise<{ status: string }> {
    return this.request("/health", { auth: false, signal });
  }

  async authTokens(signal?: AbortSignal): Promise<AuthTokenListResult> {
    return this.request("/auth/tokens", { signal });
  }

  async createAuthToken(
    name: string,
    role: "chat" | "mcp",
    options: { memoryAccess?: "read" | "read-write"; signal?: AbortSignal } = {}
  ): Promise<AuthTokenCreateResult> {
    return this.request("/auth/tokens", {
      method: "POST",
      body: {
        name,
        role,
        ...(role === "chat" && options.memoryAccess
          ? { memory_access: options.memoryAccess }
          : {})
      },
      signal: options.signal
    });
  }

  async revokeAuthToken(
    tokenId: string,
    signal?: AbortSignal
  ): Promise<AuthTokenRevokeResult> {
    return this.request(`/auth/tokens/${encodeURIComponent(tokenId)}`, {
      method: "DELETE",
      signal
    });
  }

  async modelUsage(
    range: "7" | "30" | "90" | "all" = "30",
    signal?: AbortSignal
  ): Promise<ModelUsageSummary> {
    return this.request(`/usage/summary?range=${range}`, { signal });
  }

  async knowledgeStatus(signal?: AbortSignal): Promise<KnowledgeStatus> {
    return this.request("/knowledge/status", { signal });
  }

  async providersStatus(signal?: AbortSignal): Promise<ProvidersStatus> {
    return this.request("/providers/status", { signal });
  }

  async liveUpstreamProbe(signal?: AbortSignal): Promise<{
    ok: boolean;
    code: string;
    message: string;
    latency_ms?: number;
    cached?: boolean;
    route?: string;
  }> {
    return this.request("/providers/live-probe", { method: "POST", signal });
  }

  async checkProviderAdminKey(
    adminKey: string,
    signal?: AbortSignal
  ): Promise<{ valid: boolean }> {
    return this.request("/providers/admin/check", {
      method: "POST",
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async providerAdminConfiguration(
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayControlSnapshot> {
    return this.request("/providers/admin/configuration", {
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async discoverProviderChannel(
    body: ModelGatewayChannelDiscoverBody,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayChannelDiscoverResult> {
    return this.request("/providers/channels/discover", {
      method: "POST",
      body,
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal,
      timeoutMs: 30000,
      timeoutMessage: "渠道模型发现超时，请确认供应商 API 地址可以访问"
    });
  }

  async probeProviderChannelCapabilities(
    body: ModelGatewayCapabilityProbeBody,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayCapabilityProbeResult> {
    return this.request("/providers/channels/probe-capabilities", {
      method: "POST",
      body,
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal,
      timeoutMs: 120000,
      timeoutMessage: "能力探测超时；请确认供应商可达，或减少探测项后重试"
    });
  }

  async validateProviderChannelBundle(
    body: ModelGatewayChannelBundleBody,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayBundleResult> {
    return this.request("/providers/channel-bundles/validate", {
      method: "POST",
      body,
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal,
      timeoutMs: 30000,
      timeoutMessage: "渠道配置校验超时，请稍后重试"
    });
  }

  async applyProviderChannelBundle(
    body: ModelGatewayChannelBundleBody,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayBundleResult> {
    return this.request("/providers/channel-bundles/apply", {
      method: "POST",
      body,
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal,
      timeoutMs: 30000,
      timeoutMessage: "渠道配置应用超时，请刷新确认是否已经生效"
    });
  }

  async setProviderObjectEnabled(
    collection: "connections" | "deployments",
    id: string,
    revision: string,
    enabled: boolean,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayObjectMutationResult> {
    return this.request(`/providers/${collection}/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: { revision, enabled },
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async deleteProviderObject(
    collection: "connections" | "deployments" | "pricing",
    id: string,
    revision: string,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayObjectMutationResult> {
    return this.request(`/providers/${collection}/${encodeURIComponent(id)}`, {
      method: "DELETE",
      body: { revision },
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async validateProviderRoutes(
    revision: string,
    routes: ModelGatewayRouteDraft[],
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayRouteChangeResult> {
    return this.request("/providers/routes/validate", {
      method: "POST",
      body: { revision, routes },
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async applyProviderRoutes(
    revision: string,
    routes: ModelGatewayRouteDraft[],
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayRouteChangeResult> {
    return this.request("/providers/routes", {
      method: "PUT",
      body: { revision, routes },
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async updateProviderSecret(
    connectionId: string,
    value: string,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<{ connection_id: string; configured: boolean }> {
    return this.request(
      `/providers/connections/${encodeURIComponent(connectionId)}/secret`,
      {
        method: "PUT",
        body: { value },
        headers: { "X-Model-Gateway-Admin-Key": adminKey },
        signal
      }
    );
  }

  async checkProviderConnection(
    connectionId: string,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayConnectionCheck> {
    return this.request(
      `/providers/connections/${encodeURIComponent(connectionId)}/check`,
      {
        method: "POST",
        headers: { "X-Model-Gateway-Admin-Key": adminKey },
        signal,
        timeoutMs: 30000,
        timeoutMessage: "渠道检查超时，请确认 Model Gateway 和供应商服务可访问"
      }
    );
  }

  async createProviderConnection(
    body: ModelGatewayConnectionCreateBody,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayConnectionCreateResult> {
    return this.request("/providers/connections", {
      method: "POST",
      body,
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async applyProviderDeployments(
    body: ModelGatewayDeploymentApplyBody,
    adminKey: string,
    signal?: AbortSignal
  ): Promise<ModelGatewayDeploymentApplyResult> {
    return this.request("/providers/deployments", {
      method: "POST",
      body,
      headers: { "X-Model-Gateway-Admin-Key": adminKey },
      signal
    });
  }

  async listKnowledgeDocuments(
    options: { status?: KnowledgeDocumentStatus; query?: string; limit?: number } = {},
    signal?: AbortSignal
  ): Promise<KnowledgeDocument[]> {
    const params = new URLSearchParams({
      status: options.status || "active",
      limit: String(options.limit ?? 500)
    });
    if (options.query?.trim()) params.set("query", options.query.trim());
    const payload = await this.request<{ data: KnowledgeDocument[] }>(
      `/knowledge/documents?${params.toString()}`,
      { signal }
    );
    return payload.data || [];
  }

  async knowledgeDocument(reference: string, signal?: AbortSignal): Promise<KnowledgeDocumentDetail> {
    return this.request(`/knowledge/documents/${encodeURIComponent(reference)}`, { signal });
  }

  async beginKnowledgeUpload(
    payload: {
      title: string;
      content_type: string;
      source_name?: string;
      replace_document_ref?: string;
      sensitivity?: string;
      tags?: string[];
      metadata?: Record<string, string | number | boolean>;
    },
    signal?: AbortSignal
  ): Promise<KnowledgeUploadSession> {
    return this.request("/knowledge/uploads", {
      method: "POST",
      body: {
        title: payload.title,
        content_type: payload.content_type,
        source_name: payload.source_name || "",
        replace_document_ref: payload.replace_document_ref || "",
        sensitivity: payload.sensitivity || "normal",
        tags: payload.tags,
        metadata: payload.metadata
      },
      signal
    });
  }

  async appendKnowledgeUpload(
    uploadId: string,
    sequence: number,
    text: string,
    signal?: AbortSignal
  ): Promise<unknown> {
    return this.request(
      `/knowledge/uploads/${encodeURIComponent(uploadId)}/parts/${sequence}`,
      { method: "PUT", body: { text }, signal }
    );
  }

  async commitKnowledgeUpload(
    uploadId: string,
    expectedParts: number,
    expectedSha256 = "",
    confirmSensitivityOverride = false,
    signal?: AbortSignal
  ): Promise<KnowledgeUploadCommitResult> {
    return this.request(`/knowledge/uploads/${encodeURIComponent(uploadId)}/commit`, {
      method: "POST",
      body: {
        expected_parts: expectedParts,
        expected_sha256: expectedSha256,
        confirm_sensitivity_override: confirmSensitivityOverride
      },
      signal,
      timeoutMs: 120000
    });
  }

  async cancelKnowledgeUpload(uploadId: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/knowledge/uploads/${encodeURIComponent(uploadId)}`, {
      method: "DELETE",
      signal
    });
  }

  async importKnowledgeFile(
    file: File,
    payload: {
      title?: string;
      source_name?: string;
      replace_document_ref?: string;
      sensitivity?: string;
      confirm_sensitivity_override?: boolean;
      tags?: string[];
      metadata?: Record<string, string | number | boolean>;
    },
    signal?: AbortSignal
  ): Promise<KnowledgeUploadCommitResult> {
    const params = new URLSearchParams({
      filename: file.name,
      title: payload.title || "",
      source_name: payload.source_name || file.name,
      replace_document_ref: payload.replace_document_ref || "",
      sensitivity: payload.sensitivity || "normal"
    });
    if (payload.tags?.length) params.set("tags", payload.tags.join(","));
    if (payload.confirm_sensitivity_override) {
      params.set("confirm_sensitivity_override", "true");
    }
    if (payload.metadata && Object.keys(payload.metadata).length) {
      params.set("metadata_json", JSON.stringify(payload.metadata));
    }
    return this.request(`/knowledge/import?${params.toString()}`, {
      method: "POST",
      rawBody: file,
      contentType: file.type || "application/octet-stream",
      signal,
      timeoutMs: 120000
    });
  }

  async updateKnowledgeDocument(
    reference: string,
    payload: {
      title?: string;
      source_name?: string;
      sensitivity?: string;
      tags?: string[];
      metadata?: Record<string, string | number | boolean>;
    },
    signal?: AbortSignal
  ): Promise<KnowledgeDocument> {
    const result = await this.request<{ document?: KnowledgeDocument } & KnowledgeDocument>(
      `/knowledge/documents/${encodeURIComponent(reference)}`,
      { method: "PATCH", body: payload, signal }
    );
    return result.document || result;
  }

  async deleteKnowledgeDocument(reference: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/knowledge/documents/${encodeURIComponent(reference)}`, {
      method: "DELETE",
      signal
    });
  }

  async restoreKnowledgeDocument(reference: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/knowledge/documents/${encodeURIComponent(reference)}/restore`, {
      method: "POST",
      signal
    });
  }

  async purgeKnowledgeDocument(reference: string, confirmDocumentId: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/knowledge/deleted/${encodeURIComponent(reference)}/purge`, {
      method: "DELETE",
      body: { confirm_document_id: confirmDocumentId },
      signal
    });
  }

  async restoreKnowledgeVersion(
    documentReference: string,
    versionReference: string,
    signal?: AbortSignal
  ): Promise<KnowledgeUploadCommitResult> {
    return this.request(
      `/knowledge/documents/${encodeURIComponent(documentReference)}/versions/${encodeURIComponent(versionReference)}/restore`,
      { method: "POST", signal, timeoutMs: 120000 }
    );
  }

  async reindexKnowledgeDocument(
    documentReference: string,
    versionReference: string,
    signal?: AbortSignal
  ): Promise<unknown> {
    return this.request(
      `/knowledge/documents/${encodeURIComponent(documentReference)}/versions/${encodeURIComponent(versionReference)}/reindex`,
      { method: "POST", signal, timeoutMs: 120000 }
    );
  }

  async searchKnowledge(
    options: {
      request: string;
      limit?: number;
      documentRefs?: string[];
      tags?: string[];
      metadataFilter?: Record<string, string | number | boolean>;
      quality?: KnowledgeSearchQuality;
      includeSensitive?: boolean;
      timeoutMs?: number;
    },
    signal?: AbortSignal
  ): Promise<KnowledgeSearchResponse> {
    return this.request("/knowledge/search", {
      method: "POST",
      body: {
        request: options.request,
        limit: options.limit ?? 5,
        document_refs: options.documentRefs || [],
        tags: options.tags || [],
        metadata_filter: options.metadataFilter || {},
        quality: options.quality || "balanced",
        include_sensitive: options.includeSensitive ?? false
      },
      signal,
      timeoutMs: options.timeoutMs ?? 35000
    });
  }

  async readKnowledge(
    options: {
      reference: string;
      cursor?: string;
      maxChars?: number;
      includeSensitive?: boolean;
    },
    signal?: AbortSignal
  ): Promise<KnowledgeReadResponse> {
    return this.request("/knowledge/read", {
      method: "POST",
      body: {
        reference: options.reference,
        cursor: options.cursor || "",
        max_chars: options.maxChars ?? 20000,
        include_sensitive: options.includeSensitive ?? false
      },
      signal
    });
  }

  async exportKnowledge(signal?: AbortSignal): Promise<KnowledgeExport> {
    return this.request("/knowledge/export", { signal, timeoutMs: 120000 });
  }

  async restoreKnowledge(data: KnowledgeExport, signal?: AbortSignal): Promise<KnowledgeRestoreResult> {
    return this.request("/knowledge/restore", {
      method: "POST",
      body: { data },
      signal,
      timeoutMs: 120000
    });
  }

  async listMemories(options: MemoryListOptions = {}, signal?: AbortSignal): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>(
      `/memories${memoryListQuery(options)}`,
      { signal }
    );
    return payload.data || [];
  }

  async listDeletedMemories(options: RedactionOptions = {}, signal?: AbortSignal): Promise<MemoryRecord[]> {
    const payload = await this.request<{ data: MemoryRecord[] }>(
      `/memories/deleted?limit=1000${redactionSuffix(options.redactSensitive)}`,
      { signal }
    );
    return payload.data || [];
  }

  async listMemorySpaces(
    options: { includeArchived?: boolean; signal?: AbortSignal } = {}
  ): Promise<MemorySpace[]> {
    const params = new URLSearchParams();
    if (options.includeArchived) params.set("include_archived", "true");
    const query = params.toString();
    const payload = await this.request<{ data: MemorySpace[] }>(
      `/memories/spaces${query ? `?${query}` : ""}`,
      { signal: options.signal }
    );
    return payload.data || [];
  }

  async createMemorySpace(
    body: {
      name: string;
      color?: string | null;
      description?: string | null;
      sort_order?: number | null;
    },
    signal?: AbortSignal
  ): Promise<{ space: MemorySpace }> {
    return this.request("/memories/spaces", {
      method: "POST",
      body,
      signal
    });
  }

  async updateMemorySpace(
    spaceId: string,
    body: {
      name?: string;
      color?: string | null;
      description?: string | null;
      sort_order?: number | null;
    },
    signal?: AbortSignal
  ): Promise<{ space: MemorySpace }> {
    return this.request(`/memories/spaces/${encodeURIComponent(spaceId)}`, {
      method: "PATCH",
      body,
      signal
    });
  }

  async archiveMemorySpace(
    spaceId: string,
    signal?: AbortSignal
  ): Promise<{ space: MemorySpace }> {
    return this.request(`/memories/spaces/${encodeURIComponent(spaceId)}/archive`, {
      method: "POST",
      signal
    });
  }

  async unarchiveMemorySpace(
    spaceId: string,
    signal?: AbortSignal
  ): Promise<{ space: MemorySpace }> {
    return this.request(`/memories/spaces/${encodeURIComponent(spaceId)}/unarchive`, {
      method: "POST",
      signal
    });
  }

  async deleteMemorySpace(
    spaceId: string,
    signal?: AbortSignal
  ): Promise<{ deleted: boolean; space_id: string }> {
    return this.request(`/memories/spaces/${encodeURIComponent(spaceId)}`, {
      method: "DELETE",
      signal
    });
  }

  async memorySpace(
    spaceId: string,
    options: RedactionOptions = {},
    signal?: AbortSignal
  ): Promise<MemorySpaceDetail> {
    return this.request(
      `/memories/spaces/${encodeURIComponent(spaceId)}?limit=1000${redactionSuffix(
        options.redactSensitive
      )}`,
      { signal }
    );
  }

  async searchMemories(
    query: string,
    limit = 20,
    options: RedactionOptions = {},
    signal?: AbortSignal
  ): Promise<MemorySearchRecord[]> {
    const payload = await this.request<{ data: MemorySearchRecord[] }>("/memories/search", {
      method: "POST",
      body: {
        query,
        limit,
        include_sensitive: options.includeSensitive ?? false,
        redact_sensitive: options.redactSensitive ?? false
      },
      signal
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
  }, signal?: AbortSignal): Promise<MemoryContextExplainResult> {
    return this.request("/memories/context/explain", {
      method: "POST",
      body: {
        query: options.query,
        limit: options.limit ?? 5,
        include_core_memory: options.includeCoreMemory ?? true,
        include_recent_context: options.includeRecentContext ?? true,
        conversation_id: options.conversationId || undefined,
        redact_sensitive: options.redactSensitive ?? false
      },
      signal
    });
  }

  async submitSearchFeedback(options: {
    query: string;
    memoryId?: string | null;
    feedback: SearchFeedbackValue;
    note?: string | null;
  }, signal?: AbortSignal): Promise<{ recorded: boolean; log: DecisionLog }> {
    return this.request("/memories/search-feedback", {
      method: "POST",
      body: {
        query: options.query,
        memory_id: options.memoryId || undefined,
        feedback: options.feedback,
        note: options.note || undefined
      },
      signal
    });
  }

  async surfaceMemories(
    limit = 8,
    mode: SurfaceMode = "balanced",
    options: RedactionOptions = {},
    signal?: AbortSignal
  ): Promise<MemorySurfaceRecord[]> {
    const payload = await this.request<{ data: MemorySurfaceRecord[] }>("/memories/surface", {
      method: "POST",
      body: {
        limit,
        mode,
        include_sensitive: options.includeSensitive ?? false,
        redact_sensitive: options.redactSensitive ?? false
      },
      signal
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
    } = {},
    signal?: AbortSignal
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
      },
      signal
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
    } = {},
    signal?: AbortSignal
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
      },
      signal
    });
  }

  async getMemory(memoryId: string, options: RedactionOptions = {}, signal?: AbortSignal): Promise<MemoryRecord> {
    const payload = await this.request<{ memory: MemoryRecord }>(
      `/memories/${encodeURIComponent(memoryId)}${redactionQuery(options.redactSensitive)}`,
      { signal }
    );
    return payload.memory;
  }

  async whyRemember(
    memoryId: string,
    options: RedactionOptions = {},
    signal?: AbortSignal
  ): Promise<MemorySourceExplanation> {
    return this.request(
      `/memories/${encodeURIComponent(memoryId)}/why${redactionQuery(options.redactSensitive)}`,
      { signal }
    );
  }

  async deleteMemory(memoryId: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/memories/${encodeURIComponent(memoryId)}`, {
      method: "DELETE",
      signal
    });
  }

  async restoreMemory(memoryId: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/memories/${encodeURIComponent(memoryId)}/restore`, {
      method: "POST",
      signal
    });
  }

  async purgeDeletedMemory(memoryId: string, signal?: AbortSignal): Promise<MemoryPurgeResult> {
    return this.request(`/memories/deleted/${encodeURIComponent(memoryId)}/purge`, {
      method: "DELETE",
      body: { confirm_memory_id: memoryId },
      signal
    });
  }

  async previewDeletedMemoriesPurge(
    memoryIds: string[],
    signal?: AbortSignal
  ): Promise<MemoryPurgePreviewResult> {
    return this.request("/memories/deleted/purge/preview", {
      method: "POST",
      body: { memory_ids: memoryIds },
      signal
    });
  }

  async commitDeletedMemoriesPurge(
    memoryIds: string[],
    fingerprint: string,
    previewToken: string,
    signal?: AbortSignal
  ): Promise<MemoryBatchPurgeResult> {
    return this.request("/memories/deleted/purge/commit", {
      method: "POST",
      body: {
        memory_ids: memoryIds,
        fingerprint,
        preview_token: previewToken
      },
      signal
    });
  }

  async updateMemory(
    memoryId: string,
    payload: MemoryUpdatePayload,
    expectedRevision: number,
    signal?: AbortSignal
  ): Promise<MemoryUpdateResult> {
    return this.request(`/memories/${encodeURIComponent(memoryId)}`, {
      method: "PATCH",
      body: { ...payload, expected_revision: expectedRevision },
      signal
    });
  }

  async updateMemorySpaces(
    memoryId: string,
    payload: MemorySpacesUpdatePayload,
    expectedRevision: number,
    signal?: AbortSignal
  ): Promise<MemoryUpdateResult> {
    return this.request(`/memories/${encodeURIComponent(memoryId)}/spaces`, {
      method: "PATCH",
      body: { ...payload, expected_revision: expectedRevision },
      signal
    });
  }

  async memoryReport(signal?: AbortSignal): Promise<MemoryReport> {
    return this.request("/memories/report?format=json", { signal });
  }

  async memoryReportMarkdown(signal?: AbortSignal): Promise<string> {
    return this.request("/memories/report?format=markdown", { text: true, signal });
  }

  async exportMemories(format: "json" | "markdown", signal?: AbortSignal): Promise<MemoryExport | string> {
    return this.request(`/memories/export?format=${format}&include_deleted=true`, {
      text: format === "markdown",
      signal
    });
  }

  async exportSelectedMemories(
    memoryIds: string[],
    signal?: AbortSignal
  ): Promise<MemoryExport> {
    return this.request("/memories/export/selection", {
      method: "POST",
      body: { memory_ids: memoryIds },
      signal
    });
  }

  async exportObsidianZip(signal?: AbortSignal): Promise<Blob> {
    return this.request("/memories/export?format=obsidian_markdown&include_deleted=true", {
      blob: true,
      signal
    });
  }

  /**
   * Portable stack zip (memory + knowledge + auth + model config, no secrets).
   * On split Docker, pass the Model Gateway admin key so Memory can fetch config.
   */
  async exportStackBackup(
    options: { modelGatewayAdminKey?: string } = {},
    signal?: AbortSignal
  ): Promise<Blob> {
    const headers: Record<string, string> = {};
    const adminKey = options.modelGatewayAdminKey?.trim();
    if (adminKey) {
      headers["X-Model-Gateway-Admin-Key"] = adminKey;
    }
    return this.request("/memories/stack-backup", {
      method: "POST",
      blob: true,
      headers,
      signal,
      timeoutMs: 120000,
      timeoutMessage: "整栈备份超时；数据量较大时可稍后重试或使用 CLI"
    });
  }

  /**
   * Dry-run validate a portable stack zip. Does not restore or write DBs.
   */
  async validateStackBackup(
    file: File | Blob,
    signal?: AbortSignal
  ): Promise<StackBackupValidationResult> {
    const body = new FormData();
    body.append("file", file, file instanceof File ? file.name : "memory-stack.zip");
    return this.request("/memories/stack-backup/validate", {
      method: "POST",
      rawBody: body,
      signal,
      timeoutMs: 120000,
      timeoutMessage: "备份校验超时；文件较大时可稍后重试"
    });
  }

  async previewConversationImport(
    content: string,
    options: { maxTurns?: number; signal?: AbortSignal } = {}
  ): Promise<ConversationImportPreviewResult> {
    return this.request("/memories/import/conversations/preview", {
      method: "POST",
      body: {
        content,
        max_turns: options.maxTurns
      },
      signal: options.signal
    });
  }

  async commitConversationImport(
    content: string,
    options: { maxTurns?: number; signal?: AbortSignal } = {}
  ): Promise<ConversationImportCommitResult> {
    return this.request("/memories/import/conversations/commit", {
      method: "POST",
      body: {
        content,
        max_turns: options.maxTurns
      },
      signal: options.signal,
      timeoutMs: 300000,
      timeoutMessage: "对话导入超时；可减少轮数后重试"
    });
  }

  async restoreFromExport(
    data: MemoryExport,
    overwrite: boolean,
    includeDeleted: boolean,
    signal?: AbortSignal
  ): Promise<RestoreResult> {
    return this.request("/memories/restore", {
      method: "POST",
      body: {
        data,
        overwrite,
        include_deleted: includeDeleted
      },
      signal
    });
  }

  async decisionLogs(limit = 100, options: { memoryId?: string } = {}, signal?: AbortSignal): Promise<DecisionLog[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (options.memoryId) {
      params.set("memory_id", options.memoryId);
    }
    const payload = await this.request<{ data: DecisionLog[] }>(
      `/memories/decision-logs?${params.toString()}`,
      { signal }
    );
    return payload.data || [];
  }

  async recentContext(signal?: AbortSignal): Promise<RecentContextSummary[]> {
    const payload = await this.request<{ data: RecentContextSummary[] }>(
      "/memories/recent-context",
      { signal }
    );
    return payload.data || [];
  }

  async conversationBranches(
    limit = 500,
    status: "active" | "archived" = "active",
    signal?: AbortSignal
  ): Promise<ConversationBranchList> {
    const params = new URLSearchParams({
      limit: String(Math.max(1, Math.min(limit, 1000))),
      status
    });
    return this.request(`/memories/conversation-branches?${params.toString()}`, { signal });
  }

  async archiveConversationBranch(
    nodeId: string,
    signal?: AbortSignal
  ): Promise<ConversationBranchArchiveResult> {
    return this.request(
      `/memories/conversation-branches/${encodeURIComponent(nodeId)}`,
      { method: "DELETE", signal }
    );
  }

  async restoreConversationBranch(
    nodeId: string,
    signal?: AbortSignal
  ): Promise<ConversationBranchRestoreResult> {
    return this.request(
      `/memories/conversation-branches/${encodeURIComponent(nodeId)}/restore`,
      { method: "POST", signal }
    );
  }

  async coreMemory(signal?: AbortSignal): Promise<CoreMemorySection[]> {
    const payload = await this.request<{ data: CoreMemorySection[] }>("/memories/core", { signal });
    return payload.data || [];
  }

  async getCoreMemorySection(
    section: CoreMemorySection["section"],
    signal?: AbortSignal
  ): Promise<CoreMemorySection> {
    const payload = await this.request<{ core_memory: CoreMemorySection }>(
      `/memories/core/${encodeURIComponent(section)}`,
      { signal }
    );
    return payload.core_memory;
  }

  async updateCoreMemorySection(
    section: CoreMemorySection["section"],
    payload: CoreMemoryUpdatePayload,
    expectedRevision: number,
    signal?: AbortSignal
  ): Promise<CoreMemoryUpdateResult> {
    return this.request(`/memories/core/${encodeURIComponent(section)}`, {
      method: "PATCH",
      body: { ...payload, expected_revision: expectedRevision },
      signal
    });
  }

  async coreHistory(signal?: AbortSignal): Promise<CoreMemoryHistoryItem[]> {
    const payload = await this.request<{ data: CoreMemoryHistoryItem[] }>(
      "/memories/core/history?limit=200",
      { signal }
    );
    return payload.data || [];
  }

  async consolidateCoreMemory(signal?: AbortSignal): Promise<unknown> {
    return this.request("/memories/core/consolidate", {
      method: "POST",
      signal
    });
  }

  async reviewMemories(signal?: AbortSignal): Promise<ReviewResult> {
    return this.request("/memories/review", {
      method: "POST",
      signal
    });
  }

  async memoryHealth(signal?: AbortSignal): Promise<DatabaseHealthResult> {
    return this.request("/memories/health", { signal });
  }

  async reEmbedMemories(
    options: {
      scan?: boolean;
      memoryIds?: string[];
      includeSensitive?: boolean;
      signal?: AbortSignal;
    } = {}
  ): Promise<{ re_embedded: number; memory_ids: string[]; failed_ids: string[] }> {
    const memoryIds = options.memoryIds?.filter(Boolean) || [];
    return this.request("/memories/re-embed", {
      method: "POST",
      body: memoryIds.length
        ? {
            memory_ids: memoryIds,
            include_sensitive: options.includeSensitive ?? false
          }
        : {
            scan: true,
            include_sensitive: options.includeSensitive ?? false
          },
      signal: options.signal,
      timeoutMs: 120000
    });
  }

  async evaluationDiagnosis(signal?: AbortSignal): Promise<MechanismDiagnosisResult> {
    return this.request("/memories/evaluation/diagnosis", { signal });
  }

  async initRecallEvaluation(signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.request("/memories/evaluation/recall/init", {
      method: "POST",
      signal
    });
  }

  async recallEvaluationWorkbench(options: { redactSensitive?: boolean } = {}, signal?: AbortSignal): Promise<RecallEvalWorkbench> {
    const suffix = options.redactSensitive === false ? "?redact_sensitive=false" : "?redact_sensitive=true";
    return this.request(`/memories/evaluation/recall/workbench${suffix}`, { signal });
  }

  async saveRecallEvaluationLabels(labels: RecallEvalLabel[], signal?: AbortSignal): Promise<{
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
          judgment: label.judgment,
          relevant_ids: label.relevant_ids,
          note: label.note || undefined
        }))
      },
      signal
    });
  }

  async runRecallEvaluation(options: {
    mode: "keyword" | "embedding";
    k?: number;
  }, signal?: AbortSignal): Promise<RecallEvalRunResult> {
    return this.request("/memories/evaluation/recall/run", {
      method: "POST",
      body: {
        mode: options.mode,
        k: options.k ?? 8
      },
      signal
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
  }, signal?: AbortSignal): Promise<ReviewRevisionPreview> {
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
      },
      signal,
      timeoutMs: REVIEW_PREVIEW_TIMEOUT_MS,
      timeoutMessage: "AI 修改预览生成超时，上游模型可能正忙，请稍后重试"
    });
  }

  async findReviewRevisionRelated(options: {
    memoryIds: string[];
    userNote: string;
    recommendationReason?: string | null;
    suggestedContent?: string | null;
    limit?: number;
  }, signal?: AbortSignal): Promise<ReviewRelatedCandidate[]> {
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
        },
        signal,
        timeoutMs: REVIEW_RELATED_TIMEOUT_MS,
        timeoutMessage: "相关记忆检索超时，向量服务可能正忙，请稍后重试"
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
  }, signal?: AbortSignal): Promise<ReviewRevisionApplyResult> {
    return this.request("/memories/review/revise/apply", {
      method: "POST",
      body: {
        memory_ids: options.memoryIds,
        operations: options.operations,
        preview_token: options.previewToken,
        risk_tags: options.riskTags || [],
        severity: options.severity || undefined
      },
      signal
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
  }, signal?: AbortSignal): Promise<ReviewActionApplyResult> {
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
      },
      signal
    });
  }

  async mergeMemories(memoryIds: string[], content?: string | null, signal?: AbortSignal): Promise<unknown> {
    return this.request("/memories/merge", {
      method: "POST",
      body: {
        memory_ids: memoryIds,
        content: content || undefined
      },
      signal
    });
  }

  private async request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers = new Headers();
    const auth = options.auth !== false;
    if (options.rawBody !== undefined) {
      // FormData must omit Content-Type so the runtime can set the multipart boundary.
      if (options.contentType) {
        headers.set("Content-Type", options.contentType);
      } else if (!(options.rawBody instanceof FormData)) {
        headers.set("Content-Type", "application/octet-stream");
      }
    } else if (options.body !== undefined) {
      headers.set("Content-Type", "application/json; charset=utf-8");
    }
    if (auth && this.settings.apiKey) {
      headers.set("Authorization", `Bearer ${this.settings.apiKey}`);
    }
    if (auth) {
      headers.set("X-User-Id", this.settings.userId || "default");
    }
    for (const [name, value] of Object.entries(options.headers || {})) {
      headers.set(name, value);
    }

    // 手动 AbortController + setTimeout 实现超时兜底，同时串联调用方传入的 signal。
    const controller = new AbortController();
    const externalSignal = options.signal;
    const abortFromOutside = () => controller.abort();
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener("abort", abortFromOutside, { once: true });
    }
    let timedOut = false;
    const timeoutId = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);

    try {
      const response = await fetch(this.base(path), {
        method: options.method || "GET",
        headers,
        body: options.rawBody !== undefined
          ? options.rawBody
          : options.body === undefined
            ? undefined
            : JSON.stringify(options.body),
        signal: controller.signal
      });

      if (!response.ok) {
        const error = await readError(response);
        const pathOnly = path.split("?")[0] || path;
        throw new ApiError(
          response.status,
          error.message,
          error.code,
          error.data,
          pathOnly
        );
      }

      if (response.status === 204) {
        return undefined as T;
      }

      if (options.text) {
        return (await response.text()) as T;
      }
      if (options.blob) {
        return (await response.blob()) as T;
      }
      return (await response.json()) as T;
    } catch (error) {
      if (timedOut && isAbortError(error)) {
        throw new ApiError(
          0,
          options.timeoutMessage || "请求超时，请检查服务连接后重试"
        );
      }
      throw error;
    } finally {
      clearTimeout(timeoutId);
      externalSignal?.removeEventListener("abort", abortFromOutside);
    }
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

async function readError(response: Response): Promise<{
  message: string;
  code?: string;
  data?: Record<string, unknown>;
}> {
  try {
    const payload = await response.json() as Record<string, unknown>;
    if (typeof payload.detail === "string") {
      return { message: payload.detail };
    }
    if (payload.detail && typeof payload.detail === "object") {
      const detail = payload.detail as Record<string, unknown>;
      return {
        message: typeof detail.message === "string"
          ? detail.message
          : JSON.stringify(detail),
        code: typeof detail.code === "string" ? detail.code : undefined,
        data: detail
      };
    }
    return { message: JSON.stringify(payload) };
  } catch {
    return { message: response.statusText || `HTTP ${response.status}` };
  }
}
