import type { KnowledgeDocument, KnowledgeVersion } from "../../types";

export function knowledgeDocumentRef(document: KnowledgeDocument): string {
  return document.ref || document.id;
}

export function knowledgeVersionRef(version: KnowledgeVersion): string {
  return version.ref || version.id;
}

export function knowledgeDocumentBytes(document: KnowledgeDocument): number | undefined {
  return document.byte_size ?? document.current_version?.byte_size;
}

export function knowledgeVersionBytes(version: KnowledgeVersion): number | undefined {
  return version.byte_size;
}

export function knowledgeVersionSha(version: KnowledgeVersion): string {
  return version.content_sha256 || "";
}
