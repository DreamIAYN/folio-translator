"""DjVu conversion.

DjVu is usually a scan container: even when a text layer exists, the reliable
path for formula-heavy books is to render the pages and run vision OCR.  We use
DjVuLibre's ``ddjvu`` to convert a DJVU file into an image-only PDF, then reuse
the normal PDF/vision pipeline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Settings


class DjVuError(Exception):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def djvu_available(settings: Settings) -> bool:
    return bool(settings.djvu_bin and Path(settings.djvu_bin).is_file())


def convert_to_pdf(source: Path, destination: Path, settings: Settings) -> Path:
    binary = Path(settings.djvu_bin)
    if not binary.is_file():
        raise DjVuError(
            "服务器未安装 DjVu 转换器 ddjvu。",
            hint="安装 djvulibre（提供 ddjvu），或设置 DJVU_BIN 指向可执行文件。",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [str(binary), "-format=pdf", str(source), str(destination)],
            capture_output=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DjVuError(f"DjVu 转换失败：{exc}") from exc
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise DjVuError(f"DjVu 转换失败：{detail or 'ddjvu 没有生成 PDF'}")
    return destination
