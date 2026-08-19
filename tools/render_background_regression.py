from __future__ import annotations

import io
import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal
from app.models import Job, Page, Segment
from app.security import hash_token
from app.services.pdf import extract_document
from app.services.renderer import render_artifact

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf"
WORK = ROOT / "tmp" / "pdfs" / "background-regression"


def render_native_regression() -> Path:
    with SessionLocal() as db:
        job = db.scalar(
            select(Job)
            .where(Job.source_filename == "test-03-layout-source.pdf")
            .order_by(Job.created_at.desc())
        )
        if job is None:
            raise RuntimeError("Run tools/run_e2e_translation_tests.py before this regression renderer")
        artifact = render_artifact(db, job, "translated", final=False)
        destination = OUTPUT / "test-03-layout-translated-background-fixed.pdf"
        shutil.copyfile(artifact.path, destination)
        return destination


def _create_gradient_source(path: Path) -> None:
    width, height = 600, 300
    x = np.linspace(0, 1, width, dtype=np.float32)
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[:, :, 0] = (35 + 70 * x).astype(np.uint8)
    pixels[:, :, 1] = (95 + 85 * x).astype(np.uint8)
    pixels[:, :, 2] = (155 + 55 * x).astype(np.uint8)
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 28) if font_path.exists() else ImageFont.load_default()
    draw.text((82, 105), "SOURCE TEXT ON A GRADIENT", font=font, fill=(235, 35, 35))
    stream = io.BytesIO()
    image.save(stream, format="PNG")

    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_image(page.rect, stream=stream.getvalue())
    doc.save(path)
    doc.close()


def render_scanned_regression() -> Path:
    source = WORK / "scanned-gradient-source.pdf"
    _create_gradient_source(source)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(
            id=str(uuid.uuid4()),
            access_token_hash=hash_token("background-regression"),
            source_filename=source.name,
            source_path=str(source),
            target_language="zh-Hans",
            output_modes="translated",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(job)
        db.commit()
        extract_document(db, job)
        page_row = db.scalar(select(Page).where(Page.job_id == job.id))
        if page_row is None:
            raise RuntimeError("Scanned regression page was not extracted")
        bbox = [78.0, 96.0, 500.0, 145.0]
        db.add(
            Segment(
                job_id=job.id,
                page_id=page_row.id,
                segment_key="gradient-segment",
                kind="paragraph",
                reading_order=0,
                bbox_json=json.dumps(bbox),
                polygon_json=json.dumps(
                    [109.2, 134.4, 700.0, 134.4, 700.0, 203.0, 109.2, 203.0]
                ),
                source_language="en",
                source_text="SOURCE TEXT ON A GRADIENT",
                target_text="渐变背景上的译文",
                font_size=18,
                font_color=0,
                confidence=0.99,
                status="edited",
            )
        )
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        destination = OUTPUT / "scanned-gradient-translated-background-fixed.pdf"
        shutil.copyfile(artifact.path, destination)
        return destination


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    for path in (render_native_regression(), render_scanned_regression()):
        print(path)


if __name__ == "__main__":
    main()
