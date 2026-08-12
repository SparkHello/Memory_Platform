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

const WORD_RE = /[a-z0-9][a-z0-9+._#-]*/gi;
const CJK_RE = /[\u4e00-\u9fff]+/g;

function contentTokens(text: string): string[] {
  const tokens: string[] = [];
  for (const match of text.matchAll(WORD_RE)) {
    const word = match[0].replace(/[+._#-]+$/, "");
    if (word.length >= 2) tokens.push(word.toLocaleLowerCase());
  }
  for (const match of text.matchAll(CJK_RE)) {
    const run = match[0];
    if (run.length === 1) {
      tokens.push(run);
      continue;
    }
    for (let index = 0; index + 1 < run.length; index += 1) {
      tokens.push(run.slice(index, index + 2));
    }
  }
  return tokens;
}

/**
 * 判断记忆正文是否已明显偏离原始来源（如人工编辑后加入了原话没有的事实）。
 * 纯展示层启发式：正文的内容词若大多数在来源原文里找不到，就提示用户
 * "来源仅供追溯"。第三人称改写（"我喜欢X"→"用户喜欢X"）不应触发。
 */
export function contentDivergesFromSource(
  content: string,
  sourceMessage: string | null | undefined
): boolean {
  if (!sourceMessage) return false;
  // "用户"/"User"是改写第三人称时加入的叙述前缀，不算内容词。
  const tokens = contentTokens(content.replace(/用户/g, "")).filter(
    (token) => token !== "user"
  );
  if (tokens.length < 4) return false;
  const haystack = sourceMessage.toLocaleLowerCase();
  const supported = tokens.filter((token) => haystack.includes(token)).length;
  return supported / tokens.length < 0.5;
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
