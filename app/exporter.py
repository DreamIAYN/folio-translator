"""Export translated books.

All exports are generated from the same segment list, so DOCX, EPUB, Markdown and
the browser reader cannot drift apart.  Two editorial modes are provided:
target-only and bilingual (source + translation).
"""

from __future__ import annotations

import base64
import functools
import html
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

try:
    import latex2mathml.converter as latex2mathml
    import mathml2omml
except ImportError:  # optional at runtime; exports fall back to LaTeX text
    latex2mathml = None
    mathml2omml = None

KATEX_DIR = Path(__file__).resolve().parent / "static" / "vendor" / "katex"
_PDF_LOCK = threading.Lock()
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml_safe(value: Any) -> str:
    return _INVALID_XML.sub("", str(value or ""))


def target_title(doc: dict[str, Any], analysis: dict[str, Any]) -> str:
    return str(
        analysis.get("title_translation")
        or (doc.get("title") or "未命名文档")
    ).strip()


def target_author(doc: dict[str, Any], analysis: dict[str, Any]) -> str:
    return str(
        analysis.get("author_translation") or doc.get("author") or ""
    ).strip()


def _asset_file(asset_root: Path | None, doc_id: str, seg: dict[str, Any]) -> Path | None:
    if asset_root is None or not seg.get("asset_path"):
        return None
    root = (Path(asset_root) / doc_id).resolve()
    path = (root / str(seg["asset_path"])).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def _visible(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in segments if (s.get("translated_text") or s.get("source_text"))]


def _heading_level(seg: dict[str, Any]) -> int:
    if seg.get("block_type") != "heading":
        return 0
    return max(1, min(4, int(seg.get("level") or 1)))


def _body_text(seg: dict[str, Any], *, source: bool = False) -> str:
    return str(seg["source_text"] if source else (seg["translated_text"] or seg["source_text"]))


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _set_docx_fonts(doc) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    style = doc.styles["Normal"]
    style.font.name = "Georgia"
    style.font.size = Pt(11)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), "宋体")
    rfonts.set(qn("w:ascii"), "Georgia")
    rfonts.set(qn("w:hAnsi"), "Georgia")


def _strip_math_delimiters(value: str) -> tuple[str, bool]:
    text = value.strip()
    if text.startswith("$$") and text.endswith("$$") and len(text) > 4:
        return text[2:-2].strip(), True
    if text.startswith("\\[") and text.endswith("\\]"):
        return text[2:-2].strip(), True
    if text.startswith("\\(") and text.endswith("\\)"):
        return text[2:-2].strip(), False
    if text.startswith("$") and text.endswith("$") and len(text) > 2:
        return text[1:-1].strip(), False
    return text, False


_MATH_PART = re.compile(r"(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|\$.+?\$)", re.DOTALL)


def _append_omml(paragraph, latex: str) -> bool:
    if latex2mathml is None or mathml2omml is None:
        return False
    try:
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls

        mathml = latex2mathml.convert(_strip_math_delimiters(latex)[0])
        omml = mathml2omml.convert(mathml)
        if "xmlns:m" not in omml:
            omml = omml.replace("<m:oMath>", f"<m:oMath {nsdecls('m')}>", 1)
        paragraph._p.append(parse_xml(omml))
        return True
    except Exception:
        return False


def _append_rich_text(paragraph, text: str, *, size=None, color=None) -> None:
    """Append text, converting inline $...$ math into Word equations when possible."""
    parts = _MATH_PART.split(text)
    if len(parts) == 1:
        run = paragraph.add_run(text)
    else:
        for part in parts:
            if not part:
                continue
            if _MATH_PART.fullmatch(part) and _append_omml(paragraph, part):
                continue
            run = paragraph.add_run(part)
            if size is not None:
                run.font.size = size
            if color is not None:
                run.font.color.rgb = color
        return
    if size is not None:
        run.font.size = size
    if color is not None:
        run.font.color.rgb = color


def _docx_add_segment(
    doc,
    seg: dict[str, Any],
    *,
    bilingual: bool,
    asset_root: Path | None = None,
    doc_id: str = "",
) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    level = _heading_level(seg)
    translated = seg.get("translated_text") or seg.get("source_text") or ""
    if seg.get("block_type") == "caption":
        asset = _asset_file(asset_root, doc_id, seg)
        if asset is not None:
            doc.add_picture(str(asset), width=Inches(6.0))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    if level:
        if bilingual and seg.get("translated_text"):
            p = doc.add_paragraph()
            run = p.add_run(seg.get("source_text") or "")
            run.italic = True
            run.font.size = Pt(10)
            p.paragraph_format.space_after = Pt(2)
        doc.add_heading(translated, level=level)
        return
    if bilingual and seg.get("translated_text"):
        source_p = doc.add_paragraph()
        source_run = source_p.add_run(seg.get("source_text") or "")
        source_run.italic = True
        source_run.font.size = Pt(9.5)
        source_run.font.color.rgb = RGBColor(0x7A, 0x72, 0x68)
        source_p.paragraph_format.space_after = Pt(2)
        translated_p = doc.add_paragraph()
        _append_rich_text(translated_p, translated)
        translated_p.paragraph_format.space_after = Pt(8)
        return
    kind = seg.get("block_type")
    if kind == "formula":
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if not _append_omml(p, translated):
            run = p.add_run(translated)
            run.font.name = "Cambria Math"
            run.italic = True
    elif kind == "list":
        p = doc.add_paragraph(style="List Bullet")
        _append_rich_text(p, translated)
    elif kind == "quote":
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Pt(24)
        _append_rich_text(p, translated)
        for run in p.runs:
            run.italic = True
    elif kind == "code":
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Pt(12)
        run = p.add_run(translated)
        run.font.name = "Consolas"
        run.font.size = Pt(9)
    else:
        p = doc.add_paragraph()
        _append_rich_text(p, translated)

def _build_docx(
    doc: dict[str, Any],
    segments: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    analysis: dict[str, Any],
    path: Path,
    *,
    bilingual: bool,
    asset_root: Path | None = None,
) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    book = Document()
    _set_docx_fonts(book)
    section = book.sections[0]
    section.left_margin = Pt(72)
    section.right_margin = Pt(72)
    section.top_margin = Pt(72)
    section.bottom_margin = Pt(72)
    title = target_title(doc, analysis) or "未命名文档"
    author = target_author(doc, analysis)
    book.core_properties.title = title
    book.core_properties.author = author or "Folio 译坊"

    p = book.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(title)
    run.bold = True
    run.font.size = Pt(26)
    p.paragraph_format.space_before = Pt(120)
    p.paragraph_format.space_after = Pt(12)
    if author:
        p = book.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(author)
        run.font.size = Pt(13)
    p = book.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(("双语对照版" if bilingual else "中文译本") + " · Folio 译坊")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x8A, 0x7F, 0x72)
    book.add_page_break()

    for seg in _visible(segments):
        _docx_add_segment(
            book,
            seg,
            bilingual=bilingual,
            asset_root=asset_root,
            doc_id=str(doc.get("id") or ""),
        )

    if glossary:
        book.add_page_break()
        book.add_heading("术语表", level=1)
        for item in glossary:
            p = book.add_paragraph()
            run = p.add_run(f"{item['source_term']}  →  {item['target_term']}")
            run.bold = True
            if item.get("note"):
                p.add_run(f"　{item['note']}").italic = True

    book.save(str(path))


# ---------------------------------------------------------------------------
# Markdown / text
# ---------------------------------------------------------------------------
def _build_markdown(
    doc: dict[str, Any],
    segments: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    analysis: dict[str, Any],
    path: Path,
    *,
    bilingual: bool,
    asset_root: Path | None = None,
) -> None:
    title = target_title(doc, analysis)
    author = target_author(doc, analysis)
    lines = [f"# {title}", ""]
    if author:
        lines += [f"*{author}*", ""]
    lines += ["---", ""]
    for seg in _visible(segments):
        translated = seg.get("translated_text") or seg.get("source_text") or ""
        level = _heading_level(seg)
        if level:
            lines += [f"{'#' * min(6, level + 1)} {translated}", ""]
            continue
        kind = seg.get("block_type")
        if seg.get("block_type") == "caption":
            asset = _asset_file(asset_root, str(doc.get("id") or ""), seg)
            if asset is not None:
                assets_dir = path.parent / "assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(asset, assets_dir / asset.name)
                lines += [f"![{translated[:120]}](assets/{asset.name})", ""]
        if bilingual and seg.get("translated_text"):
            source = (seg.get("source_text") or "").replace("\n", "\n> ")
            lines += [f"> **原文**　{source}", ""]
        if kind == "code":
            lines += ["```", translated, "```", ""]
        elif kind == "formula":
            if "$$" in translated or "\\[" in translated:
                lines += [translated, ""]
            else:
                lines += ["$$", translated, "$$", ""]
        elif kind == "quote":
            lines += ["> " + translated.replace("\n", "\n> "), ""]
        elif kind == "table":
            lines += [translated, ""]
        else:
            lines += [translated, ""]
    if glossary:
        lines += ["## 术语表", ""]
        for item in glossary:
            note = f" — {item['note']}" if item.get("note") else ""
            lines.append(f"- **{item['source_term']}** → {item['target_term']}{note}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_text(
    doc: dict[str, Any],
    segments: list[dict[str, Any]],
    analysis: dict[str, Any],
    path: Path,
    *,
    bilingual: bool,
) -> None:
    title = target_title(doc, analysis)
    author = target_author(doc, analysis)
    lines = [title]
    if author:
        lines += [author]
    lines += ["", "=" * 36, ""]
    for seg in _visible(segments):
        translated = seg.get("translated_text") or seg.get("source_text") or ""
        if _heading_level(seg):
            lines += ["", translated, "-" * min(30, max(8, len(translated))), ""]
        elif seg.get("block_type") == "list":
            lines.append(f"· {translated}")
        else:
            if bilingual and seg.get("translated_text"):
                lines += [translated, "", f"[原文] {seg.get('source_text')}", ""]
            else:
                lines += [translated, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML / EPUB
# ---------------------------------------------------------------------------
def _segment_html(
    seg: dict[str, Any],
    *,
    bilingual: bool,
    asset_root: Path | None = None,
    doc_id: str = "",
) -> str:
    translated = html.escape(seg.get("translated_text") or seg.get("source_text") or "")
    source = html.escape(seg.get("source_text") or "")
    kind = seg.get("block_type")
    level = _heading_level(seg)
    figure = ""
    asset = _asset_file(asset_root, doc_id, seg)
    if asset is not None:
        encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
        figure = (
            f'<figure><img src="data:image/png;base64,{encoded}" '
            f'alt="{html.escape((seg.get("source_text") or "")[:200])}"></figure>'
        )
    if level:
        tag = f"h{min(6, level + 1)}"
        block = f"<{tag}>{translated}</{tag}>"
        if bilingual and seg.get("translated_text"):
            block = f'<p class="src heading-src">{source}</p>' + block
        return figure + block
    if kind == "code":
        return figure + f'<pre class="code"><code>{translated}</code></pre>'
    if kind == "formula":
        def display_math(value: str) -> str:
            if "$" in value or "\\[" in value or "\\(" in value:
                # A single $...$ is valid KaTeX; leave the author's delimiters intact.
                if value.strip().startswith("$") and not value.strip().startswith("$$"):
                    inner = value.strip()[1:-1].strip()
                    return f"$${inner}$$"
                return value
            return f"$${value}$$"

        content = display_math(translated)
        block = f'<div class="math">{content}</div>'
        if bilingual and seg.get("translated_text"):
            source_math = display_math(source)
            block = f'<div class="math src">{source_math}</div>' + block
        return figure + block
    if kind == "quote":
        block = f"<blockquote>{translated}</blockquote>"
    elif kind == "list":
        block = f"<li>{translated}</li>"
    elif kind == "table":
        block = f'<pre class="table">{translated}</pre>'
    else:
        block = f"<p>{translated}</p>"
    if bilingual and seg.get("translated_text"):
        block = f'<p class="src">{source}</p>' + block
    return figure + block


def _build_html(
    doc: dict[str, Any],
    segments: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    analysis: dict[str, Any],
    path: Path,
    *,
    bilingual: bool,
    asset_root: Path | None = None,
) -> None:
    title = html.escape(target_title(doc, analysis))
    author = html.escape(target_author(doc, analysis))
    body: list[str] = []
    in_list = False
    for seg in _visible(segments):
        level = _heading_level(seg)
        if in_list and level:
            body.append("</ul>")
            in_list = False
        if seg.get("block_type") == "list" and not in_list:
            body.append("<ul>")
            in_list = True
        elif seg.get("block_type") != "list" and in_list:
            body.append("</ul>")
            in_list = False
        body.append(
            _segment_html(
                seg,
                bilingual=bilingual,
                asset_root=asset_root,
                doc_id=str(doc.get("id") or ""),
            )
        )
    if in_list:
        body.append("</ul>")
    katex_assets: tuple[str, str] = ("", "")
    if any(
        s.get("block_type") == "formula"
        or any(
            token in (s.get("translated_text") or s.get("source_text") or "")
            for token in ("$", "\\(", "\\[")
        )
        for s in segments
    ):
        katex_assets = _katex_assets()
    glossary_html = ""
    if glossary:
        rows = "".join(
            f"<tr><td>{html.escape(i['source_term'])}</td><td>{html.escape(i['target_term'])}</td>"
            f"<td>{html.escape(i.get('note') or '')}</td></tr>"
            for i in glossary
        )
        glossary_html = (
            '<section class="glossary"><h2>术语表</h2>'
            f"<table><thead><tr><th>原文</th><th>译文</th><th>备注</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section>"
        )
    summary = html.escape(str(analysis.get("summary") or ""))
    page = f"""<!doctype html>
<html lang="{html.escape(doc.get('target_lang') or 'zh-CN')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
{katex_assets[0]}
<style>
:root{{--ink:#2d2924;--muted:#8a7f72;--paper:#fbf8f1;--line:#e5dccb;--accent:#b24a2f}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);
font-family:Georgia,"Songti SC","Noto Serif CJK SC",serif;line-height:1.9}}
.book{{max-width:760px;margin:0 auto;padding:64px 28px 120px}}
h1{{font-size:2.4em;line-height:1.25;margin:.2em 0}} .byline{{color:var(--muted);margin-bottom:2em}}
h2,h3,h4{{line-height:1.4;margin-top:1.6em}} p{{margin:.7em 0}}
.src{{color:var(--muted);font-size:.88em;border-left:2px solid var(--line);padding-left:.9em;
margin:.5em 0 .2em}} .heading-src{{border:0;padding:0}}
blockquote{{border-left:3px solid var(--accent);margin:1em 0;padding:.2em 1em;color:#5d554b}}
pre{{background:#f2ecdf;padding:14px;overflow:auto;border-radius:6px;font-size:.85em}}
.math{{text-align:center;margin:1em 0;overflow-x:auto}} .math.src{{color:var(--muted);font-size:.9em}}
figure{{margin:1.2em 0;text-align:center}} figure img{{max-width:100%;height:auto;border:1px solid var(--line);border-radius:4px;padding:8px;background:#fff}}
ul{{padding-left:1.4em}} table{{width:100%;border-collapse:collapse;margin:1em 0}}
td,th{{border-bottom:1px solid var(--line);padding:8px;text-align:left}}
.glossary{{margin-top:4em;border-top:1px solid var(--line)}} .brief{{background:#f4eee1;
padding:16px 20px;border-radius:8px;color:#5d554b;font-size:.92em}}
@media(max-width:600px){{.book{{padding:32px 18px 80px}}h1{{font-size:1.8em}}}}
</style>
</head>
<body><main class="book">
<h1>{title}</h1>
<div class="byline">{author or '佚名'} · {'双语对照' if bilingual else '译本'}</div>
{f'<div class="brief"><strong>内容简介</strong><br>{summary}</div>' if summary else ''}
{''.join(body)}
{glossary_html}
</main>{katex_assets[1]}</body></html>"""
    path.write_text(page, encoding="utf-8")


def _build_epub(
    doc: dict[str, Any],
    segments: list[dict[str, Any]],
    analysis: dict[str, Any],
    path: Path,
    *,
    asset_root: Path | None = None,
) -> None:
    from ebooklib import epub

    title = target_title(doc, analysis)
    author = target_author(doc, analysis)
    book = epub.EpubBook()
    book.set_identifier(f"folio-{doc['id']}")
    book.set_title(title)
    book.set_language((doc.get("target_lang") or "zh-CN").split("-")[0])
    if author:
        book.add_author(author)
    book.add_metadata("DC", "description", str(analysis.get("summary") or ""))

    image_names: dict[str, str] = {}
    for seg in segments:
        asset = _asset_file(asset_root, str(doc.get("id") or ""), seg)
        if asset is None or asset.name in image_names:
            continue
        media_type = "image/png" if asset.suffix.lower() == ".png" else "image/jpeg"
        image = epub.EpubImage()
        image.file_name = f"images/{asset.name}"
        image.media_type = media_type
        image.content = asset.read_bytes()
        book.add_item(image)
        image_names[str(seg.get("asset_path"))] = f"images/{asset.name}"

    chapters: list[Any] = []
    current: list[str] = []
    current_title = title
    index = 1

    def flush() -> None:
        nonlocal current, current_title, index
        if not current:
            return
        content = "".join(current)
        chapter = epub.EpubHtml(
            title=current_title or f"第 {index} 节",
            file_name=f"chap_{index:03d}.xhtml",
            lang=(doc.get("target_lang") or "zh-CN").split("-")[0],
        )
        chapter.content = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{html.escape(current_title)}</title>
<style>body{{font-family:serif;line-height:1.8}} h2,h3{{line-height:1.35}}
.src{{color:#777;font-size:.85em}} blockquote{{border-left:2px solid #b24a2f;
padding-left:1em;color:#555}} pre{{background:#f2ecdf;padding:10px;white-space:pre-wrap}}</style>
</head><body>{content}</body></html>"""
        book.add_item(chapter)
        chapters.append(chapter)
        index += 1
        current = []

    for seg in _visible(segments):
        level = _heading_level(seg)
        if level and level <= 2 and current:
            flush()
        if seg.get("block_type") == "caption":
            image_name = image_names.get(str(seg.get("asset_path")))
            if image_name:
                current.append(
                    f'<figure><img src="{html.escape(image_name)}" '
                    f'alt="{html.escape((seg.get("source_text") or "")[:160])}"/></figure>'
                )
        if level == 1:
            current_title = seg.get("translated_text") or seg.get("source_text") or title
        translated = html.escape(seg.get("translated_text") or seg.get("source_text") or "")
        if level:
            tag = f"h{min(6, level + 1)}"
            current.append(f"<{tag}>{translated}</{tag}>")
        elif seg.get("block_type") == "code":
            current.append(f"<pre>{translated}</pre>")
        elif seg.get("block_type") == "quote":
            current.append(f"<blockquote>{translated}</blockquote>")
        elif seg.get("block_type") == "list":
            current.append(f"<p>· {translated}</p>")
        else:
            current.append(f"<p>{translated}</p>")
    flush()
    if not chapters:
        chapters = []
    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    epub.write_epub(str(path), book)


@functools.lru_cache(maxsize=1)
def _katex_assets() -> tuple[str, str]:
    """Return inline KaTeX CSS (woff2 fonts embedded) and JS for standalone HTML."""
    css_path = KATEX_DIR / "katex.min.css"
    if not css_path.is_file():
        return ("", "")
    css = css_path.read_text(encoding="utf-8")

    def replace_font(match: re.Match[str]) -> str:
        name = match.group(1)
        font = KATEX_DIR / "fonts" / name
        if not font.is_file():
            return match.group(0)
        encoded = base64.b64encode(font.read_bytes()).decode("ascii")
        return f'url(data:font/woff2;base64,{encoded})'

    css = re.sub(r"url\(fonts/([^)]+\.woff2)\)", replace_font, css)
    js = (KATEX_DIR / "katex.min.js").read_text(encoding="utf-8")
    auto = (KATEX_DIR / "auto-render.min.js").read_text(encoding="utf-8")
    tail = (
        f"<script>{js}</script><script>{auto}</script>"
        "<script>renderMathInElement(document.body,{delimiters:["
        "{left:'$$',right:'$$',display:true},{left:'\\[',right:'\\]',display:true},"
        "{left:'$',right:'$',display:false},{left:'\\(',right:'\\)',display:false}],"
        "throwOnError:false});</script>"
    )
    return (f"<style>{css}</style>", tail)


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def find_chromium() -> str | None:
    import os

    configured = os.environ.get("CHROMIUM_BIN", "").strip()
    if configured and Path(configured).is_file():
        return configured
    for name in ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    cache = Path.home() / ".dsh" / "cache" / "chromium"
    if cache.is_dir():
        for candidate in cache.glob("chrome-headless-shell/*/chrome-headless-shell"):
            if candidate.is_file():
                return str(candidate)
    return None


def _available_memory_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def html_to_pdf(html_path: Path, pdf_path: Path, *, timeout: float = 600.0) -> Path:
    """Print the standalone HTML (KaTeX + images already inlined) to PDF.

    A full book PDF is rendered by Chromium, which can use >1 GB transiently.
    Serialize PDF builds and refuse to start when the host is short on memory.
    """
    if _available_memory_mb() < 1200:
        raise RuntimeError("可用内存不足 1.2 GB，已跳过 PDF 生成以保护系统。")
    if not _PDF_LOCK.acquire(timeout=1):
        raise RuntimeError("已有 PDF 正在生成，请稍后重试。")
    try:
        return _html_to_pdf_locked(html_path, pdf_path, timeout=timeout)
    finally:
        _PDF_LOCK.release()


def _html_to_pdf_locked(html_path: Path, pdf_path: Path, *, timeout: float = 600.0) -> Path:
    chrome = find_chromium()
    if not chrome:
        raise RuntimeError("服务器未找到 Chromium，无法生成 PDF。请设置 CHROMIUM_BIN。")
    html_path = html_path.resolve()
    pdf_path = pdf_path.resolve()
    html = html_path.read_text(encoding="utf-8")
    font = (Path(__file__).resolve().parent / "static" / "fonts" / "FandolSong-Regular.otf")
    if not font.is_file():
        raise RuntimeError("缺少中文字体 FandolSong-Regular.otf，无法生成中文 PDF。")
    font_uri = font.as_uri()
    style = f"""<style>
@font-face {{ font-family: "FolioCJK"; src: url("{font_uri}") format("opentype"); }}
body, .book {{ font-family: "FolioCJK", Georgia, serif !important; }}
@page {{ size: A4; margin: 16mm 14mm; }}
* {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
h1, h2, h3, h4 {{ break-after: avoid; }}
figure, pre, table {{ break-inside: avoid; }}
p {{ orphans: 3; widows: 3; }}
</style>"""
    print_path = html_path.with_name(html_path.stem + ".print.html")
    print_path.write_text(html.replace("</head>", style + "</head>"), encoding="utf-8")
    try:
        result = subprocess.run(
            [
                chrome,
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-pdf-header-footer",
                "--renderer-process-limit=1",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--mute-audio",
                "--no-first-run",
                "--disable-default-apps",
                "--virtual-time-budget=60000",
                f"--print-to-pdf={pdf_path}",
                print_path.as_uri(),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PDF 生成超时。") from exc
    finally:
        print_path.unlink(missing_ok=True)
    if result.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"Chromium 生成 PDF 失败：{detail or result.returncode}")
    return pdf_path


def export_all(
    doc: dict[str, Any],
    segments: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    analysis: dict[str, Any],
    output_dir: Path,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Model/OCR output occasionally contains C0 control characters. They are
    # valid in strings but not in XML, and python-docx/lxml rejects them.
    doc = {
        **doc,
        "title": _xml_safe(doc.get("title")),
        "author": _xml_safe(doc.get("author")),
    }
    segments = [
        {
            **seg,
            "source_text": _xml_safe(seg.get("source_text")),
            "translated_text": _xml_safe(seg.get("translated_text")),
            "chapter": _xml_safe(seg.get("chapter")),
        }
        for seg in segments
    ]
    glossary = [
        {
            **item,
            "source_term": _xml_safe(item.get("source_term")),
            "target_term": _xml_safe(item.get("target_term")),
            "note": _xml_safe(item.get("note")),
        }
        for item in glossary
    ]
    analysis = {
        **analysis,
        "title_translation": _xml_safe(analysis.get("title_translation")),
        "author_translation": _xml_safe(analysis.get("author_translation")),
        "summary": _xml_safe(analysis.get("summary")),
        "translation_strategy": _xml_safe(analysis.get("translation_strategy")),
    }
    _build_docx(
        doc,
        segments,
        glossary,
        analysis,
        output_dir / "translated.docx",
        bilingual=False,
        asset_root=asset_root,
    )
    _build_docx(
        doc,
        segments,
        glossary,
        analysis,
        output_dir / "bilingual.docx",
        bilingual=True,
        asset_root=asset_root,
    )
    _build_markdown(
        doc,
        segments,
        glossary,
        analysis,
        output_dir / "translated.md",
        bilingual=False,
        asset_root=asset_root,
    )
    _build_markdown(
        doc,
        segments,
        glossary,
        analysis,
        output_dir / "bilingual.md",
        bilingual=True,
        asset_root=asset_root,
    )
    _build_text(doc, segments, analysis, output_dir / "translated.txt", bilingual=False)
    _build_html(
        doc,
        segments,
        glossary,
        analysis,
        output_dir / "book.html",
        bilingual=True,
        asset_root=asset_root,
    )
    _build_epub(
        doc, segments, analysis, output_dir / "translated.epub", asset_root=asset_root
    )
    manifest = {
        "title": target_title(doc, analysis),
        "author": target_author(doc, analysis),
        "files": [
            "translated.docx",
            "bilingual.docx",
            "translated.md",
            "bilingual.md",
            "translated.txt",
            "book.html",
            "translated.epub",
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


EXPORT_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "bilingual_docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
    "bilingual_md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
}

EXPORT_FILES = {
    "docx": "translated.docx",
    "bilingual_docx": "bilingual.docx",
    "md": "translated.md",
    "bilingual_md": "bilingual.md",
    "txt": "translated.txt",
    "html": "book.html",
    "epub": "translated.epub",
    "pdf": "book.pdf",
}
