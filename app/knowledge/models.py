from typing import Literal

from pydantic import BaseModel, Field


KnowledgeSensitivity = Literal["normal", "private", "sensitive"]
KnowledgeDocumentStatus = Literal["active", "deleted"]
KnowledgeIndexStatus = Literal["pending", "indexing", "ready", "failed"]
KnowledgeUploadStatus = Literal[
    "open",
    "committing",
    "committed",
    "failed",
    "expired",
]


class KnowledgeDocument(BaseModel):
    id: str
    ref: str
    user_id: str
    title: str
    source_name: str = ""
    content_type: str = "text/markdown"
    sensitivity: KnowledgeSensitivity = "normal"
    status: KnowledgeDocumentStatus = "active"
    current_version_id: str | None = None
    current_version_ref: str = ""
    current_version_number: int | None = None
    index_status: KnowledgeIndexStatus | None = None
    byte_size: int = 0
    character_count: int = 0
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class KnowledgeVersion(BaseModel):
    id: str
    ref: str
    document_id: str
    document_ref: str
    user_id: str
    version_number: int = Field(ge=1)
    content_sha256: str
    byte_size: int = Field(ge=0)
    character_count: int = Field(ge=0)
    index_status: KnowledgeIndexStatus
    index_error: str | None = None
    created_at: str
    indexed_at: str | None = None
    content: str | None = None


class KnowledgeChunk(BaseModel):
    id: str
    ref: str
    document_id: str
    document_ref: str
    version_id: str
    version_ref: str
    user_id: str
    ordinal: int = Field(ge=0)
    title_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content: str
    created_at: str


class KnowledgeSearchHit(BaseModel):
    document_ref: str
    version_ref: str
    chunk_ref: str
    title: str
    source_name: str = ""
    content_type: str
    sensitivity: KnowledgeSensitivity
    title_path: list[str] = Field(default_factory=list)
    ordinal: int = Field(ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    excerpt: str
    score: float
    match_signals: list[str] = Field(default_factory=list)


class KnowledgeCommitResult(BaseModel):
    document: KnowledgeDocument
    version: KnowledgeVersion
    created: bool = True
    deduplicated: bool = False

    @property
    def index_status(self) -> KnowledgeIndexStatus:
        return self.version.index_status


class KnowledgeUploadSession(BaseModel):
    id: str
    user_id: str
    title: str
    content_type: str
    source_name: str = ""
    sensitivity: KnowledgeSensitivity = "normal"
    replace_document_id: str | None = None
    replace_document_ref: str = ""
    expected_current_version_id: str | None = None
    expected_current_version_ref: str = ""
    status: KnowledgeUploadStatus = "open"
    created_at: str
    updated_at: str
    expires_at: str
    committed_document_ref: str = ""
    committed_version_ref: str = ""


class KnowledgeUploadPart(BaseModel):
    upload_id: str
    sequence: int = Field(ge=0)
    character_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    content_sha256: str
    created_at: str
    duplicate: bool = False
