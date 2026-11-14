"""Run the Folio server: ``folio`` or ``python -m app``."""

from __future__ import annotations

import argparse

import uvicorn

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Folio 译坊 — AI book translation platform")
    parser.add_argument("--host", help="bind host (default: APP_HOST)")
    parser.add_argument("--port", type=int, help="bind port (default: APP_PORT)")
    parser.add_argument("--reload", action="store_true", help="enable uvicorn auto-reload")
    args = parser.parse_args()
    settings = Settings.from_env()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level="info",
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
