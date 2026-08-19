from __future__ import annotations

import json
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Job, QualityIssue, Segment, TermEntry
from app.security import create_access_token, hash_token
from app.services.pipeline import process_job
from app.services.quality import unresolved_count
from app.services.renderer import render_artifact

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf"
RESULTS_PATH = ROOT / "output" / "translation-e2e-results.json"
FONT_PATH = Path("C:/Windows/Fonts/NotoSansSC-VF.ttf")


def register_fonts() -> None:
    if FONT_PATH.exists():
        pdfmetrics.registerFont(TTFont("NotoSC", str(FONT_PATH)))


def header(canvas: Canvas, title: str, subtitle: str, page_size=A4) -> None:
    width, height = page_size
    canvas.setFillColor(colors.HexColor("#1e1d19"))
    canvas.setFont("Helvetica-Bold", 22)
    canvas.drawString(48, height - 58, title)
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(colors.HexColor("#9b2c25"))
    canvas.drawString(48, height - 78, subtitle.upper())
    canvas.setStrokeColor(colors.HexColor("#b4231f"))
    canvas.setLineWidth(2)
    canvas.line(48, height - 92, width - 48, height - 92)


def footer(canvas: Canvas, page_number: int, page_size=A4) -> None:
    width, _height = page_size
    canvas.setFillColor(colors.HexColor("#69645a"))
    canvas.setFont("Helvetica", 8)
    canvas.drawString(48, 32, "Folio Translator / deterministic E2E fixture")
    canvas.drawRightString(width - 48, 32, str(page_number))


def make_business_report(path: Path) -> None:
    width, height = A4
    canvas = Canvas(str(path), pagesize=A4)
    header(canvas, "Q2 Operating Review", "Test 01 / business report and terminology")
    canvas.setFillColor(colors.HexColor("#22201c"))
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(48, height - 128, "Executive summary")
    canvas.setFont("Helvetica", 10.5)
    text = canvas.beginText(48, height - 151)
    text.setLeading(16)
    text.textLine("Revenue increased by 18.4% as demand recovered across enterprise accounts.")
    text.textLine("Gross margin reached 42.7%, while customer retention improved to 93.2%.")
    canvas.drawText(text)

    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(48, height - 214, "Key metrics")
    x_positions = [48, 235, 365, 480]
    rows = [
        ("Metric", "Q1", "Q2", "Change"),
        ("Revenue", "$12.8M", "$15.2M", "+18.4%"),
        ("Gross margin", "39.8%", "42.7%", "+2.9pp"),
        ("Customer retention", "91.0%", "93.2%", "+2.2pp"),
    ]
    top = height - 238
    for row_index, row in enumerate(rows):
        y = top - row_index * 28
        canvas.setFillColor(colors.HexColor("#e9dfd0") if row_index == 0 else colors.white)
        canvas.rect(48, y - 8, width - 96, 26, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#27231d"))
        canvas.setFont("Helvetica-Bold" if row_index == 0 else "Helvetica", 9.5)
        for x, value in zip(x_positions, row, strict=True):
            canvas.drawString(x, y, value)
    canvas.setFont("Helvetica", 10.5)
    canvas.drawString(48, height - 390, "Management expects stable demand through the next reporting period.")
    footer(canvas, 1)
    canvas.save()


def make_mixed_language(path: Path) -> None:
    width, height = A4
    canvas = Canvas(str(path), pagesize=A4)
    header(canvas, "Global Launch Notes", "Test 02 / mixed languages and skip rules")
    lines = [
        ("Helvetica-Bold", "English", "The launch team approved the revised onboarding sequence."),
        ("Helvetica-Bold", "Français", "La nouvelle interface réduit le temps de configuration initial."),
        ("NotoSC", "日本語", "新しいワークフローは確認作業をより簡単にします。"),
        ("NotoSC", "目标语言内容", "这一行已经是简体中文，应当保持原样。"),
    ]
    y = height - 132
    for font, label, content in lines:
        canvas.setFont(font, 9)
        canvas.setFillColor(colors.HexColor("#a3312b"))
        canvas.drawString(48, y, label)
        canvas.setFont(font, 11)
        canvas.setFillColor(colors.HexColor("#24211c"))
        canvas.drawString(48, y - 20, content)
        y -= 72
    canvas.setFont("Helvetica", 10)
    canvas.drawString(48, y, "https://example.com/product/launch")
    canvas.drawString(48, y - 38, "launch-team@example.com")
    canvas.drawString(48, y - 76, "x = a + b / 2")
    footer(canvas, 1)
    canvas.showPage()

    header(canvas, "Regional Feedback", "Test 02 / page 2", A4)
    regional = [
        ("Deutsch", "Die Testgruppe bewertete die Navigation als klar und zuverlässig."),
        ("Español", "Los usuarios completaron la tarea sin asistencia adicional."),
        ("Português", "O relatório final destacou uma melhoria consistente na precisão."),
    ]
    y = height - 140
    for label, content in regional:
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.HexColor("#a3312b"))
        canvas.drawString(48, y, label)
        canvas.setFont("Helvetica", 11)
        canvas.setFillColor(colors.HexColor("#24211c"))
        canvas.drawString(48, y - 22, content)
        y -= 92
    footer(canvas, 2)
    canvas.save()


def draw_wrapped(canvas: Canvas, text: str, x: float, y: float, width: float, leading: float) -> None:
    words = text.split()
    line = ""
    cursor = y
    for word in words:
        candidate = f"{line} {word}".strip()
        if pdfmetrics.stringWidth(candidate, "Helvetica", 9.5) > width and line:
            canvas.drawString(x, cursor, line)
            cursor -= leading
            line = word
        else:
            line = candidate
    if line:
        canvas.drawString(x, cursor, line)


def make_layout_stress(path: Path) -> None:
    page_size = landscape(A4)
    width, height = page_size
    canvas = Canvas(str(path), pagesize=page_size)
    header(canvas, "Editorial Systems Brief", "Test 03 / columns, callouts and expansion", page_size)
    left = (
        "A translation system must preserve more than isolated sentences. It needs to maintain "
        "hierarchy, reading order, numerical accuracy, and the relationship between captions and "
        "the visual evidence they describe. A small wording change can materially alter meaning."
    )
    right = (
        "Automated quality checks should reveal uncertainty instead of hiding it. Low-confidence "
        "recognition, unchanged passages, missing translations, and text that cannot fit inside the "
        "available space all require a visible review decision before final publication."
    )
    canvas.setFillColor(colors.HexColor("#24211c"))
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(48, height - 132, "Preserve structure")
    canvas.drawString(width / 2 + 18, height - 132, "Expose uncertainty")
    canvas.setFont("Helvetica", 9.5)
    draw_wrapped(canvas, left, 48, height - 158, width / 2 - 76, 14)
    draw_wrapped(canvas, right, width / 2 + 18, height - 158, width / 2 - 66, 14)

    canvas.setFillColor(colors.HexColor("#efe3d2"))
    canvas.roundRect(48, 120, width - 96, 105, 8, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor("#8c251f"))
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(68, 196, "REVIEW PRINCIPLE")
    canvas.setFillColor(colors.HexColor("#26221d"))
    canvas.setFont("Helvetica", 10)
    canvas.drawString(
        68,
        172,
        "A clean final document should be impossible to export while unresolved warnings remain.",
    )
    canvas.drawString(
        68,
        150,
        "The draft remains available so an editor can inspect imperfect output without losing work.",
    )
    footer(canvas, 1, page_size)
    canvas.save()


def create_sources() -> list[dict]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cases = [
        {
            "slug": "test-01-business",
            "source": OUTPUT / "test-01-business-source.pdf",
            "builder": make_business_report,
        },
        {
            "slug": "test-02-multilingual",
            "source": OUTPUT / "test-02-multilingual-source.pdf",
            "builder": make_mixed_language,
        },
        {
            "slug": "test-03-layout",
            "source": OUTPUT / "test-03-layout-source.pdf",
            "builder": make_layout_stress,
        },
    ]
    for case in cases:
        case["builder"](case["source"])
    return cases


def run_case(case: dict) -> dict:
    settings = get_settings()
    job_id = str(uuid.uuid4())
    token = create_access_token()
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    stored_source = job_dir / "source.pdf"
    shutil.copy2(case["source"], stored_source)
    with SessionLocal() as db:
        db.add(
            Job(
                id=job_id,
                access_token_hash=hash_token(token),
                source_filename=case["source"].name,
                source_path=str(stored_source),
                target_language="zh-Hans",
                output_modes="translated,bilingual",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        db.commit()

    started = datetime.now(UTC)
    process_job(job_id)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job is not None
        segments = list(
            db.scalars(
                select(Segment)
                .where(Segment.job_id == job_id)
                .order_by(Segment.page_id, Segment.reading_order)
            )
        )
        issues_before_render = list(
            db.scalars(select(QualityIssue).where(QualityIssue.job_id == job_id))
        )
        if job.status != "review_required":
            return {
                "slug": case["slug"],
                "job_id": job_id,
                "status": job.status,
                "error": job.error,
                "elapsed_seconds": round(elapsed, 2),
            }
        translated = render_artifact(db, job, "translated", final=False)
        bilingual = render_artifact(db, job, "bilingual", final=False)
        translated_out = OUTPUT / f"{case['slug']}-translated-draft.pdf"
        bilingual_out = OUTPUT / f"{case['slug']}-bilingual-draft.pdf"
        shutil.copy2(translated.path, translated_out)
        shutil.copy2(bilingual.path, bilingual_out)
        issues_after_render = list(
            db.scalars(select(QualityIssue).where(QualityIssue.job_id == job_id))
        )
        target_text = "\n".join(segment.target_text or "" for segment in segments)
        return {
            "slug": case["slug"],
            "job_id": job_id,
            "status": job.status,
            "elapsed_seconds": round(elapsed, 2),
            "pages": job.page_count,
            "segments": len(segments),
            "segment_statuses": dict(Counter(segment.status for segment in segments)),
            "detected_languages": dict(
                Counter(segment.source_language or "und" for segment in segments)
            ),
            "issues_before_render": [issue.code for issue in issues_before_render],
            "issues_after_render": [issue.code for issue in issues_after_render],
            "unresolved_issues": unresolved_count(db, job_id),
            "term_checks": {
                "gross_margin": "毛利率" in target_text,
                "customer_retention": "客户留存率" in target_text,
            }
            if case["slug"] == "test-01-business"
            else None,
            "preserved_checks": {
                "url": "https://example.com/product/launch" in target_text,
                "email": "launch-team@example.com" in target_text,
                "equation": "x = a + b / 2" in target_text,
                "existing_chinese": "这一行已经是简体中文，应当保持原样。" in target_text,
            }
            if case["slug"] == "test-02-multilingual"
            else None,
            "sample_translations": [
                {
                    "source": segment.source_text,
                    "target": segment.target_text,
                    "language": segment.source_language,
                    "status": segment.status,
                }
                for segment in segments
                if segment.status not in {"skipped"}
            ][:8],
            "source_pdf": str(case["source"]),
            "translated_pdf": str(translated_out),
            "bilingual_pdf": str(bilingual_out),
        }


def main() -> None:
    register_fonts()
    init_db()
    cases = create_sources()
    with SessionLocal() as db:
        db.execute(delete(TermEntry).where(TermEntry.notes == "e2e-test"))
        db.add_all(
            [
                TermEntry(
                    source_language="en",
                    target_language="zh-Hans",
                    source_term="gross margin",
                    target_term="毛利率",
                    notes="e2e-test",
                ),
                TermEntry(
                    source_language="en",
                    target_language="zh-Hans",
                    source_term="customer retention",
                    target_term="客户留存率",
                    notes="e2e-test",
                ),
            ]
        )
        db.commit()
    try:
        results = [run_case(case) for case in cases]
    finally:
        with SessionLocal() as db:
            db.execute(delete(TermEntry).where(TermEntry.notes == "e2e-test"))
            db.commit()
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
