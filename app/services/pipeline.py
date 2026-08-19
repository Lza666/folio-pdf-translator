from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.document_ir import (
    StructureValidationError,
    apply_structured_translation,
    copy_source_structure_to_target,
    encode_structure_for_translation,
)
from app.language import is_translatable, same_target_language
from app.memory import exact_match, terms_for
from app.models import Job, JobStatus, Page, Segment
from app.providers.base import ProviderError, ProviderNotConfigured, TranslationItem
from app.services.compaction import compact_overflow_translations
from app.services.pdf import PDFValidationError, extract_document, inspect_pdf, ocr_scanned_page
from app.services.quality import run_quality_checks
from app.services.renderer import find_compression_candidates
from app.services.settings import build_ocr, build_translator


class PipelineControl(RuntimeError):
    pass


def _check_control(job: Job) -> None:
    if job.control_requested == "cancel":
        job.status = JobStatus.canceled.value
        raise PipelineControl("任务已取消")
    if job.control_requested == "pause":
        job.status = JobStatus.paused.value
        raise PipelineControl("任务已暂停")


def _set_progress(job: Job, status: JobStatus, stage: str, progress: float, page: int = 0) -> None:
    job.status = status.value
    job.stage = stage
    job.progress = max(job.progress, min(progress, 1.0))
    job.current_page = page


def process_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status in {JobStatus.expired.value, JobStatus.completed.value}:
            return
        job.control_requested = None
        job.error = None
        try:
            _set_progress(job, JobStatus.validating, "校验 PDF", 0.03)
            db.commit()
            inspection = inspect_pdf(Path(job.source_path), job.id)
            job.page_count = inspection.page_count
            job.is_encrypted = inspection.encrypted
            job.has_signature = inspection.has_signature
            if inspection.has_form:
                raise PDFValidationError("首版不处理带交互表单字段的 PDF")
            warnings = []
            if inspection.has_signature:
                warnings.append("源文件包含数字签名，译文不会保留签名效力")
            if inspection.has_attachments:
                warnings.append("源文件附件不会复制到译文")
            job.warning = "；".join(warnings) or None
            _check_control(job)

            _set_progress(job, JobStatus.extracting, "提取页面结构", 0.10)
            db.commit()
            extract_document(db, job)
            _check_control(job)

            scanned_pages = list(
                db.scalars(
                    select(Page)
                    .where(Page.job_id == job.id, Page.page_type == "scanned")
                    .order_by(Page.page_number)
                )
            )
            if scanned_pages:
                provider = build_ocr(db)
                for index, page in enumerate(scanned_pages, start=1):
                    _check_control(job)
                    _set_progress(
                        job,
                        JobStatus.ocr,
                        f"OCR 第 {page.page_number} 页",
                        0.20 + 0.20 * index / len(scanned_pages),
                        page.page_number,
                    )
                    db.commit()
                    ocr_scanned_page(db, job, page, provider)

            segments = list(
                db.scalars(
                    select(Segment)
                    .where(Segment.job_id == job.id)
                    .order_by(Segment.page_id, Segment.reading_order)
                )
            )
            pending: list[Segment] = []
            for segment in segments:
                if segment.target_text or segment.ignored:
                    continue
                if not is_translatable(segment.source_text) or same_target_language(
                    segment.source_language, job.target_language
                ):
                    segment.target_text = segment.source_text
                    segment.structure_json = copy_source_structure_to_target(segment.structure_json)
                    segment.status = "skipped"
                    continue
                remembered = exact_match(
                    db, segment.source_language or "und", job.target_language, segment.source_text
                )
                if remembered:
                    segment.target_text = remembered
                    segment.status = "memory"
                    segment.confirmed = True
                else:
                    pending.append(segment)
            db.commit()

            translator = None
            if pending:
                translator = build_translator(db)
                batch_size = 12
                for offset in range(0, len(pending), batch_size):
                    _check_control(job)
                    batch = pending[offset : offset + batch_size]
                    progress = 0.45 + 0.40 * min(offset + len(batch), len(pending)) / len(pending)
                    _set_progress(job, JobStatus.translating, "翻译文本片段", progress)
                    db.commit()
                    source_languages = {segment.source_language or "und" for segment in batch}
                    terms = {
                        term.source_term: term.target_term
                        for term in terms_for(db, job.target_language, source_languages)
                    }
                    items = [
                        TranslationItem(
                            id=segment.segment_key,
                            text=encode_structure_for_translation(segment.structure_json)
                            or segment.source_text,
                            source_language=segment.source_language,
                            context=(
                                "Preserve the supplied FOLIO XML document structure and inline styles."
                                if segment.structure_json
                                else None
                            ),
                        )
                        for segment in batch
                    ]
                    results = translator.translate(items, job.target_language, terms)
                    for segment in batch:
                        translated = results[segment.segment_key]
                        if segment.structure_json:
                            last_error: StructureValidationError | None = None
                            for structural_attempt in range(2):
                                try:
                                    target_text, updated_structure = apply_structured_translation(
                                        segment.structure_json, translated
                                    )
                                    segment.target_text = target_text
                                    segment.structure_json = updated_structure
                                    last_error = None
                                    break
                                except StructureValidationError as exc:
                                    last_error = exc
                                    if structural_attempt == 0:
                                        retry_item = next(
                                            item for item in items if item.id == segment.segment_key
                                        )
                                        translated = translator.translate(
                                            [retry_item], job.target_language, terms
                                        )[segment.segment_key]
                            if last_error is not None:
                                raise ProviderError(
                                    f"结构化译文两次校验失败：{last_error}"
                                ) from last_error
                        else:
                            segment.target_text = translated
                        segment.status = "translated"
                        segment.translation_hash = hashlib.sha256(
                            (segment.source_text + "\0" + segment.target_text).encode()
                        ).hexdigest()
                    db.commit()

            compression_candidates = find_compression_candidates(db, job)
            eligible_compression = [
                candidate
                for candidate in compression_candidates
                if not candidate.segment.confirmed
                and candidate.segment.status in {"translated", "ai_compacted"}
            ]
            if eligible_compression:
                _set_progress(job, JobStatus.translating, "压缩超出版面的译文", 0.87)
                db.commit()
                translator = translator or build_translator(db)
                compact_overflow_translations(
                    db,
                    job,
                    translator,
                    initial_candidates=compression_candidates,
                )

            run_quality_checks(db, job)
            _set_progress(job, JobStatus.review_required, "等待人工校对", 0.90)
            db.commit()
        except PipelineControl:
            db.commit()
        except ProviderNotConfigured as exc:
            job.status = JobStatus.paused.value
            job.stage = "等待服务配置"
            job.error = str(exc)
            db.commit()
        except (ProviderError, PDFValidationError) as exc:
            job.status = JobStatus.paused.value
            job.stage = "需要处理"
            job.error = str(exc)
            db.commit()
        except Exception as exc:
            job.status = JobStatus.failed.value
            job.stage = "处理失败"
            job.error = f"{type(exc).__name__}: {exc}"
            db.commit()
