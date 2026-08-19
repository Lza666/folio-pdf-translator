import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.services.renderer as renderer_service
from app.db import Base
from app.models import Job, Page, QualityIssue, Segment
from app.security import hash_token
from app.services.compaction import compact_overflow_translations
from app.services.pdf import _native_block_units, extract_document, inspect_pdf
from app.services.quality import run_quality_checks
from app.services.renderer import FinalQualityGateError, _background_repair, render_artifact


class ShortTranslationProvider:
    def __init__(self) -> None:
        self.calls = 0

    def translate(self, items, target_language, terms):
        self.calls += 1
        return {item.id: "精简译文" for item in items}


def make_job(sample_pdf: Path) -> Job:
    return Job(
        id="test-job",
        access_token_hash=hash_token("token"),
        source_filename="sample.pdf",
        source_path=str(sample_pdf),
        target_language="zh-Hans",
        output_modes="translated,bilingual",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


def test_extract_and_render(sample_pdf: Path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(sample_pdf)
        db.add(job)
        db.commit()
        inspection = inspect_pdf(sample_pdf, job.id)
        assert inspection.page_count == 1
        extract_document(db, job)
        segments = list(db.scalars(select(Segment).where(Segment.job_id == job.id)))
        assert segments
        for segment in segments:
            if segment.status != "skipped":
                segment.target_text = "已翻译的文本。"
                segment.confirmed = True
                segment.status = "edited"
        db.commit()
        run_quality_checks(db, job)
        translated = render_artifact(db, job, "translated", final=False)
        bilingual = render_artifact(db, job, "bilingual", final=False)
        assert Path(translated.path).exists()
        assert Path(bilingual.path).exists()
        translated_doc = pymupdf.open(translated.path)
        bilingual_doc = pymupdf.open(bilingual.path)
        source_doc = pymupdf.open(sample_pdf)
        assert len(translated_doc) == len(source_doc) == len(bilingual_doc) == 1
        assert bilingual_doc[0].rect.width > source_doc[0].rect.width * 1.9
        assert "DRAFT" not in translated_doc[0].get_text()
        assert "校样" not in translated_doc[0].get_text()


def test_rendered_pdf_removes_embedded_files(sample_pdf: Path):
    embedded = sample_pdf.with_name("embedded.pdf")
    doc = pymupdf.open(sample_pdf)
    doc.embfile_add("note.txt", b"untrusted attachment")
    doc.save(embedded)
    doc.close()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(embedded)
        job.id = "embedded-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        for segment in db.scalars(select(Segment).where(Segment.job_id == job.id)):
            if segment.status != "skipped":
                segment.target_text = "Translated text."
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        rendered = pymupdf.open(artifact.path)
        assert rendered.embfile_names() == []


def test_cjk_output_embeds_readable_font(sample_pdf: Path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(sample_pdf)
        job.id = "cjk-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        for segment in db.scalars(select(Segment).where(Segment.job_id == job.id)):
            if segment.status != "skipped":
                segment.target_text = "中文翻译测试"
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        rendered = pymupdf.open(artifact.path)
        assert "中文翻译测试" in rendered[0].get_text()
        assert any(font[4] == "foliofont" for font in rendered[0].get_fonts())


def test_variable_cjk_font_is_pinned_to_regular_weight():
    source = Path("C:/Windows/Fonts/NotoSansSC-VF.ttf")
    if not source.is_file():
        return
    regular = renderer_service._regular_font_instance(source)
    assert regular != source
    font = renderer_service.TTFont(regular)
    try:
        assert "fvar" not in font
        assert font["OS/2"].usWeightClass == 400
        assert font["name"].getDebugName(2) == "Regular"
    finally:
        font.close()


def test_final_output_blocks_unacknowledged_warning(sample_pdf: Path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(sample_pdf)
        job.id = "quality-gate-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        for segment in db.scalars(select(Segment).where(Segment.job_id == job.id)):
            if segment.status != "skipped":
                segment.target_text = segment.source_text
        db.commit()
        run_quality_checks(db, job)
        import pytest

        with pytest.raises(FinalQualityGateError, match="质量问题"):
            render_artifact(db, job, "translated", final=True)


def test_native_colored_background_remains_visible(tmp_path: Path):
    source = tmp_path / "colored-native.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=300)
    panel = pymupdf.Rect(48, 62, 547, 190)
    panel_color = (0.10, 0.34, 0.58)
    page.draw_rect(panel, color=None, fill=panel_color)
    page.insert_textbox(
        pymupdf.Rect(72, 92, 520, 145),
        "A clean final document must preserve its colored background.",
        fontsize=16,
        fontname="helv",
        color=(1, 1, 1),
    )
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(source)
        job.id = "colored-native-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segments = list(db.scalars(select(Segment).where(Segment.job_id == job.id)))
        assert segments
        for segment in segments:
            segment.target_text = "彩色背景应当完整保留。"
            segment.status = "edited"
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        artifact_path = artifact.path

    rendered = pymupdf.open(artifact_path)
    assert "彩色背景应当完整保留" in rendered[0].get_text()
    assert "A clean final document" not in rendered[0].get_text()
    pix = rendered[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    panel_pixels = pixels[124:380, 96:1094]
    expected = np.array(panel_color) * 255
    distances = np.linalg.norm(panel_pixels.astype(np.float32) - expected, axis=2)
    white = np.all(panel_pixels > 245, axis=2)
    assert float(np.mean(distances < 12)) > 0.70
    assert float(np.mean(white)) < 0.02


def test_scanned_gradient_background_uses_inpaint_patch(tmp_path: Path):
    width, height = 600, 300
    x = np.linspace(0, 1, width, dtype=np.float32)
    image_array = np.zeros((height, width, 3), dtype=np.uint8)
    image_array[:, :, 0] = (35 + 70 * x).astype(np.uint8)
    image_array[:, :, 1] = (95 + 85 * x).astype(np.uint8)
    image_array[:, :, 2] = (155 + 55 * x).astype(np.uint8)
    image = Image.fromarray(image_array)
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 28) if font_path.exists() else ImageFont.load_default()
    draw.text((82, 105), "SOURCE TEXT ON A GRADIENT", font=font, fill=(235, 35, 35))
    image_stream = io.BytesIO()
    image.save(image_stream, format="PNG")

    source = tmp_path / "scanned-gradient.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_image(page.rect, stream=image_stream.getvalue())
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(source)
        job.id = "scanned-gradient-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        page_row = db.scalar(select(Page).where(Page.job_id == job.id))
        assert page_row is not None and page_row.page_type == "scanned"
        bbox = [78.0, 96.0, 500.0, 145.0]
        segment = Segment(
            job_id=job.id,
            page_id=page_row.id,
            segment_key="gradient-segment",
            kind="paragraph",
            reading_order=0,
            bbox_json=json.dumps(bbox),
            polygon_json=json.dumps([109.2, 134.4, 700.0, 134.4, 700.0, 203.0, 109.2, 203.0]),
            source_language="en",
            source_text="SOURCE TEXT ON A GRADIENT",
            target_text="渐变背景上的译文",
            font_size=18,
            font_color=0,
            confidence=0.99,
            status="edited",
        )
        db.add(segment)
        db.commit()
        repair = _background_repair(page_row, segment, pymupdf.Rect(bbox))
        assert repair.fill is False
        assert repair.patch_rect is not None and repair.patch_png
        artifact = render_artifact(db, job, "translated", final=False)
        artifact_path = artifact.path

    rendered = pymupdf.open(artifact_path)
    pix = rendered[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    region = pixels[190:300, 150:1020]
    red_source_pixels = (region[:, :, 0] > 190) & (region[:, :, 1] < 90) & (region[:, :, 2] < 90)
    white = np.all(region > 245, axis=2)
    assert float(np.mean(red_source_pixels)) < 0.0005
    assert float(np.mean(white)) < 0.01


def test_native_table_block_is_split_into_independent_cells():
    block = {
        "bbox": [42.0, 72.0, 520.0, 84.0],
        "lines": [
            {
                "bbox": [42.0, 72.0, 112.0, 84.0],
                "dir": [1.0, 0.0],
                "spans": [{"text": "Product", "size": 8.0, "font": "Helvetica"}],
            },
            {
                "bbox": [190.0, 72.0, 246.0, 84.0],
                "dir": [1.0, 0.0],
                "spans": [{"text": "Revenue", "size": 8.0, "font": "Helvetica"}],
            },
            {
                "bbox": [410.0, 72.0, 454.0, 84.0],
                "dir": [1.0, 0.0],
                "spans": [{"text": "Growth", "size": 8.0, "font": "Helvetica"}],
            },
        ],
    }

    units = _native_block_units(block, page_width=595.0, page_height=842.0)

    assert [unit[0] for unit in units] == ["Product", "Revenue", "Growth"]
    assert all(unit[3] == "table_cell" for unit in units)
    assert units[0][1][2] < units[1][1][0]
    assert units[1][1][2] < units[2][1][0]


def test_unfit_translation_preserves_native_source_text(tmp_path: Path):
    source = tmp_path / "overflow-native.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=180)
    page.insert_text((30, 58), "Original source stays visible", fontsize=10, fontname="helv")
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(source)
        job.id = "overflow-preserves-source-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segment = db.scalar(select(Segment).where(Segment.job_id == job.id))
        assert segment is not None
        segment.target_text = "这是无法容纳的超长译文" * 30
        segment.status = "edited"
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        issue = db.scalar(
            select(QualityIssue).where(
                QualityIssue.job_id == job.id,
                QualityIssue.segment_id == segment.id,
                QualityIssue.code == "overflow",
            )
        )
        assert issue is not None
        artifact_path = artifact.path

    rendered = pymupdf.open(artifact_path)
    text = rendered[0].get_text()
    assert "Original source stays visible" in text
    assert "这是无法容纳的超长译文" not in text


def test_ai_compaction_retranslates_only_after_safe_layouts_fail(tmp_path: Path):
    source = tmp_path / "ai-compaction.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=180)
    page.insert_text((30, 58), "Compact source for constrained translation", fontsize=10)
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    provider = ShortTranslationProvider()
    with Session(engine) as db:
        job = make_job(source)
        job.id = "ai-compaction-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segment = db.scalar(select(Segment).where(Segment.job_id == job.id))
        assert segment is not None
        segment.target_text = "这是一段在缩小字体并安全扩展文本框后仍然完全无法容纳的机器译文" * 20
        segment.status = "translated"
        db.commit()

        changed = compact_overflow_translations(db, job, provider)

        assert changed == 1
        assert provider.calls == 1
        assert segment.target_text == "精简译文"
        assert segment.status == "ai_compacted"
        issue = db.scalar(
            select(QualityIssue).where(
                QualityIssue.segment_id == segment.id,
                QualityIssue.code == "ai_compacted",
            )
        )
        assert issue is not None
        assert renderer_service.find_compression_candidates(db, job) == []


def test_ai_compaction_never_rewrites_confirmed_translation(tmp_path: Path):
    source = tmp_path / "confirmed-compaction.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=180)
    page.insert_text((30, 58), "Confirmed source translation", fontsize=10)
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    provider = ShortTranslationProvider()
    with Session(engine) as db:
        job = make_job(source)
        job.id = "confirmed-compaction-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segment = db.scalar(select(Segment).where(Segment.job_id == job.id))
        assert segment is not None
        original = "这是一段已经由人工确认且不能被模型自动改写的超长译文" * 20
        segment.target_text = original
        segment.status = "edited"
        segment.confirmed = True
        db.commit()

        changed = compact_overflow_translations(db, job, provider)

        assert changed == 0
        assert provider.calls == 0
        assert segment.target_text == original


def test_native_text_below_seven_points_can_be_replaced(tmp_path: Path):
    source = tmp_path / "small-font-native.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=180)
    page.insert_text(
        (30, 58),
        "Small source text needs translation",
        fontsize=6.5,
        fontname="helv",
    )
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(source)
        job.id = "small-font-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segment = db.scalar(select(Segment).where(Segment.job_id == job.id))
        assert segment is not None
        assert segment.font_size is not None and segment.font_size < 7
        segment.target_text = "小字译文"
        segment.status = "edited"
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        artifact_path = artifact.path

    rendered = pymupdf.open(artifact_path)
    text = rendered[0].get_text()
    assert "小字译文" in text
    assert "Small source text needs translation" not in text


def test_model_line_breaks_are_reflowed_inside_native_paragraph(tmp_path: Path):
    source = tmp_path / "model-line-breaks.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=180)
    page.insert_textbox(
        pymupdf.Rect(30, 48, 270, 72),
        "The source paragraph occupies two compact lines.\nIts translation should reflow safely.",
        fontsize=7.8,
        fontname="helv",
    )
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = make_job(source)
        job.id = "model-line-breaks-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segment = db.scalar(select(Segment).where(Segment.job_id == job.id))
        assert segment is not None
        segment.target_text = "1.1\n处理系统应保留页面尺寸，\n并允许自动重新换行。"
        segment.status = "edited"
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        overflow = db.scalar(
            select(QualityIssue).where(
                QualityIssue.job_id == job.id,
                QualityIssue.segment_id == segment.id,
                QualityIssue.code == "overflow",
            )
        )
        assert overflow is None
        artifact_path = artifact.path

    rendered = pymupdf.open(artifact_path)
    text = rendered[0].get_text()
    assert "系统应保留页面尺寸" in text
    assert "source paragraph" not in text


def test_native_overflow_expands_around_text_and_vector_obstacles(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "four-direction-expansion.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=260)
    page.insert_textbox(
        pymupdf.Rect(180, 110, 260, 124),
        "Compact source",
        fontsize=7.5,
        fontname="helv",
    )
    page.insert_textbox(
        pymupdf.Rect(282, 105, 408, 126),
        "RIGHT BLOCKER MUST REMAIN",
        fontsize=7.5,
        fontname="helv",
    )
    page.insert_textbox(
        pymupdf.Rect(172, 140, 350, 158),
        "BOTTOM BLOCKER MUST REMAIN",
        fontsize=7.5,
        fontname="helv",
    )
    page.draw_line(
        pymupdf.Point(274, 96),
        pymupdf.Point(274, 132),
        color=(0.7, 0.2, 0.2),
        width=1.5,
    )
    page.draw_line(
        pymupdf.Point(162, 134),
        pymupdf.Point(360, 134),
        color=(0.7, 0.2, 0.2),
        width=1.5,
    )
    doc.save(source)
    doc.close()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    measurement_calls = 0
    required_text_height = renderer_service._required_text_height

    def count_measurements(*args, **kwargs):
        nonlocal measurement_calls
        measurement_calls += 1
        return required_text_height(*args, **kwargs)

    monkeypatch.setattr(renderer_service, "_required_text_height", count_measurements)
    with Session(engine) as db:
        job = make_job(source)
        job.id = "four-direction-expansion-job"
        db.add(job)
        db.commit()
        extract_document(db, job)
        segment = db.scalar(
            select(Segment).where(
                Segment.job_id == job.id,
                Segment.source_text == "Compact source",
            )
        )
        assert segment is not None
        original_rect = pymupdf.Rect(json.loads(segment.bbox_json))
        segment.target_text = "这是一个必须通过智能扩展文本框才能完整显示且不能覆盖相邻内容的译文"
        segment.status = "edited"
        db.commit()
        artifact = render_artifact(db, job, "translated", final=False)
        overflow = db.scalar(
            select(QualityIssue).where(
                QualityIssue.job_id == job.id,
                QualityIssue.segment_id == segment.id,
                QualityIssue.code == "overflow",
            )
        )
        assert overflow is None
        artifact_path = artifact.path

    rendered = pymupdf.open(artifact_path)
    page = rendered[0]
    text = page.get_text()
    assert "智能扩展文本框" in text
    assert "Compact source" not in text
    assert "RIGHT BLOCKER MUST REMAIN" in text
    assert "BOTTOM BLOCKER MUST REMAIN" in text
    first_words = page.search_for("这是一个")
    assert first_words
    assert first_words[0].x0 < original_rect.x0 or first_words[0].y0 < original_rect.y0
    assert 0 < measurement_calls <= 16


def test_font_size_probes_do_not_accumulate_failed_layouts(sample_pdf: Path):
    job = make_job(sample_pdf)
    segment = Segment(
        job_id=job.id,
        page_id=1,
        segment_key="isolated-font-probe",
        bbox_json=json.dumps([0.0, 0.0, 171.171, 10.58]),
        source_text="No text block may silently disappear during reflow.",
        target_text="在重新排版过程中,任何文本块都不应悄无声息地消失。",
        font_size=7.7,
        font_color=0,
        status="edited",
    )
    fitted = renderer_service._fit_text_size(
        pymupdf.Rect(0.0, 0.0, 171.171, 10.58),
        segment,
        "foliofont",
        renderer_service._font_file(job),
    )
    assert fitted is not None
    assert fitted < 7.7
