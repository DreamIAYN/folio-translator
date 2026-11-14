"""AI translation pipeline.

The pipeline is intentionally explicit and resumable:

1. analyse the book once and extract a glossary;
2. translate independent segment groups in parallel;
3. optionally run a second-pass reviewer;
4. export final artefacts.

Every AI call goes through mini-harness.  Each segment is persisted immediately,
so a failed or stopped job can be resumed without paying for completed work.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import prompts
from .config import Settings
from .db import Database
from .exporter import export_all
from .figures import extract_figures_for_page
from .harness import HarnessClient, HarnessError
from .vision_ocr import blocks_to_parsed, ocr_page

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


def group_segments(
    segments: list[dict[str, Any]], max_chars: int = 4200, max_items: int = 10
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    chapter = None
    for seg in segments:
        if not seg.get("translatable"):
            continue
        text = str(seg.get("source_text") or "")
        this_chapter = seg.get("chapter") or ""
        if current and (
            size + len(text) > max_chars
            or len(current) >= max_items
            or (chapter is not None and this_chapter != chapter)
        ):
            groups.append(current)
            current = []
            size = 0
        current.append(seg)
        size += len(text)
        chapter = this_chapter
    if current:
        groups.append(current)
    return groups


def _sample_text(segments: Iterable[dict[str, Any]], limit: int = 18_000) -> str:
    parts: list[str] = []
    total = 0
    for seg in segments:
        text = str(seg.get("source_text") or "")
        if not text:
            continue
        prefix = ""
        if seg.get("block_type") == "heading":
            prefix = "\n## "
        chunk = prefix + text + "\n"
        if total + len(chunk) > limit:
            remaining = limit - total
            if remaining > 200:
                parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)


def _usage_total(usage: dict[str, Any]) -> int:
    return int(usage.get("total_tokens") or 0)


class TranslationPipeline:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._cancel_lock = threading.Lock()

    # -- public entry point ------------------------------------------------
    def run_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        doc = self.db.get_document(job["document_id"])
        if not doc:
            return
        try:
            normal = HarnessClient(self.settings)
            fast = None
            client = normal
            fast_url = self.settings.harness_fast_url
            if (
                not bool(job.get("thinking_enabled", 1))
                and fast_url
                and fast_url != self.settings.harness_base_url
            ):
                fast = HarnessClient(self.settings, base_url=fast_url)
                try:
                    fast.health()
                    client = fast
                    logger.info("job %s using fast no-thinking harness", job_id)
                except HarnessError as exc:
                    logger.warning("fast harness unavailable, falling back: %s", exc.message)
                    fast.close()
                    fast = None
            try:
                self._run(client, job, doc)
            finally:
                normal.close()
                if fast is not None:
                    fast.close()
        except JobCancelled:
            logger.info("translation job %s cancelled", job_id)
            self.db.update_job(job_id, status="cancelled", finished_at=_now())
            self.db.execute(
                "UPDATE documents SET status='paused', updated_at=? WHERE id=?",
                (_now(), doc["id"]),
            )
        except HarnessError as exc:
            logger.exception("harness error in job %s", job_id)
            message = exc.message
            if exc.code == "quota_exceeded":
                message = "mini-harness 额度不足，请管理员补充额度后重试。"
            self._fail(job_id, doc["id"], message)
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            self._fail(job_id, doc["id"], f"{type(exc).__name__}: {exc}")

    def _fail(self, job_id: str, doc_id: str, message: str) -> None:
        self.db.update_job(
            job_id,
            status="failed",
            stage="failed",
            error=message[:2000],
            finished_at=_now(),
        )
        self.db.execute(
            "UPDATE documents SET status='failed', error=?, updated_at=? WHERE id=?",
            (message[:1000], _now(), doc_id),
        )

    # -- main pipeline -----------------------------------------------------
    def _run(self, client: HarnessClient, job: dict[str, Any], doc: dict[str, Any]) -> None:
        job_id = job["id"]
        doc_id = doc["id"]
        self.db.execute(
            "UPDATE documents SET status='processing', error='', updated_at=? WHERE id=?",
            (_now(), doc_id),
        )
        segments = self.db.list_segments(doc_id)
        ocr_warning = ""
        ocr_ran = False
        if self._needs_vision(doc):
            ocr_warning = self._run_vision_ocr(client, job, doc)
            ocr_ran = True
            doc = self.db.get_document(doc_id) or doc
            segments = self.db.list_segments(doc_id)
            self.db.update_job(job_id, stage="analysis", progress=15)
        translatable = [s for s in segments if s["translatable"]]
        total = len(translatable)
        if not total:
            raise RuntimeError("文档没有可翻译的段落。")

        done_ids = {s["id"] for s in translatable if s.get("translated_text")}
        total_done = len(done_ids)
        self.db.update_job(
            job_id,
            total_segments=total,
            translated_segments=total_done,
            progress=15 if ocr_ran else 4,
            stage="analysis",
        )
        self.db.execute(
            "UPDATE documents SET status='processing', updated_at=? WHERE id=?",
            (_now(), doc_id),
        )

        self._attach_figures(job, doc, segments)
        self._check_cancelled(job_id)
        custom_prompt = self._current_prompt(doc_id)
        analysis = self._get_analysis(client, job, doc, segments, custom_prompt)
        self.db.update_job(
            job_id, progress=18 if ocr_ran else 12, stage="glossary"
        )
        glossary = self.db.list_glossary(doc_id)
        if not bool(job.get("glossary_enabled", 1)):
            glossary = []
        self._check_cancelled(job_id)

        pending = [s for s in translatable if s["id"] not in done_ids]
        groups = group_segments(
            pending,
            max_chars=self.settings.translation_group_chars,
            max_items=10,
        )
        translate_start = 20 if ocr_ran else 15
        self.db.update_job(
            job_id,
            stage="translation",
            progress=translate_start if total else 90,
            total_segments=total,
        )
        done_lock = threading.Lock()
        started = time.monotonic()

        for batch_start in range(0, len(groups), self.settings.translation_concurrency):
            self._check_cancelled(job_id)
            batch = groups[batch_start : batch_start + self.settings.translation_concurrency]
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {
                    pool.submit(
                        self._translate_group,
                        client,
                        job,
                        doc,
                        analysis,
                        glossary,
                        group,
                        segments,
                    ): group
                    for group in batch
                }
                for future in as_completed(futures):
                    group = futures[future]
                    try:
                        result = future.result()
                    except JobCancelled:
                        continue
                    except HarnessError:
                        raise
                    except Exception as exc:
                        logger.warning("group failed, isolating segments: %s", exc)
                        result = self._translate_individually(
                            client, job, doc, analysis, glossary, group
                        )
                    with done_lock:
                        total_done += result["done"]
                        prompt_tokens = result["prompt_tokens"]
                        completion_tokens = result["completion_tokens"]
                        if prompt_tokens or completion_tokens:
                            self.db.add_job_usage(
                                job_id, prompt_tokens, completion_tokens
                            )
                        progress = translate_start + (65 if ocr_ran else 70) * (
                            total_done / max(1, total)
                        )
                        self.db.update_job(
                            job_id,
                            translated_segments=total_done,
                            progress=min(86, progress),
                        )
            logger.info(
                "job %s: %s/%s segments in %.1fs",
                job_id,
                total_done,
                total,
                time.monotonic() - started,
            )

        # A group can fail on malformed JSON while its neighbours succeed. Give
        # every still-missing segment one isolated retry before closing the job.
        missing = [
            s
            for s in self.db.list_segments(doc_id)
            if s["translatable"] and not s.get("translated_text")
        ]
        if missing:
            logger.info("job %s: retrying %s missing segments", job_id, len(missing))
            result = self._translate_individually(
                client, job, doc, analysis, glossary, missing
            )
            total_done += result["done"]
            self.db.add_job_usage(
                job_id, result["prompt_tokens"], result["completion_tokens"]
            )
            self.db.update_job(
                job_id, translated_segments=total_done, progress=86
            )

        if bool(job.get("review_enabled", 1)):
            self._check_cancelled(job_id)
            segments = self.db.list_segments(doc_id)
            self.db.update_job(job_id, stage="review", progress=88)
            reviewed = self._review(client, job, doc, analysis, glossary, segments)
            self.db.update_job(job_id, reviewed_segments=reviewed, progress=95)

        self._check_cancelled(job_id)
        self.db.update_job(job_id, stage="export", progress=97)
        segments = self.db.list_segments(doc_id)
        translated_total = sum(
            1 for s in segments if s["translatable"] and s.get("translated_text")
        )
        remaining = sum(1 for s in segments if s["translatable"] and not s.get("translated_text"))
        glossary = self.db.list_glossary(doc_id)
        export_dir = self.settings.export_root / doc_id
        export_all(
            doc,
            segments,
            glossary,
            self.db.json_field(doc, "analysis_json"),
            export_dir,
            asset_root=self.settings.asset_root,
        )
        warnings = []
        if ocr_warning:
            warnings.append(ocr_warning)
        if remaining:
            warnings.append(f"{remaining} 个段落在多次重试后仍未完成，可稍后在阅读器中检查。")
        warning = "；".join(warnings)
        self.db.update_job(
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            translated_segments=translated_total,
            finished_at=_now(),
            error=warning,
        )
        self.db.execute(
            "UPDATE documents SET status='completed', error='', updated_at=? WHERE id=?",
            (_now(), doc_id),
        )

    def _current_prompt(self, doc_id: str) -> str:
        value = self.db.scalar(
            "SELECT custom_prompt FROM documents WHERE id=?", (doc_id,)
        )
        return str(value or "").strip()[:4000]

    # -- figures -----------------------------------------------------------
    def _attach_figures(
        self, job: dict[str, Any], doc: dict[str, Any], segments: list[dict[str, Any]]
    ) -> None:
        if (doc.get("ext") or "").lower() != ".pdf":
            return
        try:
            metadata = json.loads(doc.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("figures_done"):
            return
        captions: dict[int, list[dict[str, Any]]] = {}
        for segment in segments:
            text = str(segment.get("source_text") or "").strip()
            if segment.get("block_type") == "paragraph" and re.match(
                r"^(?:fig(?:ure)?\.?|图|表)\s*\d", text, re.IGNORECASE
            ):
                segment["block_type"] = "caption"
                self.db.execute(
                    "UPDATE segments SET block_type='caption' WHERE id=?",
                    (segment["id"],),
                )
            if segment.get("block_type") == "caption" and not segment.get("asset_path"):
                captions.setdefault(int(segment.get("page") or 0), []).append(segment)
        if not captions:
            metadata["figures_done"] = True
            metadata["figures_count"] = 0
            self.db.execute(
                "UPDATE documents SET metadata_json=?, updated_at=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), _now(), doc["id"]),
            )
            return
        pdf_path = self.settings.upload_root / doc["id"] / f"source{doc['ext']}"
        if not pdf_path.is_file():
            return
        self.db.update_job(
            job["id"], stage="figures", progress=16 if metadata.get("vision_ocr") else 6
        )
        asset_dir = self.settings.asset_root / doc["id"]
        count = 0
        for page_no, page_captions in sorted(captions.items()):
            if page_no < 1:
                continue
            crops = extract_figures_for_page(
                pdf_path,
                page_no,
                [str(c.get("source_text") or "") for c in page_captions],
                asset_dir,
                dpi=200,
            )
            for crop, segment in zip(crops, page_captions):
                self.db.update_segment_asset(
                    segment["id"], crop.filename, crop.width, crop.height
                )
                segment["asset_path"] = crop.filename
                segment["asset_width"] = crop.width
                segment["asset_height"] = crop.height
                count += 1
            logger.info("cropped %s figures on page %s", len(crops), page_no)
        metadata["figures_done"] = True
        metadata["figures_count"] = count
        self.db.execute(
            "UPDATE documents SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), _now(), doc["id"]),
        )

    # -- vision OCR --------------------------------------------------------
    @staticmethod
    def _needs_vision(doc: dict[str, Any]) -> bool:
        if (doc.get("ext") or "").lower() != ".pdf":
            return False
        try:
            metadata = json.loads(doc.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return bool(metadata.get("vision_ocr") or metadata.get("needs_vision"))

    def _run_vision_ocr(
        self, client: HarnessClient, job: dict[str, Any], doc: dict[str, Any]
    ) -> str:
        doc_id = doc["id"]
        try:
            existing_meta = json.loads(doc.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            existing_meta = {}
        existing_segments = int(
            self.db.scalar("SELECT COUNT(*) FROM segments WHERE document_id=?", (doc_id,)) or 0
        )
        if existing_meta.get("ocr_done") and existing_segments:
            return ""
        pdf_path = self.settings.upload_root / doc_id / f"source{doc['ext']}"
        if not pdf_path.is_file():
            raise RuntimeError("找不到原始 PDF，无法进行视觉识别。")
        total_pages = int(doc.get("page_count") or 0)
        if total_pages <= 0:
            raise RuntimeError("PDF 页数无效，无法进行视觉识别。")
        if total_pages > self.settings.vision_ocr_max_pages:
            raise RuntimeError(
                f"PDF 共 {total_pages} 页，超过视觉识别上限 "
                f"{self.settings.vision_ocr_max_pages} 页。"
            )
        self.db.update_job(
            job["id"],
            stage="ocr",
            progress=5,
            total_segments=0,
            translated_segments=0,
        )
        # A page result is cached so a failed translation can resume OCR cheaply.
        # Pages are independent: run a bounded batch in parallel instead of
        # waiting for one vision request at a time.
        cached_pages = {
            int(row["page_no"])
            for row in self.db.list_ocr_pages(doc_id)
            if row["status"] == "done"
        }
        pending = [p for p in range(1, total_pages + 1) if p not in cached_pages]
        done_count = len(cached_pages)
        concurrency = max(1, self.settings.vision_ocr_concurrency)
        for start in range(0, len(pending), concurrency):
            self._check_cancelled(job["id"])
            batch = pending[start : start + concurrency]
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {
                    pool.submit(
                        ocr_page,
                        client,
                        self.settings,
                        pdf_path=pdf_path,
                        page_no=page_no,
                        total_pages=total_pages,
                        title=doc["title"],
                        model=self.settings.vision_ocr_model,
                    ): page_no
                    for page_no in batch
                }
                for future in as_completed(futures):
                    page_no = futures[future]
                    result = future.result()
                    if result.usage:
                        self.db.add_job_usage(
                            job["id"],
                            int(result.usage.get("prompt_tokens") or 0),
                            int(result.usage.get("completion_tokens") or 0),
                        )
                    if result.error:
                        self.db.upsert_ocr_page(
                            doc_id, page_no, status="failed", error=result.error
                        )
                        logger.warning("OCR page %s failed: %s", page_no, result.error)
                    else:
                        self.db.upsert_ocr_page(
                            doc_id,
                            page_no,
                            blocks=result.blocks,
                            status="done",
                            tokens=int(result.usage.get("total_tokens") or 0),
                        )
                    done_count += 1
                    progress = 5 + 9 * (done_count / total_pages)
                    self.db.update_job(job["id"], progress=progress)
                    logger.info(
                        "OCR page %s/%s done (batch concurrency %s)",
                        page_no,
                        total_pages,
                        concurrency,
                    )
        pages = self.db.list_ocr_pages(doc_id)
        blocks: list[dict[str, Any]] = []
        failed_pages = []
        for page in pages:
            if page["status"] != "done":
                failed_pages.append(page["page_no"])
                continue
            try:
                value = json.loads(page["blocks_json"] or "[]")
            except ValueError:
                value = []
            if isinstance(value, list):
                blocks.extend(item for item in value if isinstance(item, dict))
        if not blocks:
            raise RuntimeError(
                "AI 视觉识别没有返回可用文字。请检查模型是否支持图片，或改用文本层模式。"
            )
        parsed = blocks_to_parsed(
            blocks,
            title=doc["title"],
            author=doc["author"],
            page_count=total_pages,
        )
        from .parser import build_segments

        segments = build_segments(parsed)
        if not segments:
            raise RuntimeError("AI 视觉识别结果为空，未生成段落。")
        self.db.replace_segments(doc_id, segments)
        try:
            metadata = json.loads(doc.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        metadata.update(
            {
                "format": "pdf",
                "vision_ocr": True,
                "ocr_model": self.settings.vision_ocr_model,
                "ocr_dpi": self.settings.vision_ocr_dpi,
                "ocr_pages": total_pages,
                "ocr_failed_pages": failed_pages,
                "ocr_done": True,
            }
        )
        self.db.execute(
            """UPDATE documents SET page_count=?, char_count=?, word_count=?,
               segment_count=?, metadata_json=?, updated_at=? WHERE id=?""",
            (
                total_pages,
                parsed.char_count,
                parsed.word_count,
                len(segments),
                json.dumps(metadata, ensure_ascii=False),
                _now(),
                doc_id,
            ),
        )
        self.db.update_job(
            job["id"],
            total_segments=sum(1 for s in segments if s["translatable"]),
            translated_segments=0,
            progress=15,
            stage="analysis",
        )
        doc["page_count"] = total_pages
        doc["char_count"] = parsed.char_count
        doc["segment_count"] = len(segments)
        if failed_pages:
            return (
                f"{len(failed_pages)} 页 OCR 失败（页码 {', '.join(map(str, failed_pages[:8]))}"
                f"{'…' if len(failed_pages) > 8 else ''}），这些页可能缺少内容。"
            )
        return ""

    # -- analysis + glossary ----------------------------------------------
    def _get_analysis(
        self,
        client: HarnessClient,
        job: dict[str, Any],
        doc: dict[str, Any],
        segments: list[dict],
        custom_prompt: str = "",
    ) -> dict[str, Any]:
        existing = self.db.json_field(doc, "analysis_json")
        if existing.get("summary") or existing.get("translation_strategy"):
            return existing

        sample = _sample_text(segments)
        metadata = dict(self.db.json_field(doc, "metadata_json"))
        metadata["page_count"] = doc.get("page_count")
        prompt = prompts.analysis_prompt(
            title=doc["title"],
            author=doc["author"],
            source_lang=doc["source_lang"],
            target_lang=doc["target_lang"],
            domain=doc["domain"],
            style=doc["style"],
            metadata=metadata,
            sample=sample,
            custom_prompt=custom_prompt,
        )
        try:
            analysis, _raw, usage = client.run_json(
                prompt,
                system=prompts.ANALYST_SYSTEM,
                model=job["model"],
                max_tokens=10_000,
                retries=1,
            )
        except HarnessError as exc:
            if exc.status is not None:
                raise
            logger.warning("analysis JSON failed, using deterministic fallback: %s", exc)
            analysis = {
                "detected_language": doc["source_lang"],
                "genre": doc["domain"],
                "summary": "",
                "tone": "",
                "target_reader": "",
                "translation_strategy": "忠实、通顺；保留术语、公式与格式。",
                "glossary": [],
            }
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.db.add_job_usage(
            job["id"], usage["prompt_tokens"], usage["completion_tokens"]
        )

        detected = str(analysis.get("detected_language") or "").strip()
        if doc["source_lang"] in {"", "auto"} and detected:
            self.db.execute(
                "UPDATE documents SET source_lang=?, updated_at=? WHERE id=?",
                (detected[:20], _now(), doc["id"]),
            )
            doc["source_lang"] = detected[:20]
        analysis.setdefault("source_language", detected)

        terms = analysis.get("glossary") or analysis.get("key_terms") or []
        if isinstance(terms, list) and terms and bool(job.get("glossary_enabled", 1)):
            self.db.upsert_glossary(doc["id"], [t for t in terms if isinstance(t, dict)])
        if not bool(job.get("glossary_enabled", 1)):
            analysis["glossary"] = []

        translated_title = str(analysis.get("title_translation") or "").strip()
        if translated_title:
            analysis["title_translation"] = translated_title[:300]
        if doc["author"] in {"", "未知作者"} and analysis.get("author_translation"):
            doc["author"] = str(analysis["author_translation"])[:200]
            self.db.execute(
                "UPDATE documents SET author=? WHERE id=?", (doc["author"], doc["id"])
            )
        analysis["detected_language"] = detected or doc["source_lang"]
        self.db.execute(
            "UPDATE documents SET analysis_json=?, updated_at=? WHERE id=?",
            (_json(analysis), _now(), doc["id"]),
        )
        return analysis

    # -- translation -------------------------------------------------------
    def _context(self, group: list[dict], all_segments: list[dict]) -> tuple[str, str]:
        by_idx = {s["idx"]: s for s in all_segments}
        first, last = group[0]["idx"], group[-1]["idx"]
        before = by_idx.get(first - 1, {}).get("source_text", "")
        after = by_idx.get(last + 1, {}).get("source_text", "")
        return str(before)[:600], str(after)[:600]

    def _translate_group(
        self,
        client: HarnessClient,
        job: dict[str, Any],
        doc: dict[str, Any],
        analysis: dict[str, Any],
        glossary: list[dict[str, Any]],
        group: list[dict[str, Any]],
        all_segments: list[dict],
    ) -> dict[str, int]:
        self._check_cancelled(job["id"])
        before, after = self._context(group, all_segments)
        chapter = group[0].get("chapter") or ""
        custom_prompt = self._current_prompt(doc["id"])
        payload_segments = [
            {"id": s["id"], "type": s["block_type"], "text": s["source_text"]} for s in group
        ]
        prompt = prompts.translator_prompt(
            title=doc["title"],
            author=doc["author"],
            source_lang=doc["source_lang"],
            target_lang=doc["target_lang"],
            style=doc["style"],
            analysis=analysis,
            glossary=glossary,
            chapter=chapter,
            context_before=before,
            context_after=after,
            segments=payload_segments,
            custom_prompt=custom_prompt,
        )
        if len(prompt) > 30_000:
            # Keep the contract intact: trim glossary first, never source text.
            prompt = prompts.translator_prompt(
                title=doc["title"],
                author=doc["author"],
                source_lang=doc["source_lang"],
                target_lang=doc["target_lang"],
                style=doc["style"],
                analysis=analysis,
                glossary=glossary[:20],
                chapter=chapter,
                context_before=before[:250],
                context_after=after[:250],
                segments=payload_segments,
                custom_prompt=custom_prompt[:1000],
            )
        expected = {s["id"] for s in group}
        try:
            parsed, _raw, usage = client.run_json(
                prompt,
                system=prompts.TRANSLATOR_SYSTEM,
                model=job["model"],
                max_tokens=min(32_000, max(12_000, int(sum(len(s["source_text"]) for s in group) * 2))),
                retries=self.settings.translation_max_retries,
            )
            translations = parsed.get("translations")
            if not isinstance(translations, list):
                raise ValueError("missing translations array")
            by_id: dict[int, str] = {}
            for entry in translations:
                if not isinstance(entry, dict):
                    continue
                try:
                    seg_id = int(entry.get("id"))
                except (TypeError, ValueError):
                    continue
                text = entry.get("text")
                if isinstance(text, str) and text.strip():
                    by_id[seg_id] = text
            missing = expected - set(by_id)
            if missing:
                raise ValueError(f"missing ids: {sorted(missing)[:8]}")
        except HarnessError as exc:
            if exc.status is not None:
                raise
            return self._translate_individually(
                client, job, doc, analysis, glossary, group
            )
        except ValueError as exc:
            logger.info("group parse issue (%s); isolating", exc)
            return self._translate_individually(
                client, job, doc, analysis, glossary, group
            )

        self._check_cancelled(job["id"])
        self._save_group(group, by_id, _usage_total(usage))
        return {
            "done": len(by_id),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }

    def _translate_individually(
        self,
        client: HarnessClient,
        job: dict[str, Any],
        doc: dict[str, Any],
        analysis: dict[str, Any],
        glossary: list[dict[str, Any]],
        group: list[dict[str, Any]],
    ) -> dict[str, int]:
        done = 0
        prompt_tokens = 0
        completion_tokens = 0
        for seg in group:
            try:
                prompt = prompts.translator_prompt(
                    title=doc["title"],
                    author=doc["author"],
                    source_lang=doc["source_lang"],
                    target_lang=doc["target_lang"],
                    style=doc["style"],
                    analysis=analysis,
                    glossary=glossary,
                    chapter=seg.get("chapter") or "",
                    context_before="",
                    context_after="",
                    segments=[
                        {
                            "id": seg["id"],
                            "type": seg["block_type"],
                            "text": seg["source_text"],
                        }
                    ],
                    custom_prompt=self._current_prompt(doc["id"]),
                )
                parsed, _raw, usage = client.run_json(
                    prompt,
                    system=prompts.TRANSLATOR_SYSTEM,
                    model=job["model"],
                    max_tokens=min(20_000, max(8000, len(seg["source_text"]) * 2)),
                    retries=1,
                )
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                entries = parsed.get("translations")
                if isinstance(entries, list) and entries:
                    text = entries[0].get("text") if isinstance(entries[0], dict) else None
                    if isinstance(text, str) and text.strip():
                        text = self._postprocess(seg["source_text"], text)
                        self.db.save_translation(seg["id"], text, _usage_total(usage))
                        done += 1
                        continue
                raise ValueError("empty translation")
            except HarnessError as exc:
                if exc.status is not None:
                    raise
                self.db.mark_segment_failed(seg["id"], exc.message)
            except Exception as exc:
                logger.warning("segment %s failed: %s", seg["id"], exc)
                self.db.mark_segment_failed(seg["id"], str(exc))
        return {
            "done": done,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    @staticmethod
    def _postprocess(source: str, text: str) -> str:
        """Remove model-added Markdown emphasis that was absent from the source."""
        if "**" not in source:
            text = text.replace("**", "")
        if source.count("`") == 0 and text.count("`") == 2 and len(text) < 500:
            text = text.replace("`", "")
        return text.strip()

    def _save_group(self, group: list[dict], translations: dict[int, str], tokens: int) -> None:
        total_chars = sum(max(1, len(s["source_text"])) for s in group) or 1
        for seg in group:
            text = translations.get(seg["id"])
            if not text:
                continue
            text = self._postprocess(seg["source_text"], text)
            share = max(1, int(tokens * len(seg["source_text"]) / total_chars))
            self.db.save_translation(seg["id"], text, share)

    # -- review ------------------------------------------------------------
    def _review(
        self,
        client: HarnessClient,
        job: dict[str, Any],
        doc: dict[str, Any],
        analysis: dict[str, Any],
        glossary: list[dict[str, Any]],
        segments: list[dict],
    ) -> int:
        candidates = [
            s
            for s in segments
            if s.get("translatable") and s.get("translated_text") and s.get("status") == "done"
        ]
        reviewed = 0
        batch_size = 6
        for start in range(0, len(candidates), batch_size):
            self._check_cancelled(job["id"])
            batch = candidates[start : start + batch_size]
            payload = [
                {
                    "id": s["id"],
                    "source": s["source_text"],
                    "translation": s["translated_text"],
                    "type": s["block_type"],
                }
                for s in batch
            ]
            prompt = prompts.reviewer_prompt(
                title=doc["title"],
                source_lang=doc["source_lang"],
                target_lang=doc["target_lang"],
                analysis=analysis,
                glossary=glossary,
                segments=payload,
                custom_prompt=self._current_prompt(doc["id"]),
            )
            try:
                parsed, _raw, usage = client.run_json(
                    prompt,
                    system=prompts.REVIEWER_SYSTEM,
                    model=job["model"],
                    max_tokens=12_000,
                    retries=1,
                )
                self.db.add_job_usage(
                    job["id"],
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                )
                revisions = parsed.get("revisions")
                if isinstance(revisions, list):
                    by_id = {s["id"]: s for s in batch}
                    for revision in revisions:
                        if not isinstance(revision, dict):
                            continue
                        try:
                            seg_id = int(revision.get("id"))
                        except (TypeError, ValueError):
                            continue
                        text = revision.get("text")
                        if seg_id in by_id and isinstance(text, str) and text.strip():
                            old = by_id[seg_id]["translated_text"]
                            text = self._postprocess(by_id[seg_id]["source_text"], text)
                            if text.strip() != old.strip():
                                self.db.save_translation(seg_id, text)
                reviewed += len(batch)
            except HarnessError as exc:
                if exc.status is not None:
                    raise
                logger.warning("review batch failed: %s", exc)
            progress = 88 + 6 * min(1.0, (start + len(batch)) / max(1, len(candidates)))
            self.db.update_job(job["id"], reviewed_segments=reviewed, progress=progress)
        return reviewed

    # -- cancellation ------------------------------------------------------
    def _check_cancelled(self, job_id: str) -> None:
        status = self.db.scalar("SELECT status FROM jobs WHERE id=?", (job_id,))
        if status == "cancelled":
            raise JobCancelled()


def _now() -> str:
    from .db import utcnow

    return utcnow()


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
