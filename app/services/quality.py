from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.document_ir import target_structure_matches
from app.language import normalize_text
from app.models import Job, QualityIssue, Segment

GENERATED_CODES = {
    "missing_translation",
    "unchanged_translation",
    "low_confidence",
    "overflow",
    "structure_unpreserved",
}


def add_issue(
    db: Session,
    job_id: str,
    code: str,
    message: str,
    *,
    severity: str = "warning",
    segment_id: int | None = None,
) -> QualityIssue:
    existing = db.scalar(
        select(QualityIssue).where(
            QualityIssue.job_id == job_id,
            QualityIssue.segment_id == segment_id,
            QualityIssue.code == code,
            QualityIssue.resolved.is_(False),
        )
    )
    if existing:
        existing.message = message
        existing.severity = severity
        return existing
    issue = QualityIssue(
        job_id=job_id,
        segment_id=segment_id,
        code=code,
        message=message,
        severity=severity,
    )
    db.add(issue)
    return issue


def run_quality_checks(db: Session, job: Job) -> None:
    db.execute(
        delete(QualityIssue).where(
            QualityIssue.job_id == job.id,
            QualityIssue.code.in_(GENERATED_CODES - {"overflow"}),
            QualityIssue.acknowledged.is_(False),
        )
    )
    segments = list(db.scalars(select(Segment).where(Segment.job_id == job.id)))
    for segment in segments:
        if segment.ignored or segment.status == "skipped":
            continue
        if not segment.target_text or not segment.target_text.strip():
            add_issue(
                db,
                job.id,
                "missing_translation",
                "此片段尚无译文",
                severity="error",
                segment_id=segment.id,
            )
        elif (
            segment.source_language != job.target_language
            and normalize_text(segment.source_text) == normalize_text(segment.target_text)
        ):
            add_issue(
                db,
                job.id,
                "unchanged_translation",
                "译文与原文完全相同，请确认是否应保留",
                severity="warning",
                segment_id=segment.id,
            )
        if (
            segment.structure_json
            and segment.target_text
            and segment.status not in {"memory", "edited", "ai_compacted"}
            and not target_structure_matches(segment.structure_json, segment.target_text)
        ):
            add_issue(
                db,
                job.id,
                "structure_unpreserved",
                "译文未完整保留段落、列表项或行内样式结构",
                severity="error",
                segment_id=segment.id,
            )
        if segment.confidence is not None and segment.confidence < 0.80:
            add_issue(
                db,
                job.id,
                "low_confidence",
                f"文本识别/语言判断置信度较低（{segment.confidence:.0%}）",
                severity="warning",
                segment_id=segment.id,
            )
    db.commit()


def unresolved_count(db: Session, job_id: str) -> int:
    return int(
        db.scalar(
            select(func.count(QualityIssue.id)).where(
                QualityIssue.job_id == job_id,
                QualityIssue.resolved.is_(False),
                QualityIssue.acknowledged.is_(False),
            )
        )
        or 0
    )


def blocking_count(db: Session, job_id: str) -> int:
    return int(
        db.scalar(
            select(func.count(QualityIssue.id)).where(
                QualityIssue.job_id == job_id,
                QualityIssue.resolved.is_(False),
                QualityIssue.acknowledged.is_(False),
                QualityIssue.severity.in_(["error", "critical"]),
            )
        )
        or 0
    )
