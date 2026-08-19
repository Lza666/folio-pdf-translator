from __future__ import annotations

import argparse
import json
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Job, Page, QualityIssue, Segment
from app.security import create_access_token, hash_token
from app.services.pipeline import process_job
from app.services.quality import unresolved_count
from app.services.renderer import render_artifact

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "tmp" / "pdfs" / "native-stress" / "sources"
OUTPUT_DIR = ROOT / "output" / "pdf"
RESULTS_PATH = ROOT / "output" / "native-pdf-stress-results.json"


def _header(canvas: Canvas, title: str, label: str, page_size=A4) -> None:
    width, height = page_size
    canvas.setFillColor(colors.HexColor("#191815"))
    canvas.setFont("Helvetica-Bold", 20)
    canvas.drawString(42, height - 48, title)
    canvas.setFillColor(colors.HexColor("#a92c25"))
    canvas.setFont("Helvetica", 8)
    canvas.drawString(42, height - 67, label.upper())
    canvas.setStrokeColor(colors.HexColor("#b4231f"))
    canvas.setLineWidth(1.5)
    canvas.line(42, height - 79, width - 42, height - 79)


def _footer(canvas: Canvas, page: int, page_size=A4) -> None:
    width, _height = page_size
    canvas.setFillColor(colors.HexColor("#69645a"))
    canvas.setFont("Helvetica", 7)
    canvas.drawString(42, 24, "Folio Translator / native PDF stress fixture")
    canvas.drawRightString(width - 42, 24, str(page))


def _draw_wrapped(
    canvas: Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    *,
    font: str = "Helvetica",
    size: float = 8.5,
    leading: float = 11.0,
) -> float:
    words = text.split()
    line = ""
    cursor = y
    canvas.setFont(font, size)
    for word in words:
        candidate = f"{line} {word}".strip()
        if line and pdfmetrics.stringWidth(candidate, font, size) > width:
            canvas.drawString(x, cursor, line)
            cursor -= leading
            line = word
        else:
            line = candidate
    if line:
        canvas.drawString(x, cursor, line)
        cursor -= leading
    return cursor


def make_colored_table(path: Path) -> None:
    width, height = A4
    canvas = Canvas(str(path), pagesize=A4)
    _header(canvas, "Service Reliability Matrix", "Native 01 / colored table cells")
    columns = [42, 184, 280, 365, 450, 553]
    headers = ["Service", "Owner", "Availability", "Latency", "Status"]
    rows = [
        ("Authentication Gateway", "Platform", "99.992%", "84 ms", "Healthy"),
        ("Document Extraction", "Applied AI", "99.870%", "410 ms", "Watch"),
        ("Translation Router", "Language", "99.950%", "1.8 sec", "Healthy"),
        ("Terminology Store", "Editorial", "99.999%", "21 ms", "Healthy"),
        ("PDF Rendering", "Publishing", "99.720%", "2.4 sec", "Degraded"),
        ("Artifact Delivery", "Platform", "99.910%", "190 ms", "Watch"),
        ("Audit Logging", "Security", "99.998%", "33 ms", "Healthy"),
        ("Cleanup Scheduler", "Operations", "99.840%", "510 ms", "Watch"),
    ]
    top = height - 116
    row_height = 42
    canvas.setFillColor(colors.HexColor("#263b59"))
    canvas.rect(42, top - 23, width - 84, 30, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 8.5)
    for index, value in enumerate(headers):
        canvas.drawString(columns[index] + 5, top - 13, value)
    for row_index, row in enumerate(rows):
        y = top - 30 - row_index * row_height
        fill = colors.HexColor("#f1e7d7") if row_index % 2 == 0 else colors.HexColor("#fbf8f2")
        canvas.setFillColor(fill)
        canvas.rect(42, y - 29, width - 84, row_height, fill=1, stroke=0)
        canvas.setStrokeColor(colors.HexColor("#c8bba8"))
        canvas.line(42, y - 29, width - 42, y - 29)
        canvas.setFillColor(colors.HexColor("#24211c"))
        canvas.setFont("Helvetica", 8.3)
        for index, value in enumerate(row[:-1]):
            canvas.drawString(columns[index] + 5, y - 8, value)
        status = row[-1]
        status_color = {
            "Healthy": "#d5eadb",
            "Watch": "#f3dfac",
            "Degraded": "#efc8c3",
        }[status]
        canvas.setFillColor(colors.HexColor(status_color))
        canvas.roundRect(columns[4] + 3, y - 17, 72, 20, 6, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#332f29"))
        canvas.setFont("Helvetica-Bold", 7.8)
        canvas.drawCentredString(columns[4] + 39, y - 10, status)
    canvas.setStrokeColor(colors.HexColor("#7f7468"))
    for x in columns:
        canvas.line(x, top + 7, x, top - 30 - len(rows) * row_height + 13)
    canvas.setFillColor(colors.HexColor("#35312b"))
    canvas.setFont("Helvetica", 9)
    canvas.drawString(42, 76, "Colored cells, vector rules, and status badges must remain intact after translation.")
    _footer(canvas, 1)
    canvas.save()


def make_dense_financial_table(path: Path) -> None:
    width, height = landscape(A4)
    canvas = Canvas(str(path), pagesize=(width, height))
    headers = ["Region", "Product", "Units", "Revenue", "Cost", "Margin", "Forecast", "Variance"]
    column_x = [36, 126, 258, 326, 404, 480, 550, 662]
    regions = ["North America", "Europe", "Japan", "Korea", "Southeast Asia", "Latin America"]
    products = ["Core Platform", "Enterprise Suite", "Workflow API", "Analytics Pack"]
    for page_number in range(1, 3):
        _header(
            canvas,
            "Consolidated Revenue Detail",
            f"Native 02 / dense financial table / page {page_number}",
            (width, height),
        )
        top = height - 103
        row_height = 18
        canvas.setFillColor(colors.HexColor("#202f44"))
        canvas.rect(34, top - 12, width - 68, 22, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 7.2)
        for x, title in zip(column_x, headers, strict=True):
            canvas.drawString(x, top - 5, title)
        for row_index in range(22):
            absolute = (page_number - 1) * 22 + row_index
            y = top - 30 - row_index * row_height
            canvas.setFillColor(
                colors.HexColor("#f3ede3") if row_index % 2 == 0 else colors.HexColor("#fffdf9")
            )
            canvas.rect(34, y - 7, width - 68, row_height, fill=1, stroke=0)
            values = [
                regions[absolute % len(regions)],
                products[absolute % len(products)],
                f"{1240 + absolute * 37:,}",
                f"${2.8 + absolute * 0.17:.2f}M",
                f"${1.6 + absolute * 0.09:.2f}M",
                f"{38.0 + absolute % 9 * 0.7:.1f}%",
                f"${3.1 + absolute * 0.16:.2f}M",
                f"{(-4.2 + absolute % 13 * 0.8):+.1f}%",
            ]
            canvas.setFillColor(colors.HexColor("#2c2924"))
            canvas.setFont("Helvetica", 6.9)
            for x, value in zip(column_x, values, strict=True):
                canvas.drawString(x, y, value)
            canvas.setStrokeColor(colors.HexColor("#d5cab9"))
            canvas.line(34, y - 7, width - 34, y - 7)
        canvas.setStrokeColor(colors.HexColor("#a99b88"))
        for x in [34, *column_x[1:], width - 34]:
            canvas.line(x, top + 10, x, top - 30 - 22 * row_height + 11)
        _footer(canvas, page_number, (width, height))
        if page_number == 1:
            canvas.showPage()
    canvas.save()


def make_three_column_report(path: Path) -> None:
    width, height = landscape(A4)
    canvas = Canvas(str(path), pagesize=(width, height))
    _header(canvas, "Research Operations Review", "Native 03 / three-column dense prose", (width, height))
    column_width = (width - 120) / 3
    paragraphs = [
        "The review team compared extraction quality across digitally generated reports, exported slide decks, and archival documents. Each page was evaluated for reading order, hierarchy, numerical fidelity, and preservation of non-text elements.",
        "Dense pages create a specific risk because translated wording may expand while the available column width remains fixed. A successful renderer must reduce line spacing and font size conservatively without allowing text to overlap adjacent columns.",
        "Tables require a different interpretation of proximity. Values that appear on the same visual line may belong to separate cells, and a row label should not be merged with the numeric series that follows it.",
        "Quality controls should identify missing translations, unchanged source passages, clipped glyphs, border collisions, and unexpected changes to page geometry. Each warning must remain traceable to the segment that created it.",
        "Editors need the ability to inspect uncertain output without losing progress. Draft rendering therefore remains available even when final publication is blocked by unresolved warnings.",
        "The final benchmark includes colored callouts, narrow side notes, repeated headers, footnotes, and long paragraphs with punctuation. These elements expose failures that short demonstration documents rarely reveal.",
    ]
    for column in range(3):
        x = 42 + column * (column_width + 18)
        y = height - 112
        canvas.setFillColor(colors.HexColor("#8f2922"))
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(x, y, f"SECTION {column + 1}")
        y -= 22
        for index in range(4):
            paragraph = paragraphs[(column * 2 + index) % len(paragraphs)]
            canvas.setFillColor(colors.HexColor("#292620"))
            y = _draw_wrapped(canvas, paragraph, x, y, column_width, size=8.2, leading=10.3)
            y -= 11
        canvas.setStrokeColor(colors.HexColor("#c9bca9"))
        if column < 2:
            separator = x + column_width + 9
            canvas.line(separator, 48, separator, height - 104)
    canvas.setFillColor(colors.HexColor("#e9ddca"))
    canvas.roundRect(width - 280, 46, 238, 62, 7, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor("#24211d"))
    canvas.setFont("Helvetica-Bold", 8.5)
    canvas.drawString(width - 265, 86, "EDITORIAL NOTE")
    canvas.setFont("Helvetica", 7.7)
    canvas.drawString(width - 265, 68, "No text block may silently disappear during reflow.")
    _footer(canvas, 1, (width, height))
    canvas.save()


def make_mixed_table_and_notes(path: Path) -> None:
    width, height = A4
    canvas = Canvas(str(path), pagesize=A4)
    _header(canvas, "Implementation Readiness Review", "Native 04 / table and dense notes")
    canvas.setFillColor(colors.HexColor("#27241f"))
    canvas.setFont("Helvetica", 8.5)
    y = _draw_wrapped(
        canvas,
        "This page combines a compact decision table with narrative findings, a colored warning box, and small footnotes. The translated document must retain every rule and background while keeping the explanatory text readable.",
        42,
        height - 108,
        width - 84,
        size=8.7,
        leading=11.2,
    )
    y -= 12
    table_top = y
    columns = [42, 190, 310, 420, 553]
    rows = [
        ("Capability", "Current state", "Risk", "Release decision"),
        ("Digital extraction", "Stable", "Low", "Approve"),
        ("Table reconstruction", "Partial", "High", "Hold"),
        ("Dense paragraph fitting", "Review", "Medium", "Conditional"),
        ("Background preservation", "Stable", "Low", "Approve"),
        ("Complex image repair", "Fallback", "Medium", "Conditional"),
    ]
    for row_index, row in enumerate(rows):
        baseline = table_top - row_index * 32
        canvas.setFillColor(
            colors.HexColor("#344762") if row_index == 0 else colors.HexColor("#f2eadf")
        )
        canvas.rect(42, baseline - 18, width - 84, 30, fill=1, stroke=0)
        canvas.setFillColor(colors.white if row_index == 0 else colors.HexColor("#24211d"))
        canvas.setFont("Helvetica-Bold" if row_index == 0 else "Helvetica", 7.8)
        for index, value in enumerate(row):
            canvas.drawString(columns[index] + 4, baseline - 8, value)
    canvas.setStrokeColor(colors.HexColor("#9f927f"))
    for x in columns:
        canvas.line(x, table_top + 12, x, table_top - len(rows) * 32 + 14)
    note_y = table_top - len(rows) * 32 - 16
    canvas.setFillColor(colors.HexColor("#f0d7d3"))
    canvas.roundRect(42, note_y - 82, width - 84, 82, 6, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor("#8f241e"))
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(56, note_y - 21, "RELEASE WARNING")
    canvas.setFillColor(colors.HexColor("#2c2822"))
    _draw_wrapped(
        canvas,
        "A table row that cannot fit must remain visible as a review issue. The renderer must not erase the source row and leave an empty cell behind.",
        56,
        note_y - 40,
        width - 112,
        size=8,
        leading=10,
    )
    canvas.setFillColor(colors.HexColor("#4f4a42"))
    canvas.setFont("Helvetica", 6.8)
    canvas.drawString(42, 48, "1. Risk ratings reflect the current prototype only. 2. Approval requires all severe issues to be acknowledged.")
    _footer(canvas, 1)
    canvas.save()


def make_dense_legal_pages(path: Path) -> None:
    width, height = A4
    canvas = Canvas(str(path), pagesize=A4)
    clauses = [
        "The processing system shall preserve page dimensions, reading order, ordinary annotations, embedded images, and visible table rules unless a documented fallback is required.",
        "Every translated segment shall retain a stable identifier so that retries, manual edits, terminology decisions, and quality findings remain attributable after a service interruption.",
        "No final artifact may be produced while an unresolved severe issue remains, including missing text, overflow, overlap, out-of-bounds placement, or a confirmed font coverage failure.",
        "The operator may export a marked draft for review, provided that the draft cannot reasonably be mistaken for an approved publication and all unresolved warnings remain accessible.",
        "Credentials, document passwords, and provider secrets shall not be written to application logs, diagnostic bundles, task metadata, or the local relational database.",
    ]
    clause_number = 1
    for page_number in range(1, 3):
        _header(canvas, "Document Processing Terms", f"Native 05 / dense legal text / page {page_number}")
        y = height - 108
        for local_index in range(10):
            clause = clauses[(clause_number - 1) % len(clauses)]
            canvas.setFillColor(colors.HexColor("#8f2922"))
            canvas.setFont("Helvetica-Bold", 7.8)
            canvas.drawString(42, y, f"{clause_number}.{local_index + 1}")
            canvas.setFillColor(colors.HexColor("#27241f"))
            y = _draw_wrapped(
                canvas,
                clause,
                78,
                y,
                width - 120,
                size=7.7,
                leading=9.4,
            )
            y -= 8
            clause_number += 1
        canvas.saveState()
        canvas.translate(width - 14, height / 2 - 70)
        canvas.rotate(90)
        canvas.setFillColor(colors.HexColor("#a92c25"))
        canvas.setFont("Helvetica-Bold", 6.5)
        canvas.drawString(0, 0, "CONFIDENTIAL REVIEW COPY")
        canvas.restoreState()
        _footer(canvas, page_number)
        if page_number == 1:
            canvas.showPage()
    canvas.save()


def create_cases() -> list[dict]:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = [
        ("native-01-colored-table", make_colored_table),
        ("native-02-dense-financial-table", make_dense_financial_table),
        ("native-03-three-column-report", make_three_column_report),
        ("native-04-table-and-notes", make_mixed_table_and_notes),
        ("native-05-dense-legal-pages", make_dense_legal_pages),
    ]
    result = []
    for slug, builder in cases:
        source = SOURCE_DIR / f"{slug}-source.pdf"
        builder(source)
        result.append({"slug": slug, "source": source})
    return result


def _drawing_counts(path: Path) -> list[int]:
    with pymupdf.open(path) as doc:
        return [len(page.get_drawings()) for page in doc]


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
                output_modes="translated",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        db.commit()

    started = datetime.now(UTC)
    process_job(job_id)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return render_result(case, job_id, elapsed)


def render_result(case: dict, job_id: str, elapsed: float) -> dict:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job is not None
        pages = list(db.scalars(select(Page).where(Page.job_id == job_id).order_by(Page.page_number)))
        segments = list(
            db.scalars(
                select(Segment)
                .where(Segment.job_id == job_id)
                .order_by(Segment.page_id, Segment.reading_order)
            )
        )
        if job.status != "review_required":
            return {
                "slug": case["slug"],
                "job_id": job_id,
                "status": job.status,
                "error": job.error,
                "elapsed_seconds": round(elapsed, 2),
            }
        artifact = render_artifact(db, job, "translated", final=False)
        output = OUTPUT_DIR / f"{case['slug']}-translated-draft.pdf"
        shutil.copy2(artifact.path, output)
        issues = list(db.scalars(select(QualityIssue).where(QualityIssue.job_id == job_id)))
        source_drawings = _drawing_counts(case["source"])
        output_drawings = _drawing_counts(output)
        return {
            "slug": case["slug"],
            "job_id": job_id,
            "status": job.status,
            "elapsed_seconds": round(elapsed, 2),
            "pages": job.page_count,
            "page_types": [page.page_type for page in pages],
            "segments": len(segments),
            "segment_statuses": dict(Counter(segment.status for segment in segments)),
            "issue_counts": dict(Counter(issue.code for issue in issues)),
            "unresolved_issues": unresolved_count(db, job_id),
            "source_drawing_counts": source_drawings,
            "output_drawing_counts": output_drawings,
            "drawing_counts_preserved": source_drawings == output_drawings,
            "sample_translations": [
                {"source": segment.source_text, "target": segment.target_text}
                for segment in segments
                if segment.status != "skipped"
            ][:6],
            "output_pdf": str(output),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rerender-existing",
        action="store_true",
        help="Reuse the job IDs in the existing JSON report without calling providers again.",
    )
    args = parser.parse_args()
    init_db()
    cases = create_cases()
    if args.rerender_existing:
        previous = {
            item["slug"]: item
            for item in json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        }
        results = [
            render_result(
                case,
                previous[case["slug"]]["job_id"],
                float(previous[case["slug"]].get("elapsed_seconds", 0)),
            )
            for case in cases
        ]
    else:
        results = [run_case(case) for case in cases]
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
