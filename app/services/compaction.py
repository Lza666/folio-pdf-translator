from __future__ import annotations

import hashlib
import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory import terms_for
from app.models import Job, QualityIssue, Segment
from app.providers.base import ProviderError, TranslationItem, TranslationProvider
from app.services.quality import add_issue
from app.services.renderer import CompressionCandidate, find_compression_candidates

NUMBER_PATTERN = re.compile(r"[+-]?\d[\d,.%]*")


def _normalized_numbers(value: str) -> list[str]:
    return [
        "".join(character for character in token if character.isdigit())
        for token in NUMBER_PATTERN.findall(value)
        if any(character.isdigit() for character in token)
    ]


def _preserves_required_content(
    segment: Segment,
    translated: str,
    terms: dict[str, str],
) -> bool:
    target_digits = "".join(character for character in translated if character.isdigit())
    if any(number not in target_digits for number in _normalized_numbers(segment.source_text)):
        return False
    return all(
        target in translated
        for source, target in terms.items()
        if source.casefold() in segment.source_text.casefold()
    )


def _minimum_safe_budget(segment: Segment) -> int:
    if segment.kind == "table_cell":
        return 2
    if segment.kind in {"title", "caption", "header", "footer"}:
        return 4
    return 8


def _resolve_issue(db: Session, segment: Segment, code: str) -> None:
    issue = db.scalar(
        select(QualityIssue).where(
            QualityIssue.segment_id == segment.id,
            QualityIssue.code == code,
            QualityIssue.resolved.is_(False),
        )
    )
    if issue is not None:
        issue.resolved = True


def _eligible(candidate: CompressionCandidate) -> bool:
    segment = candidate.segment
    return (
        not segment.confirmed
        and segment.status in {"translated", "ai_compacted"}
        and candidate.max_characters >= _minimum_safe_budget(segment)
    )


def compact_overflow_translations(
    db: Session,
    job: Job,
    translator: TranslationProvider,
    initial_candidates: list[CompressionCandidate] | None = None,
) -> int:
    """Ask the model for shorter faithful translations after both safe layouts fail."""
    changed_ids: set[int] = set()
    candidates = initial_candidates
    for attempt in range(2):
        if candidates is None:
            candidates = find_compression_candidates(db, job)
        eligible = [candidate for candidate in candidates if _eligible(candidate)]
        if not eligible:
            break
        source_languages = {
            candidate.segment.source_language or "und" for candidate in eligible
        }
        terms = {
            term.source_term: term.target_term
            for term in terms_for(db, job.target_language, source_languages)
        }
        factor = 0.90 if attempt == 0 else 0.80
        budgets = {
            candidate.segment.segment_key: max(
                _minimum_safe_budget(candidate.segment),
                math.floor(candidate.max_characters * factor),
            )
            for candidate in eligible
        }
        items = [
            TranslationItem(
                id=candidate.segment.segment_key,
                text=candidate.segment.source_text,
                source_language=candidate.segment.source_language,
                context=f"Previous translation: {candidate.segment.target_text}",
                max_characters=budgets[candidate.segment.segment_key],
            )
            for candidate in eligible
        ]
        try:
            results = translator.translate(items, job.target_language, terms)
        except ProviderError as exc:
            for candidate in eligible:
                add_issue(
                    db,
                    job.id,
                    "compression_failed",
                    f"模型未能生成满足版面预算的短译文：{exc}",
                    severity="error",
                    segment_id=candidate.segment.id,
                )
            db.commit()
            break
        for candidate in eligible:
            segment = candidate.segment
            translated = results[segment.segment_key].strip()
            if not _preserves_required_content(segment, translated, terms):
                add_issue(
                    db,
                    job.id,
                    "compression_failed",
                    "短译文未完整保留数字或强制术语，已保留原译文",
                    severity="error",
                    segment_id=segment.id,
                )
                continue
            segment.target_text = translated
            segment.status = "ai_compacted"
            segment.translation_hash = hashlib.sha256(
                (segment.source_text + "\0" + translated).encode()
            ).hexdigest()
            _resolve_issue(db, segment, "compression_failed")
            add_issue(
                db,
                job.id,
                "ai_compacted",
                f"译文因版面容量限制由模型压缩至 {len(translated)}/{budgets[segment.segment_key]} 字符，请人工确认语义完整性",
                severity="warning",
                segment_id=segment.id,
            )
            changed_ids.add(segment.id)
        db.commit()
        candidates = find_compression_candidates(db, job)

    remaining = find_compression_candidates(db, job)
    for candidate in remaining:
        if candidate.segment.confirmed or candidate.segment.status not in {
            "translated",
            "ai_compacted",
        }:
            continue
        if candidate.max_characters < _minimum_safe_budget(candidate.segment):
            add_issue(
                db,
                job.id,
                "compression_failed",
                f"版面最多容纳 {candidate.max_characters} 个字符，低于自动语义压缩安全阈值",
                severity="error",
                segment_id=candidate.segment.id,
            )
        else:
            add_issue(
                db,
                job.id,
                "compression_failed",
                "两次受限重译后仍无法安全放置，请人工缩写或调整版式",
                severity="error",
                segment_id=candidate.segment.id,
            )
    db.commit()
    return len(changed_ids)
