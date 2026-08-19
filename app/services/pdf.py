from __future__ import annotations

import hashlib
import io
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from pypdf import PdfReader
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.document_ir import InlineRun, ListItem, Paragraph, SegmentStructure, structure_to_json
from app.language import detect_language, is_translatable
from app.models import Job, Page, Segment
from app.providers.base import OCRProvider
from app.security import secret_store


class PDFValidationError(ValueError):
    pass


@dataclass(slots=True)
class PDFInspection:
    page_count: int
    encrypted: bool
    has_signature: bool
    has_form: bool
    has_attachments: bool


def _password_for(job_id: str) -> str:
    return secret_store.get(f"pdf-password:{job_id}") or ""


def inspect_pdf(path: Path, job_id: str) -> PDFInspection:
    settings = get_settings()
    if path.stat().st_size > settings.max_file_mb * 1024 * 1024:
        raise PDFValidationError(f"文件超过 {settings.max_file_mb}MB 限制")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PDFValidationError("文件不是有效 PDF")
    password = _password_for(job_id)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise PDFValidationError("PDF 已损坏或无法解析") from exc
    encrypted = bool(doc.needs_pass)
    if encrypted and (not password or doc.authenticate(password) <= 0):
        doc.close()
        raise PDFValidationError("PDF 已加密，请提供正确的打开密码")
    if len(doc) > settings.max_pages:
        doc.close()
        raise PDFValidationError(f"PDF 超过 {settings.max_pages} 页限制")
    if len(doc) == 0:
        doc.close()
        raise PDFValidationError("PDF 没有页面")
    has_attachments = bool(doc.embfile_names())
    page_count = len(doc)
    doc.close()

    has_form = False
    has_signature = False
    try:
        reader = PdfReader(path, password=password or None, strict=False)
        fields = reader.get_fields() or {}
        has_signature = any(str(field.get("/FT")) == "/Sig" for field in fields.values())
        has_form = any(str(field.get("/FT")) != "/Sig" for field in fields.values())
    except Exception:
        # PyMuPDF already validated the document; pypdf is only used for feature inspection.
        pass
    return PDFInspection(
        page_count=page_count,
        encrypted=encrypted,
        has_signature=has_signature,
        has_form=has_form,
        has_attachments=has_attachments,
    )


def _bbox_from_block(block: dict) -> list[float]:
    return [round(float(value), 3) for value in block["bbox"]]


def _block_text(block: dict) -> str:
    lines: list[str] = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        if text.strip():
            lines.append(text.rstrip())
    return "\n".join(lines).strip()


def _line_text(line: dict) -> str:
    return "".join(span.get("text", "") for span in line.get("spans", [])).strip()


def _rect_union(rects: list[list[float]]) -> list[float]:
    return [
        round(min(rect[0] for rect in rects), 3),
        round(min(rect[1] for rect in rects), 3),
        round(max(rect[2] for rect in rects), 3),
        round(max(rect[3] for rect in rects), 3),
    ]


def _style_key(span: dict) -> tuple[str | None, float, int, bool, bool]:
    flags = int(span.get("flags", 0))
    return (
        span.get("font"),
        round(float(span.get("size", 11.0)), 3),
        int(span.get("color", 0)),
        bool(flags & 16),
        bool(flags & 2),
    )


def _runs_for_lines(lines: list[dict], element_id: str) -> list[InlineRun]:
    runs: list[InlineRun] = []
    for line_index, line in enumerate(lines):
        meaningful = [span for span in line.get("spans", []) if span.get("text", "").strip()]
        for span_index, span in enumerate(meaningful):
            text = span.get("text", "")
            if line_index > 0 and span_index == 0 and runs and not runs[-1].source_text.endswith((" ", "\n")):
                text = " " + text.lstrip()
            bbox = [round(float(value), 3) for value in span.get("bbox", line["bbox"])]
            key = _style_key(span)
            if runs:
                previous = runs[-1]
                previous_key = (
                    previous.font_name,
                    round(float(previous.font_size or 11.0), 3),
                    int(previous.font_color or 0),
                    previous.bold,
                    previous.italic,
                )
                if previous_key == key:
                    previous.source_text += text
                    previous.bbox = _rect_union([previous.bbox, bbox])
                    continue
            runs.append(
                InlineRun(
                    id=f"{element_id}-r{len(runs)}",
                    source_text=text,
                    bbox=bbox,
                    font_name=key[0],
                    font_size=key[1],
                    font_color=key[2],
                    bold=key[3],
                    italic=key[4],
                )
            )
    return runs


def _bullet_marker_rects(page) -> list[list[float]]:
    markers: list[list[float]] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None or drawing.get("fill") is None:
            continue
        width, height = float(rect.width), float(rect.height)
        if 1.5 <= width <= 8.0 and 1.5 <= height <= 8.0 and 0.55 <= width / height <= 1.8:
            markers.append([float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)])
    return markers


def _marker_for_line(line: dict, markers: list[list[float]]) -> list[float] | None:
    bbox = [float(value) for value in line["bbox"]]
    line_center = (bbox[1] + bbox[3]) / 2
    candidates = [
        marker
        for marker in markers
        if marker[2] <= bbox[0]
        and bbox[0] - marker[2] <= 24.0
        and bbox[1] - 3.0 <= (marker[1] + marker[3]) / 2 <= bbox[3] + 3.0
    ]
    return min(candidates, key=lambda marker: abs((marker[1] + marker[3]) / 2 - line_center)) if candidates else None


def _first_span(line: dict) -> dict:
    return next((span for span in line.get("spans", []) if span.get("text", "").strip()), {})


def _semantic_block_units(
    block: dict,
    markers: list[list[float]],
) -> list[tuple[str, list[float], dict, str, str]]:
    raw_lines = block.get("lines", [])
    nonempty_lines = [line for line in raw_lines if _line_text(line)]
    if not nonempty_lines:
        return []
    heights = [float(line["bbox"][3]) - float(line["bbox"][1]) for line in nonempty_lines]
    typical_height = statistics.median(heights) if heights else 11.0
    groups: list[tuple[list[dict], list[float] | None]] = []
    current: list[dict] = []
    current_marker: list[float] | None = None
    previous_line: dict | None = None

    def flush() -> None:
        nonlocal current, current_marker
        if current:
            groups.append((current, current_marker))
        current, current_marker = [], None

    for line in raw_lines:
        if not _line_text(line):
            flush()
            previous_line = None
            continue
        marker = _marker_for_line(line, markers)
        first = _first_span(line)
        starts_bold = bool(int(first.get("flags", 0)) & 16)
        large_gap = False
        if previous_line is not None:
            gap = float(line["bbox"][1]) - float(previous_line["bbox"][3])
            large_gap = gap > typical_height * 0.45
        if current and (marker is not None or starts_bold or large_gap):
            flush()
        if not current:
            current_marker = marker
        current.append(line)
        previous_line = line
    flush()

    units: list[tuple[str, list[float], dict, str, str]] = []
    for element_index, (lines, marker) in enumerate(groups):
        element_id = f"e{element_index}"
        runs = _runs_for_lines(lines, element_id)
        if not runs:
            continue
        content_bbox = _rect_union(
            [[float(value) for value in line["bbox"]] for line in lines]
        )
        if marker is not None:
            bbox = _rect_union([content_bbox, marker])
            element = ListItem(
                id=element_id,
                bbox=bbox,
                runs=runs,
                marker_bbox=[round(value, 3) for value in marker],
                marker_drawn=True,
                content_indent=max(0.0, content_bbox[0] - bbox[0]),
            )
            kind = "list_item"
        else:
            bbox = content_bbox
            element = Paragraph(id=element_id, bbox=bbox, runs=runs)
            largest_size = max(float(run.font_size or 0) for run in runs)
            kind = "title" if largest_size >= 11.5 and all(run.bold for run in runs) else "paragraph"
        structure = SegmentStructure(elements=[element])
        units.append(
            (
                "".join(run.source_text for run in runs).strip(),
                bbox,
                _first_span(lines[0]),
                kind,
                structure_to_json(structure),
            )
        )
    return units


def _native_block_units(
    block: dict,
    page_width: float,
    page_height: float,
    markers: list[list[float]] | None = None,
) -> list[tuple[str, list[float], dict, str, str]]:
    markers = markers or []
    """Split table-like blocks whose visual lines share one horizontal baseline."""
    lines = [line for line in block.get("lines", []) if _line_text(line)]
    if len(lines) >= 2:
        bboxes = [[float(value) for value in line["bbox"]] for line in lines]
        sizes = [
            float(span.get("size", 0.0))
            for line in lines
            for span in line.get("spans", [])
            if span.get("text", "").strip()
        ]
        centers = [(bbox[1] + bbox[3]) / 2 for bbox in bboxes]
        baseline_tolerance = max(3.0, (max(sizes) if sizes else 8.0) * 0.75)
        horizontal = all(abs(float(line.get("dir", (1, 0))[1])) < 0.1 for line in lines)
        ordered = sorted(zip(lines, bboxes, strict=True), key=lambda item: item[1][0])
        separated = all(
            ordered[index + 1][1][0] - ordered[index][1][2] >= 3.0
            for index in range(len(ordered) - 1)
        )
        if horizontal and separated and max(centers) - min(centers) <= baseline_tolerance:
            starts = [bbox[0] for _line, bbox in ordered]
            start_gaps = [starts[index + 1] - starts[index] for index in range(len(starts) - 1)]
            typical_gap = statistics.median(start_gaps) if start_gaps else 72.0
            row_y0 = max(0.0, min(bbox[1] for _line, bbox in ordered) - 1.5)
            row_y1 = min(page_height, max(bbox[3] for _line, bbox in ordered) + 2.5)
            block_x1 = float(block["bbox"][2])
            units: list[tuple[str, list[float], dict, str, str]] = []
            for index, (line, bbox) in enumerate(ordered):
                if index + 1 < len(ordered):
                    cell_x1 = ordered[index + 1][1][0] - 3.0
                else:
                    cell_x1 = min(page_width - 4.0, block_x1 + typical_gap * 0.5)
                cell_x1 = max(cell_x1, bbox[2] + 2.0)
                first_span = next(
                    (
                        span
                        for span in line.get("spans", [])
                        if span.get("text", "").strip()
                    ),
                    {},
                )
                cell_bbox = [
                    round(bbox[0], 3),
                    round(row_y0, 3),
                    round(cell_x1, 3),
                    round(row_y1, 3),
                ]
                element_id = f"e{index}"
                structure = SegmentStructure(
                    elements=[
                        Paragraph(
                            id=element_id,
                            bbox=cell_bbox,
                            runs=_runs_for_lines([line], element_id),
                        )
                    ]
                )
                units.append(
                    (
                        _line_text(line),
                        cell_bbox,
                        first_span,
                        "table_cell",
                        structure_to_json(structure),
                    )
                )
            return units
    return _semantic_block_units(block, markers)


def _segment_key(page_number: int, order: int, text: str, bbox: list[float]) -> str:
    material = json.dumps([page_number, order, text, bbox], ensure_ascii=False).encode()
    return f"p{page_number}-{hashlib.sha256(material).hexdigest()[:20]}"


def extract_document(db: Session, job: Job) -> None:
    existing = db.scalar(select(Page.id).where(Page.job_id == job.id).limit(1))
    if existing is not None:
        return
    source = Path(job.source_path)
    password = _password_for(job.id)
    doc = pymupdf.open(source)
    if doc.needs_pass and doc.authenticate(password) <= 0:
        raise PDFValidationError("无法使用保存的密码打开 PDF")
    job_dir = source.parent
    previews_dir = job_dir / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    for page_index, pdf_page in enumerate(doc):
        rect = pdf_page.rect
        if rect.width * 2 * rect.height * 2 > settings.max_page_pixels:
            raise PDFValidationError(f"第 {page_index + 1} 页渲染尺寸异常")
        raw = pdf_page.get_text("dict", sort=True)
        blocks = [block for block in raw.get("blocks", []) if block.get("type") == 0]
        extracted = [(_block_text(block), block) for block in blocks]
        char_count = sum(len(text.strip()) for text, _block in extracted)
        page_type = "native" if char_count >= 20 else "scanned"
        pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
        preview_path = previews_dir / f"page-{page_index + 1:04d}.png"
        pix.save(preview_path)
        page_row = Page(
            job_id=job.id,
            page_number=page_index + 1,
            width=rect.width,
            height=rect.height,
            rotation=pdf_page.rotation,
            page_type=page_type,
            preview_path=str(preview_path),
            extraction_hash=hashlib.sha256(pix.samples).hexdigest(),
            status="extracted" if page_type == "native" else "ocr_pending",
        )
        db.add(page_row)
        db.flush()
        if page_type == "native":
            bullet_markers = _bullet_marker_rects(pdf_page)
            order = 0
            for text, block in extracted:
                if not text:
                    continue
                for unit_text, bbox, first_span, kind, structure_json in _native_block_units(
                    block, rect.width, rect.height, bullet_markers
                ):
                    language, confidence = detect_language(unit_text)
                    segment = Segment(
                        job_id=job.id,
                        page_id=page_row.id,
                        segment_key=_segment_key(page_index + 1, order, unit_text, bbox),
                        kind=kind,
                        reading_order=order,
                        bbox_json=json.dumps(bbox),
                        source_language=language,
                        source_text=unit_text,
                        structure_json=structure_json,
                        font_name=first_span.get("font"),
                        font_size=float(first_span.get("size", 11.0)),
                        font_color=int(first_span.get("color", 0)),
                        confidence=confidence,
                        status="pending" if is_translatable(unit_text) else "skipped",
                        target_text=unit_text if not is_translatable(unit_text) else None,
                    )
                    db.add(segment)
                    order += 1
        db.commit()
    doc.close()


def ocr_scanned_page(db: Session, job: Job, page: Page, provider: OCRProvider) -> None:
    if page.status == "ocr_complete":
        return
    preview = Path(page.preview_path or "")
    if not preview.exists():
        raise PDFValidationError(f"第 {page.page_number} 页缺少 OCR 预览")
    image_bytes = preview.read_bytes()
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as image:
        image_width, image_height = image.size
    items = provider.analyze(image_bytes)
    db.execute(delete(Segment).where(Segment.page_id == page.id))
    for order, item in enumerate(items):
        xs = item.polygon[0::2]
        ys = item.polygon[1::2]
        bbox = [
            min(xs) * page.width / image_width,
            min(ys) * page.height / image_height,
            max(xs) * page.width / image_width,
            max(ys) * page.height / image_height,
        ]
        language, language_confidence = detect_language(item.text)
        db.add(
            Segment(
                job_id=job.id,
                page_id=page.id,
                segment_key=_segment_key(page.page_number, order, item.text, bbox),
                kind=item.kind,
                reading_order=order,
                bbox_json=json.dumps([round(value, 3) for value in bbox]),
                polygon_json=json.dumps(item.polygon),
                source_language=language,
                source_text=item.text,
                font_size=10.0,
                font_color=0,
                confidence=item.confidence if item.confidence is not None else language_confidence,
                status="pending" if is_translatable(item.text) else "skipped",
                target_text=item.text if not is_translatable(item.text) else None,
            )
        )
    page.status = "ocr_complete"
    db.commit()
