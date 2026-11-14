from __future__ import annotations

import zipfile
from pathlib import Path

import pymupdf
from docx import Document
from ebooklib import epub

from app.auth import hash_password, verify_password
from app.db import Database
from app.exporter import EXPORT_FILES, export_all
from app.figures import extract_figures_for_page
from app.library import _clean_title
from app.parser import build_segments, parse_document, probe_pdf
from app.pipeline import group_segments
from app.vision_ocr import blocks_to_parsed, normalize_blocks


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_txt_chapter_parsing(tmp_path: Path) -> None:
    path = tmp_path / "book.txt"
    path.write_text(
        "CHAPTER 1\n\nThe first paragraph.\n\nA second paragraph.\n\nCHAPTER 2\n\nAnother.\n",
        encoding="utf-8",
    )
    doc = parse_document(path, "Fallback")
    assert doc.title == "Fallback"
    assert [b.kind for b in doc.blocks].count("heading") == 2
    assert any(b.chapter == "CHAPTER 1" for b in doc.blocks)
    assert doc.char_count > 20


def test_markdown_parsing_keeps_code_untranslated(tmp_path: Path) -> None:
    path = tmp_path / "book.md"
    path.write_text(
        "# Graph Theory\n\nA graph is a pair.\n\n- vertices\n- edges\n\n```python\nx = 1\n```\n",
        encoding="utf-8",
    )
    doc = parse_document(path, "Graph")
    assert doc.title == "Graph Theory"
    kinds = [b.kind for b in doc.blocks]
    assert "heading" in kinds and "list" in kinds and "code" in kinds
    segments = build_segments(doc)
    code = next(s for s in segments if s["block_type"] == "code")
    assert code["translatable"] is False
    assert code["status"] == "skipped"


def test_docx_parsing(tmp_path: Path) -> None:
    path = tmp_path / "book.docx"
    doc = Document()
    doc.add_heading("Chapter One", level=1)
    doc.add_paragraph("A paragraph of text.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "term"
    table.rows[0].cells[1].text = "definition"
    doc.save(path)
    parsed = parse_document(path, "DOCX")
    assert parsed.title
    assert any(b.kind == "heading" for b in parsed.blocks)
    assert any(b.kind == "table" and "definition" in b.text for b in parsed.blocks)


def test_epub_parsing(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    book = epub.EpubBook()
    book.set_identifier("test-epub")
    book.set_title("A Small Book")
    book.add_author("A. Writer")
    chapter = epub.EpubHtml(title="Chapter 1", file_name="c1.xhtml", lang="en")
    chapter.content = "<h1>Chapter 1</h1><p>Hello world.</p><ul><li>One</li></ul>"
    book.add_item(chapter)
    book.toc = (chapter,)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]
    epub.write_epub(str(path), book)
    parsed = parse_document(path, "EPUB")
    assert parsed.title == "A Small Book"
    assert parsed.author == "A. Writer"
    texts = " ".join(b.text for b in parsed.blocks)
    assert "Hello world" in texts and "One" in texts


def test_pdf_parsing(tmp_path: Path) -> None:
    path = tmp_path / "book.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "1  Introduction", fontsize=22)
    page.insert_text((72, 120), "This is the first paragraph of the book.", fontsize=11)
    pdf.save(path)
    pdf.close()
    parsed = parse_document(path, "PDF Book")
    assert parsed.page_count == 1
    assert parsed.blocks
    assert "first paragraph" in " ".join(b.text for b in parsed.blocks)


def test_long_text_splitting() -> None:
    from app.parser import split_long_text

    text = ("A sentence with some length. " * 400).strip()
    pieces = split_long_text(text, 500)
    assert len(pieces) > 1
    assert all(len(piece) <= 800 for piece in pieces)


def test_grouping_respects_chapters_and_budget() -> None:
    segments = [
        {"id": i, "source_text": "x" * 2000, "chapter": "A" if i < 2 else "B", "translatable": 1}
        for i in range(4)
    ]
    groups = group_segments(segments, max_chars=3000, max_items=10)
    assert len(groups) == 4
    assert all(len(g) == 1 for g in groups)


def test_exporter_writes_all_formats(tmp_path: Path) -> None:
    doc = {
        "id": "doc_test",
        "title": "Graph Theory",
        "author": "Reinhard Diestel",
        "target_lang": "zh-CN",
    }
    segments = [
        {
            "id": 1,
            "idx": 1,
            "chapter": "1",
            "block_type": "heading",
            "level": 1,
            "page": 1,
            "source_text": "Graph Theory",
            "translated_text": "图论",
            "translatable": 1,
        },
        {
            "id": 2,
            "idx": 2,
            "chapter": "1",
            "block_type": "paragraph",
            "level": 0,
            "page": 1,
            "source_text": "A graph is a pair of sets.",
            "translated_text": "图是一个集合对。",
            "translatable": 1,
        },
        {
            "id": 3,
            "idx": 3,
            "chapter": "1",
            "block_type": "code",
            "level": 0,
            "page": 1,
            "source_text": "x = 1",
            "translated_text": "x = 1",
            "translatable": 0,
        },
    ]
    glossary = [{"source_term": "graph", "target_term": "图", "note": "数学对象"}]
    analysis = {
        "title_translation": "图论",
        "author_translation": "Reinhard Diestel",
        "summary": "一本图论教材。",
        "translation_strategy": "保留公式与编号。",
    }
    out = tmp_path / "out"
    manifest = export_all(doc, segments, glossary, analysis, out)
    assert set(manifest["files"]) == set(EXPORT_FILES.values()) - {"book.pdf"}
    for name in manifest["files"]:
        assert (out / name).is_file() and (out / name).stat().st_size > 0
    with zipfile.ZipFile(out / "translated.epub") as archive:
        assert "mimetype" in archive.namelist()
    assert "图论" in (out / "translated.md").read_text(encoding="utf-8")


def test_figure_crop_from_pdf(tmp_path: Path) -> None:
    path = tmp_path / "figure.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=440, height=700)
    # Simple vector figure above a caption.
    page.draw_rect(pymupdf.Rect(100, 80, 200, 180), color=(0, 0, 0), width=1)
    page.draw_line((100, 80), (200, 180), color=(0, 0, 0))
    page.insert_text((110, 205), "G", fontsize=12)
    page.insert_text((90, 240), "Fig. 1.1. Test figure", fontsize=11)
    pdf.save(path)
    pdf.close()
    out = tmp_path / "assets"
    crops = extract_figures_for_page(
        path, 1, ["Fig. 1.1. Test figure"], out
    )
    assert len(crops) == 1
    crop = crops[0]
    assert (out / crop.filename).is_file()
    assert crop.width > 20 and crop.height > 20
    assert crop.bbox[3] <= 238  # caption itself is not included


def test_vision_block_normalization_and_latex() -> None:
    raw = {
        "blocks": [
            {"type": "heading", "level": 2, "text": "1.1 Graphs"},
            {"type": "formula", "text": r"G \cup G' - G \cap G'"},
            {"type": "nonsense", "text": "plain text"},
            {"type": "paragraph", "text": ""},
        ]
    }
    blocks = normalize_blocks(raw, page_no=3)
    assert len(blocks) == 3
    assert blocks[0]["type"] == "heading" and blocks[0]["level"] == 2
    assert blocks[1]["type"] == "formula" and blocks[1]["text"].startswith("$$")
    assert blocks[2]["type"] == "paragraph"
    parsed = blocks_to_parsed(
        blocks, title="Graph Theory", author="Diestel", page_count=4
    )
    assert parsed.blocks[0].kind == "heading"
    assert parsed.blocks[1].kind == "formula"
    assert all(b.page == 3 for b in parsed.blocks)


def test_formula_export_includes_katex(tmp_path: Path) -> None:
    doc = {"id": "doc_f", "title": "Graph Theory", "author": "Diestel", "target_lang": "zh-CN"}
    segments = [
        {
            "id": 1,
            "idx": 1,
            "chapter": "1",
            "block_type": "formula",
            "level": 0,
            "page": 1,
            "source_text": r"$$G \cup G'$$",
            "translated_text": r"$$G \cup G'$$",
            "translatable": 1,
        }
    ]
    out = tmp_path / "formula"
    export_all(doc, segments, [], {"title_translation": "图论"}, out)
    page = (out / "book.html").read_text(encoding="utf-8")
    assert "renderMathInElement" in page
    assert "data:font/woff2" in page
    assert "$$G \\cup G&#x27;$$" in page
    assert "$$G \\cup G'$$" in (out / "translated.md").read_text(encoding="utf-8")
    from docx import Document as DocxDocument

    docx = DocxDocument(out / "translated.docx")
    xml = "".join(p._p.xml for p in docx.paragraphs)
    assert "oMath" in xml and "∪" in xml


def test_ocr_cache_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "ocr.db")
    user = db.create_user(email="o@example.com", name="O", password_hash="x")
    doc_id = db.create_document(
        user_id=user["id"],
        title="O",
        author="",
        source_lang="en",
        target_lang="zh-CN",
        domain="math",
        style="publish",
        filename="o.pdf",
        ext=".pdf",
        size_bytes=1,
        page_count=1,
        char_count=0,
        word_count=0,
        segment_count=0,
        status="ready",
        metadata_json="{}",
    )
    db.upsert_ocr_page(doc_id, 1, blocks=[{"type": "formula", "text": "$$x$$"}])
    page = db.ocr_page(doc_id, 1)
    assert page and page["status"] == "done"
    assert "$$x$$" in page["blocks_json"]


def test_pdf_probe_without_text(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(path)
    pdf.close()
    parsed = probe_pdf(path, "Blank")
    assert parsed.page_count == 1
    assert parsed.metadata["needs_vision"] is True
    assert parsed.blocks == []


def test_database_crud(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    user = db.create_user(email="a@example.com", name="A", password_hash="x")
    doc_id = db.create_document(
        user_id=user["id"],
        title="Test",
        author="",
        source_lang="en",
        target_lang="zh-CN",
        domain="auto",
        style="publish",
        filename="test.txt",
        ext=".txt",
        size_bytes=10,
        page_count=0,
        char_count=10,
        word_count=2,
        segment_count=2,
        status="ready",
        metadata_json="{}",
    )
    db.add_segments(
        doc_id,
        [
            {
                "idx": 0,
                "chapter": "C1",
                "block_type": "heading",
                "source_text": "Hello",
                "translatable": True,
            },
            {
                "idx": 1,
                "chapter": "C1",
                "block_type": "paragraph",
                "source_text": "World",
                "translatable": True,
            },
        ],
    )
    rows = db.list_segments(doc_id)
    assert len(rows) == 2
    db.save_translation(rows[0]["id"], "你好")
    page, total = db.segment_page(doc_id, offset=0, limit=10, query="你好")
    assert total == 1 and page[0]["translated_text"] == "你好"
    db.upsert_glossary(doc_id, [{"source": "graph", "target": "图"}])
    assert db.list_glossary(doc_id)[0]["target_term"] == "图"
    assert db.user_stats(user["id"])["documents"] == 1


def test_library_title_cleanup() -> None:
    assert _clean_title("GTM173.Graph.Theory,.Reinhard.Diestel.（图论）.pdf") == (
        "Graph Theory, Reinhard Diestel （图论）"
    )
    assert "ISBN" not in _clean_title("GTM1.Book.(ISBN.123)(600dpi)(T)(300s)_MDgt_.djvu")
