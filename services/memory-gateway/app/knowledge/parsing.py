from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import PurePosixPath
import posixpath
import re
import signal
import sys
import threading
import time
from typing import Final
from urllib.parse import unquote, urlsplit
import zipfile
import xml.etree.ElementTree as ET

from app.knowledge.store import KnowledgeValidationError


_SUPPORTED_EXTENSIONS: Final = {".txt", ".md", ".markdown", ".pdf", ".docx", ".epub"}
_MAX_ARCHIVE_FILES: Final = 10_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES: Final = 100 * 1024 * 1024
_MAX_TABLE_CELLS: Final = 200_000
_MAX_PDF_PAGES: Final = 1000
_MAX_PDF_TEXT_CHARS: Final = 10_000_000
_PDF_WALL_SECONDS: Final = 30.0
_PDF_CPU_SECONDS: Final = 20
_PDF_ADDRESS_SPACE_BYTES: Final = 512 * 1024 * 1024
_PDF_MEMORY_EXIT_CODE: Final = 75
_PDF_PARSE_SLOTS = threading.BoundedSemaphore(1)
_WORD_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CONTAINER_NS: Final = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS: Final = "http://www.idpf.org/2007/opf"
_DC_NS: Final = "http://purl.org/dc/elements/1.1/"


@dataclass(frozen=True, slots=True)
class ParsedKnowledgeDocument:
    text: str
    suggested_title: str
    source_name: str
    source_format: str
    content_type: str = "text/markdown"
    page_count: int | None = None
    warnings: tuple[str, ...] = ()


class KnowledgeFileParseError(KnowledgeValidationError):
    """Stable machine code plus a user-safe parsing explanation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_knowledge_file(
    data: bytes,
    *,
    filename: str,
    content_type: str = "",
) -> ParsedKnowledgeDocument:
    if not isinstance(data, bytes) or not data:
        raise KnowledgeValidationError("uploaded file must not be empty")
    safe_name = _safe_filename(filename)
    extension = PurePosixPath(safe_name.lower()).suffix
    if extension not in _SUPPORTED_EXTENSIONS:
        raise KnowledgeValidationError(
            "supported knowledge files are TXT, Markdown, PDF, DOCX, and EPUB"
        )
    if extension in {".txt", ".md", ".markdown"}:
        return _parse_text(data, safe_name, extension)
    if extension == ".pdf":
        return _parse_pdf_isolated(data, safe_name)
    if extension == ".docx":
        return _parse_docx(data, safe_name)
    return _parse_epub(data, safe_name)


def _parse_text(data: bytes, filename: str, extension: str) -> ParsedKnowledgeDocument:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KnowledgeValidationError("text and Markdown files must use UTF-8") from exc
    text = _clean_text(text)
    if not text.strip():
        raise KnowledgeValidationError("knowledge file contains no readable text")
    return ParsedKnowledgeDocument(
        text=text,
        suggested_title=_filename_title(filename),
        source_name=filename,
        source_format="markdown" if extension in {".md", ".markdown"} else "text",
        content_type="text/markdown" if extension in {".md", ".markdown"} else "text/plain",
    )


def _parse_pdf_isolated(data: bytes, filename: str) -> ParsedKnowledgeDocument:
    if not _PDF_PARSE_SLOTS.acquire(timeout=_PDF_WALL_SECONDS):
        raise KnowledgeFileParseError(
            "knowledge_pdf_busy",
            "PDF parser is busy; retry after the current import finishes",
        )
    try:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_pdf_worker_entry,
            args=(data, filename, sender),
            name="memory-gateway-pdf-parser",
            daemon=True,
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(_PDF_WALL_SECONDS):
                _terminate_process(process)
                raise KnowledgeFileParseError(
                    "knowledge_pdf_wall_timeout",
                    "PDF parsing exceeded the 30 second wall-time limit",
                )
            try:
                kind, payload = receiver.recv()
            except EOFError:
                kind, payload = "exit", None
        finally:
            receiver.close()
        process.join(timeout=1.0)
        if process.is_alive():
            _terminate_process(process)
        if kind == "ok" and isinstance(payload, ParsedKnowledgeDocument):
            return payload
        if kind == "error" and isinstance(payload, tuple) and len(payload) == 2:
            raise KnowledgeFileParseError(str(payload[0]), str(payload[1]))
        _raise_pdf_worker_exit(process.exitcode)
    finally:
        _PDF_PARSE_SLOTS.release()


def _pdf_worker_entry(data: bytes, filename: str, sender: Connection) -> None:
    try:
        _apply_pdf_worker_limits()
        result = _parse_pdf_in_worker(data, filename)
        sender.send(("ok", result))
    except KnowledgeFileParseError as exc:
        sender.send(("error", (exc.code, str(exc))))
    except MemoryError:
        sender.send(
            (
                "error",
                (
                    "knowledge_pdf_memory_limit",
                    "PDF parsing exceeded the 512 MiB address-space limit",
                ),
            )
        )
    except BaseException:
        sender.send(
            (
                "error",
                ("knowledge_pdf_invalid", "PDF could not be parsed safely"),
            )
        )
    finally:
        sender.close()


def _apply_pdf_worker_limits() -> None:
    try:
        import resource
    except ImportError as exc:
        raise KnowledgeFileParseError(
            "knowledge_pdf_sandbox_unavailable",
            "PDF parsing is unavailable because OS resource limits are unsupported",
        ) from exc
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (_PDF_CPU_SECONDS, _PDF_CPU_SECONDS + 1),
    )
    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (_PDF_ADDRESS_SPACE_BYTES, _PDF_ADDRESS_SPACE_BYTES),
        )
    except (OSError, ValueError):
        if sys.platform != "darwin":
            raise
        # Darwin exposes RLIMIT_AS but rejects lowering it. Keep production
        # Linux on the strict virtual-address limit and enforce the same byte
        # ceiling against peak resident memory in the macOS development build.
        threading.Thread(
            target=_darwin_memory_watchdog,
            args=(resource,),
            daemon=True,
        ).start()


def _darwin_memory_watchdog(resource_module) -> None:
    while True:
        peak_bytes = int(
            resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss
        )
        if peak_bytes > _PDF_ADDRESS_SPACE_BYTES:
            os._exit(_PDF_MEMORY_EXIT_CODE)
        time.sleep(0.05)


def _terminate_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        process.join(timeout=0.1)
        return
    process.terminate()
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)


def _raise_pdf_worker_exit(exitcode: int | None) -> None:
    if exitcode == _PDF_MEMORY_EXIT_CODE:
        raise KnowledgeFileParseError(
            "knowledge_pdf_memory_limit",
            "PDF parsing exceeded the 512 MiB memory limit",
        )
    if exitcode == -getattr(signal, "SIGXCPU", -1):
        raise KnowledgeFileParseError(
            "knowledge_pdf_cpu_limit",
            "PDF parsing exceeded the 20 second CPU limit",
        )
    if exitcode == -getattr(signal, "SIGKILL", -1):
        raise KnowledgeFileParseError(
            "knowledge_pdf_worker_terminated",
            "PDF parser was terminated by its resource boundary",
        )
    raise KnowledgeFileParseError(
        "knowledge_pdf_worker_failed",
        "PDF parser process exited without a valid result",
    )


def _parse_pdf_in_worker(data: bytes, filename: str) -> ParsedKnowledgeDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise KnowledgeValidationError(
            "PDF parsing requires the pypdf package; reinstall project dependencies"
        ) from exc
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise KnowledgeFileParseError(
                "knowledge_pdf_encrypted",
                "encrypted PDF files are not supported",
            )
        page_count = len(reader.pages)
        if page_count > _MAX_PDF_PAGES:
            raise KnowledgeFileParseError(
                "knowledge_pdf_page_limit",
                "PDF exceeds the 1000 page limit",
            )
        sections: list[str] = []
        empty_pages = 0
        extracted_characters = 0
        for index, page in enumerate(reader.pages, start=1):
            extracted = page.extract_text() or ""
            extracted = _clean_text(extracted).strip()
            if not extracted:
                empty_pages += 1
                continue
            extracted_characters += len(extracted)
            if extracted_characters > _MAX_PDF_TEXT_CHARS:
                raise KnowledgeFileParseError(
                    "knowledge_pdf_text_limit",
                    "PDF extracted text exceeds the 10000000 character limit",
                )
            sections.append(f"# 第 {index} 页\n\n{extracted}")
    except KnowledgeFileParseError:
        raise
    except Exception as exc:
        raise KnowledgeFileParseError(
            "knowledge_pdf_invalid",
            "PDF could not be parsed safely",
        ) from exc
    if not sections:
        raise KnowledgeFileParseError(
            "knowledge_pdf_no_text",
            "PDF contains no extractable text; scanned PDFs require OCR before import",
        )
    metadata_title = ""
    try:
        metadata_title = str(getattr(reader.metadata, "title", "") or "").strip()
    except Exception:
        metadata_title = ""
    warnings = ()
    if empty_pages:
        warnings = (f"{empty_pages} page(s) contained no extractable text",)
    return ParsedKnowledgeDocument(
        text="\n\n".join(sections).strip() + "\n",
        suggested_title=metadata_title or _filename_title(filename),
        source_name=filename,
        source_format="pdf",
        page_count=page_count,
        warnings=warnings,
    )


def _parse_docx(data: bytes, filename: str) -> ParsedKnowledgeDocument:
    with _safe_zip(data, "DOCX") as archive:
        try:
            document_root = ET.fromstring(archive.read("word/document.xml"))
        except (KeyError, ET.ParseError) as exc:
            raise KnowledgeValidationError("DOCX document.xml is missing or invalid") from exc
        style_headings = _docx_heading_styles(archive)
        body = document_root.find(f"{{{_WORD_NS}}}body")
        if body is None:
            raise KnowledgeValidationError("DOCX document body is missing")
        blocks: list[str] = []
        for child in body:
            local_name = _local_name(child.tag)
            if local_name == "p":
                paragraph = _docx_paragraph(child, style_headings)
                if paragraph:
                    blocks.append(paragraph)
            elif local_name == "tbl":
                table = _docx_table(child)
                if table:
                    blocks.append(table)
    text = _clean_text("\n\n".join(blocks)).strip()
    if not text:
        raise KnowledgeValidationError("DOCX contains no readable paragraphs or tables")
    return ParsedKnowledgeDocument(
        text=text + "\n",
        suggested_title=_filename_title(filename),
        source_name=filename,
        source_format="docx",
    )


def _docx_heading_styles(archive: zipfile.ZipFile) -> dict[str, int]:
    try:
        root = ET.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return {}
    result: dict[str, int] = {}
    for style in root.findall(f".//{{{_WORD_NS}}}style"):
        style_id = style.get(f"{{{_WORD_NS}}}styleId", "")
        name = style.find(f"{{{_WORD_NS}}}name")
        name_value = name.get(f"{{{_WORD_NS}}}val", "") if name is not None else ""
        match = re.search(r"(?:heading|标题)\s*([1-6])", name_value, re.IGNORECASE)
        if match and style_id:
            result[style_id] = int(match.group(1))
    return result


def _docx_paragraph(element: ET.Element, heading_styles: dict[str, int]) -> str:
    fragments: list[str] = []
    for node in element.iter():
        local_name = _local_name(node.tag)
        if local_name == "t" and node.text:
            fragments.append(node.text)
        elif local_name == "tab":
            fragments.append("\t")
        elif local_name in {"br", "cr"}:
            fragments.append("\n")
    text = "".join(fragments).strip()
    if not text:
        return ""
    properties = element.find(f"{{{_WORD_NS}}}pPr")
    style_id = ""
    is_list = False
    if properties is not None:
        style = properties.find(f"{{{_WORD_NS}}}pStyle")
        style_id = style.get(f"{{{_WORD_NS}}}val", "") if style is not None else ""
        is_list = properties.find(f"{{{_WORD_NS}}}numPr") is not None
    level = heading_styles.get(style_id)
    if level is None:
        match = re.search(r"(?:heading|标题)\s*([1-6])", style_id, re.IGNORECASE)
        level = int(match.group(1)) if match else None
    if level:
        return f"{'#' * level} {text}"
    if is_list:
        return f"- {text}"
    return text


def _docx_table(element: ET.Element) -> str:
    rows: list[list[str]] = []
    for row in element.findall(f"{{{_WORD_NS}}}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{{{_WORD_NS}}}tc"):
            values = [
                _docx_paragraph(paragraph, {})
                for paragraph in cell.findall(f"{{{_WORD_NS}}}p")
            ]
            cells.append("<br>".join(value for value in values if value).replace("|", "\\|"))
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    if len(rows) * width > _MAX_TABLE_CELLS:
        raise KnowledgeValidationError("DOCX table exceeds the supported cell count")
    normalized = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in normalized]
    lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


def _parse_epub(data: bytes, filename: str) -> ParsedKnowledgeDocument:
    with _safe_zip(data, "EPUB") as archive:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(f".//{{{_CONTAINER_NS}}}rootfile")
            package_path = rootfile.get("full-path", "") if rootfile is not None else ""
            if not package_path:
                raise KnowledgeValidationError("EPUB package path is missing")
            package = ET.fromstring(archive.read(package_path))
        except (KeyError, ET.ParseError) as exc:
            raise KnowledgeValidationError("EPUB package metadata is missing or invalid") from exc
        package_dir = PurePosixPath(package_path).parent
        manifest: dict[str, tuple[str, str]] = {}
        for item in package.findall(f".//{{{_OPF_NS}}}manifest/{{{_OPF_NS}}}item"):
            item_id = item.get("id", "")
            href = item.get("href", "")
            media_type = item.get("media-type", "")
            if item_id and href:
                manifest[item_id] = (href, media_type)
        spine_ids = [
            item.get("idref", "")
            for item in package.findall(f".//{{{_OPF_NS}}}spine/{{{_OPF_NS}}}itemref")
        ]
        title_node = package.find(f".//{{{_DC_NS}}}title")
        book_title = (title_node.text or "").strip() if title_node is not None else ""
        sections: list[str] = []
        for index, item_id in enumerate(spine_ids, start=1):
            href, media_type = manifest.get(item_id, ("", ""))
            if not href or media_type not in {"application/xhtml+xml", "text/html"}:
                continue
            href_path = unquote(urlsplit(href).path)
            member_path = PurePosixPath(
                posixpath.normpath(str(package_dir / PurePosixPath(href_path)))
            )
            if member_path.is_absolute() or ".." in member_path.parts:
                continue
            member = str(member_path)
            try:
                raw = archive.read(member)
            except KeyError:
                continue
            chapter = _html_to_markdown(_decode_markup(raw)).strip()
            if not chapter:
                continue
            if not re.match(r"^#{1,6}\s", chapter):
                chapter = f"# 章节 {index}\n\n{chapter}"
            sections.append(chapter)
    if not sections:
        raise KnowledgeValidationError("EPUB contains no readable spine documents")
    return ParsedKnowledgeDocument(
        text="\n\n".join(sections).strip() + "\n",
        suggested_title=book_title or _filename_title(filename),
        source_name=filename,
        source_format="epub",
    )


class _MarkdownHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0
        self.list_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "math", "nav"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._break(2)
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "blockquote", "pre"}:
            self._break(2)
        elif tag in {"ul", "ol"}:
            self.list_depth += 1
            self._break(1)
        elif tag == "li":
            self._break(1)
            self.parts.append("  " * max(0, self.list_depth - 1) + "- ")
        elif tag == "br":
            self._break(1)
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "math", "nav"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)
            self._break(1)
        elif re.fullmatch(r"h[1-6]", tag) or tag in {
            "p",
            "div",
            "section",
            "article",
            "blockquote",
            "pre",
            "li",
            "tr",
        }:
            self._break(2 if tag != "li" else 1)

    def handle_data(self, data: str) -> None:
        if self.ignored_depth or not data:
            return
        compact = re.sub(r"\s+", " ", data)
        if compact.strip():
            if self.parts and not self.parts[-1].endswith((" ", "\n", "\t")):
                self.parts.append(" ")
            self.parts.append(compact.strip())

    def _break(self, count: int) -> None:
        current = "".join(self.parts[-3:])
        missing = count - len(current) + len(current.rstrip("\n"))
        if missing > 0:
            self.parts.append("\n" * missing)


def _html_to_markdown(value: str) -> str:
    parser = _MarkdownHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise KnowledgeValidationError("EPUB chapter HTML could not be parsed") from exc
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _decode_markup(data: bytes) -> str:
    head = data[:500].decode("ascii", errors="ignore")
    match = re.search(r"encoding=[\"']([^\"']+)", head, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


class _safe_zip:
    def __init__(self, data: bytes, label: str) -> None:
        self.data = data
        self.label = label
        self.archive: zipfile.ZipFile | None = None

    def __enter__(self) -> zipfile.ZipFile:
        try:
            archive = zipfile.ZipFile(BytesIO(self.data))
        except zipfile.BadZipFile as exc:
            raise KnowledgeValidationError(f"{self.label} archive is invalid") from exc
        infos = archive.infolist()
        if len(infos) > _MAX_ARCHIVE_FILES:
            archive.close()
            raise KnowledgeValidationError(f"{self.label} archive contains too many files")
        total = sum(max(0, info.file_size) for info in infos)
        if total > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            archive.close()
            raise KnowledgeValidationError(
                f"{self.label} expanded content exceeds 100 MiB"
            )
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                archive.close()
                raise KnowledgeValidationError(f"{self.label} archive contains an unsafe path")
        self.archive = archive
        return archive

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.archive is not None:
            self.archive.close()


def _safe_filename(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeValidationError("filename must not be blank")
    normalized = value.replace("\\", "/").split("/")[-1].strip()
    if not normalized or normalized in {".", ".."} or len(normalized) > 500:
        raise KnowledgeValidationError("filename is invalid")
    return normalized


def _filename_title(filename: str) -> str:
    title = PurePosixPath(filename).stem.strip()
    return title[:300] or "知识文档"


def _clean_text(value: str) -> str:
    return value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
