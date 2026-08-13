export type PageKey =
  | "dashboard"
  | "memories"
  | "knowledge"
  | "knowledgeSearch"
  | "core"
  | "review"
  | "recall"
  | "evaluation"
  | "recent"
  | "reports"
  | "logs"
  | "usage"
  | "providers"
  | "settings"
  | "developer";

export type MemoryType =
  | "episodic"
  | "semantic"
  | "procedural"
  | "emotional"
  | "reflective";

export type MemoryStability = "temporary" | "medium" | "stable";
export type MemoryStatus = "dynamic" | "resolved" | "archived" | "pinned";
export type MemorySensitivity = "normal" | "private" | "sensitive";
export type MemoryRedactionReason = "private" | "sensitive";
export type SurfaceMode = "balanced" | "important" | "emotional" | "stale" | "review_due";
export type SurfaceSignal =
  | "expired"
  | "review_due"
  | "near_expiry"
  | "sensitive"
  | "stale"
  | "emotion_uncertain"
  | "low_life";
export type DatabaseHealthStatus = "ok" | "warning" | "error";
export type DatabaseHealthSeverity = "info" | "warning" | "error";
export type DatabaseHealthIssueType =
  | "orphan_core_evidence"
  | "archived_core_evidence"
  | "orphan_space_link_memory"
  | "orphan_space_link_space"
  | "embedding_missing"
  | "embedding_invalid"
  | "embedding_dimension_mismatch"
  | "export_consistency_error"
  | "export_space_reference_missing"
  | "stale_search_cache_reference"
  | "orphan_core_history_evidence"
  | "orphan_decision_log_reference"
  | "invalid_decision_log_json";
export type MemoryAction = "create" | "update" | "ignore";
export type DecisionLogAction = MemoryAction | "purge";
export type ReviewAction = "keep" | "merge" | "lower" | "delete" | "review";
export type ReviewRiskTag =
  | "duplicate"
  | "conflict"
  | "expired"
  | "time_uncertain"
  | "sensitive"
  | "low_value"
  | "core_evidence"
  | "stale"
  | "emotion_uncertain";
export type ReviewSeverity = "low" | "medium" | "high";
export type ReviewNextAction =
  | "ai_modify"
  | "confirm_valid"
  | "move_to_trash"
  | "lower_importance"
  | "merge"
  | "review_core_memory"
  | "snooze";
export type ReviewGovernanceAction =
  | "confirm_valid"
  | "snooze"
  | "lower_importance"
  | "move_to_trash"
  | "merge";
export type ReviewRevisionOperationKind = "update" | "merge" | "archive" | "no_change";
export type ReviewPolicyCode = "temporary" | "sensitive" | "time_variable" | "stage" | "stable";
export type CoreSectionName =
  | "profile"
  | "preferences"
  | "relationships"
  | "routines"
  | "goals"
  | "communication";

export interface ConnectionSettings {
  apiBaseUrl: string;
  apiKey: string;
  userId: string;
}

export type AuthTokenRole = "chat" | "mcp" | "console";

export type AuthTokenMemoryAccess = "read" | "read-write";

export interface AuthTokenRecord {
  token_id: string;
  name: string;
  user_id: string;
  role: AuthTokenRole;
  memory_access?: AuthTokenMemoryAccess;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
  is_current: boolean;
  can_revoke: boolean;
  revoke_block_reason?: "last_active_console_token" | null;
}

export interface AuthTokenListResult {
  data: AuthTokenRecord[];
  current_user_id: string;
  legacy_key_enabled: boolean;
  authenticated_with_legacy_key: boolean;
  allowed_create_roles: Array<"chat" | "mcp">;
}

export interface AuthTokenCreateResult {
  token: string;
  record: AuthTokenRecord;
}

export interface AuthTokenRevokeResult {
  revoked: boolean;
  already_revoked: boolean;
  record: AuthTokenRecord;
}

export interface UsageTotals {
  calls: number;
  measured_calls: number;
  priced_calls: number;
  unmeasured_calls: number;
  unpriced_calls: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_cny: number;
  cache_hit_rate?: number | null;
}

export interface UsageModelBreakdown extends UsageTotals {
  provider: string;
  provider_label: string;
  model: string;
  kind: "chat" | "embedding";
}

export interface UsageOperationBreakdown extends UsageTotals {
  operation: string;
}

export interface UsageDailyBreakdown extends UsageTotals {
  date: string;
}

export interface UsageEvent {
  id: string;
  operation: string;
  provider: string;
  provider_label: string;
  provider_code: string;
  model: string;
  kind: "chat" | "embedding";
  input_tokens?: number | null;
  cached_input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  usage_available: boolean;
  price_available: boolean;
  cost_cny?: number | null;
  currency: string;
  price_key: string;
  pricing_as_of: string;
  pricing_source_url: string;
  created_at: string;
}

export interface UsagePrice {
  key: string;
  provider: string;
  provider_label: string;
  model: string;
  kind: "chat" | "embedding";
  currency: string;
  input_cache_hit_per_million: string;
  input_cache_miss_per_million: string;
  output_per_million: string;
  source_url: string;
  input_token_min: number;
  input_token_max?: number | null;
  input_range_label: string;
  as_of: string;
}

export interface LocalModelUsageSummary {
  range: {
    days?: number | null;
    start?: string | null;
    end: string;
  };
  totals: UsageTotals;
  by_model: UsageModelBreakdown[];
  by_operation: UsageOperationBreakdown[];
  daily: UsageDailyBreakdown[];
  recent: UsageEvent[];
  pricing: {
    as_of: string;
    currency: string;
    models: UsagePrice[];
    note: string;
  };
}

export interface CentralUsageDeployment {
  deployment_id: string;
  connection_id: string;
  channel_operator: string;
  model_author: string;
  upstream_model: string;
  calls: number;
  total_tokens: number;
}

export interface CentralModelUsageSummary {
  days: number;
  filters: {
    client_id: string;
    operation: string;
    user_tag: string;
  };
  calls: number;
  complete_calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_costs: Record<string, string>;
  incomplete_cost_calls: number;
  attempts: {
    recorded: number;
    known_cost_attempts: number;
    unknown_cost_attempts: number;
    not_sent_attempts: number;
    legacy_logical_events_without_attempts: number;
    by_currency: Record<
      string,
      { known_attempts: number; unknown_attempts: number; estimated_cost: string }
    >;
  };
  deployments: CentralUsageDeployment[];
  retention: {
    raw_days: number;
    daily_days: number;
  };
}

export type ModelUsageSummary = LocalModelUsageSummary | CentralModelUsageSummary;

export interface MemoryRecord {
  id: string;
  user_id?: string;
  content: string;
  type: MemoryType;
  importance: number;
  confidence: number;
  valence: number;
  arousal: number;
  source_message?: string | null;
  source_conversation_id?: string | null;
  last_used_at?: string | null;
  embedding_space_id?: string | null;
  usage_count: number;
  stability: MemoryStability;
  valid_from?: string | null;
  valid_until?: string | null;
  review_after?: string | null;
  sensitivity: MemorySensitivity;
  evidence_memory_ids: string[];
  topics: string[];
  entities: string[];
  space_ids: string[];
  temporal_subject?: string | null;
  temporal_predicate?: string | null;
  status: MemoryStatus;
  digested: boolean;
  decay_lambda?: number | null;
  supersedes?: string | null;
  superseded_by?: string | null;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  archived?: number;
  revision: number;
  redacted?: boolean;
  redaction_reason?: MemoryRedactionReason;
  redacted_fields?: string[];
}

export interface MemoryUpdatePayload {
  content?: string;
  type?: MemoryType;
  importance?: number;
  confidence?: number;
  valence?: number;
  arousal?: number;
  stability?: MemoryStability;
  valid_from?: string | null;
  valid_until?: string | null;
  review_after?: string | null;
  sensitivity?: MemorySensitivity;
  source_message?: string | null;
  source_conversation_id?: string | null;
  topics?: string[];
  entities?: string[];
  temporal_subject?: string | null;
  temporal_predicate?: string | null;
  status?: MemoryStatus;
}

export interface MemorySpacesUpdatePayload {
  space_ids?: string[];
  create_space_names?: string[];
}

export interface MemorySpace {
  id: string;
  user_id?: string;
  name: string;
  normalized_name: string;
  created_at: string;
  updated_at: string;
  archived?: number;
  color?: string | null;
  description?: string | null;
  sort_order?: number;
  active_memory_count?: number;
  last_memory_updated_at?: string | null;
}

export interface MemorySpaceDetail {
  space: MemorySpace;
  memories: MemoryRecord[];
}

export interface MemoryUpdateResult {
  updated: boolean;
  memory?: MemoryRecord;
  archived?: boolean;
  memory_id?: string;
  revision?: number;
}

export interface MemoryScoreBreakdown {
  semantic_score: number;
  keyword_score: number;
  importance_score: number;
  recency_score: number;
  usage_score: number;
  emotion_score: number;
  final_score: number;
}

export interface MemorySearchRecord extends MemoryRecord {
  relevance: number;
  channels: string[];
  topic_score: number;
  total_score: number;
  final_score: number;
  activation_count: number;
  last_active_at?: string | null;
  freshness_bonus: number;
  score_breakdown: MemoryScoreBreakdown;
  excluded_reason?: string;
}

export interface MemorySourceExplanation {
  memory_id: string;
  content: string;
  source_excerpt?: string | null;
  source_conversation_id?: string | null;
  saved_at: string;
  updated_at: string;
  confidence: number;
  is_core_memory_evidence: boolean;
  core_memory_sections: CoreSectionName[];
  evidence_memory_ids: string[];
  redacted?: boolean;
  redaction_reason?: MemoryRedactionReason;
  redacted_fields?: string[];
}

export interface CoreMemorySection {
  id: string;
  section: CoreSectionName;
  content: string;
  evidence_memory_ids: string[];
  confidence: number;
  version: number;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface CoreMemoryUpdatePayload {
  content?: string;
  evidence_memory_ids?: string[];
  confidence?: number;
}

export interface CoreMemoryUpdateResult {
  updated: boolean;
  action: "create" | "update" | "ignore";
  core_memory: CoreMemorySection;
}

export interface CoreMemoryHistoryItem extends CoreMemorySection {
  core_memory_section_id: string;
  replaced_at: string;
}

export interface ReviewRecommendation {
  action: ReviewAction;
  reason: string;
  memory_ids: string[];
  relation: "none" | "same" | "supplement" | "conflict" | "supersede";
  suggested_content?: string | null;
  risk_tags: ReviewRiskTag[];
  severity: ReviewSeverity;
  next_action_options: ReviewNextAction[];
  core_memory_sections: CoreSectionName[];
}

export interface ReviewResult {
  total: number;
  recommendations: ReviewRecommendation[];
}

export interface DatabaseHealthIssue {
  type: DatabaseHealthIssueType;
  severity: DatabaseHealthSeverity;
  object_id: string;
  related_id?: string | null;
  message: string;
  recommended_action: string;
}

export interface DatabaseHealthResult {
  status: DatabaseHealthStatus;
  checked_at: string;
  summary: {
    errors: number;
    warnings: number;
    info: number;
  };
  issues: DatabaseHealthIssue[];
}

export type MechanismVerdictState =
  | "active"
  | "degenerate"
  | "dormant"
  | "sparse"
  | "insufficient_data";

export interface MechanismVerdict {
  mechanism: string;
  state: MechanismVerdictState;
  message: string;
  metrics: Record<string, unknown>;
}

export interface MechanismDiagnosisResult {
  database: string;
  user_id?: string | null;
  memory_count: number;
  metrics: Record<string, unknown>;
  verdicts: MechanismVerdict[];
}

export type RecallEvalJudgment = "unlabeled" | "relevant" | "no_answer";

export interface RecallEvalLabel {
  id: string;
  query: string;
  judgment: RecallEvalJudgment;
  relevant_ids: string[];
  note?: string | null;
}

export interface RecallEvalValidationIssue {
  code: string;
  label_id?: string;
  memory_id?: string;
  message: string;
}

export interface RecallEvalSummary {
  queries_total: number;
  queries_graded: number;
  queries_relevant?: number;
  queries_no_answer?: number;
  queries_unlabeled?: number;
  target_min?: number;
  target_max?: number;
  k?: number;
  requested_mode?: "keyword" | "embedding";
  hit_rate?: number;
  precision_at_k?: number;
  recall_at_k?: number;
  mrr?: number;
  ndcg_at_k?: number;
  no_answer_false_positive_rate?: number;
  no_answer_abstention_rate?: number;
  no_answer_mean_retrieved?: number;
  retrieval_mode_counts?: Record<string, number>;
  fallback_queries?: number;
}

export interface RecallEvalQueryResult {
  id?: string;
  query: string;
  judgment: RecallEvalJudgment;
  graded: boolean;
  relevant_count: number;
  retrieved: number;
  relevant_hits: number;
  hit: number;
  precision: number;
  recall: number;
  reciprocal_rank: number;
  ndcg: number;
  false_positive: boolean;
  requested_mode: "keyword" | "embedding";
  retrieval_mode: "keyword" | "embedding" | "hybrid" | "keyword_fallback" | "none";
  fallback_reason?: string | null;
  embedding_available?: boolean | null;
  predicted_ids: string[];
  predicted_channels?: Record<string, string[]>;
}

export interface RecallEvalRunResult {
  mode: "keyword" | "embedding";
  user_id: string;
  summary: RecallEvalSummary;
  per_query: RecallEvalQueryResult[];
  validation_issues: RecallEvalValidationIssue[];
}

export interface RecallEvalWorkbench {
  snapshot: string;
  labels_path: string;
  user_id: string;
  target_label_min: number;
  target_label_max: number;
  labels: RecallEvalLabel[];
  summary: RecallEvalSummary;
  validation_issues: RecallEvalValidationIssue[];
  candidates: MemoryRecord[];
  last_results: {
    keyword?: RecallEvalRunResult | null;
    embedding?: RecallEvalRunResult | null;
  };
}

export interface MemoryReviewPolicy {
  code: ReviewPolicyCode;
  interval_days: number;
  review_after: string;
  reason: string;
}

export interface ReviewRevisionOperation {
  operation: ReviewRevisionOperationKind;
  reason: string;
  memory_ids: string[];
  target_memory_id?: string | null;
  content?: string | null;
  type?: MemoryType | null;
  importance?: number | null;
  confidence?: number | null;
  valence?: number | null;
  arousal?: number | null;
  stability?: MemoryStability | null;
  valid_until?: string | null;
  sensitivity?: MemorySensitivity | null;
  review_policy?: MemoryReviewPolicy | null;
}

export interface ReviewRevisionPreview {
  operations: ReviewRevisionOperation[];
  preview_token: string;
  reason: string;
}

export interface ReviewRelatedCandidate {
  memory: MemoryRecord;
  relation: ReviewRecommendation["relation"];
  reason: string;
  channels: string[];
  score: number;
  is_core_memory_evidence: boolean;
  core_memory_sections: CoreMemorySection[];
}

export interface ReviewRevisionApplyResult {
  applied: boolean;
  results: Array<Record<string, unknown>>;
  affected_core_sections: CoreMemorySection[];
}

export interface ReviewActionApplyResult {
  applied: boolean;
  action: ReviewGovernanceAction;
  results: Array<Record<string, unknown>>;
  affected_core_sections: CoreMemorySection[];
}

export interface RecentContextTurn {
  user: string;
  assistant: string;
  sensitivity: MemorySensitivity;
}

export interface RecentContextSummary {
  id: string;
  user_id?: string;
  conversation_id?: string | null;
  summary: string;
  compressed_summary: string;
  recent_turns: RecentContextTurn[];
  turn_count: number;
  created_at: string;
  updated_at: string;
  archived?: number;
}

export interface ConversationBranchNode extends RecentContextSummary {
  history_fingerprint: string;
  parent_history_fingerprint: string;
  turn_fingerprint: string;
  assistant_digest: string;
}

export interface ConversationBranchList {
  data: ConversationBranchNode[];
  meta: {
    status: "active" | "archived";
    total: number;
    returned: number;
    truncated: boolean;
  };
}

export interface ConversationBranchArchiveResult {
  id: string;
  archived: boolean;
  archived_count: number;
}

export interface ConversationBranchRestoreResult {
  id: string;
  restored: boolean;
  restored_count: number;
}

export interface RecentContextPayload {
  found: boolean;
  summary: string;
}

export interface MemoryContextExplainResult {
  context_package: {
    query: string;
    core_memory: CoreMemorySection[];
    search_results: MemorySearchRecord[];
    recent_context: RecentContextPayload;
  };
  core_memory: CoreMemorySection[];
  search_results: MemorySearchRecord[];
  recent_context: RecentContextPayload;
  candidate_pool: MemorySearchRecord[];
  excluded_candidates: MemorySearchRecord[];
}

export type SearchFeedbackValue = "useful" | "not_useful" | "wrong" | "missing";

export interface DecisionLog {
  id: string;
  conversation_id?: string | null;
  candidate_json: string;
  decision: DecisionLogAction;
  reason: string;
  created_at: string;
}

export interface MemoryPurgeResult {
  purged: boolean;
  id: string;
  audit_log_id: string;
  affected_core_memory_sections: CoreMemorySection[];
}

export interface MemoryPurgeEffects {
  requested_memories_deleted: number;
  dependent_memories_deleted: number;
  memories_deleted: number;
  space_links_deleted: number;
  temporal_references_relinked: number;
  core_sections_scrubbed: number;
  core_history_scrubbed: number;
  decision_logs_scrubbed: number;
}

export interface MemoryPurgeCoreImpact {
  id: string;
  section: CoreSectionName;
  version: number;
  active: boolean;
}

export interface MemoryPurgePreviewResult {
  requested_memory_ids: string[];
  purge_memory_ids: string[];
  dependent_memory_ids: string[];
  affected_core_memory_sections: MemoryPurgeCoreImpact[];
  fingerprint: string;
  effects: MemoryPurgeEffects;
  preview_token: string;
  expires_at: string;
}

export interface MemoryBatchPurgeResult {
  purged: boolean;
  requested_memory_ids: string[];
  purged_memory_ids: string[];
  dependent_memory_ids: string[];
  affected_core_memory_sections: MemoryPurgeCoreImpact[];
  fingerprint: string;
  effects: MemoryPurgeEffects;
  audit_log_id: string;
  evaluation_cleanup?: Record<string, unknown>;
  warnings?: string[];
}

export interface MemoryReportSection {
  section: string;
  title: string;
  core_summary: string;
  core_confidence?: number | null;
  core_version?: number | null;
  memories: MemoryRecord[];
}

export interface MemoryReport {
  user_id: string;
  generated_at: string;
  counts: {
    active_memories: number;
    deleted_memories: number;
    core_sections: number;
  };
  memory_spaces?: MemorySpace[];
  sections: MemoryReportSection[];
  markdown?: string;
}

export interface MemoryExport {
  version?: number;
  exported_at?: string;
  user_id?: string;
  embedding_included?: boolean;
  memories?: MemoryRecord[];
  deleted_memories?: MemoryRecord[];
  core_memory_sections?: CoreMemorySection[];
  memory_spaces?: MemorySpace[];
  [key: string]: unknown;
}

export interface RestoreResult {
  spaces_created?: number;
  spaces_updated?: number;
  spaces_skipped?: number;
  spaces_invalid?: number;
  created: number;
  updated: number;
  skipped: number;
  invalid: number;
  include_deleted: boolean;
  overwrite: boolean;
  dry_run: boolean;
}

export interface StackBackupValidationResult {
  ok: boolean;
  restorable: boolean;
  format?: string;
  version?: number;
  secrets_included?: boolean;
  components?: Record<string, { status?: string; required?: boolean }>;
  files?: Array<{ path: string; size_bytes?: number | null }>;
  stats?: {
    memory_users?: number | null;
    active_memories?: number | null;
    deleted_memories?: number | null;
    knowledge_documents?: number | null;
    auth_token_hashes?: number | null;
  };
  restore_requires_stopped_services?: boolean;
  message?: string;
}

export interface ConversationImportPreviewResult {
  format: string;
  turn_count: number;
  total_chars: number;
  truncated: boolean;
  warnings: string[];
  sample_turns: Array<{
    index: number;
    user_text: string;
    assistant_text?: string | null;
    user_chars: number;
    assistant_chars: number;
  }>;
  will_not_auto_pin: boolean;
  note?: string;
}

export interface ConversationImportCommitResult {
  batch_id: string;
  format: string;
  turn_count: number;
  truncated: boolean;
  warnings: string[];
  created: number;
  updated: number;
  ignored: number;
  turns: Array<{
    index: number;
    status: string;
    reason?: string;
    error?: string;
    created: number;
    updated: number;
    ignored: number;
    memory_ids?: string[];
  }>;
}

export interface MemorySurfaceRecord extends MemoryRecord {
  final_score: number;
  activation_count: number;
  last_active_at?: string | null;
  freshness_bonus: number;
  surface_reason: string;
  surface_score: number;
  surface_mode: SurfaceMode;
  surface_reason_text: string;
  life_score: number;
  days_since_last_active: number;
  review_signals: SurfaceSignal[];
}

export interface MemoryNetworkNode {
  id: string;
  kind: "core" | "memory";
  label: string;
  content?: string;
  section?: CoreSectionName;
  type?: MemoryType;
  importance?: number;
  confidence?: number;
  valence?: number;
  arousal?: number;
  stability?: MemoryStability;
  sensitivity?: MemorySensitivity;
  usage_count?: number;
  last_used_at?: string | null;
  source_message?: string | null;
  source_conversation_id?: string | null;
  evidence_memory_ids?: string[];
  topics?: string[];
  entities?: string[];
  space_ids?: string[];
  updated_at?: string;
  redacted?: boolean;
  redaction_reason?: MemoryRedactionReason;
  redacted_fields?: string[];
}

export interface MemoryNetworkEdge {
  id: string;
  source: string;
  target: string;
  kind: "core_evidence" | "similarity";
  weight: number;
  label: string;
}

export interface MemoryNetwork {
  nodes: MemoryNetworkNode[];
  edges: MemoryNetworkEdge[];
  meta: {
    memory_count: number;
    core_count: number;
    similarity_threshold: number;
    max_similarity_edges: number;
    filters?: Record<string, unknown>;
  };
}

export interface TraversalEdge {
  source: string;
  target: string;
  weight: number;
  kind: string;
  label: string;
}

export interface TraversalResultItem {
  memory: MemoryRecord;
  score: number;
  depth: number;
  path: TraversalEdge[];
}

export interface TraversalMeta {
  depth: number;
  limit: number;
  similarity_threshold: number;
  candidate_count: number;
  edge_count: number;
  reachable_count: number;
  iterations: number;
  converged: boolean;
}

export interface TraversalResponse {
  seed: MemoryRecord;
  results: TraversalResultItem[];
  meta: TraversalMeta;
}

export type KnowledgeDocumentStatus = "active" | "deleted";
export type KnowledgeIndexStatus = "pending" | "indexing" | "ready" | "indexed" | "failed";
export type KnowledgeEmbeddingStatus = "pending" | "indexing" | "ready" | "partial" | "failed" | "disabled";
export type KnowledgeSearchQuality = "fast" | "balanced" | "deep";

export interface KnowledgeVersion {
  id: string;
  ref: string;
  version_ref?: string;
  document_id?: string;
  document_ref?: string;
  version_number: number;
  content_sha256: string;
  sha256?: string;
  byte_size: number;
  size_bytes?: number;
  character_count?: number;
  content?: string;
  index_status: KnowledgeIndexStatus;
  index_error?: string | null;
  embedding_status?: KnowledgeEmbeddingStatus;
  embedding_model?: string;
  embedded_at?: string | null;
  embedding_error?: string | null;
  created_at: string;
}

export interface KnowledgeDocument {
  id: string;
  ref: string;
  document_ref?: string;
  user_id?: string;
  title: string;
  source_name: string;
  content_type: string;
  sensitivity: MemorySensitivity;
  detected_sensitivity?: MemorySensitivity;
  sensitivity_override_confirmed?: boolean;
  tags?: string[];
  metadata?: Record<string, string | number | boolean>;
  status: KnowledgeDocumentStatus;
  current_version_id?: string | null;
  current_version_ref?: string | null;
  current_version_number?: number | null;
  current_version?: KnowledgeVersion | null;
  byte_size?: number;
  size_bytes?: number;
  character_count?: number;
  index_status?: KnowledgeIndexStatus;
  index_error?: string | null;
  created_at: string;
  updated_at: string;
  deleted_at?: string | null;
}

export interface KnowledgeDocumentDetail {
  document: KnowledgeDocument;
  versions: KnowledgeVersion[];
}

export interface KnowledgeStatus {
  available?: boolean;
  status?: string;
  error?: string | null;
  active_documents?: number;
  deleted_documents?: number;
  failed_indexes?: number;
  indexing_failed?: number;
  failed_versions?: number;
  index_failures?: number;
  counts?: {
    active?: number;
    deleted?: number;
    failed?: number;
    failed_indexes?: number;
    [key: string]: number | undefined;
  };
  agent_enabled?: boolean;
  agent_egress_policy?: string;
  agent_timeout_seconds?: number;
  agent_provider_priority?: string;
  agent_configured_providers?: string[];
  agent_rate_limit_cooldown_seconds?: number;
  llm_provider_priority?: string;
  llm_configured_providers?: string[];
  llm_rate_limit_cooldown_seconds?: number;
  agent_mimo_model?: string;
  agent_kimi_model?: string;
  agent_flash_model?: string;
  agent_pro_model?: string;
  sensitive_egress_enabled?: boolean;
  embedding_enabled?: boolean;
  embedding_model?: string;
  max_document_bytes?: number;
  embedding_batch_size?: number;
  hybrid_vector_weight?: number;
  embedding_min_cosine?: number;
}

export interface KnowledgeUploadSession {
  id: string;
  upload_id?: string;
  expires_at?: string;
  expected_version_id?: string | null;
}

export interface KnowledgeUploadCommitResult {
  document: KnowledgeDocument;
  version: KnowledgeVersion;
  created?: boolean;
  deduplicated?: boolean;
  duplicate?: boolean;
  embedding?: {
    status?: KnowledgeEmbeddingStatus;
    stored?: number;
    total?: number;
  };
  import?: {
    source_format?: string;
    page_count?: number | null;
    warnings?: string[];
  };
}

export interface KnowledgeSearchHit {
  document_ref: string;
  version_ref: string;
  chunk_ref: string;
  title: string;
  source_name?: string;
  heading_path?: string[] | string;
  title_path?: string[] | string;
  char_start?: number;
  char_end?: number;
  start_char?: number;
  end_char?: number;
  line_start?: number;
  line_end?: number;
  start_line?: number;
  end_line?: number;
  excerpt: string;
  score?: number;
  match_signals?: string[];
  channels?: string[];
  match_reason?: string;
  sensitivity?: MemorySensitivity;
}

export interface KnowledgeAgentStep {
  model?: string;
  round?: number;
  tool?: string;
  action?: string;
  status?: string;
  query?: string;
  reference_count?: number;
  result_count?: number;
  summary?: string;
  [key: string]: unknown;
}

export interface KnowledgeSearchResponse {
  data?: KnowledgeSearchHit[];
  results?: KnowledgeSearchHit[];
  local_candidates?: KnowledgeSearchHit[];
  request?: string;
  agent_used?: boolean;
  agent_model?: string | null;
  model?: string | null;
  agent_rounds?: number;
  rounds?: number;
  upgraded?: boolean;
  escalated?: boolean;
  agent_attempted?: boolean;
  fallback_reason?: string | null;
  elapsed_ms?: number;
  baseline_count?: number;
  query_plan?: string[];
  steps?: KnowledgeAgentStep[];
  tool_steps?: KnowledgeAgentStep[];
  metadata?: {
    agent_used?: boolean;
    agent_attempted?: boolean;
    model?: string;
    rounds?: number;
    flash_rounds?: number;
    pro_rounds?: number;
    escalated?: boolean;
    fallback_reason?: string;
    elapsed_ms?: number;
    baseline_count?: number;
    tool_steps?: KnowledgeAgentStep[];
  };
  agent?: {
    agent_used?: boolean;
    agent_attempted?: boolean;
    model?: string;
    rounds?: number;
    flash_rounds?: number;
    pro_rounds?: number;
    escalated?: boolean;
    fallback_reason?: string;
    elapsed_ms?: number;
    baseline_count?: number;
    tool_steps?: KnowledgeAgentStep[];
  };
  [key: string]: unknown;
}

export interface KnowledgeReadResponse {
  reference: string;
  document_ref: string;
  version_ref: string;
  chunk_ref?: string | null;
  title?: string;
  content?: string;
  text?: string;
  char_start?: number;
  char_end?: number;
  start_char?: number;
  end_char?: number;
  line_start?: number;
  line_end?: number;
  complete: boolean;
  next_cursor: string;
  [key: string]: unknown;
}

export interface KnowledgeExport {
  version?: number;
  exported_at?: string;
  user_id?: string;
  documents?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface KnowledgeRestoreResult {
  restored_documents?: number;
  restored_versions?: number;
  failed_versions?: number;
  skipped_documents?: number;
  document_refs?: string[];
  chunks_rebuilt?: boolean;
  fts_rebuilt?: boolean;
  [key: string]: unknown;
}


export interface ProviderModelInfo {
  id: string;
  kind: "chat" | "embedding";
}

export interface ProviderInfo {
  id: string;
  name: string;
  protocol: string;
  api_host: string;
  api_key_env: string;
  legacy_api_key_envs: string[];
  configured: boolean;
  models: ProviderModelInfo[];
  urls: { website?: string; api_key?: string; docs?: string };
}

export interface RouteTargetInfo {
  target: string;
  provider_id?: string;
  provider_name?: string;
  model?: string;
  valid: boolean;
  configured: boolean;
}

export interface RouteInfo {
  id: string;
  description: string;
  targets: RouteTargetInfo[];
  usable: boolean;
  migrated: boolean;
}

export interface ModelGatewayConnectionInfo {
  id: string;
  channel_operator: string;
  base_url: string;
  adapter: ModelGatewayAdapter;
  usage_scope: ModelGatewayUsageScope;
  allowed_private_networks: string[];
  connect_timeout_seconds: number;
  read_timeout_seconds: number;
  write_timeout_seconds: number;
  pool_timeout_seconds: number;
  enabled: boolean;
  configured: boolean;
}

export type ModelGatewayAdapter =
  | "generic"
  | "kimi"
  | "deepseek"
  | "mimo"
  | "dashscope_openai";
export type ModelGatewayUsageScope = "backend_allowed" | "interactive_only" | "disabled";
export type ModelGatewayPlan =
  | "payg"
  | "subscription"
  | "free_tier"
  | "token_plan"
  | "coding_plan"
  | "direct_tool_only"
  | "custom";
export type ModelGatewayFallbackScope = "none" | "same_channel" | "any_channel";
export type ModelGatewayRouteOperation = "keep" | "prepend" | "append" | "replace";

export interface ModelGatewayCapabilities {
  streaming?: boolean;
  tools?: boolean;
  parallel_tools?: boolean;
  reasoning?: boolean;
  multimodal_input?: boolean;
  json_object?: boolean;
  json_schema?: boolean;
}

export interface ModelGatewayDeploymentInfo {
  id: string;
  connection: string;
  upstream_model: string;
  model_author: string;
  model_family: string;
  kind: "chat" | "embedding";
  adapter_profile: "inherit" | "dashscope_deepseek_v4";
  capabilities: ModelGatewayCapabilities;
  dimensions: number | null;
  embedding_space: string;
  pricing?: string | null;
  enabled: boolean;
}

export interface ModelGatewayRouteInfo {
  id: string;
  kind: "chat" | "embedding";
  targets: string[];
  required_capabilities: string[];
  max_attempts: number;
  fallback_scope: ModelGatewayFallbackScope;
  enabled: boolean;
}

export interface ModelGatewayPricingInfo {
  id: string;
  mode: "per_token" | "subscription" | "free_tier" | "custom" | "unknown";
  currency: string;
  unit_tokens: number;
  tiers: Array<Record<string, unknown>>;
  source_url: string;
  effective_from: string;
  checked_at: string;
  notes: string;
}

export interface ModelGatewayControlSnapshot {
  revision: string;
  admin_required: boolean;
  connections: ModelGatewayConnectionInfo[];
  deployments: ModelGatewayDeploymentInfo[];
  routes: ModelGatewayRouteInfo[];
  pricing?: ModelGatewayPricingInfo[];
}

export interface ModelGatewayRouteDraft {
  id: string;
  targets: string[];
  enabled: boolean;
}

export interface ModelGatewayRouteChangeResult {
  valid?: boolean;
  applied?: boolean;
  revision: string;
  changed_routes: string[];
  warnings: string[];
  restart_required?: boolean;
}

export interface ModelGatewayConnectionCheck {
  mode: "discovery" | "live";
  summary: Record<string, number>;
  connections: Array<{
    connection_id: string;
    status: string;
    level: "ok" | "warning" | "error" | "skipped";
    detail: string;
    http_status?: number | null;
    discovered_model_count?: number;
    discovered_models?: string[];
  }>;
}

export interface ModelGatewayChannelDiscoverBody {
  revision: string;
  candidate_key: string;
  channel_operator: string;
  base_url: string;
  adapter?: ModelGatewayAdapter;
  auth_type?: "bearer" | "x-api-key";
  allowed_private_networks?: string[];
  models_endpoint?: string | null;
}

export interface ModelGatewayChannelDiscoverResult {
  valid: boolean;
  persisted: false;
  revision: string;
  candidate: {
    connection_id: string;
    channel_operator: string;
    base_url: string;
    adapter: ModelGatewayAdapter;
    auth_type: "bearer" | "x-api-key";
    allowed_private_networks: string[];
    models_endpoint: string | null;
  };
  models: Array<{
    id: string;
    model_author: "unknown" | string;
    aliases: string[];
  }>;
  report: ModelGatewayConnectionCheck;
}

export interface ModelGatewayCapabilityProbeBody {
  revision: string;
  candidate_key: string;
  channel_operator: string;
  base_url: string;
  adapter?: ModelGatewayAdapter;
  auth_type?: "bearer" | "x-api-key";
  allowed_private_networks?: string[];
  upstream_model: string;
  probes?: Array<"chat" | "streaming" | "tools" | "reasoning" | "json_object">;
}

export interface ModelGatewayCapabilityProbeResult {
  persisted: false;
  revision: string;
  upstream_model: string;
  ok: boolean;
  capabilities: ModelGatewayCapabilities & {
    streaming?: boolean;
    tools?: boolean;
    parallel_tools?: boolean;
    reasoning?: boolean;
    multimodal_input?: boolean;
    json_object?: boolean;
    json_schema?: boolean;
  };
  details: Record<
    string,
    { ok: boolean; http_status?: number | null; detail?: string }
  >;
  note?: string;
}

export interface ModelGatewayConnectionCreateResult {
  valid: boolean;
  applied: boolean;
  connection_id: string;
  revision: string;
}

export interface ModelGatewayConnectionCreateBody {
  revision: string;
  channel_operator: string;
  adapter: string;
  base_url: string;
  dry_run?: boolean;
}

export interface ModelGatewayDeploymentDraftInput {
  id?: string;
  upstream_model: string;
  model_author?: string;
  kind: "chat" | "embedding";
  adapter_profile?: "inherit" | "dashscope_deepseek_v4";
  reasoning_default?: "inherit" | "enabled" | "disabled";
  capabilities?: ModelGatewayCapabilities;
  dimensions?: number | null;
  embedding_space?: string;
  pricing?: string | null;
  enabled?: boolean;
}

export interface ModelGatewayRouteAssignmentInput {
  id: string;
  operation?: ModelGatewayRouteOperation;
  kind: "chat" | "embedding";
  targets?: string[];
  max_attempts?: number;
  fallback_scope?: ModelGatewayFallbackScope;
  enabled?: boolean;
}

export interface ModelGatewayBundleConnectionInput {
  id?: string;
  channel_operator: string;
  adapter: ModelGatewayAdapter;
  base_url: string;
  secret: string;
  auth_type?: "bearer" | "x-api-key";
  plan?: ModelGatewayPlan;
  usage_scope?: ModelGatewayUsageScope;
  allowed_private_networks?: string[];
  connect_timeout_seconds?: number;
  read_timeout_seconds?: number;
  write_timeout_seconds?: number;
  pool_timeout_seconds?: number;
  enabled?: boolean;
}

export interface ModelGatewayChannelBundleBody {
  revision: string;
  connection: ModelGatewayBundleConnectionInput;
  /** Distinct HTTPS URL for embedding; omitted when it matches the chat channel. */
  embedding_base_url?: string;
  deployments?: ModelGatewayDeploymentDraftInput[];
  pricing?: Array<{ id: string; value: Omit<ModelGatewayPricingInfo, "id"> }>;
  routes?: ModelGatewayRouteAssignmentInput[];
}

export interface ModelGatewayBundleResult {
  valid: boolean;
  applied: boolean;
  connection_id: string;
  embedding_connection_id?: string;
  deployment_ids: string[];
  changed_routes: string[];
  revision: string;
  discovery: ModelGatewayConnectionCheck;
}

export interface ModelGatewayObjectMutationResult {
  revision: string;
  id: string;
  updated?: boolean;
  enabled?: boolean;
  deleted?: boolean;
  collection?: "connections" | "deployments" | "pricing";
}

export interface ModelGatewayDeploymentApplyBody {
  revision: string;
  connection: string;
  deployments: ModelGatewayDeploymentDraftInput[];
  routes: ModelGatewayRouteAssignmentInput[];
  dry_run?: boolean;
}

export interface ModelGatewayDeploymentApplyResult {
  valid: boolean;
  applied: boolean;
  deployments: Array<{
    id: string;
    upstream_model: string;
    kind: "chat" | "embedding";
  }>;
  changed_routes: string[];
  warnings: string[];
  revision: string;
}

export interface ProvidersStatus {
  runtime: {
    model_gateway_enabled: boolean;
    model_gateway_base_url: string;
    chat_source: "model_gateway" | "legacy_direct";
    knowledge_source: string;
    providers_path: string;
    routes_path: string;
  };
  embedding: {
    model: string;
    base_url: string;
    dimensions: number;
    configured: boolean;
    mode: "auto" | "pinned";
    state: "ready" | "off" | "invalid" | "unavailable";
    code: string;
    space_id?: string;
  };
  providers: ProviderInfo[];
  routes: RouteInfo[];
  control: ModelGatewayControlSnapshot | null;
  config_error: string;
  setup: {
    state: "ready" | "needs_model" | "configuration_error";
    service_ready: boolean;
    model_gateway_connected: boolean;
    chat_ready: boolean;
    required_chat_routes: string[];
    usable_chat_routes: string[];
    missing_chat_routes: string[];
    next_action: "configure_model" | "repair_model_gateway" | "connect_client";
    live_probe?: {
      ok: boolean;
      code: string;
      message: string;
      latency_ms?: number;
      cached?: boolean;
      route?: string;
    } | null;
    upstream_ready?: boolean | null;
  };
}
