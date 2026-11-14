"""Document ingestion.

Every supported format is reduced to an ordered list of semantic blocks:
headings, paragraphs, list items, quotes, tables and code.  Translation works on
blocks, which keeps reading order and lets the reader render a clean bilingual
view without caring about the original file format.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".epub",
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".djvu",
}

_MATH_HINTS = re.compile(r"[=≈≠≤≥∑∏∫√∞∂∇∈∉⊂⊃∪∩→↦∀∃±×÷^_{}\\]")
_PAGE_NUMBER = re.compile(r"^[\s\-–—·•]*([ivxlcdm]+|\d{1,4})[\s\-–—·•]*$", re.IGNORECASE)
_CAPTION = re.compile(r"^(?:fig(?:ure)?\.?|图|表)\s*\d", re.IGNORECASE)
_CHAPTER = re.compile(
    r"^(chapter|part|section|book|lecture|appendix|preface|introduction|contents|"
    r"第[一二三四五六七八九十百零〇\d]+[章节篇部卷]|序言|前言|目录|附录|参考文献)\b",
    re.IGNORECASE,
)


class ParseError(Exception):
    """A user-correctable ingestion error."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(slots=True)
class Block:
    text: str
    kind: str = "paragraph"
    level: int = 0
    page: int = 0
    chapter: str = ""
    translatable: bool = True


@dataclass(slots=True)
class ParsedDocument:
    title: str
    author: str = ""
    blocks: list[Block] = field(default_factory=list)
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return sum(len(block.text) for block in self.blocks if block.translatable)

    @property
    def word_count(self) -> int:
        return _word_count(" ".join(b.text for b in self.blocks if b.translatable))


# ---------------------------------------------------------------------------
# text utilities
# ---------------------------------------------------------------------------
def _word_count(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*", text))
    return cjk + words


def _clean(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\ufeff", "").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _join_pdf_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            if out and not out.endswith(" "):
                out += " "
            continue
        if not out:
            out = line
        elif out.endswith("-") and line[:1].islower():
            out = out[:-1] + line
        elif out.endswith(("—", "–", "/")) or re.search(r"[\u3400-\u9fff]$", out) and re.match(r"[\u3400-\u9fff]", line):
            out += line
        else:
            out += " " + line
    return re.sub(r"\s+", " ", out).strip()


def _looks_like_heading(text: str, *, size: float = 0, body_size: float = 0, bold: bool = False) -> bool:
    text = text.strip()
    if not text or len(text) > 180:
        return False
    if text.endswith((".", "。", ";", "；", ",", "，")) and len(text) > 50:
        return False
    if _CHAPTER.match(text):
        return True
    if size and body_size and size >= body_size * 1.18 and len(text) <= 120:
        return True
    if bold and len(text) <= 80 and "。" not in text and text.count(" ") <= 12:
        return True
    return False


def _chapter_number(text: str) -> int:
    match = re.match(r"^(#{1,6})\s", text)
    if match:
        return len(match.group(1))
    match = re.match(r"^chapter\s+(\d+)", text, re.IGNORECASE)
    if match:
        return 1
    return 1


def split_long_text(text: str, limit: int = 3800) -> list[str]:
    """Split an oversized block at sentence/paragraph boundaries."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    sentence = re.compile(r"(?<=[.!?。！？；;:])\s+|\n{2,}")
    while len(remaining) > limit:
        window = remaining[:limit]
        cuts = [m.end() for m in sentence.finditer(window)]
        cut = cuts[-1] if cuts else window.rfind(" ")
        if cut < limit * 0.45:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _is_running_header(text: str, page_number: int) -> bool:
    """Recognise ``2 1. The Basics`` / ``1.1 Graphs 3`` style running heads."""
    stripped = text.strip()
    start = re.match(r"^(\d{1,4})\s+", stripped)
    if start and int(start.group(1)) == page_number:
        return True
    end = re.search(r"\s+(\d{1,4})$", stripped)
    if end and int(end.group(1)) == page_number:
        return True
    return False


def _strip_repeated_marginalia(pages: list[list[str]]) -> set[str]:
    """Find running headers/footers repeated across pages."""
    if len(pages) < 4:
        return set()
    counts: dict[str, int] = {}
    for page in pages:
        candidates = page[:1] + page[-1:]
        for line in candidates:
            normalized = re.sub(r"\d+", "#", line.strip().lower())
            normalized = re.sub(r"[ivxlcdm]+", "i", normalized)
            if 2 <= len(normalized) <= 120:
                counts[normalized] = counts.get(normalized, 0) + 1
    threshold = max(3, int(len(pages) * 0.3))
    return {line for line, count in counts.items() if count >= threshold}


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _parse_pdf(path: Path, title_hint: str) -> ParsedDocument:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - optional fallback
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise ParseError("服务器未安装 PDF 解析组件。") from exc

    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise ParseError(f"无法打开 PDF：{exc}") from exc

    metadata = doc.metadata or {}
    page_texts: list[list[str]] = []
    page_blocks: list[list[dict[str, Any]]] = []
    for page in doc:
        raw = page.get_text("dict")
        page_height = float(page.rect.height or 1)
        blocks: list[dict[str, Any]] = []
        lines_for_margin: list[str] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines: list[str] = []
            sizes: list[float] = []
            bold = False
            for line in block.get("lines", []):
                span_text = "".join(span.get("text", "") for span in line.get("spans", []))
                if span_text.strip():
                    lines.append(span_text)
                    for span in line.get("spans", []):
                        sizes.append(float(span.get("size") or 0))
                        bold = bold or bool(int(span.get("flags") or 0) & 16)
            text = _join_pdf_lines(lines)
            if not text:
                continue
            avg_size = sum(sizes) / len(sizes) if sizes else 0
            bbox = block.get("bbox") or (0, 0, 0, page_height)
            y0, y1 = float(bbox[1]), float(bbox[3])
            blocks.append(
                {
                    "text": text,
                    "size": avg_size,
                    "bold": bold,
                    "margin": y0 < page_height * 0.09 or y1 > page_height * 0.91,
                }
            )
            lines_for_margin.append(text)
        page_blocks.append(blocks)
        page_texts.append(lines_for_margin)

    repeated = _strip_repeated_marginalia(page_texts)
    all_sizes = [
        b["size"]
        for page in page_blocks
        for b in page
        if b["size"] and len(b["text"]) > 40
    ]
    body_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 0

    total_chars = 0
    parsed: list[Block] = []
    chapter = ""
    for page_no, page in enumerate(page_blocks, start=1):
        for block in page:
            text = block["text"].strip()
            total_chars += len(text)
            normalized = re.sub(r"\d+", "#", text.strip().lower())
            normalized = re.sub(r"[ivxlcdm]+", "i", normalized)
            if normalized in repeated and len(text) < 140:
                continue
            if _PAGE_NUMBER.match(text):
                continue
            if (
                page_no > 1
                and block.get("margin")
                and len(text) < 140
                and _is_running_header(text, page_no)
            ):
                continue
            if len(text) < 2:
                continue
            is_heading = _looks_like_heading(
                text,
                size=block["size"],
                body_size=body_size,
                bold=block["bold"],
            )
            kind = (
                "heading"
                if is_heading
                else ("caption" if _CAPTION.match(text) else "paragraph")
            )
            level = 0
            if is_heading:
                level = 1
                if body_size and block["size"] >= body_size * 1.55:
                    level = 1
                elif body_size and block["size"] >= body_size * 1.28:
                    level = 2
                else:
                    level = 3
                chapter = text[:200]
            parsed.append(Block(text=text, kind=kind, level=level, page=page_no, chapter=chapter))

    page_count = len(doc)
    doc.close()
    if page_count >= 1 and total_chars < max(20, page_count * 20):
        raise ParseError(
            "这份 PDF 几乎没有可提取的文字，可能是扫描件。",
            hint="请选择「AI 视觉识别」，或先用 OCR（如 ocrmypdf）生成文字层。",
        )
    title = str(metadata.get("title") or "").strip() or title_hint
    author = str(metadata.get("author") or "").strip()
    return ParsedDocument(
        title=title,
        author=author,
        blocks=parsed,
        page_count=page_count,
        metadata={
            "format": "pdf",
            "producer": metadata.get("producer") or "",
            "created": metadata.get("creationDate") or "",
        },
    )


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _parse_docx(path: Path, title_hint: str) -> ParsedDocument:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover
        raise ParseError("服务器未安装 DOCX 解析组件。") from exc

    doc = Document(str(path))
    parsed: list[Block] = []
    chapter = ""
    for item in doc.iter_inner_content():
        if isinstance(item, Paragraph):
            text = _clean(item.text)
            if not text:
                continue
            style = (item.style.name if item.style is not None else "") or ""
            heading_match = re.match(r"heading\s*(\d+)", style, re.IGNORECASE)
            is_heading = bool(heading_match) or style.lower() in {"title", "subtitle"}
            kind = "paragraph"
            level = 0
            if is_heading:
                kind = "heading"
                level = int(heading_match.group(1)) if heading_match else 1
                chapter = text[:200]
            elif _CAPTION.match(text):
                kind = "caption"
            elif "list" in style.lower() or item._p.pPr is not None and item._p.pPr.numPr is not None:
                kind = "list"
            parsed.append(Block(text=text, kind=kind, level=level, chapter=chapter))
        elif isinstance(item, Table):
            rows: list[str] = []
            for row in item.rows:
                cells = [_clean(cell.text) for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parsed.append(
                    Block(
                        text="\n".join(rows),
                        kind="table",
                        chapter=chapter,
                        translatable=True,
                    )
                )

    props = doc.core_properties
    title = (props.title or "").strip() or title_hint
    author = (props.author or "").strip()
    return ParsedDocument(
        title=title,
        author=author,
        blocks=parsed,
        metadata={"format": "docx"},
    )


# ---------------------------------------------------------------------------
# HTML / EPUB
# ---------------------------------------------------------------------------
def _blocks_from_html(
    markup: str,
    *,
    chapter: str = "",
    base_page: int = 0,
    skip_nav: bool = True,
) -> tuple[list[Block], str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml" if _has_lxml() else "html.parser")
    if skip_nav:
        for tag in soup.find_all(["script", "style", "nav", "noscript", "svg"]):
            tag.decompose()
        for tag in soup.find_all(attrs={"aria-hidden": "true"}):
            tag.decompose()

    blocks: list[Block] = []
    current_chapter = chapter
    selector = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "table"]
    for tag in soup.find_all(selector):
        if tag.find_parent(["li", "blockquote", "pre", "table"]):
            continue
        name = tag.name.lower()
        if name == "table":
            rows = []
            for tr in tag.find_all("tr"):
                cells = [_clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                if any(cells):
                    rows.append(" | ".join(cells))
            text = "\n".join(rows)
            kind = "table"
            level = 0
            translatable = True
        else:
            text = _clean(tag.get_text(" ", strip=True))
            kind = "paragraph"
            level = 0
            translatable = True
            if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                kind = "heading"
                level = int(name[1])
                current_chapter = text[:200] or current_chapter
            elif name == "li":
                kind = "list"
            elif name == "blockquote":
                kind = "quote"
            elif _CAPTION.match(text):
                kind = "caption"
            elif name == "pre":
                kind = "code"
                translatable = False
        if not text or len(text) < 1:
            continue
        blocks.append(
            Block(
                text=text,
                kind=kind,
                level=level,
                page=base_page,
                chapter=current_chapter,
                translatable=translatable,
            )
        )
    return blocks, current_chapter


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401

        return True
    except ImportError:
        return False


def _parse_html(path: Path, title_hint: str) -> ParsedDocument:
    markup = _decode_bytes(path.read_bytes())
    blocks, _ = _blocks_from_html(markup)
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml" if _has_lxml() else "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else ""
    return ParsedDocument(
        title=title or title_hint,
        blocks=blocks,
        metadata={"format": "html"},
    )


def _parse_epub(path: Path, title_hint: str) -> ParsedDocument:
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError as exc:  # pragma: no cover
        raise ParseError("服务器未安装 EPUB 解析组件。") from exc

    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception as exc:
        raise ParseError(f"无法打开 EPUB：{exc}") from exc

    title = title_hint
    author = ""
    try:
        titles = book.get_metadata("DC", "title")
        if titles:
            title = _clean(str(titles[0][0])) or title
        creators = book.get_metadata("DC", "creator")
        if creators:
            author = _clean(str(creators[0][0]))
    except Exception:
        pass

    parsed: list[Block] = []
    chapter = ""
    page = 0
    for item_id, _linear in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        page += 1
        try:
            markup = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        blocks, chapter = _blocks_from_html(markup, chapter=chapter, base_page=page)
        parsed.extend(blocks)

    return ParsedDocument(
        title=title,
        author=author,
        blocks=parsed,
        page_count=page,
        metadata={"format": "epub"},
    )


# ---------------------------------------------------------------------------
# plain text / markdown
# ---------------------------------------------------------------------------
def _parse_markdown(path: Path, title_hint: str) -> ParsedDocument:
    text = _clean(_decode_bytes(path.read_bytes()))
    lines = text.split("\n")
    blocks: list[Block] = []
    chapter = ""
    paragraph: list[str] = []
    in_code = False
    code_lines: list[str] = []
    title = title_hint

    def flush_paragraph() -> None:
        nonlocal paragraph
        joined = " ".join(line.strip() for line in paragraph).strip()
        paragraph = []
        if not joined:
            return
        blocks.append(Block(text=joined, kind="paragraph", chapter=chapter))

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                code_lines.append(line)
                blocks.append(
                    Block(
                        text="\n".join(code_lines),
                        kind="code",
                        chapter=chapter,
                        translatable=False,
                    )
                )
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                in_code = True
                code_lines = [line]
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if level == 1 and (not title or title == title_hint):
                title = heading_text
            chapter = heading_text
            blocks.append(
                Block(text=heading_text, kind="heading", level=level, chapter=chapter)
            )
        elif not stripped or re.match(r"^([-*_])\1{2,}$", stripped):
            flush_paragraph()
        elif re.match(r"^>\s?", stripped):
            flush_paragraph()
            quote = re.sub(r"^>\s?", "", stripped)
            if quote:
                blocks.append(Block(text=quote, kind="quote", chapter=chapter))
        elif re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            flush_paragraph()
            blocks.append(Block(text=stripped, kind="list", chapter=chapter))
        elif "|" in stripped and stripped.count("|") >= 2:
            flush_paragraph()
            table_lines = [stripped]
            while i + 1 < len(lines) and "|" in lines[i + 1] and lines[i + 1].count("|") >= 2:
                i += 1
                table_lines.append(lines[i].strip())
            blocks.append(
                Block(text="\n".join(table_lines), kind="table", chapter=chapter)
            )
        else:
            paragraph.append(line)
        i += 1

    if in_code and code_lines:
        blocks.append(
            Block(
                text="\n".join(code_lines),
                kind="code",
                chapter=chapter,
                translatable=False,
            )
        )
    flush_paragraph()
    return ParsedDocument(title=title, blocks=blocks, metadata={"format": "markdown"})


def _parse_txt(path: Path, title_hint: str) -> ParsedDocument:
    text = _clean(_decode_bytes(path.read_bytes()))
    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(raw_blocks) <= 1:
        raw_blocks = [b.strip() for b in text.split("\n") if b.strip()]
    blocks: list[Block] = []
    chapter = ""
    for raw in raw_blocks:
        if _PAGE_NUMBER.match(raw):
            continue
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if not lines:
            continue
        first = lines[0]
        is_heading = (
            len(lines) == 1
            and len(first) <= 120
            and (
                _CHAPTER.match(first)
                or (
                    first.isupper()
                    and len(first) > 3
                    and len(first.split()) <= 10
                )
            )
        )
        joined = "\n".join(lines)
        if is_heading:
            chapter = first[:200]
            blocks.append(
                Block(text=first, kind="heading", level=1, chapter=chapter)
            )
            joined = "\n".join(lines[1:]).strip()
        if joined:
            blocks.append(Block(text=joined, kind="paragraph", chapter=chapter))
    return ParsedDocument(title=title_hint, blocks=blocks, metadata={"format": "txt"})


def _parse_djvu(path: Path, title_hint: str) -> ParsedDocument:
    binary = shutil.which("djvutxt")
    if not binary:
        raise ParseError(
            "服务器缺少 DjVu 文字提取工具。",
            hint="可安装 djvulibre（提供 djvutxt），或先用 Calibre 将 DJVU 转为 PDF/EPUB 再上传。",
        )
    try:
        result = subprocess.run(
            [binary, str(path)], capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ParseError(f"DjVu 提取失败：{exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ParseError(
            "DjVu 文件没有可提取的文字层。",
            hint="请先做 OCR，或转换为 PDF/DOCX 后上传。",
        )
    text = _clean(result.stdout.decode("utf-8", errors="replace"))
    fake = ParsedDocument(title=title_hint, metadata={"format": "djvu"})
    for raw in re.split(r"\n\s*\n", text):
        cleaned = raw.strip()
        if cleaned and not _PAGE_NUMBER.match(cleaned):
            fake.blocks.append(Block(text=cleaned))
    return fake


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def probe_pdf(path: Path, title_hint: str) -> ParsedDocument:
    """Read PDF metadata/page count without requiring a text layer.

    Used when the user selects AI vision OCR: the text layer may be empty or
    formula-mangled, so ingestion must succeed and let the OCR stage fill blocks.
    """
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise ParseError("服务器未安装 PDF 解析组件。") from exc
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise ParseError(f"无法打开 PDF：{exc}") from exc
    metadata = doc.metadata or {}
    page_count = len(doc)
    doc.close()
    title = str(metadata.get("title") or "").strip() or title_hint
    author = str(metadata.get("author") or "").strip()
    return ParsedDocument(
        title=title,
        author=author,
        blocks=[],
        page_count=page_count,
        metadata={
            "format": "pdf",
            "producer": metadata.get("producer") or "",
            "created": metadata.get("creationDate") or "",
            "needs_vision": True,
        },
    )


def parse_document(path: Path, title_hint: str) -> ParsedDocument:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ParseError(
            f"暂不支持 {ext or '未知'} 格式。",
            hint="支持 PDF、DOCX、EPUB、TXT、Markdown、HTML，以及带文字层的 DJVU。",
        )
    if ext == ".pdf":
        parsed = _parse_pdf(path, title_hint)
    elif ext == ".docx":
        parsed = _parse_docx(path, title_hint)
    elif ext == ".epub":
        parsed = _parse_epub(path, title_hint)
    elif ext in {".md", ".markdown"}:
        parsed = _parse_markdown(path, title_hint)
    elif ext in {".html", ".htm"}:
        parsed = _parse_html(path, title_hint)
    elif ext == ".djvu":
        parsed = _parse_djvu(path, title_hint)
    else:
        parsed = _parse_txt(path, title_hint)

    cleaned_blocks: list[Block] = []
    seen_recent: list[str] = []
    for block in parsed.blocks:
        text = _clean(block.text)
        if not text:
            continue
        # Drop repeated page furniture and exact duplicate neighbours.
        fingerprint = re.sub(r"\s+", " ", text)[:180]
        if fingerprint in seen_recent[-6:]:
            continue
        seen_recent.append(fingerprint)
        if block.kind == "heading":
            parts = split_long_text(text, 1000)
            for part in parts:
                cleaned_blocks.append(
                    Block(
                        text=part,
                        kind="heading",
                        level=block.level or 1,
                        page=block.page,
                        chapter=part[:200],
                        translatable=True,
                    )
                )
        else:
            for part in split_long_text(text, 3800):
                cleaned_blocks.append(
                    Block(
                        text=part,
                        kind=block.kind,
                        level=block.level,
                        page=block.page,
                        chapter=block.chapter,
                        translatable=block.translatable,
                    )
                )
    if not cleaned_blocks:
        raise ParseError(
            "没有从文档中提取到可翻译的文字。",
            hint="如果是扫描版，请先 OCR；如果内容主要是图片，请上传带文字的版本。",
        )
    limit = settings_limit()
    if len(cleaned_blocks) > limit:
        raise ParseError(
            f"文档段落数（{len(cleaned_blocks)}）超过单次翻译上限（{limit}）。",
            hint="请把书拆成多卷/多部分后分别上传，或调大 MAX_SOURCE_SEGMENTS。",
        )
    parsed.blocks = cleaned_blocks
    parsed.title = (parsed.title or title_hint).strip()[:300]
    parsed.author = parsed.author.strip()[:200]
    return parsed


def settings_limit() -> int:
    # Imported lazily to keep this module easy to unit test.
    from .config import settings

    return settings.max_source_segments


def build_segments(parsed: ParsedDocument) -> list[dict[str, Any]]:
    """Turn blocks into persisted segment dictionaries with running heading context."""
    chapter = ""
    segments: list[dict[str, Any]] = []
    for idx, block in enumerate(parsed.blocks):
        if block.kind == "heading" and block.text.strip():
            chapter = block.text.strip()[:200]
        segments.append(
            {
                "idx": idx,
                "chapter": chapter,
                "block_type": block.kind,
                "level": block.level,
                "page": block.page,
                "source_text": block.text,
                "translated_text": "",
                "status": "pending" if block.translatable else "skipped",
                "translatable": block.translatable,
            }
        )
    return segments


def decode_preview(data: bytes) -> str:
    return html.escape(_decode_bytes(data)[:2000])
