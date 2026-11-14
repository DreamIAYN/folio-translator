"""Background execution for translation jobs.

One process-local thread pool.  Each job is claimed atomically in SQLite, so
double-submission is harmless.  On restart, running jobs are marked interrupted
and queued jobs are resumed.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import Settings
from .db import Database
from .pipeline import TranslationPipeline

logger = logging.getLogger(__name__)


class TranslationService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.pipeline = TranslationPipeline(settings, db)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="folio-job")

    def start(self) -> None:
        if not self.settings.skip_recovery:
            self.db.mark_interrupted_jobs()
        for job in self.db.list_queued_jobs():
            self.enqueue(job["id"])

    def enqueue(self, job_id: str) -> None:
        self._pool.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        if not self.db.claim_job(job_id):
            return
        logger.info("starting translation job %s", job_id)
        try:
            self.pipeline.run_job(job_id)
        except Exception:
            # The pipeline persists its own failures; this is the last safety net.
            logger.exception("unhandled job failure %s", job_id)
            self.db.update_job(
                job_id,
                status="failed",
                stage="failed",
                error="内部错误，请查看服务日志。",
            )

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.db.scalar("SELECT COUNT(*) FROM jobs WHERE status='running'") or 0,
            "queued": self.db.scalar("SELECT COUNT(*) FROM jobs WHERE status='queued'") or 0,
        }
