from __future__ import annotations

from io import BytesIO
import zipfile

import pytest

from app.knowledge.parsing import parse_knowledge_file
from app.knowledge.store import KnowledgeValidationError


def _zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    return _zip_bytes(
        {
            "word/document.xml": """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>架构说明</w:t></w:r></w:p>
    <w:p><w:r><w:t>DOCX-MARKER-42</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>组件</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>本地索引</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>"""
        }
    )


def _epub_bytes() -> bytes:
    return _zip_bytes(
        {
            "META-INF/container.xml": """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>
</container>""",
            "OEBPS/content.opf": """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试电子书</dc:title></metadata>
  <manifest>
    <item id="chapter-1" href="chapter-1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter-2" href="chapter-2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter-1"/><itemref idref="chapter-2"/></spine>
</package>""",
            "OEBPS/chapter-1.xhtml": "<html><body><h1>第一章</h1><p>EPUB-MARKER-42</p></body></html>",
            "OEBPS/chapter-2.xhtml": "<html><body><h2>第二章</h2><p>严格按 spine 顺序读取。</p></body></html>",
        }
    )


def _pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 14 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload += f"{index} 0 obj\n".encode() + value + b"\nendobj\n"
    xref_offset = len(payload)
    payload += f"xref\n0 {len(objects) + 1}\n".encode()
    payload += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        payload += f"{offset:010d} 00000 n \n".encode()
    payload += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(payload)


def test_local_parsers_preserve_readable_pdf_docx_and_epub_text() -> None:
    pdf = parse_knowledge_file(_pdf_bytes("Knowledge PDF marker ALPHA-42"), filename="guide.pdf")
    docx = parse_knowledge_file(_docx_bytes(), filename="architecture.docx")
    epub = parse_knowledge_file(_epub_bytes(), filename="book.epub")

    assert pdf.source_format == "pdf"
    assert pdf.page_count == 1
    assert "ALPHA-42" in pdf.text
    assert docx.source_format == "docx"
    assert "# 架构说明" in docx.text
    assert "| 组件 | 本地索引 |" in docx.text
    assert epub.source_format == "epub"
    assert epub.suggested_title == "测试电子书"
    assert epub.text.index("EPUB-MARKER-42") < epub.text.index("spine 顺序")


def test_text_parser_rejects_non_utf8_and_archives_reject_unsafe_paths() -> None:
    with pytest.raises(KnowledgeValidationError, match="UTF-8"):
        parse_knowledge_file(b"\xff\xfe", filename="legacy.txt")
    with pytest.raises(KnowledgeValidationError, match="unsafe path"):
        parse_knowledge_file(
            _zip_bytes({"../word/document.xml": "<document/>"}),
            filename="unsafe.docx",
        )


def test_docx_table_cell_count_limit_rejects_amplification() -> None:
    row = "<w:tr>" + "<w:tc/>" * 450 + "</w:tr>"
    table = "<w:tbl>" + row * 450 + "</w:tbl>"
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{table}</w:body>"
        "</w:document>"
    )
    with pytest.raises(KnowledgeValidationError, match="cell count"):
        parse_knowledge_file(
            _zip_bytes({"word/document.xml": document}),
            filename="huge-table.docx",
        )


def test_epub_binary_import_is_searchable_and_keeps_metadata(
    client,
    auth_headers,
) -> None:
    headers = {**auth_headers, "X-User-Id": "alice", "Content-Type": "application/epub+zip"}
    response = client.post(
        "/knowledge/import",
        params={
            "filename": "book.epub",
            "tags": "手册,电子书",
            "metadata_json": '{"department":"研发","year":2026,"source_format":"spoofed"}',
        },
        headers=headers,
        content=_epub_bytes(),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["document"]["title"] == "测试电子书"
    assert payload["document"]["tags"] == ["手册", "电子书"]
    assert payload["document"]["metadata"]["source_format"] == "epub"
    assert payload["document"]["content_type"] == "text/markdown"
    assert payload["document"]["metadata"]["department"] == "研发"
    assert payload["version"]["embedding_status"] == "disabled"

    search = client.post(
        "/knowledge/search",
        headers={**auth_headers, "X-User-Id": "alice"},
        json={
            "request": "EPUB-MARKER-42",
            "tags": ["手册"],
            "metadata_filter": {"year": 2026},
        },
    )
    assert search.status_code == 200, search.text
    assert search.json()["results"][0]["document_ref"] == payload["document"]["document_ref"]


def test_binary_import_reports_invalid_metadata_as_validation_error(
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/knowledge/import",
        params={
            "filename": "book.epub",
            "metadata_json": '{"nested":{"not":"allowed"}}',
        },
        headers={**auth_headers, "Content-Type": "application/epub+zip"},
        content=_epub_bytes(),
    )

    assert response.status_code == 422
    assert "metadata values" in response.json()["detail"]


def test_import_requires_click_through_before_honoring_lower_user_sensitivity(
    client,
    auth_headers,
) -> None:
    headers = {
        **auth_headers,
        "X-User-Id": "alice",
        "Content-Type": "text/plain; charset=utf-8",
    }
    params = {
        "filename": "security-examples.txt",
        "title": "安全示例",
        "sensitivity": "normal",
    }
    content = "教材示例：password=demo-only，不能用于真实系统。".encode()

    warning = client.post(
        "/knowledge/import",
        params=params,
        headers=headers,
        content=content,
    )

    assert warning.status_code == 409
    detail = warning.json()["detail"]
    assert detail == {
        "code": "sensitivity_confirmation_required",
        "message": (
            "本地规则认为该文档比你选择的敏感级别更高。"
            "请检查后明确确认，系统才会按你的选择导入。"
        ),
        "declared_sensitivity": "normal",
        "detected_sensitivity": "sensitive",
    }

    confirmed = client.post(
        "/knowledge/import",
        params={**params, "confirm_sensitivity_override": "true"},
        headers=headers,
        content=content,
    )

    assert confirmed.status_code == 200, confirmed.text
    document = confirmed.json()["document"]
    assert document["sensitivity"] == "normal"
    assert document["detected_sensitivity"] == "sensitive"
    assert document["sensitivity_override_confirmed"] is True
