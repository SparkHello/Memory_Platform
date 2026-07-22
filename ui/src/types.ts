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
  active_memory_count?: number;
  last_memory_updated_at?: string | null;
}

export interface MemorySpaceDetail {
  space: MemorySpace;
  memories: MemoryRecord[];
}

export interface MemoryUpdateResult {
  updated: boolean;
  memory: MemoryRecord;
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

export interface RecentContextSummary {
  id: string;
  conversation_id?: string | null;
  summary: string;
  created_at: string;
  updated_at: string;
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
