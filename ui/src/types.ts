export type PageKey =
  | "dashboard"
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
