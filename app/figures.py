"""Crop figures from original PDF pages.

A book page often contains vector drawings (graphs, diagrams) that are lost by
text extraction.  Rather than asking a model to describe a picture, we crop the
original artwork and attach it to the OCR caption block.  The crop is rendered
at high resolution and kept as a local asset, so the bilingual reader and exports
show the real figure from the source book.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CAPTION_NUMBER = re.compile(r"(?:fig(?:ure)?\.?|图)\s*(\d+(?:[.\-]\d+)*)", re.IGNORECASE)


@dataclass(slots=True)
class FigureCrop:
    caption: str
    filename: str
    width: int
    height: int
    bbox: tuple[float, float, float, float]


def _union(rects: list[Any]) -> Any:
    import pymupdf

    x0 = min(float(r.x0) for r in rects)
    y0 = min(float(r.y0) for r in rects)
    x1 = max(float(r.x1) for r in rects)
    y1 = max(float(r.y1) for r in rects)
    return pymupdf.Rect(x0, y0, x1, y1)


def _drawing_rects(page) -> list[Any]:
    rects = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None:
            continue
        if float(rect.width) >= 3 or float(rect.height) >= 3:
            rects.append(rect)
    for image in page.get_images(full=True):
        for rect in page.get_image_rects(image[0]):
            # A full-page image is a scan, not a figure; cropping all of it is useless.
            if rect.width * rect.height < page.rect.width * page.rect.height * 0.55:
                rects.append(rect)
    return rects


def _groups(rects: list[Any], gap: float = 34.0) -> list[Any]:
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (float(r.y0), float(r.x0)))
    groups: list[list[Any]] = []
    current: list[Any] = []
    bottom = -1e9
    for rect in ordered:
        if current and float(rect.y0) > bottom + gap:
            groups.append(current)
            current = []
        current.append(rect)
        bottom = max(bottom, float(rect.y1))
    if current:
        groups.append(current)
    return [_union(group) for group in groups]


def _caption_rect(page, caption: str, caption_index: int):
    match = _CAPTION_NUMBER.search(caption)
    if match:
        number = match.group(1)
        for query in (f"Fig. {number}", f"Figure {number}", f"图 {number}"):
            found = page.search_for(query)
            if found:
                return min(found, key=lambda r: float(r.y0))
    found = page.search_for("Fig.")
    if found and caption_index < len(found):
        return found[caption_index]
    found = page.search_for("图")
    if found and caption_index < len(found):
        return found[caption_index]
    return None


def _text_boxes(page) -> list[tuple[Any, str]]:
    boxes: list[tuple[Any, str]] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        text = " ".join(
            "".join(span.get("text", "") for span in line.get("spans", []))
            for line in block.get("lines", [])
        ).strip()
        if text:
            boxes.append((block.get("bbox"), text))
    return boxes


def _figure_rect(
    page,
    caption: str,
    caption_rect,
    drawing_groups: list[Any],
    text_boxes: list[tuple[Any, str]],
    caption_index: int,
):
    import pymupdf

    caption_top = float(caption_rect.y0)
    caption_bottom = float(caption_rect.y1)
    above = [
        g
        for g in drawing_groups
        if float(g.y1) <= caption_top + 4 and float(g.y1) >= caption_top - 330
    ]
    below = [
        g
        for g in drawing_groups
        if float(g.y0) >= caption_bottom - 4 and float(g.y0) <= caption_bottom + 330
    ]
    group = max(above, key=lambda g: float(g.y1)) if above else (
        min(below, key=lambda g: float(g.y0)) if below else None
    )
    if group is None:
        return None

    pieces = [group]
    x0, y0, x1, y1 = map(float, (group.x0, group.y0, group.x1, group.y1))
    for bbox, text in text_boxes:
        bx0, by0, bx1, by1 = (float(v) for v in bbox)
        cy = (by0 + by1) / 2
        cx = (bx0 + bx1) / 2
        if by0 >= caption_top - 2 and by1 <= caption_bottom + 2:
            continue
        if text.startswith(("Fig.", "Figure", "图 ")):
            continue
        if len(text) > 60:
            continue
        # Figure labels normally sit just under (or just above) the drawing.
        below_label = y1 - 2 <= cy <= y1 + 18
        above_label = y0 - 14 <= cy <= y0 - 2 and len(text) <= 12
        if (below_label or above_label) and x0 - 55 <= cx <= x1 + 55:
            pieces.append(pymupdf.Rect(bx0, by0, bx1, by1))
            x0, y0, x1, y1 = min(x0, bx0), min(y0, by0), max(x1, bx1), max(y1, by1)

    rect = pymupdf.Rect(x0 - 7, y0 - 7, x1 + 7, y1 + 7)
    if group.y1 <= caption_top:
        rect.y1 = min(rect.y1, caption_top - 2)
    else:
        rect.y0 = max(rect.y0, caption_bottom + 2)
    rect &= page.rect
    if rect.width < 24 or rect.height < 24:
        return None
    return rect


def extract_figures_for_page(
    pdf_path: Path,
    page_no: int,
    captions: list[str],
    output_dir: Path,
    *,
    dpi: int = 200,
) -> list[FigureCrop]:
    """Crop figures associated with caption blocks on one PDF page."""
    if not captions:
        return []
    try:
        import pymupdf
    except ImportError:
        return []

    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return []
    try:
        if page_no < 1 or page_no > len(doc):
            return []
        page = doc[page_no - 1]
        drawing_groups = _groups(_drawing_rects(page))
        text_boxes = _text_boxes(page)
        output_dir.mkdir(parents=True, exist_ok=True)
        crops: list[FigureCrop] = []
        for index, caption in enumerate(captions):
            anchor = _caption_rect(page, caption, index)
            if anchor is None:
                continue
            rect = _figure_rect(page, caption, anchor, drawing_groups, text_boxes, index)
            if rect is None:
                continue
            scale = max(1.0, dpi / 72.0)
            pix = page.get_pixmap(
                clip=rect,
                matrix=pymupdf.Matrix(scale, scale),
                alpha=False,
                colorspace=pymupdf.csRGB,
            )
            filename = f"fig_p{page_no:03d}_{index + 1}.png"
            pix.save(str(output_dir / filename))
            crops.append(
                FigureCrop(
                    caption=caption,
                    filename=filename,
                    width=pix.width,
                    height=pix.height,
                    bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                )
            )
        return crops
    finally:
        doc.close()
