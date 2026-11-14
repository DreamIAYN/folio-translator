"""Password hashing and cookie-backed sessions.

Sessions are opaque random tokens.  Only their SHA-256 digests are stored, and
the cookie is HttpOnly + SameSite=Lax.  This is intentionally small: the app
runs next to mini-harness on one host, not as a public multi-tenant SaaS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, Response

from .config import Settings
from .db import Database

SESSION_COOKIE = "folio_session"
_PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Database, user_id: str, settings: Settings, response: Response) -> str:
    token = secrets.token_urlsafe(40)
    db.create_session(token_hash=_token_hash(token), user_id=user_id, days=settings.session_days)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return token


def destroy_session(db: Database, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(_token_hash(token))
    response.delete_cookie(SESSION_COOKIE, path="/")


def user_from_request(db: Database, request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return db.get_session_user(_token_hash(token))


def require_user(db: Database, request: Request) -> dict:
    user = user_from_request(db, request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def ensure_demo_user(db: Database, settings: Settings) -> dict:
    existing = db.get_user_by_email(settings.demo_email)
    if existing:
        return existing
    return db.create_user(
        email=settings.demo_email,
        name="演示读者",
        password_hash=hash_password(settings.demo_password),
        role="demo",
    )


def session_expiry(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()
