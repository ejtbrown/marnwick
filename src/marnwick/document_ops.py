from __future__ import annotations

import html
import io
import os
from pathlib import Path
import re
import stat
import textwrap
from xml.etree import ElementTree
import zipfile

from PIL import Image, ImageDraw, ImageFont, ImageOps
from PySide6.QtCore import QBuffer, QIODevice, QSize
from PySide6.QtPdf import QPdfDocument

from .media import MediaKind

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_CHARACTERS = 2_000_000
DOCUMENT_RENDER_VERSION = 1

_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_ODT_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}
_UNSAFE_XML_DECLARATION = re.compile(br"<!\s*(?:doctype|entity)\b", re.IGNORECASE)


def read_document_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    elif path.is_symlink():
        raise OSError(f"refusing symbolic-link document: {path}")
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("document source is not a regular file")
        if before.st_size > MAX_SOURCE_BYTES:
            raise ValueError(
                f"document exceeds the {MAX_SOURCE_BYTES:,}-byte preview limit"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"document exceeds the {MAX_SOURCE_BYTES:,}-byte preview limit")
    if (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_nlink),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_ctime_ns),
    ) != (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_nlink),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    ):
        raise OSError(f"document changed while it was being read: {path}")
    return data


def _read_archive_member(data: bytes, member_name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(member_name)
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"{member_name} exceeds the document preview limit")
        with archive.open(info) as member:
            data = member.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    if len(data) > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError(f"{member_name} exceeds the document preview limit")
    return data


def _safe_xml_root(data: bytes) -> ElementTree.Element:
    if _UNSAFE_XML_DECLARATION.search(data):
        raise ValueError("document XML declarations are not supported")
    return ElementTree.fromstring(data)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-32"):
        try:
            return data.decode(encoding)[:MAX_DOCUMENT_CHARACTERS]
        except UnicodeError:
            continue
    return data.decode("latin-1", errors="replace")[:MAX_DOCUMENT_CHARACTERS]


def _text_to_html(text: str) -> str:
    return (
        "<html><head><style>"
        "body { color: #181818; background: white; font-family: sans-serif; "
        "font-size: 11pt; line-height: 1.35; margin: 32px; }"
        "pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: monospace; }"
        "</style></head><body><pre>"
        f"{html.escape(text)}"
        "</pre></body></html>"
    )


def _word_run_html(run: ElementTree.Element) -> str:
    pieces: list[str] = []
    for child in run:
        local_name = child.tag.rpartition("}")[2]
        if local_name == "t":
            pieces.append(html.escape(child.text or ""))
        elif local_name == "tab":
            pieces.append("&emsp;")
        elif local_name in {"br", "cr"}:
            pieces.append("<br>")
    value = "".join(pieces)
    properties = run.find("w:rPr", _WORD_NS)
    if properties is not None:
        if properties.find("w:b", _WORD_NS) is not None:
            value = f"<strong>{value}</strong>"
        if properties.find("w:i", _WORD_NS) is not None:
            value = f"<em>{value}</em>"
        if properties.find("w:u", _WORD_NS) is not None:
            value = f"<u>{value}</u>"
    return value


def _word_paragraph_html(paragraph: ElementTree.Element) -> str:
    body = "".join(_word_run_html(run) for run in paragraph.findall(".//w:r", _WORD_NS))
    style = paragraph.find("w:pPr/w:pStyle", _WORD_NS)
    style_value = ""
    if style is not None:
        style_value = next(
            (value for key, value in style.attrib.items() if key.rpartition("}")[2] == "val"),
            "",
        )
    heading_match = re.match(r"heading\s*([1-6])", style_value, flags=re.IGNORECASE)
    if heading_match:
        level = heading_match.group(1)
        return f"<h{level}>{body}</h{level}>"
    return f"<p>{body or '&nbsp;'}</p>"


def _load_docx_html(data: bytes) -> str:
    root = _safe_xml_root(_read_archive_member(data, "word/document.xml"))
    body = root.find("w:body", _WORD_NS)
    if body is None:
        return ""
    blocks: list[str] = []
    for child in body:
        local_name = child.tag.rpartition("}")[2]
        if local_name == "p":
            blocks.append(_word_paragraph_html(child))
        elif local_name == "tbl":
            rows: list[str] = []
            for row in child.findall("w:tr", _WORD_NS):
                cells = []
                for cell in row.findall("w:tc", _WORD_NS):
                    value = "".join(
                        _word_paragraph_html(paragraph)
                        for paragraph in cell.findall("w:p", _WORD_NS)
                    )
                    cells.append(f"<td>{value}</td>")
                rows.append(f"<tr>{''.join(cells)}</tr>")
            blocks.append(f"<table>{''.join(rows)}</table>")
    return _wrap_rich_html("".join(blocks))


def _odt_inline_html(element: ElementTree.Element) -> str:
    pieces = [html.escape(element.text or "")]
    for child in element:
        local_name = child.tag.rpartition("}")[2]
        if local_name == "tab":
            value = "&emsp;"
        elif local_name == "line-break":
            value = "<br>"
        else:
            value = _odt_inline_html(child)
        pieces.append(value)
        pieces.append(html.escape(child.tail or ""))
    return "".join(pieces)


def _load_odt_html(data: bytes) -> str:
    root = _safe_xml_root(_read_archive_member(data, "content.xml"))
    text_body = root.find(".//office:body/office:text", _ODT_NS)
    if text_body is None:
        return ""
    blocks: list[str] = []
    for element in text_body.iter():
        local_name = element.tag.rpartition("}")[2]
        if local_name == "h":
            level_text = next(
                (
                    value
                    for key, value in element.attrib.items()
                    if key.rpartition("}")[2] == "outline-level"
                ),
                "1",
            )
            level = min(6, max(1, int(level_text) if level_text.isdigit() else 1))
            blocks.append(f"<h{level}>{_odt_inline_html(element)}</h{level}>")
        elif local_name == "p":
            blocks.append(f"<p>{_odt_inline_html(element) or '&nbsp;'}</p>")
    return _wrap_rich_html("".join(blocks))


def _wrap_rich_html(body: str) -> str:
    return (
        "<html><head><style>"
        "body { color: #181818; background: white; font-family: sans-serif; "
        "font-size: 11pt; line-height: 1.35; margin: 32px; }"
        "table { border-collapse: collapse; width: 100%; }"
        "td { border: 1px solid #aaa; padding: 5px; vertical-align: top; }"
        "</style></head><body>"
        f"{body[:MAX_DOCUMENT_CHARACTERS]}"
        "</body></html>"
    )


def load_document_html(
    path: Path,
    kind: MediaKind,
    *,
    source_data: bytes | None = None,
) -> str:
    data = source_data if source_data is not None else read_document_bytes(path)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"document exceeds the {MAX_SOURCE_BYTES:,}-byte preview limit")
    if kind == "text":
        return _text_to_html(_decode_text(data))
    if kind == "docx":
        return _load_docx_html(data)
    if kind == "odt":
        return _load_odt_html(data)
    raise ValueError(f"{kind!r} is not a rich-text document type")


def _plain_text_from_html(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>|</p>|</h[1-6]>|</tr>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value)


def _pdf_first_page(data: bytes, preview_width: int) -> tuple[Image.Image, int, int]:
    source_buffer = QBuffer()
    source_buffer.setData(data)
    if not source_buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ValueError("could not open PDF data")
    document = QPdfDocument()
    document.load(source_buffer)
    if document.status() != QPdfDocument.Status.Ready or document.pageCount() < 1:
        raise ValueError(f"could not load PDF: {document.error()}")
    points = document.pagePointSize(0)
    source_width = max(1, round(points.width()))
    source_height = max(1, round(points.height()))
    render_height = max(1, round(preview_width * source_height / source_width))
    qimage = document.render(0, QSize(preview_width, render_height))
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not qimage.save(buffer, "PNG"):
        raise ValueError("could not render the first PDF page")
    with Image.open(io.BytesIO(bytes(buffer.data()))) as rendered:
        return rendered.convert("RGB"), source_width, source_height


def _text_first_page(value: str, page_width: int, page_height: int) -> Image.Image:
    page = Image.new("RGB", (page_width, page_height), "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=max(10, page_width // 28))
    margin = max(14, page_width // 14)
    line_height = max(12, int(font.size * 1.35))
    approximate_columns = max(12, (page_width - (2 * margin)) // max(5, font.size // 2))
    lines: list[str] = []
    for logical_line in value.expandtabs(4).splitlines() or [""]:
        lines.extend(
            textwrap.wrap(
                logical_line,
                width=approximate_columns,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    y = margin
    for line in lines:
        if y + line_height > page_height - margin:
            break
        draw.text((margin, y), line, fill=(32, 35, 40), font=font)
        y += line_height
    return page


def render_document_thumbnail(
    path: Path,
    kind: MediaKind,
    native_size: int,
    *,
    source_data: bytes | None = None,
) -> tuple[Image.Image, int, int]:
    """Render the first page inside a folded-corner document silhouette."""

    native_size = max(96, int(native_size))
    margin = max(8, native_size // 24)
    film_width = native_size - (2 * margin)
    film_height = native_size - (2 * margin)
    fold = max(22, native_size // 6)
    preview_inset = max(8, native_size // 28)
    preview_width = film_width - (2 * preview_inset)
    preview_height = film_height - (2 * preview_inset)

    data = source_data if source_data is not None else read_document_bytes(path)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"document exceeds the {MAX_SOURCE_BYTES:,}-byte preview limit")
    if kind == "pdf":
        page, source_width, source_height = _pdf_first_page(data, preview_width)
    else:
        rich_html = load_document_html(path, kind, source_data=data)
        plain_text = _plain_text_from_html(rich_html)
        source_width, source_height = 816, 1056
        page = _text_first_page(plain_text, source_width, source_height)

    canvas = Image.new("RGB", (native_size, native_size), (52, 57, 65))
    draw = ImageDraw.Draw(canvas)
    left, top = margin, margin
    right, bottom = native_size - margin - 1, native_size - margin - 1
    polygon = [
        (left, top),
        (right - fold, top),
        (right, top + fold),
        (right, bottom),
        (left, bottom),
    ]
    draw.polygon(polygon, fill=(246, 246, 244), outline=(188, 191, 195), width=max(1, native_size // 160))
    paper_mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(paper_mask).polygon(polygon, fill=255)

    available_width = preview_width
    available_height = preview_height - (fold // 3)
    contained = ImageOps.contain(
        page.convert("RGB"),
        (available_width, available_height),
        method=Image.Resampling.LANCZOS,
    )
    preview_left = left + preview_inset + ((available_width - contained.width) // 2)
    preview_top = top + preview_inset + ((available_height - contained.height) // 2)
    preview_box = (
        preview_left,
        preview_top,
        preview_left + contained.width,
        preview_top + contained.height,
    )
    canvas.paste(
        contained,
        (preview_left, preview_top),
        mask=paper_mask.crop(preview_box),
    )

    draw = ImageDraw.Draw(canvas)
    draw.polygon(
        [(right - fold, top), (right - fold, top + fold), (right, top + fold)],
        fill=(218, 220, 222),
        outline=(173, 177, 181),
    )
    draw.line((right - fold, top, right, top + fold), fill=(173, 177, 181), width=max(1, native_size // 160))
    return canvas, source_width, source_height
