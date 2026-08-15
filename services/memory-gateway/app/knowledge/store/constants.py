"""Constants shared by the knowledge store modules."""

from __future__ import annotations

import re
import threading
from typing import Final

_DOCUMENT_PREFIX: Final = "knowledge://document/"
_VERSION_PREFIX: Final = "knowledge://version/"
_CHUNK_PREFIX: Final = "knowledge://chunk/"
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTENT_TYPES: Final = {"text/plain", "text/markdown"}
_SENSITIVITIES: Final = {"normal", "private", "sensitive"}
_UPLOAD_PART_MAX_CHARS: Final = 1_048_576
_UPLOAD_TTL_HOURS: Final = 24
_MAX_RESTORE_TOTAL_BYTES: Final = 100 * 1024 * 1024
_READ_MAX_CHARS: Final = 20_000
_SEARCH_MAX_RESULTS: Final = 20
_SEARCH_EXCERPT_CHARS: Final = 800
_KNOWLEDGE_DB_INIT_LOCK = threading.Lock()
