"""Curated server-side reference shelf.

The shelf lets the platform import books that already live on the host without a
browser round-trip.  Only direct children of the configured directory can be
imported; the client never supplies a filesystem path, only an exact file name.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .djvu import djvu_available
from .parser import SUPPORTED_EXTENSIONS

SUPPORTED = {".pdf", ".docx", ".epub", ".txt", ".md", ".markdown", ".html", ".htm", ".djvu"}


def _clean_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^GTM\d+[._\s-]*", "", stem, flags=re.IGNORECASE)
    stem = re.sub(
        r"\((?:GTM[^)]*|ISBN[^)]*|\d+\s*dpi|\d+s|T|MDgt|MDat|600dpi[^)]*)\)",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    stem = re.sub(r"_?MD(?:gt|at)_?", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\.(djvu|pdf|epub|docx|txt|md)$", "", stem, flags=re.IGNORECASE)
    stem = stem.replace(".", " ").replace("_", " ")
    stem = re.sub(r"\s*[,;]\s*", ", ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .,-")
    return stem[:240] or filename


def _guess_tags(filename: str) -> list[str]:
    lower = filename.lower()
    tags = []
    if any(k in lower for k in ("algebra", "topology", "graph", "number theory", "geometry", "analysis")):
        tags.append("数学")
    if any(k in lower for k in ("gtm", "springer")):
        tags.append("GTM")
    if lower.endswith(".djvu"):
        tags.append("DjVu")
    return tags


class ReferenceLibrary:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    def list_entries(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._cache and now - self._cache[0] < 30:
            return self._cache[1]
        directory = self.settings.reference_books_dir
        entries: list[dict[str, Any]] = []
        if directory.is_dir():
            for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                ext = path.suffix.lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue
                supported = ext in SUPPORTED and not (
                    ext == ".djvu" and not djvu_available(self.settings)
                )
                note = ""
                if ext == ".djvu" and not supported:
                    note = "服务器未安装 ddjvu，无法转换"
                elif ext == ".djvu":
                    note = "自动转 PDF 后进行 AI 视觉识别"
                entries.append(
                    {
                        "name": path.name,
                        "title": _clean_title(path.name),
                        "format": ext.lstrip("."),
                        "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                        "supported": supported,
                        "note": note,
                        "tags": _guess_tags(path.name),
                    }
                )
        self._cache = (now, entries)
        return entries

    def resolve(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("无效的书目文件名")
        directory = self.settings.reference_books_dir.resolve()
        path = (directory / name).resolve()
        if path.parent != directory or not path.is_file():
            raise ValueError("书目文件不存在")
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError("不支持该文件格式")
        return path

    def copy_to(self, name: str, destination: Path) -> Path:
        source = self.resolve(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination
