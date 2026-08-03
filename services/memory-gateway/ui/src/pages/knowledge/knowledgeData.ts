import type { KnowledgeDocument, KnowledgeVersion } from "../../types";

export function knowledgeDocumentRef(document: KnowledgeDocument): string {
  return document.document_ref || document.ref || document.id;
}

export function knowledgeVersionRef(version: KnowledgeVersion): string {
  return version.version_ref || version.ref || version.id;
}

export function knowledgeDocumentBytes(document: KnowledgeDocument): number | undefined {
  return document.byte_size ?? document.size_bytes ?? document.current_version?.byte_size ?? document.current_version?.size_bytes;
}

export function knowledgeVersionBytes(version: KnowledgeVersion): number | undefined {
  return version.byte_size ?? version.size_bytes;
}

export function knowledgeVersionSha(version: KnowledgeVersion): string {
  return version.content_sha256 || version.sha256 || "";
}
