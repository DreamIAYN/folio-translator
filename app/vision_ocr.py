"""AI vision OCR for scanned or formula-heavy PDF pages.

Text extraction from PDFs often linearises figure formulas into unreadable
fragments (``G G ∪ - G ∩``).  This module renders each page and asks a
vision-capable model through mini-harness to transcribe the page as structured
blocks, with all mathematics written as LaTeX.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .harness import HarnessClient, HarnessError

OCR_SYSTEM = """You are a precise document OCR and mathematical typesetting engine.
You receive one page image from a book or academic document. Transcribe it; never
translate, summarise, explain or describe the picture.

Output ONLY one valid JSON object with this shape:
{"blocks":[{"type":"heading|paragraph|list|quote|table|code|formula|caption|note","level":0,"text":"..."}]}

Rules:
- Write ALL mathematics in LaTeX. Inline math uses $...$; display math uses $$...$$.
  Preserve fractions, radicals, subscripts, superscripts, set symbols, operators,
  Greek letters, arrows, quantifiers and equation numbering exactly.
- Do not replace a formula with a description. Do not "fix" the author's notation.
- Keep the reading order. Merge running prose into readable paragraphs; keep list
  items, theorem statements, proofs, quotations and tables as separate blocks.
- A displayed equation or a multi-line derivation is a "formula" block containing
  $$...$$. A figure's caption is a "caption" block starting with "Fig." if present.
- For figures and diagrams, transcribe legible mathematical labels, axis labels and
  equations into "formula" or "caption" blocks. Never describe the drawing itself or
  invent relationships that are not written.
- Tables: one "table" block using Markdown pipe rows.
- Marginal annotations: if a term clearly belongs to a phrase, put it in parentheses
  inline; otherwise omit it. Ignore page numbers, running headers and running footers.
- Heading levels: chapter=1, section=2, subsection=3, other=4.
- Text outside math must be ordinary Unicode text, not LaTeX commands.
- If the page is blank, return {"blocks":[]}.
- No Markdown fences, no preamble, no commentary.
"""

ALLOWED_TYPES = {
    "heading",
    "paragraph",
    "list",
    "quote",
    "table",
    "code",
    "formula",
    "caption",
    "note",
}


@dataclass(slots=True)
class OCRPage:
    page_no: int
    blocks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""


def render_pdf_page(pdf_path: Path, page_no: int, dpi: int) -> tuple[bytes, str]:
    """Render one 1-based PDF page to JPEG bytes and a data URL."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("服务器未安装 PDF 解析组件。") from exc

    doc = pymupdf.open(pdf_path)
    try:
        if page_no < 1 or page_no > len(doc):
            raise ValueError(f"page {page_no} out of range")
        page = doc[page_no - 1]
        scale = max(1.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        data = pix.tobytes("jpeg", jpg_quality=90)
    finally:
        doc.close()
    encoded = base64.b64encode(data).decode("ascii")
    return data, f"data:image/jpeg;base64,{encoded}"


def normalize_blocks(raw: dict[str, Any], page_no: int) -> list[dict[str, Any]]:
    entries = raw.get("blocks")
    if not isinstance(entries, list):
        # Tolerate a model that returns a single block or raw text.
        if isinstance(raw.get("text"), str):
            entries = [{"type": "paragraph", "text": raw["text"]}]
        else:
            raise ValueError("OCR response has no blocks array")

    blocks: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "paragraph").lower().strip()
        if kind not in ALLOWED_TYPES:
            kind = "paragraph"
        text = str(entry.get("text") or "").strip()
        text = re.sub(r"^```(?:json|markdown|md)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        if not text:
            continue
        if kind == "formula":
            if "$" not in text and "\\(" not in text and "\\[" not in text:
                text = f"$${text}$$"
            elif text.startswith("\\[") and text.endswith("\\]"):
                text = f"$${text[2:-2].strip()}$$"
        try:
            level = int(entry.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        if kind == "heading":
            level = min(4, max(1, level or 1))
        else:
            level = 0
        blocks.append({"type": kind, "level": level, "text": text[:12000], "page": page_no})
    return blocks


def ocr_page(
    client: HarnessClient,
    settings: Settings,
    *,
    pdf_path: Path,
    page_no: int,
    total_pages: int,
    title: str,
    model: str | None = None,
) -> OCRPage:
    image_bytes, data_url = render_pdf_page(pdf_path, page_no, settings.vision_ocr_dpi)
    prompt = f"""Transcribe page {page_no} of {total_pages} of the document "{title}".

The image below is the complete page. It may contain prose, theorem statements,
proofs, displayed equations, tables, marginal annotations and figure captions.
Return the JSON described in the system instruction. Preserve every mathematical
expression as LaTeX; do not translate any text.

Approximate image size: {len(image_bytes) // 1024} KB."""
    try:
        parsed, _raw, usage = client.run_json(
            prompt,
            system=OCR_SYSTEM,
            model=model or settings.vision_ocr_model,
            max_tokens=settings.vision_ocr_max_tokens,
            retries=1,
            images=[data_url],
        )
        blocks = normalize_blocks(parsed, page_no)
        return OCRPage(page_no=page_no, blocks=blocks, usage=usage)
    except HarnessError as exc:
        if exc.status is not None:
            raise
        return OCRPage(page_no=page_no, error=str(exc)[:1000])
    except Exception as exc:
        return OCRPage(page_no=page_no, error=str(exc)[:1000])


def blocks_to_parsed(blocks: list[dict[str, Any]], *, title: str, author: str, page_count: int):
    """Convert OCR blocks to parser.Block/ParsedDocument."""
    from .parser import Block, ParsedDocument, _clean

    parsed_blocks: list[Block] = []
    chapter = ""
    for item in blocks:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        kind = str(item.get("type") or "paragraph")
        if kind == "heading":
            chapter = text[:200]
        parsed_blocks.append(
            Block(
                text=_clean(text) if kind not in {"formula", "code"} else text,
                kind=kind,
                level=int(item.get("level") or 0),
                page=int(item.get("page") or 0),
                chapter=chapter,
                translatable=kind not in {"code"},
            )
        )
    return ParsedDocument(
        title=title,
        author=author,
        blocks=parsed_blocks,
        page_count=page_count,
        metadata={"format": "pdf", "ocr": "vision"},
    )
