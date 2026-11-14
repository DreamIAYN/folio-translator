"""Folio 译坊 — FastAPI application.

The web layer owns upload, accounts, job orchestration and reading; all model
work is delegated to mini-harness through ``HarnessClient``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .config import settings
from .db import Database, new_id, utcnow
from .djvu import DjVuError, convert_to_pdf, djvu_available
from .exporter import EXPORT_FILES, EXPORT_MIME, export_all, html_to_pdf
from .harness import HarnessClient
from .library import ReferenceLibrary
from .parser import ParseError, build_segments, parse_document, probe_pdf
from .service import TranslationService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

db = Database(settings.database_path)
service = TranslationService(settings, db)
library = ReferenceLibrary(settings)
for directory in (settings.upload_root, settings.export_root, settings.asset_root):
    directory.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    auth.ensure_demo_user(db, settings)
    db.cleanup_sessions()
    service.start()
    logger.info("Folio ready at http://%s:%s", settings.host, settings.port)
    yield
    service.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class ImportBookIn(BaseModel):
    name: str
    title: str = ""
    source_lang: str = settings.default_source_lang
    target_lang: str = settings.default_target_lang
    domain: str = "auto"
    style: str = "publish"
    model: str = ""
    review: bool = False
    glossary: bool = True
    thinking: bool = False
    custom_prompt: str = ""
    ocr_mode: str = "auto"


class RetryIn(BaseModel):
    mode: Literal["resume", "fresh"] = "resume"
    model: str = ""
    review: bool | None = None
    glossary: bool | None = None
    thinking: bool | None = None


class PromptIn(BaseModel):
    prompt: str = Field(default="", max_length=4000)
    thinking: bool | None = None


class GlossaryTermIn(BaseModel):
    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=500)


class GlossaryPatchIn(BaseModel):
    target: str | None = Field(default=None, min_length=1, max_length=300)
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_DOMAINS = {
    "auto",
    "math",
    "science",
    "cs",
    "medicine",
    "law",
    "history",
    "philosophy",
    "literature",
    "business",
    "social",
    "general",
}
ALLOWED_STYLES = {"publish", "academic", "literary", "faithful", "concise"}
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")


def current_user(request: Request) -> dict:
    return auth.require_user(db, request)


def _safe_text(value: str, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())[:limit]


def _norm_lang(value: str, fallback: str = "auto") -> str:
    value = (value or "").strip()
    if value == "auto" or LANG_RE.match(value):
        return value
    return fallback


def _norm_domain(value: str) -> str:
    return value if value in ALLOWED_DOMAINS else "auto"


def _norm_style(value: str) -> str:
    return value if value in ALLOWED_STYLES else "publish"


def _session_payload(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}


def _analysis(doc: dict) -> dict:
    try:
        value = json.loads(doc.get("analysis_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _metadata(doc: dict) -> dict:
    try:
        value = json.loads(doc.get("metadata_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _job_public(job: dict | None) -> dict | None:
    if not job:
        return None
    keep = {
        "id",
        "status",
        "stage",
        "progress",
        "total_segments",
        "translated_segments",
        "reviewed_segments",
        "model",
        "review_enabled",
        "glossary_enabled",
        "thinking_enabled",
        "tokens_prompt",
        "tokens_completion",
        "tokens_total",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    }
    return {key: job.get(key) for key in keep}


def _doc_summary(doc: dict, job: dict | None = None) -> dict:
    analysis = _analysis(doc)
    metadata = _metadata(doc)
    job = job or db.latest_job(doc["id"])
    return {
        "id": doc["id"],
        "title": doc["title"],
        "translated_title": analysis.get("title_translation") or "",
        "author": doc["author"] or analysis.get("author_translation") or "",
        "source_lang": doc["source_lang"],
        "target_lang": doc["target_lang"],
        "domain": doc["domain"],
        "style": doc["style"],
        "filename": doc["filename"],
        "ext": metadata.get("source_format") or doc["ext"],
        "size_bytes": doc["size_bytes"],
        "page_count": doc["page_count"],
        "char_count": doc["char_count"],
        "word_count": doc["word_count"],
        "segment_count": doc["segment_count"],
        "custom_prompt": doc.get("custom_prompt", ""),
        "status": doc["status"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
        "job": _job_public(job),
        "job_status": (job or {}).get("status", "none"),
        "progress": (job or {}).get("progress", 0),
        "tokens_total": doc.get("tokens_total", (job or {}).get("tokens_total", 0)),
        "error": (job or {}).get("error", "") or doc.get("error", ""),
    }


def _ocr_progress(doc: dict) -> dict:
    done = int(
        db.scalar(
            "SELECT COUNT(*) FROM ocr_pages WHERE document_id=? AND status='done'",
            (doc["id"],),
        )
        or 0
    )
    failed = int(
        db.scalar(
            "SELECT COUNT(*) FROM ocr_pages WHERE document_id=? AND status='failed'",
            (doc["id"],),
        )
        or 0
    )
    return {"done": done, "failed": failed, "total": int(doc.get("page_count") or 0)}


def _doc_detail(doc: dict) -> dict:
    segments = db.list_segments(doc["id"])
    translatable = [s for s in segments if s["translatable"]]
    translated = [s for s in translatable if s.get("translated_text")]
    payload = _doc_summary(doc)
    total_tokens = int(
        db.scalar(
            "SELECT COALESCE(SUM(tokens_total),0) FROM jobs WHERE document_id=?",
            (doc["id"],),
        )
        or 0
    )
    payload.update(
        {
            "tokens_total": total_tokens,
            "analysis": _analysis(doc),
            "metadata": _metadata(doc),
            "glossary": db.list_glossary(doc["id"]),
            "chapters": db.chapters(doc["id"]),
            "ocr_progress": _ocr_progress(doc),
            "stats": {
                "segments": len(segments),
                "translatable": len(translatable),
                "translated": len(translated),
                "source_chars": sum(len(s["source_text"]) for s in translatable),
                "translated_chars": sum(len(s["translated_text"]) for s in translated),
            },
            "exports": [
                {"key": key, "filename": filename}
                for key, filename in EXPORT_FILES.items()
                if (settings.export_root / doc["id"] / filename).is_file()
                or (key == "pdf" and doc["status"] == "completed")
            ],
        }
    )
    return payload


def _assert_doc(doc_id: str, user: dict) -> dict:
    doc = db.get_document(doc_id, user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


def _cleanup(path: Path) -> None:
    try:
        resolved = path.resolve()
        roots = {
            settings.upload_root.resolve(),
            settings.export_root.resolve(),
            settings.asset_root.resolve(),
        }
        if resolved in roots or not any(root in resolved.parents for root in roots):
            return
        if resolved.exists():
            shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        logger.warning("could not clean up %s", path, exc_info=True)


def _validate_options(
    *, source_lang: str, target_lang: str, domain: str, style: str, model: str
) -> dict[str, str]:
    source = _norm_lang(source_lang, "auto")
    target = _norm_lang(target_lang, settings.default_target_lang)
    if target == "auto":
        target = settings.default_target_lang
    if source == target:
        raise HTTPException(status_code=422, detail="源语言与目标语言不能相同")
    return {
        "source_lang": source,
        "target_lang": target,
        "domain": _norm_domain(domain),
        "style": _norm_style(style),
        "model": _safe_text(model, 100) or settings.harness_model,
    }


def _create_job(
    doc_id: str,
    user_id: str,
    model: str,
    review: bool,
    glossary: bool,
    thinking: bool = False,
) -> str:
    total = int(
        db.scalar(
            "SELECT COUNT(*) FROM segments WHERE document_id=? AND translatable=1", (doc_id,)
        )
        or 0
    )
    completed = int(
        db.scalar(
            """SELECT COUNT(*) FROM segments WHERE document_id=? AND translatable=1
               AND translated_text<>''""",
            (doc_id,),
        )
        or 0
    )
    return db.create_job(
        document_id=doc_id,
        user_id=user_id,
        status="queued",
        stage="queued",
        progress=0,
        total_segments=total,
        translated_segments=completed,
        model=model,
        review_enabled=1 if review else 0,
        glossary_enabled=1 if glossary else 0,
        thinking_enabled=1 if thinking else 0,
    )


def _ingest_saved(
    *,
    doc_id: str,
    dest: Path,
    original_filename: str,
    size: int,
    user: dict,
    options: dict[str, str],
    title_hint: str,
    review: bool,
    glossary: bool,
    thinking: bool = False,
    custom_prompt: str = "",
    ocr_mode: str = "auto",
) -> dict:
    ocr_mode = ocr_mode if ocr_mode in {"auto", "text", "vision"} else "auto"
    source_format = dest.suffix.lower()
    if source_format == ".djvu":
        try:
            dest = convert_to_pdf(dest, dest.parent / "source.pdf", settings)
        except DjVuError as exc:
            _cleanup(dest.parent)
            raise HTTPException(
                status_code=422,
                detail={"message": exc.message, "hint": exc.hint},
            ) from exc
        # DjVu scans have no reliable text layer; always use vision OCR.
        ocr_mode = "vision"
    is_pdf = dest.suffix.lower() == ".pdf"
    vision_ocr = False
    parsed = None
    if is_pdf and ocr_mode == "vision":
        try:
            parsed = probe_pdf(dest, title_hint)
            vision_ocr = True
        except ParseError as exc:
            _cleanup(dest.parent)
            raise HTTPException(
                status_code=422, detail={"message": exc.message, "hint": exc.hint}
            ) from exc
    else:
        try:
            parsed = parse_document(dest, title_hint)
        except ParseError as exc:
            if is_pdf and ocr_mode == "auto":
                try:
                    parsed = probe_pdf(dest, title_hint)
                    vision_ocr = True
                except ParseError:
                    _cleanup(dest.parent)
                    raise HTTPException(
                        status_code=422,
                        detail={"message": exc.message, "hint": exc.hint},
                    ) from exc
            else:
                _cleanup(dest.parent)
                raise HTTPException(
                    status_code=422, detail={"message": exc.message, "hint": exc.hint}
                ) from exc
        except Exception as exc:
            _cleanup(dest.parent)
            raise HTTPException(status_code=422, detail=f"文档解析失败：{exc}") from exc

    segments = [] if vision_ocr else build_segments(parsed)
    if not segments and not vision_ocr:
        _cleanup(dest.parent)
        raise HTTPException(status_code=422, detail="文档中未找到可翻译内容")
    title = _safe_text(title_hint or parsed.title or Path(original_filename).stem, 300)
    metadata = dict(parsed.metadata)
    metadata["page_count"] = parsed.page_count
    metadata["ocr_mode"] = ocr_mode
    metadata["source_format"] = source_format
    metadata["source_filename"] = Path(original_filename).name
    if vision_ocr:
        metadata["vision_ocr"] = True
        metadata["needs_vision"] = True
    db.create_document(
        id=doc_id,
        user_id=user["id"],
        title=title or "未命名文档",
        author=_safe_text(parsed.author, 200),
        source_lang=options["source_lang"],
        target_lang=options["target_lang"],
        domain=options["domain"],
        style=options["style"],
        filename=Path(original_filename).name[:300],
        ext=dest.suffix.lower(),
        size_bytes=size,
        page_count=parsed.page_count,
        char_count=parsed.char_count,
        word_count=parsed.word_count,
        segment_count=len(segments),
        status="ready",
        custom_prompt=_safe_text(custom_prompt, 4000),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    if segments:
        db.add_segments(doc_id, segments)
    job_id = _create_job(
        doc_id, user["id"], options["model"], review, glossary, thinking
    )
    service.enqueue(job_id)
    return _doc_detail(_assert_doc(doc_id, user))

def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\u3400-\u9fff.\- ]+", "_", name, flags=re.UNICODE)
    return name.strip(" ._")[:160] or "book"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
@app.post("/api/auth/register")
def register(body: RegisterIn, response: Response) -> dict:
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="请输入有效的邮箱地址")
    if db.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
    user = db.create_user(
        email=email,
        name=_safe_text(body.name, 80) or email.split("@")[0],
        password_hash=auth.hash_password(body.password),
    )
    auth.create_session(db, user["id"], settings, response)
    return {"user": _session_payload(user)}


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response) -> dict:
    user = db.get_user_by_email(body.email.strip().lower())
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    auth.create_session(db, user["id"], settings, response)
    return {"user": _session_payload(user)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    auth.destroy_session(db, request, response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    user = auth.user_from_request(db, request)
    return {"user": _session_payload(user) if user else None}


# ---------------------------------------------------------------------------
# system / dashboard
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    harness_ok = False
    harness_error = ""
    try:
        with HarnessClient(settings) as client:
            client.health()
        harness_ok = True
    except Exception as exc:
        harness_error = str(exc)
    return {
        "ok": True,
        "harness": {
            "ok": harness_ok,
            "base_url": settings.harness_base_url,
            "error": harness_error,
            "configured": bool(settings.harness_api_key),
        },
    }


@app.get("/api/models")
def models() -> dict:
    items = []
    default = settings.harness_model
    try:
        with HarnessClient(settings) as client:
            data = client.models()
        # The platform's configured model wins over the harness-wide default;
        # this lets an operator switch away from a provider with exhausted credit.
        for item in data.get("items", []):
            items.append(
                {
                    "id": item.get("id"),
                    "label": item.get("label") or item.get("id"),
                    "context_window": item.get("context_window"),
                    "max_output_tokens": item.get("max_output_tokens"),
                }
            )
    except Exception:
        items = [{"id": model, "label": model} for model in settings.harness_models]
    return {"default": default, "items": items}


@app.get("/api/stats")
def stats(request: Request) -> dict:
    user = current_user(request)
    return {
        "stats": db.user_stats(user["id"]),
        "queue": service.stats(),
        "library_count": len(library.list_entries()),
    }


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------
@app.get("/api/documents")
def list_documents(request: Request) -> dict:
    user = current_user(request)
    docs = db.list_documents(user["id"])
    return {"items": [_doc_summary(doc) for doc in docs]}


@app.post("/api/documents", status_code=201)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    source_lang: str = Form(settings.default_source_lang),
    target_lang: str = Form(settings.default_target_lang),
    domain: str = Form("auto"),
    style: str = Form("publish"),
    model: str = Form(""),
    review: bool = Form(False),
    glossary: bool = Form(True),
    thinking: bool = Form(False),
    custom_prompt: str = Form(""),
    ocr_mode: str = Form("auto"),
) -> dict:
    user = current_user(request)
    options = _validate_options(
        source_lang=source_lang,
        target_lang=target_lang,
        domain=domain,
        style=style,
        model=model,
    )
    original = Path(file.filename or "document.txt").name
    ext = Path(original).suffix.lower()
    if ext not in {
        ".pdf",
        ".docx",
        ".epub",
        ".txt",
        ".md",
        ".markdown",
        ".html",
        ".htm",
        ".djvu",
    }:
        raise HTTPException(
            status_code=422,
            detail="不支持的格式。请上传 PDF、DOCX、EPUB、TXT、Markdown、HTML 或带文字层的 DJVU。",
        )
    doc_id = new_id("doc")
    dest_dir = settings.upload_root / doc_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"source{ext}"
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {settings.max_upload_mb} MB 上限",
                    )
                out.write(chunk)
    except HTTPException:
        _cleanup(dest_dir)
        raise
    except OSError as exc:
        _cleanup(dest_dir)
        raise HTTPException(status_code=500, detail=f"保存上传文件失败：{exc}") from exc
    if size == 0:
        _cleanup(dest_dir)
        raise HTTPException(status_code=422, detail="文件为空")
    return _ingest_saved(
        doc_id=doc_id,
        dest=dest,
        original_filename=original,
        size=size,
        user=user,
        options=options,
        title_hint=_safe_text(title, 300),
        review=review,
        glossary=glossary,
        thinking=thinking,
        custom_prompt=custom_prompt,
        ocr_mode=ocr_mode,
    )


@app.get("/api/library")
def get_library(request: Request) -> dict:
    current_user(request)
    path = settings.reference_books_dir
    return {
        "items": library.list_entries(),
        "directory_label": path.name,
        "directory_exists": path.is_dir(),
        "djvu_ready": djvu_available(settings),
    }


@app.post("/api/library/import", status_code=201)
def import_library_book(body: ImportBookIn, request: Request) -> dict:
    user = current_user(request)
    options = _validate_options(
        source_lang=body.source_lang,
        target_lang=body.target_lang,
        domain=body.domain,
        style=body.style,
        model=body.model,
    )
    try:
        source = library.resolve(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    doc_id = new_id("doc")
    dest_dir = settings.upload_root / doc_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"source{source.suffix.lower()}"
    try:
        shutil.copy2(source, dest)
    except OSError as exc:
        _cleanup(dest_dir)
        raise HTTPException(status_code=500, detail=f"复制书目文件失败：{exc}") from exc
    size = dest.stat().st_size
    return _ingest_saved(
        doc_id=doc_id,
        dest=dest,
        original_filename=source.name,
        size=size,
        user=user,
        options=options,
        title_hint=_safe_text(body.title, 300) or library.resolve(body.name).stem,
        review=body.review,
        glossary=body.glossary,
        thinking=body.thinking,
        custom_prompt=body.custom_prompt,
        ocr_mode=body.ocr_mode,
    )


@app.get("/api/documents/{doc_id}")
def document_detail(doc_id: str, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    return _doc_detail(doc)


@app.get("/api/documents/{doc_id}/job")
def document_job(doc_id: str, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    return {
        "job": _job_public(db.latest_job(doc["id"])),
        "document_status": doc["status"],
        "ocr_progress": _ocr_progress(doc),
    }


@app.get("/api/documents/{doc_id}/segments")
def document_segments(
    doc_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=100),
    q: str = Query("", max_length=200),
    chapter: str = Query("", max_length=300),
) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    rows, total = db.segment_page(
        doc["id"], offset=offset, limit=limit, query=q.strip(), chapter=chapter
    )
    items = [
        {
            "id": row["id"],
            "idx": row["idx"],
            "chapter": row["chapter"],
            "block_type": row["block_type"],
            "level": row["level"],
            "page": row["page"],
            "source_text": row["source_text"],
            "translated_text": row["translated_text"],
            "status": row["status"],
            "translatable": bool(row["translatable"]),
            "asset_path": row.get("asset_path", ""),
            "asset_width": row.get("asset_width", 0),
            "asset_height": row.get("asset_height", 0),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }


@app.get("/api/documents/{doc_id}/glossary")
def get_glossary(doc_id: str, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    return {"items": db.list_glossary(doc["id"])}


@app.post("/api/documents/{doc_id}/glossary", status_code=201)
def add_glossary_term(doc_id: str, body: GlossaryTermIn, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    db.upsert_glossary(
        doc["id"],
        [{"source": body.source, "target": body.target, "note": body.note, "confidence": 1.0}],
    )
    return {"items": db.list_glossary(doc["id"])}


@app.patch("/api/documents/{doc_id}/glossary/{term_id}")
def patch_glossary_term(
    doc_id: str, term_id: int, body: GlossaryPatchIn, request: Request
) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    fields: dict[str, Any] = {}
    if body.target is not None:
        fields["target_term"] = body.target
    if body.note is not None:
        fields["note"] = body.note
    if not db.update_glossary_term(term_id, doc["id"], **fields):
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"items": db.list_glossary(doc["id"])}


@app.delete("/api/documents/{doc_id}/glossary/{term_id}")
def delete_glossary_term(doc_id: str, term_id: int, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    if not db.delete_glossary_term(term_id, doc["id"]):
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"items": db.list_glossary(doc["id"])}


@app.patch("/api/documents/{doc_id}/prompt")
def update_prompt(doc_id: str, body: PromptIn, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    prompt = _safe_text(body.prompt, 4000)
    db.execute(
        "UPDATE documents SET custom_prompt=?, updated_at=? WHERE id=?",
        (prompt, utcnow(), doc["id"]),
    )
    job = db.latest_job(doc["id"])
    if body.thinking is not None and job and job["status"] not in {"queued", "running"}:
        db.update_job(job["id"], thinking_enabled=1 if body.thinking else 0)
    return {
        "ok": True,
        "custom_prompt": prompt,
        "thinking_enabled": (
            db.latest_job(doc["id"]) or {}
        ).get("thinking_enabled", 1),
        "document": _doc_detail(_assert_doc(doc_id, user)),
    }


@app.post("/api/documents/{doc_id}/cancel")
def cancel_document(doc_id: str, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    job = db.latest_job(doc["id"])
    if not job or job["status"] not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="当前没有可取消的任务")
    db.update_job(
        job["id"],
        status="cancelled",
        stage="cancelled",
        error="用户已取消",
        finished_at=utcnow(),
    )
    db.execute(
        "UPDATE documents SET status='paused', updated_at=? WHERE id=?",
        (utcnow(), doc["id"]),
    )
    return {"ok": True, "document": _doc_detail(_assert_doc(doc_id, user))}


@app.post("/api/documents/{doc_id}/retry")
def retry_document(doc_id: str, body: RetryIn, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    latest = db.latest_job(doc["id"])
    if latest and latest["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务正在运行，请先等待或取消")
    if body.mode == "fresh":
        db.clear_translations(doc["id"])
    model = _safe_text(body.model, 100) or (
        latest["model"] if latest else settings.harness_model
    )
    review = (
        body.review
        if body.review is not None
        else bool(latest["review_enabled"]) if latest else False
    )
    glossary = (
        body.glossary
        if body.glossary is not None
        else bool(latest["glossary_enabled"]) if latest else True
    )
    thinking = (
        body.thinking
        if body.thinking is not None
        else bool(latest["thinking_enabled"]) if latest else False
    )
    job_id = _create_job(doc["id"], user["id"], model, review, glossary, thinking)
    db.execute(
        "UPDATE documents SET status='processing', error='', updated_at=? WHERE id=?",
        (utcnow(), doc["id"]),
    )
    service.enqueue(job_id)
    return {"ok": True, "job": _job_public(db.get_job(job_id)), "job_id": job_id}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, request: Request) -> dict:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    job = db.latest_job(doc["id"])
    if job and job["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务正在运行，请先取消再删除")
    if not db.delete_document(doc["id"], user["id"]):
        raise HTTPException(status_code=404, detail="文档不存在")
    _cleanup(settings.upload_root / doc["id"])
    _cleanup(settings.export_root / doc["id"])
    _cleanup(settings.asset_root / doc["id"])
    return {"ok": True}


@app.get("/api/documents/{doc_id}/assets/{filename}")
def document_asset(doc_id: str, filename: str, request: Request) -> FileResponse:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    if not filename or "/" in filename or "\\" in filename or filename.startswith(".."):
        raise HTTPException(status_code=404, detail="图片不存在")
    path = settings.asset_root / doc["id"] / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    media = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
    return FileResponse(path, media_type=media)


@app.get("/api/documents/{doc_id}/download")
def download_document(
    doc_id: str, request: Request, fmt: str = Query("docx")
) -> FileResponse:
    user = current_user(request)
    doc = _assert_doc(doc_id, user)
    if doc["status"] != "completed":
        raise HTTPException(status_code=409, detail="翻译尚未完成，暂时无法导出")
    if fmt not in EXPORT_FILES:
        raise HTTPException(status_code=422, detail="不支持的导出格式")
    output_dir = settings.export_root / doc["id"]
    path = output_dir / EXPORT_FILES[fmt]
    if not path.is_file():
        segments = db.list_segments(doc["id"])
        glossary = db.list_glossary(doc["id"])
        export_all(
            doc,
            segments,
            glossary,
            _analysis(doc),
            output_dir,
            asset_root=settings.asset_root,
        )
        if fmt == "pdf":
            try:
                html_to_pdf(output_dir / "book.html", path)
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=500, detail="导出文件生成失败")
    title = _analysis(doc).get("title_translation") or doc["title"]
    suffix = path.suffix
    filename = f"{_safe_filename(str(title))}{suffix}"
    return FileResponse(
        path,
        media_type=EXPORT_MIME.get(fmt, "application/octet-stream"),
        filename=filename,
    )


# ---------------------------------------------------------------------------
# SPA
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(detail, status_code=exc.status_code)
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
