"""Application configuration.

The app deliberately keeps configuration boring: environment variables with a
small ``.env`` loader, no settings framework.  This makes the service portable
and keeps the mini-harness endpoint as the only external dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = (
    "deepseek-v4.1-flash",
    "deepseek-flash",
    "deepseek-v4-pro",
    "glm-5.3-flash",
    "glm-5.3",
    "minimax-m3",
)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def _env_path(name: str, default: str) -> Path:
    value = Path(_env(name, default)).expanduser()
    return value if value.is_absolute() else (ROOT / value).resolve()


@dataclass(slots=True)
class Settings:
    app_name: str = "Folio 译坊"
    host: str = "127.0.0.1"
    port: int = 8090
    database_path: Path = ROOT / "data" / "folio.db"
    upload_root: Path = ROOT / "data" / "uploads"
    export_root: Path = ROOT / "data" / "exports"
    asset_root: Path = ROOT / "data" / "assets"
    static_dir: Path = ROOT / "app" / "static"
    skip_recovery: bool = False
    djvu_bin: Path = ROOT / "vendor" / "djvulibre" / "bin" / "ddjvu"
    reference_books_dir: Path = Path.home() / "books" / "ocr1"

    harness_base_url: str = "http://127.0.0.1:8000"
    harness_fast_url: str = ""
    harness_api_key: str = ""
    harness_model: str = "deepseek-v4.1-flash"
    harness_timeout_s: float = 900.0
    harness_models: tuple[str, ...] = DEFAULT_MODELS

    max_upload_mb: int = 100
    translation_group_chars: int = 4200
    translation_concurrency: int = 6
    vision_ocr_concurrency: int = 4
    vision_ocr_model: str = "deepseek-v4.1-flash"
    vision_ocr_dpi: int = 180
    vision_ocr_max_pages: int = 2000
    vision_ocr_max_tokens: int = 24000
    translation_max_retries: int = 2
    max_source_segments: int = 20000
    default_target_lang: str = "zh-CN"
    default_source_lang: str = "auto"
    demo_email: str = "demo@folio.local"
    demo_password: str = "folio2026"
    session_days: int = 30

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(ROOT / ".env")
        models = tuple(
            m.strip() for m in _env("HARNESS_MODELS").split(",") if m.strip()
        )
        return cls(
            app_name=_env("APP_NAME", "Folio 译坊"),
            host=_env("APP_HOST", "127.0.0.1"),
            port=_env_int("APP_PORT", 8090),
            database_path=_env_path("APP_DB_PATH", "data/folio.db"),
            upload_root=_env_path("APP_UPLOAD_ROOT", "data/uploads"),
            export_root=_env_path("APP_EXPORT_ROOT", "data/exports"),
            asset_root=_env_path("APP_ASSET_ROOT", "data/assets"),
            static_dir=_env_path("APP_STATIC_DIR", "app/static"),
            skip_recovery=_env_bool("APP_SKIP_RECOVERY", False),
            djvu_bin=_env_path("DJVU_BIN", "vendor/djvulibre/bin/ddjvu"),
            reference_books_dir=_env_path(
                "REFERENCE_BOOKS_DIR", "~/books/ocr1"
            ),
            harness_base_url=_env("HARNESS_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            harness_fast_url=_env("HARNESS_FAST_URL", "").rstrip("/"),
            harness_api_key=_env("HARNESS_API_KEY"),
            harness_model=_env("HARNESS_MODEL", "deepseek-v4.1-flash"),
            harness_timeout_s=float(_env("HARNESS_TIMEOUT_SECONDS", "900") or 900),
            harness_models=models or DEFAULT_MODELS,
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 100),
            translation_group_chars=_env_int("TRANSLATION_GROUP_CHARS", 4200),
            translation_concurrency=max(1, _env_int("TRANSLATION_CONCURRENCY", 6)),
            vision_ocr_concurrency=max(1, _env_int("VISION_OCR_CONCURRENCY", 4)),
            vision_ocr_model=_env("VISION_OCR_MODEL", "deepseek-v4.1-flash"),
            vision_ocr_dpi=max(96, _env_int("VISION_OCR_DPI", 180)),
            vision_ocr_max_pages=max(1, _env_int("VISION_OCR_MAX_PAGES", 2000)),
            vision_ocr_max_tokens=max(4000, _env_int("VISION_OCR_MAX_TOKENS", 24000)),
            translation_max_retries=max(0, _env_int("TRANSLATION_MAX_RETRIES", 2)),
            max_source_segments=max(50, _env_int("MAX_SOURCE_SEGMENTS", 20000)),
            default_target_lang=_env("DEFAULT_TARGET_LANG", "zh-CN"),
            default_source_lang=_env("DEFAULT_SOURCE_LANG", "auto"),
            demo_email=_env("DEMO_EMAIL", "demo@folio.local").lower(),
            demo_password=_env("DEMO_PASSWORD", "folio2026"),
            session_days=max(1, _env_int("SESSION_DAYS", 30)),
        )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings.from_env()
