from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Job, QualityIssue, Segment
from app.security import hash_token
from app.services.compaction import compact_overflow_translations
from app.services.pdf import extract_document
from app.services.renderer import find_compression_candidates, render_artifact


class ConstrainedFixtureTranslator:
    def translate(self, items, target_language, terms):
        options = ["2026服务报告须精炼且语义完整。", "2026报告须完整。", "2026完整。"]
        return {
            item.id: next(text for text in options if len(text) <= (item.max_characters or 999))
            for item in items
        }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    work = root / "tmp" / "pdfs" / "ai-compaction"
    output = root / "output" / "pdf" / "native-ai-compaction-fallback.pdf"
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    source = work / "source.pdf"

    doc = pymupdf.open()
    page = doc.new_page(width=420, height=260)
    page.insert_textbox(
        pymupdf.Rect(180, 110, 260, 124),
        "2026 report",
        fontsize=7.5,
    )
    page.insert_textbox(
        pymupdf.Rect(282, 105, 408, 126),
        "RIGHT BLOCKER MUST REMAIN",
        fontsize=7.5,
    )
    page.insert_textbox(
        pymupdf.Rect(172, 140, 350, 158),
        "BOTTOM BLOCKER MUST REMAIN",
        fontsize=7.5,
    )
    page.draw_line(pymupdf.Point(274, 96), pymupdf.Point(274, 132), color=(0.7, 0.2, 0.2))
    page.draw_line(pymupdf.Point(162, 134), pymupdf.Point(360, 134), color=(0.7, 0.2, 0.2))
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(
            id="visual-ai-compaction",
            access_token_hash=hash_token("visual"),
            source_filename=source.name,
            source_path=str(source),
            target_language="zh-Hans",
            output_modes="translated",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(job)
        db.commit()
        extract_document(db, job)
        main_segment = db.scalar(
            select(Segment).where(
                Segment.job_id == job.id,
                Segment.source_text == "2026 report",
            )
        )
        if main_segment is None:
            raise RuntimeError("fixture segment was not extracted")
        for segment in db.scalars(select(Segment).where(Segment.job_id == job.id)):
            if segment.id == main_segment.id:
                segment.target_text = (
                    "2026年的服务报告必须在极其有限的版面内保留所有信息、数字、语气和专业含义，"
                    "同时不能覆盖右侧文本、下方文本或红色结构线。"
                ) * 8
                segment.status = "translated"
            else:
                segment.target_text = segment.source_text
                segment.status = "skipped"
        db.commit()

        before = find_compression_candidates(db, job)
        changed = compact_overflow_translations(db, job, ConstrainedFixtureTranslator(), before)
        after = find_compression_candidates(db, job)
        artifact = render_artifact(db, job, "translated", final=False)
        shutil.copyfile(artifact.path, output)
        issues = list(
            db.scalars(select(QualityIssue).where(QualityIssue.job_id == job.id))
        )
        print(
            json.dumps(
                {
                    "before_budget": before[0].max_characters if before else None,
                    "changed": changed,
                    "remaining_overflows": len(after),
                    "final_translation": main_segment.target_text,
                    "issues": [issue.code for issue in issues],
                    "output": str(output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
