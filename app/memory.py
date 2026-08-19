from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.language import normalize_text
from app.models import TermEntry, TranslationMemory, TranslationMemoryVersion


def upsert_memory(
    db: Session,
    *,
    source_language: str,
    target_language: str,
    source_text: str,
    target_text: str,
    context: str | None = None,
    source_job_id: str | None = None,
) -> TranslationMemory:
    normalized = normalize_text(source_text)
    unit = db.scalar(
        select(TranslationMemory).where(
            TranslationMemory.source_language == source_language,
            TranslationMemory.target_language == target_language,
            TranslationMemory.normalized_source == normalized,
        )
    )
    if unit is None:
        unit = TranslationMemory(
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            normalized_source=normalized,
            target_text=target_text,
            context=context,
            source_job_id=source_job_id,
            version=1,
        )
        db.add(unit)
        db.flush()
    elif unit.target_text != target_text:
        unit.version += 1
        unit.target_text = target_text
        unit.context = context
        unit.source_job_id = source_job_id
    existing_version = db.scalar(
        select(TranslationMemoryVersion).where(
            TranslationMemoryVersion.unit_id == unit.id,
            TranslationMemoryVersion.version == unit.version,
        )
    )
    if existing_version is None:
        db.add(
            TranslationMemoryVersion(
                unit_id=unit.id, version=unit.version, target_text=target_text
            )
        )
    db.flush()
    return unit


def exact_match(db: Session, source_language: str, target_language: str, text: str) -> str | None:
    unit = db.scalar(
        select(TranslationMemory).where(
            TranslationMemory.source_language == source_language,
            TranslationMemory.target_language == target_language,
            TranslationMemory.normalized_source == normalize_text(text),
            TranslationMemory.active.is_(True),
        )
    )
    return unit.target_text if unit else None


def fuzzy_matches(
    db: Session, source_language: str, target_language: str, text: str, limit: int = 5
) -> list[dict]:
    units = list(
        db.scalars(
            select(TranslationMemory).where(
                TranslationMemory.source_language == source_language,
                TranslationMemory.target_language == target_language,
                TranslationMemory.active.is_(True),
            )
        )
    )
    if not units:
        return []
    choices = {unit.id: unit.normalized_source for unit in units}
    by_id = {unit.id: unit for unit in units}
    matches = process.extract(
        normalize_text(text), choices, scorer=fuzz.ratio, score_cutoff=92, limit=limit
    )
    results = []
    for _choice, score, unit_id in matches:
        unit = by_id[unit_id]
        results.append(
            {"id": unit.id, "source_text": unit.source_text, "target_text": unit.target_text, "score": score}
        )
    return results


def terms_for(db: Session, target_language: str, source_languages: set[str]) -> list[TermEntry]:
    return list(
        db.scalars(
            select(TermEntry).where(
                TermEntry.target_language == target_language,
                TermEntry.source_language.in_(source_languages),
                TermEntry.active.is_(True),
            )
        )
    )


def export_memory_csv(units: list[TranslationMemory]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["source_language", "target_language", "source_text", "target_text", "context"])
    for unit in units:
        writer.writerow(
            [unit.source_language, unit.target_language, unit.source_text, unit.target_text, unit.context or ""]
        )
    return buffer.getvalue()


def export_memory_tmx(units: list[TranslationMemory]) -> bytes:
    root = ET.Element("tmx", version="1.4")
    ET.SubElement(
        root,
        "header",
        creationtool="Folio Translator",
        creationtoolversion="0.1.0",
        segtype="paragraph",
        adminlang="en",
        srclang="*all*",
        datatype="PlainText",
    )
    body = ET.SubElement(root, "body")
    for unit in units:
        tu = ET.SubElement(body, "tu")
        for language, text in (
            (unit.source_language, unit.source_text),
            (unit.target_language, unit.target_text),
        ):
            tuv = ET.SubElement(tu, "tuv", {"xml:lang": language})
            ET.SubElement(tuv, "seg").text = text
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
