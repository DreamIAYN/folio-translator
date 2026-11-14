"""SQLite persistence layer.

A single process, a single SQLite database, and a re-entrant lock around writes
is enough for this product: translation jobs are I/O-bound and the database is
only touched at segment boundaries.  WAL keeps readers (the web UI polling
progress) from blocking the translation workers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    author        TEXT NOT NULL DEFAULT '',
    source_lang   TEXT NOT NULL DEFAULT 'auto',
    target_lang   TEXT NOT NULL DEFAULT 'zh-CN',
    domain        TEXT NOT NULL DEFAULT 'auto',
    style         TEXT NOT NULL DEFAULT 'publish',
    filename      TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    page_count    INTEGER NOT NULL DEFAULT 0,
    char_count    INTEGER NOT NULL DEFAULT 0,
    word_count    INTEGER NOT NULL DEFAULT 0,
    segment_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ready',
    analysis_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error         TEXT NOT NULL DEFAULT '',
    custom_prompt TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'queued',
    stage               TEXT NOT NULL DEFAULT 'queued',
    progress            REAL NOT NULL DEFAULT 0,
    total_segments      INTEGER NOT NULL DEFAULT 0,
    translated_segments INTEGER NOT NULL DEFAULT 0,
    reviewed_segments   INTEGER NOT NULL DEFAULT 0,
    model               TEXT NOT NULL DEFAULT '',
    review_enabled      INTEGER NOT NULL DEFAULT 1,
    glossary_enabled    INTEGER NOT NULL DEFAULT 1,
    thinking_enabled    INTEGER NOT NULL DEFAULT 1,
    tokens_prompt       INTEGER NOT NULL DEFAULT 0,
    tokens_completion   INTEGER NOT NULL DEFAULT 0,
    tokens_total        INTEGER NOT NULL DEFAULT 0,
    error               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    started_at          TEXT NOT NULL DEFAULT '',
    finished_at         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    chapter         TEXT NOT NULL DEFAULT '',
    block_type      TEXT NOT NULL DEFAULT 'paragraph',
    level           INTEGER NOT NULL DEFAULT 0,
    page            INTEGER NOT NULL DEFAULT 0,
    source_text     TEXT NOT NULL,
    translated_text TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    tokens          INTEGER NOT NULL DEFAULT 0,
    translatable    INTEGER NOT NULL DEFAULT 1,
    error           TEXT NOT NULL DEFAULT '',
    asset_path      TEXT NOT NULL DEFAULT '',
    asset_width     INTEGER NOT NULL DEFAULT 0,
    asset_height    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_segments_doc ON segments(document_id, idx);

CREATE TABLE IF NOT EXISTS glossary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0.8,
    UNIQUE(document_id, source_term)
);
CREATE INDEX IF NOT EXISTS idx_glossary_doc ON glossary(document_id);

CREATE TABLE IF NOT EXISTS ocr_pages (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'done',
    blocks_json TEXT NOT NULL DEFAULT '[]',
    tokens      INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY(document_id, page_no)
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{secrets.token_hex(8)}"


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(segments)").fetchall()
        }
        additions = {
            "asset_path": "TEXT NOT NULL DEFAULT ''",
            "asset_width": "INTEGER NOT NULL DEFAULT 0",
            "asset_height": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, ddl in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE segments ADD COLUMN {name} {ddl}")
        doc_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "custom_prompt" not in doc_columns:
            self._conn.execute(
                "ALTER TABLE documents ADD COLUMN custom_prompt TEXT NOT NULL DEFAULT ''"
            )
        job_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "thinking_enabled" not in job_columns:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN thinking_enabled INTEGER NOT NULL DEFAULT 1"
            )

    # -- low-level ---------------------------------------------------------
    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    # -- users / sessions --------------------------------------------------
    def create_user(self, *, email: str, name: str, password_hash: str, role: str = "user") -> dict:
        user_id = new_id("usr")
        now = utcnow()
        self.execute(
            "INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, email.lower(), name, password_hash, role, now),
        )
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> dict | None:
        return self.query_one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_user_by_email(self, email: str) -> dict | None:
        return self.query_one("SELECT * FROM users WHERE email=?", (email.lower(),))

    def create_session(self, *, token_hash: str, user_id: str, days: int) -> None:
        now = datetime.now(UTC)
        self.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (token_hash, user_id, now.isoformat(), (now + timedelta(days=days)).isoformat()),
        )

    def get_session_user(self, token_hash: str) -> dict | None:
        row = self.query_one(
            """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>?""",
            (token_hash, utcnow()),
        )
        return row

    def delete_session(self, token_hash: str) -> None:
        self.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    # -- documents / jobs --------------------------------------------------
    def create_document(self, **fields: Any) -> str:
        doc_id = fields.pop("id", None) or new_id("doc")
        now = utcnow()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        fields["id"] = doc_id
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO documents({columns}) VALUES({placeholders})",
            list(fields.values()),
        )
        return doc_id

    def get_document(self, doc_id: str, user_id: str | None = None) -> dict | None:
        if user_id:
            return self.query_one(
                "SELECT * FROM documents WHERE id=? AND user_id=?", (doc_id, user_id)
            )
        return self.query_one("SELECT * FROM documents WHERE id=?", (doc_id,))

    def list_documents(self, user_id: str, limit: int = 100) -> list[dict]:
        return self.query_all(
            """SELECT d.*, j.status AS job_status, j.stage AS job_stage,
                      j.progress AS job_progress, j.error AS job_error,
                      COALESCE((SELECT SUM(jt.tokens_total) FROM jobs jt
                                WHERE jt.document_id=d.id), 0) AS tokens_total
               FROM documents d
               LEFT JOIN jobs j ON j.id = (
                   SELECT id FROM jobs WHERE document_id=d.id ORDER BY created_at DESC LIMIT 1
               )
               WHERE d.user_id=? ORDER BY d.created_at DESC LIMIT ?""",
            (user_id, limit),
        )

    def create_job(self, **fields: Any) -> str:
        job_id = fields.pop("id", None) or new_id("job")
        fields.setdefault("created_at", utcnow())
        fields["id"] = job_id
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO jobs({columns}) VALUES({placeholders})",
            list(fields.values()),
        )
        return job_id

    def get_job(self, job_id: str) -> dict | None:
        return self.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def latest_job(self, doc_id: str) -> dict | None:
        return self.query_one(
            "SELECT * FROM jobs WHERE document_id=? ORDER BY created_at DESC LIMIT 1",
            (doc_id,),
        )

    def list_queued_jobs(self) -> list[dict]:
        return self.query_all("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at")

    def claim_job(self, job_id: str) -> bool:
        now = utcnow()
        cur = self.execute(
            """UPDATE jobs SET status='running', started_at=?, stage='extract', progress=1
               WHERE id=? AND status='queued'""",
            (now, job_id),
        )
        return cur.rowcount > 0

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key}=?" for key in fields)
        self.execute(f"UPDATE jobs SET {columns} WHERE id=?", [*fields.values(), job_id])

    def add_job_usage(self, job_id: str, prompt: int, completion: int) -> None:
        total = int(prompt) + int(completion)
        self.execute(
            """UPDATE jobs SET tokens_prompt=tokens_prompt+?, tokens_completion=tokens_completion+?,
               tokens_total=tokens_total+? WHERE id=?""",
            (prompt, completion, total, job_id),
        )

    # -- segments ----------------------------------------------------------
    def add_segments(self, doc_id: str, segments: list[dict]) -> None:
        rows = [
            (
                doc_id,
                seg["idx"],
                seg.get("chapter", ""),
                seg.get("block_type", "paragraph"),
                int(seg.get("level", 0)),
                int(seg.get("page", 0)),
                seg["source_text"],
                seg.get("translated_text", ""),
                seg.get("status", "pending"),
                int(seg.get("tokens", 0)),
                1 if seg.get("translatable", True) else 0,
                seg.get("error", ""),
                seg.get("asset_path", ""),
                int(seg.get("asset_width", 0)),
                int(seg.get("asset_height", 0)),
            )
            for seg in segments
        ]
        self.executemany(
            """INSERT INTO segments(document_id,idx,chapter,block_type,level,page,source_text,
               translated_text,status,tokens,translatable,error,asset_path,asset_width,
               asset_height)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    def list_segments(self, doc_id: str) -> list[dict]:
        return self.query_all(
            "SELECT * FROM segments WHERE document_id=? ORDER BY idx", (doc_id,)
        )

    def segment_page(
        self,
        doc_id: str,
        *,
        offset: int,
        limit: int,
        query: str = "",
        chapter: str = "",
    ) -> tuple[list[dict], int]:
        conditions = ["document_id=?"]
        params: list[Any] = [doc_id]
        if query:
            conditions.append("(source_text LIKE ? OR translated_text LIKE ?)")
            q = f"%{query}%"
            params.extend([q, q])
        if chapter:
            conditions.append("chapter=?")
            params.append(chapter)
        where = " AND ".join(conditions)
        total = int(
            self.scalar(f"SELECT COUNT(*) FROM segments WHERE {where}", params) or 0
        )
        rows = self.query_all(
            f"SELECT * FROM segments WHERE {where} ORDER BY idx LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return rows, total

    def chapters(self, doc_id: str) -> list[str]:
        rows = self.query_all(
            """SELECT chapter, MIN(idx) AS first_idx FROM segments
               WHERE document_id=? AND chapter<>'' GROUP BY chapter ORDER BY first_idx""",
            (doc_id,),
        )
        return [r["chapter"] for r in rows]

    def update_segment_asset(
        self, segment_id: int, asset_path: str, width: int = 0, height: int = 0
    ) -> None:
        self.execute(
            "UPDATE segments SET asset_path=?, asset_width=?, asset_height=? WHERE id=?",
            (asset_path, width, height, segment_id),
        )

    def save_translation(self, segment_id: int, text: str, tokens: int = 0) -> None:
        self.execute(
            "UPDATE segments SET translated_text=?, status='done', tokens=?, error='' WHERE id=?",
            (text, tokens, segment_id),
        )

    def mark_segment_failed(self, segment_id: int, error: str) -> None:
        self.execute(
            "UPDATE segments SET status='failed', error=? WHERE id=?", (error[:800], segment_id)
        )

    def replace_segments(self, doc_id: str, segments: list[dict]) -> None:
        with self.transaction():
            self.execute("DELETE FROM segments WHERE document_id=?", (doc_id,))
            self.add_segments(doc_id, segments)

    def clear_translations(self, doc_id: str) -> None:
        self.execute(
            """UPDATE segments SET translated_text='', status=CASE WHEN translatable=1
               THEN 'pending' ELSE 'skipped' END, tokens=0, error='' WHERE document_id=?""",
            (doc_id,),
        )

    def upsert_ocr_page(
        self,
        doc_id: str,
        page_no: int,
        *,
        blocks: list[dict] | None = None,
        status: str = "done",
        tokens: int = 0,
        error: str = "",
    ) -> None:
        self.execute(
            """INSERT INTO ocr_pages(document_id,page_no,status,blocks_json,tokens,error,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(document_id,page_no) DO UPDATE SET
                 status=excluded.status, blocks_json=excluded.blocks_json,
                 tokens=excluded.tokens, error=excluded.error, updated_at=excluded.updated_at""",
            (
                doc_id,
                page_no,
                status,
                json.dumps(blocks or [], ensure_ascii=False),
                tokens,
                error[:1000],
                utcnow(),
            ),
        )

    def list_ocr_pages(self, doc_id: str) -> list[dict]:
        return self.query_all(
            "SELECT * FROM ocr_pages WHERE document_id=? ORDER BY page_no", (doc_id,)
        )

    def ocr_page(self, doc_id: str, page_no: int) -> dict | None:
        return self.query_one(
            "SELECT * FROM ocr_pages WHERE document_id=? AND page_no=?", (doc_id, page_no)
        )

    def clear_ocr_pages(self, doc_id: str) -> None:
        self.execute("DELETE FROM ocr_pages WHERE document_id=?", (doc_id,))

    def reset_to_queued(self, job_id: str) -> None:
        self.execute(
            """UPDATE jobs SET status='queued', stage='queued', progress=0, error='',
               translated_segments=0, reviewed_segments=0, started_at='', finished_at=''
               WHERE id=?""",
            (job_id,),
        )

    # -- glossary ----------------------------------------------------------
    def upsert_glossary(
        self, doc_id: str, items: list[dict], *, replace: bool = False
    ) -> None:
        if replace:
            self.execute("DELETE FROM glossary WHERE document_id=?", (doc_id,))
        for item in items:
            source = str(item.get("source") or item.get("source_term") or "").strip()
            target = str(item.get("target") or item.get("target_term") or "").strip()
            if not source or not target:
                continue
            self.execute(
                """INSERT INTO glossary(document_id,source_term,target_term,note,confidence)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(document_id,source_term) DO UPDATE SET
                     target_term=excluded.target_term, note=excluded.note,
                     confidence=excluded.confidence""",
                (
                    doc_id,
                    source[:300],
                    target[:300],
                    str(item.get("note") or "")[:500],
                    float(item.get("confidence") or 0.8),
                ),
            )

    def list_glossary(self, doc_id: str) -> list[dict]:
        return self.query_all(
            "SELECT * FROM glossary WHERE document_id=? ORDER BY id", (doc_id,)
        )

    def update_glossary_term(self, term_id: int, doc_id: str, **fields: Any) -> bool:
        allowed = {k: v for k, v in fields.items() if k in {"target_term", "note"}}
        if not allowed:
            return False
        columns = ", ".join(f"{key}=?" for key in allowed)
        cur = self.execute(
            f"UPDATE glossary SET {columns} WHERE id=? AND document_id=?",
            [*allowed.values(), term_id, doc_id],
        )
        return cur.rowcount > 0

    def delete_glossary_term(self, term_id: int, doc_id: str) -> bool:
        cur = self.execute(
            "DELETE FROM glossary WHERE id=? AND document_id=?", (term_id, doc_id)
        )
        return cur.rowcount > 0

    # -- stats / housekeeping ---------------------------------------------
    def user_stats(self, user_id: str) -> dict:
        docs = self.scalar("SELECT COUNT(*) FROM documents WHERE user_id=?", (user_id,)) or 0
        chars = (
            self.scalar(
                """SELECT COALESCE(SUM(char_count),0) FROM documents WHERE user_id=?""",
                (user_id,),
            )
            or 0
        )
        translated = (
            self.scalar(
                """SELECT COALESCE(SUM(LENGTH(translated_text)),0) FROM segments s
                   JOIN documents d ON d.id=s.document_id WHERE d.user_id=?""",
                (user_id,),
            )
            or 0
        )
        tokens = (
            self.scalar(
                "SELECT COALESCE(SUM(tokens_total),0) FROM jobs WHERE user_id=?", (user_id,)
            )
            or 0
        )
        completed = (
            self.scalar(
                """SELECT COUNT(*) FROM documents WHERE user_id=? AND status='completed'""",
                (user_id,),
            )
            or 0
        )
        return {
            "documents": int(docs),
            "completed": int(completed),
            "source_chars": int(chars),
            "translated_chars": int(translated),
            "tokens": int(tokens),
        }

    def delete_document(self, doc_id: str, user_id: str) -> bool:
        cur = self.execute(
            "DELETE FROM documents WHERE id=? AND user_id=?", (doc_id, user_id)
        )
        return cur.rowcount > 0

    def cleanup_sessions(self) -> None:
        self.execute("DELETE FROM sessions WHERE expires_at<?", (utcnow(),))

    def mark_interrupted_jobs(self) -> None:
        self.execute(
            """UPDATE jobs SET status='failed', stage='interrupted',
               error='服务重启导致任务中断，可点击“重新翻译”继续。'
               WHERE status='running'"""
        )
        self.execute(
            """UPDATE documents SET status='failed'
               WHERE id IN (
                   SELECT j.document_id FROM jobs j
                   WHERE j.id = (
                       SELECT j2.id FROM jobs j2 WHERE j2.document_id=j.document_id
                       ORDER BY j2.created_at DESC LIMIT 1
                   ) AND j.status='failed'
               )"""
        )

    def json_field(self, record: dict, key: str) -> dict:
        try:
            value = json.loads(record.get(key) or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}
