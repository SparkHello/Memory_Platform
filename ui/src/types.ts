export type PageKey =
  | "dashboard"
  | "gateway-overview"
  | "gateway-config"
  | "gateway-import-export"
  | "providers"
  | "routes"
  | "billing"
  | "usage"
  | "memories"
  | "core"
  | "review"
  | "recent"
  | "reports"
  | "logs"
  | "settings"
  | "developer";

export type MemoryType =
  | "project"
  | "preference"
  | "fact"
  | "learning"
  | "style"
  | "person"
  | "relationship";

export type MemoryStability = "temporary" | "medium" | "stable";
export type MemorySensitivity = "normal" | "private" | "sensitive";
export type MemoryAction = "create" | "update" | "ignore";
export type ReviewAction = "keep" | "merge" | "lower" | "delete" | "review";
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
  source_message?: string | null;
  source_conversation_id?: string | null;
  last_used_at?: string | null;
  usage_count: number;
  stability: MemoryStability;
  valid_until?: string | null;
  review_after?: string | null;
  sensitivity: MemorySensitivity;
  evidence_memory_ids: string[];
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  archived?: number;
}

export interface MemoryUpdatePayload {
  content?: string;
  type?: MemoryType;
  importance?: number;
  confidence?: number;
  stability?: MemoryStability;
  valid_until?: string | null;
  review_after?: string | null;
  sensitivity?: MemorySensitivity;
  source_message?: string | null;
  source_conversation_id?: string | null;
}

export interface MemoryUpdateResult {
  updated: boolean;
  memory: MemoryRecord;
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
}

export interface ReviewResult {
  total: number;
  recommendations: ReviewRecommendation[];
}

export interface RecentContextSummary {
  id: string;
  conversation_id?: string | null;
  summary: string;
  created_at: string;
  updated_at: string;
}

export interface DecisionLog {
  id: string;
  conversation_id?: string | null;
  candidate_json: string;
  decision: MemoryAction;
  reason: string;
  created_at: string;
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
  [key: string]: unknown;
}

export interface RestoreResult {
  created: number;
  updated: number;
  skipped: number;
  invalid: number;
  include_deleted: boolean;
  overwrite: boolean;
}

export interface ProviderSummary {
  id: string;
  provider?: string;
  name: string;
  enabled: boolean;
  base_url: string;
  api_key_env?: string;
  api_key_configured: boolean;
  timeout_seconds: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RouteSummary {
  id?: string | null;
  virtual_model: string;
  provider: string;
  upstream_model: string;
  provider_model_id?: string | null;
  priority: number;
  input_price_per_million: number;
  output_price_per_million: number;
  currency: string;
  min_balance: number;
  enabled?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface GatewayProvidersResponse {
  enabled: boolean;
  path: string;
  source?: ProviderConfigSource;
  errors: string[];
  router: {
    default_model?: string | null;
    fallback_enabled: boolean;
  };
  providers: ProviderSummary[];
  provider_models: ProviderModelSummary[];
  routes: RouteSummary[];
}

export type ProviderConfigSource = "sqlite" | "toml" | "legacy";

export interface ProviderConfigResponse {
  source: ProviderConfigSource;
  providers: ProviderSummary[];
  provider_models: ProviderModelSummary[];
  routes: RouteSummary[];
}

export interface ProviderModelSummary {
  id: string;
  provider: string;
  upstream_model: string;
  display_name: string;
  api_format: "openai_compatible" | "claude_sdk";
  pricing_mode: "flat" | "tiered";
  pricing_tiers_json: string;
  input_price_per_million: number;
  output_price_per_million: number;
  currency: string;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProviderConfigPayload {
  provider?: string;
  name?: string;
  base_url?: string;
  api_key?: string;
  enabled?: boolean;
  timeout_seconds?: number;
}

export interface RouteConfigPayload {
  virtual_model?: string;
  provider_model_id?: string;
  provider?: string;
  upstream_model?: string;
  priority?: number;
  input_price_per_million?: number;
  output_price_per_million?: number;
  currency?: string;
  min_balance?: number;
  enabled?: boolean;
}

export interface ProviderModelConfigPayload {
  provider?: string;
  upstream_model?: string;
  display_name?: string;
  api_format?: "openai_compatible" | "claude_sdk";
  pricing_mode?: "flat" | "tiered";
  pricing_tiers_json?: string;
  input_price_per_million?: number;
  output_price_per_million?: number;
  currency?: string;
  enabled?: boolean;
}

export interface ProviderTestResult {
  success: boolean;
  status?: number | null;
  error_type?: string | null;
  message: string;
}

export interface BalanceRecord {
  provider: string;
  currency: string;
  balance: number;
  updated_at?: string | null;
}

export interface BalanceAdjustmentResult {
  balance: BalanceRecord;
  adjustment: {
    id: string;
    provider: string;
    amount_delta: number;
    balance_after: number;
    currency: string;
    reason: string;
    created_at: string;
  };
}

export interface UsageEvent {
  id: string;
  user_id?: string | null;
  conversation_id?: string | null;
  virtual_model: string;
  provider: string;
  upstream_model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  input_cost: number;
  output_cost: number;
  total_cost: number;
  currency: string;
  estimated: boolean;
  status: string;
  error_type?: string | null;
  created_at: string;
}

export interface UsageSummary {
  provider: string;
  virtual_model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  input_cost: number;
  output_cost: number;
  total_cost: number;
  currency: string;
}
