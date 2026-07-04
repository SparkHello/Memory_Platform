import type {
  MemoryRecord,
  MemorySpace,
  MemorySpacesUpdatePayload,
  MemoryStatus,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  MemoryUpdatePayload
} from "../types";
import { clampNumber, nullableText } from "./format";

export type MemoryFilters = {
  type: "all" | MemoryType;
  status: "all" | MemoryStatus;
  sensitivity: "all" | MemorySensitivity;
  stability: "all" | MemoryStability;
  minImportance: number;
  maxImportance: number;
  minConfidence: number;
  maxConfidence: number;
  hasValidUntil: boolean;
  hasReviewAfter: boolean;
  spaceId: "all" | string;
  topicQuery: string;
  entityQuery: string;
};

export type MemoryEditDraft = {
  content: string;
  type: MemoryType;
  importance: number;
  confidence: number;
  valence: number;
  arousal: number;
  status: MemoryStatus;
  stability: MemoryStability;
  sensitivity: MemorySensitivity;
  valid_until: string;
  review_after: string;
  source_message: string;
  source_conversation_id: string;
  topics: string[];
  entities: string[];
  space_names: string[];
};

export function memoryToEditDraft(
  memory: MemoryRecord,
  spaces: MemorySpace[] = []
): MemoryEditDraft {
  const spacesById = new Map(spaces.map((space) => [space.id, space.name]));
  return {
    content: memory.content,
    type: memory.type,
    importance: memory.importance,
    confidence: memory.confidence,
    valence: memory.valence ?? 0.5,
    arousal: memory.arousal ?? 0.3,
    status: memory.status || "dynamic",
    stability: memory.stability,
    sensitivity: memory.sensitivity,
    valid_until: memory.valid_until || "",
    review_after: memory.review_after || "",
    source_message: memory.source_message || "",
    source_conversation_id: memory.source_conversation_id || "",
    topics: memory.topics || [],
    entities: memory.entities || [],
    space_names: (memory.space_ids || []).map((spaceId) => spacesById.get(spaceId) || spaceId)
  };
}

export function editDraftToPayload(draft: MemoryEditDraft): MemoryUpdatePayload {
  return {
    content: draft.content.trim(),
    type: draft.type,
    importance: Math.round(clampNumber(draft.importance, 1, 10)),
    confidence: clampNumber(draft.confidence, 0, 1),
    valence: clampNumber(draft.valence, 0, 1),
    arousal: clampNumber(draft.arousal, 0, 1),
    status: draft.status,
    stability: draft.stability,
    sensitivity: draft.sensitivity,
    valid_until: nullableText(draft.valid_until),
    review_after: nullableText(draft.review_after),
    source_message: nullableText(draft.source_message),
    source_conversation_id: nullableText(draft.source_conversation_id),
    topics: normalizeTags(draft.topics),
    entities: normalizeTags(draft.entities)
  };
}

export function editDraftToSpacesPayload(draft: MemoryEditDraft): MemorySpacesUpdatePayload {
  return {
    space_ids: [],
    create_space_names: normalizeTags(draft.space_names)
  };
}

export function normalizeTags(values: string[]): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const value of values) {
    const normalized = value.trim().replace(/\s+/g, " ");
    if (!normalized) continue;
    const key = normalized.toLocaleLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    tags.push(normalized);
  }
  return tags;
}
