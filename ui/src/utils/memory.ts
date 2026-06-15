import type {
  MemoryRecord,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  MemoryUpdatePayload
} from "../types";
import { clampNumber, nullableText } from "./format";

export type MemoryFilters = {
  type: "all" | MemoryType;
  sensitivity: "all" | MemorySensitivity;
  stability: "all" | MemoryStability;
  minImportance: number;
  maxImportance: number;
  minConfidence: number;
  maxConfidence: number;
  hasValidUntil: boolean;
  hasReviewAfter: boolean;
};

export type MemoryEditDraft = {
  content: string;
  type: MemoryType;
  importance: number;
  confidence: number;
  stability: MemoryStability;
  sensitivity: MemorySensitivity;
  valid_until: string;
  review_after: string;
  source_message: string;
  source_conversation_id: string;
};

export function memoryToEditDraft(memory: MemoryRecord): MemoryEditDraft {
  return {
    content: memory.content,
    type: memory.type,
    importance: memory.importance,
    confidence: memory.confidence,
    stability: memory.stability,
    sensitivity: memory.sensitivity,
    valid_until: memory.valid_until || "",
    review_after: memory.review_after || "",
    source_message: memory.source_message || "",
    source_conversation_id: memory.source_conversation_id || ""
  };
}

export function editDraftToPayload(draft: MemoryEditDraft): MemoryUpdatePayload {
  return {
    content: draft.content.trim(),
    type: draft.type,
    importance: Math.round(clampNumber(draft.importance, 1, 10)),
    confidence: clampNumber(draft.confidence, 0, 1),
    stability: draft.stability,
    sensitivity: draft.sensitivity,
    valid_until: nullableText(draft.valid_until),
    review_after: nullableText(draft.review_after),
    source_message: nullableText(draft.source_message),
    source_conversation_id: nullableText(draft.source_conversation_id)
  };
}
