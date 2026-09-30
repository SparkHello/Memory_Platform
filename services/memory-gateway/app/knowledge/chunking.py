from dataclasses import dataclass
import re


_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\n|$)")
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n+")


@dataclass(frozen=True, slots=True)
class KnowledgeChunkDraft:
    ordinal: int
    title_path: tuple[str, ...]
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    content: str


@dataclass(frozen=True, slots=True)
class _Section:
    start: int
    end: int
    title_path: tuple[str, ...]


def chunk_knowledge_text(
    text: str,
    *,
    target_chars: int = 1600,
    overlap_chars: int = 200,
) -> list[KnowledgeChunkDraft]:
    """Split text without rewriting it and retain exact source offsets.

    Markdown headings form hard section boundaries.  Long sections prefer a
    paragraph boundary near ``target_chars`` and otherwise use an exact
    character boundary.  Overlap is applied only inside the same heading
    section, so a chunk is never mislabeled with a neighbouring heading.
    """

    if not text:
        return []
    if target_chars < 32:
        raise ValueError("target_chars must be at least 32")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than target_chars")

    drafts: list[KnowledgeChunkDraft] = []
    # Chunk starts and chunk ends each advance monotonically, but a start can
    # sit before the previous end because of overlap, so give each its own
    # forward-only counter.
    start_lines = _LineCounter(text)
    end_lines = _LineCounter(text)
    for section in _markdown_sections(text):
        start = section.start
        while start < section.end:
            end = _choose_end(
                text,
                start=start,
                section_end=section.end,
                target_chars=target_chars,
            )
            if end <= start:
                end = min(section.end, start + target_chars)
            content = text[start:end]
            drafts.append(
                KnowledgeChunkDraft(
                    ordinal=len(drafts),
                    title_path=section.title_path,
                    char_start=start,
                    char_end=end,
                    line_start=start_lines.line_at(start),
                    line_end=end_lines.last_touched_line(start, end),
                    content=content,
                )
            )
            if end >= section.end:
                break
            next_start = max(start + 1, end - overlap_chars)
            start = _prefer_overlap_boundary(
                text,
                lower=start + 1,
                desired=next_start,
                upper=end,
            )
    return drafts


def _markdown_sections(text: str) -> list[_Section]:
    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        return [_Section(start=0, end=len(text), title_path=())]

    sections: list[_Section] = []
    if headings[0].start() > 0:
        sections.append(_Section(start=0, end=headings[0].start(), title_path=()))

    path: list[str] = []
    for index, heading in enumerate(headings):
        level = len(heading.group(1))
        title = heading.group(2).strip()
        path = path[: level - 1]
        path.append(title)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections.append(
            _Section(
                start=heading.start(),
                end=end,
                title_path=tuple(path),
            )
        )
    sections = [section for section in sections if section.end > section.start]
    return _merge_heading_only_sections(text, sections)


def _merge_heading_only_sections(text: str, sections: list[_Section]) -> list[_Section]:
    """Fold sections that contain nothing but their heading into the next one.

    A bare ``# Title`` chunk carries no answer, yet bm25 length normalisation
    ranked it above real content whenever a query mentioned the document
    name.  The heading text stays in the merged chunk and in the child's
    ``title_path``, so nothing is lost.
    """
    merged: list[_Section] = []
    pending_start: int | None = None
    for index, section in enumerate(sections):
        body = text[section.start : section.end]
        heading = _HEADING_RE.match(body)
        heading_only = (
            heading is not None
            and section.title_path
            and not body[heading.end() :].strip()
            and index + 1 < len(sections)
        )
        if heading_only:
            if pending_start is None:
                pending_start = section.start
            continue
        start = section.start if pending_start is None else pending_start
        pending_start = None
        merged.append(_Section(start=start, end=section.end, title_path=section.title_path))
    return merged


def _choose_end(text: str, *, start: int, section_end: int, target_chars: int) -> int:
    hard_end = min(section_end, start + target_chars)
    if hard_end >= section_end:
        return section_end

    minimum = min(hard_end, start + max(32, target_chars // 2))
    preferred = [
        match.end()
        for match in _PARAGRAPH_BREAK_RE.finditer(text, minimum, hard_end)
    ]
    if preferred:
        return preferred[-1]

    newline = text.rfind("\n", minimum, hard_end)
    return newline + 1 if newline >= minimum else hard_end


def _prefer_overlap_boundary(
    text: str,
    *,
    lower: int,
    desired: int,
    upper: int,
) -> int:
    if desired <= lower:
        return lower
    boundary = text.rfind("\n", lower, desired + 1)
    if boundary >= lower and desired - boundary <= 80:
        return boundary + 1
    return min(desired, upper)


class _LineCounter:
    """Incremental newline counter.

    Chunk offsets grow monotonically, so counting newlines from the previous
    offset keeps line numbering linear in the document size instead of
    rescanning from zero for every chunk (which made a 50 MiB import take
    minutes inside the commit transaction).
    """

    __slots__ = ("_text", "_offset", "_line")

    def __init__(self, text: str) -> None:
        self._text = text
        self._offset = 0
        self._line = 1

    def line_at(self, offset: int) -> int:
        offset = max(0, offset)
        if offset < self._offset:
            self._offset = 0
            self._line = 1
        self._line += self._text.count("\n", self._offset, offset)
        self._offset = offset
        return self._line

    def last_touched_line(self, start: int, end: int) -> int:
        if end <= start:
            return self.line_at(start)
        return self.line_at(end - 1)


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _last_touched_line(text: str, start: int, end: int) -> int:
    if end <= start:
        return _line_at(text, start)
    return text.count("\n", 0, end - 1) + 1
