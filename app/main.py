from __future__ import annotations

import asyncio
import csv
import io
import json
import shutil
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import __version__
from app.config import get_settings
from app.db import engine, get_db, init_db
from app.document_ir import structure_as_api_value
from app.memory import export_memory_csv, export_memory_tmx, fuzzy_matches, upsert_memory
from app.models import (
    Artifact,
    Job,
    JobStatus,
    Page,
    QualityIssue,
    Segment,
    TermEntry,
    TranslationMemory,
)
from app.schemas import (
    LANGUAGES,
    ArtifactOut,
    HealthOut,
    JobCreated,
    JobOut,
    MemoryCreate,
    MemoryOut,
    PageOut,
    ProviderSettings,
    ProviderSettingsUpdate,
    ProviderTestRequest,
    ProviderTestResult,
    RenderRequest,
    SegmentOut,
    SegmentUpdate,
    TermCreate,
)
from app.security import create_access_token, hash_token, secret_store, verify_token
from app.services.jobs import cleanup_expired_jobs, enqueue_job, queue_health
from app.services.quality import run_quality_checks, unresolved_count
from app.services.renderer import FinalQualityGateError, render_artifact
from app.services.settings import (
    build_ocr,
    build_translator,
    get_provider_settings,
    update_provider_settings,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    cleanup_expired_jobs()

    async def cleanup_loop() -> None:
        while True:
            await asyncio.sleep(3600)
            cleanup_expired_jobs()

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(
    title="Folio Translator",
    version=__version__,
    description="Local-first multilingual PDF translation workbench",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _token(request: Request, query_token: str | None, header_token: str | None) -> str:
    return query_token or header_token or request.cookies.get("folio_job_token", "")


def authorized_job(
    db: Session,
    job_id: str,
    request: Request,
    token: str | None = None,
    x_job_token: str | None = None,
) -> Job:
    job = db.get(Job, job_id)
    supplied = _token(request, token, x_job_token)
    if job is None or not supplied or not verify_token(supplied, job.access_token_hash):
        raise HTTPException(status_code=404, detail="任务不存在或访问令牌无效")
    if job.status == JobStatus.expired.value:
        raise HTTPException(status_code=410, detail="任务文件已过期")
    return job


def _job_out(db: Session, job: Job) -> JobOut:
    artifacts = list(db.scalars(select(Artifact).where(Artifact.job_id == job.id)))
    return JobOut.model_validate(
        {
            **{column.name: getattr(job, column.name) for column in Job.__table__.columns},
            "unresolved_issues": unresolved_count(db, job.id),
            "artifacts": [ArtifactOut.model_validate(item) for item in artifacts],
        }
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"languages": LANGUAGES, "max_file_mb": settings.max_file_mb, "max_pages": settings.max_pages},
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str, request: Request, token: str = Query(""), db: Session = Depends(get_db)):
    job = authorized_job(db, job_id, request, token)
    response = templates.TemplateResponse(request, "job.html", {"job": job, "token": token})
    response.set_cookie("folio_job_token", token, httponly=True, samesite="strict", max_age=604800)
    return response


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
def review_page(job_id: str, request: Request, token: str = Query(""), db: Session = Depends(get_db)):
    job = authorized_job(db, job_id, request, token)
    response = templates.TemplateResponse(
        request,
        "review.html",
        {"job": job, "token": token, "target_name": LANGUAGES.get(job.target_language, job.target_language)},
    )
    response.set_cookie("folio_job_token", token, httponly=True, samesite="strict", max_age=604800)
    return response


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "settings.html", {"settings": get_provider_settings(db)})


@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request, db: Session = Depends(get_db)):
    memories = list(db.scalars(select(TranslationMemory).order_by(TranslationMemory.confirmed_at.desc()).limit(100)))
    terms = list(db.scalars(select(TermEntry).order_by(TermEntry.created_at.desc()).limit(100)))
    return templates.TemplateResponse(
        request, "memory.html", {"memories": memories, "terms": terms, "languages": LANGUAGES}
    )


@app.get("/api/v1/health", response_model=HealthOut)
def health() -> HealthOut:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        database = "ok"
    except Exception:
        database = "error"
    return HealthOut(status="ok" if database == "ok" else "degraded", database=database, queue=queue_health(), version=__version__)


@app.post("/api/v1/jobs", response_model=JobCreated)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    target_language: str = Form(...),
    output_modes: list[str] = Form(["translated", "bilingual"]),
    pdf_password: str = Form(""),
    db: Session = Depends(get_db),
):
    if target_language not in LANGUAGES:
        raise HTTPException(422, "不支持的目标语言")
    filename = Path(file.filename or "document.pdf").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "只接受 PDF 文件")
    job_id = str(uuid.uuid4())
    token = create_access_token()
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    source = job_dir / "source.pdf"
    size = 0
    try:
        with source.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_file_mb * 1024 * 1024:
                    raise HTTPException(413, f"文件超过 {settings.max_file_mb}MB 限制")
                stream.write(chunk)
        if source.read_bytes()[:5] != b"%PDF-":
            raise HTTPException(415, "文件内容不是 PDF")
        job = Job(
            id=job_id,
            access_token_hash=hash_token(token),
            source_filename=filename,
            source_path=str(source),
            target_language=target_language,
            output_modes=",".join(mode for mode in output_modes if mode in {"translated", "bilingual"}) or "translated",
            expires_at=datetime.now(UTC) + timedelta(days=settings.retention_days),
        )
        db.add(job)
        db.commit()
        if pdf_password:
            secret_store.set(f"pdf-password:{job_id}", pdf_password)
        enqueue_job(job_id)
    except Exception:
        db.rollback()
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    url = str(request.url_for("job_page", job_id=job_id).include_query_params(token=token))
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url, status_code=303)
    return JobCreated(id=job_id, token=token, url=url)


@app.get("/api/v1/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    request: Request,
    token: str | None = Query(None),
    x_job_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token, x_job_token)
    return _job_out(db, job)


@app.post("/api/v1/jobs/{job_id}/{action}", response_model=JobOut)
def control_job(
    job_id: str,
    action: str,
    request: Request,
    token: str | None = Query(None),
    x_job_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise HTTPException(404, "未知任务操作")
    job = authorized_job(db, job_id, request, token, x_job_token)
    if action in {"pause", "cancel"}:
        job.control_requested = action
        if job.status in {JobStatus.review_required.value, JobStatus.paused.value}:
            job.status = JobStatus.canceled.value if action == "cancel" else JobStatus.paused.value
    else:
        job.control_requested = None
        job.error = None
        job.status = JobStatus.uploaded.value
        db.commit()
        enqueue_job(job.id)
    db.commit()
    return _job_out(db, job)


@app.get("/api/v1/jobs/{job_id}/pages/{page_number}", response_model=PageOut)
def get_page(
    job_id: str,
    page_number: int,
    request: Request,
    token: str | None = Query(None),
    x_job_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token, x_job_token)
    page = db.scalar(select(Page).where(Page.job_id == job.id, Page.page_number == page_number))
    if page is None:
        raise HTTPException(404, "页面不存在")
    segments = list(
        db.scalars(
            select(Segment)
            .options(selectinload(Segment.issues))
            .where(Segment.page_id == page.id)
            .order_by(Segment.reading_order)
        )
    )
    return PageOut(
        page_number=page.page_number,
        width=page.width,
        height=page.height,
        rotation=page.rotation,
        page_type=page.page_type,
        preview_url=str(request.url_for("page_preview", job_id=job.id, page_number=page.page_number).include_query_params(token=token or "")),
        segments=[
            SegmentOut(
                id=segment.id,
                segment_key=segment.segment_key,
                kind=segment.kind,
                reading_order=segment.reading_order,
                bbox=json.loads(segment.bbox_json),
                source_language=segment.source_language,
                source_text=segment.source_text,
                target_text=segment.target_text,
                structure=structure_as_api_value(segment.structure_json),
                confidence=segment.confidence,
                status=segment.status,
                confirmed=segment.confirmed,
                ignored=segment.ignored,
                issues=segment.issues,
            )
            for segment in segments
        ],
    )


@app.get("/api/v1/jobs/{job_id}/pages/{page_number}/preview", name="page_preview")
def page_preview(
    job_id: str,
    page_number: int,
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token)
    page = db.scalar(select(Page).where(Page.job_id == job.id, Page.page_number == page_number))
    if page is None or not page.preview_path or not Path(page.preview_path).exists():
        raise HTTPException(404, "预览不存在")
    return FileResponse(page.preview_path, media_type="image/png")


@app.patch("/api/v1/jobs/{job_id}/segments/{segment_id}", response_model=SegmentOut)
def update_segment(
    job_id: str,
    segment_id: int,
    payload: SegmentUpdate,
    request: Request,
    token: str | None = Query(None),
    x_job_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token, x_job_token)
    segment = db.scalar(
        select(Segment).options(selectinload(Segment.issues)).where(Segment.id == segment_id, Segment.job_id == job.id)
    )
    if segment is None:
        raise HTTPException(404, "片段不存在")
    if payload.target_text is not None:
        segment.target_text = payload.target_text.strip()
        segment.status = "edited"
    if payload.confirmed is not None:
        segment.confirmed = payload.confirmed
    if payload.ignored is not None:
        segment.ignored = payload.ignored
    for issue in segment.issues:
        if issue.id in payload.acknowledge_issue_ids:
            issue.acknowledged = True
    if payload.remember:
        if not segment.confirmed or not segment.target_text:
            raise HTTPException(422, "只有已确认的非空译文才能写入翻译记忆")
        upsert_memory(
            db,
            source_language=segment.source_language or "und",
            target_language=job.target_language,
            source_text=segment.source_text,
            target_text=segment.target_text,
            source_job_id=job.id,
        )
    db.commit()
    run_quality_checks(db, job)
    db.refresh(segment)
    return SegmentOut(
        id=segment.id,
        segment_key=segment.segment_key,
        kind=segment.kind,
        reading_order=segment.reading_order,
        bbox=json.loads(segment.bbox_json),
        source_language=segment.source_language,
        source_text=segment.source_text,
        target_text=segment.target_text,
        structure=structure_as_api_value(segment.structure_json),
        confidence=segment.confidence,
        status=segment.status,
        confirmed=segment.confirmed,
        ignored=segment.ignored,
        issues=segment.issues,
    )


@app.get("/api/v1/jobs/{job_id}/segments/{segment_id}/suggestions")
def segment_suggestions(
    job_id: str,
    segment_id: int,
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token)
    segment = db.scalar(select(Segment).where(Segment.id == segment_id, Segment.job_id == job.id))
    if segment is None:
        raise HTTPException(404, "片段不存在")
    return fuzzy_matches(db, segment.source_language or "und", job.target_language, segment.source_text)


@app.get("/api/v1/jobs/{job_id}/issues")
def job_issues(
    job_id: str,
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token)
    return list(
        db.scalars(
            select(QualityIssue)
            .where(QualityIssue.job_id == job.id, QualityIssue.resolved.is_(False))
            .order_by(QualityIssue.severity, QualityIssue.id)
        )
    )


@app.post("/api/v1/jobs/{job_id}/render", response_model=ArtifactOut)
def render_job(
    job_id: str,
    payload: RenderRequest,
    request: Request,
    token: str | None = Query(None),
    x_job_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token, x_job_token)
    if payload.mode not in job.output_modes.split(","):
        raise HTTPException(422, "创建任务时未启用此输出模式")
    job.status = JobStatus.rendering.value
    job.stage = "生成 PDF"
    db.commit()
    try:
        artifact = render_artifact(db, job, payload.mode, payload.final)
    except FinalQualityGateError as exc:
        job.status = JobStatus.review_required.value
        job.stage = "等待解决质量问题"
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    job.status = JobStatus.completed.value if payload.final else JobStatus.review_required.value
    job.stage = "输出已生成" if payload.final else "草稿已生成，等待校对"
    job.progress = 1.0 if payload.final else max(job.progress, 0.95)
    db.commit()
    return artifact


@app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_id}")
def download_artifact(
    job_id: str,
    artifact_id: int,
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    job = authorized_job(db, job_id, request, token)
    artifact = db.scalar(select(Artifact).where(Artifact.id == artifact_id, Artifact.job_id == job.id))
    if artifact is None or not Path(artifact.path).exists():
        raise HTTPException(404, "输出文件不存在")
    return FileResponse(
        artifact.path,
        media_type="application/pdf",
        filename=f"{Path(job.source_filename).stem}-{artifact.kind}.pdf",
    )


@app.get("/api/v1/settings/providers", response_model=ProviderSettings)
def provider_settings(db: Session = Depends(get_db)):
    return get_provider_settings(db)


@app.put("/api/v1/settings/providers", response_model=ProviderSettings)
def save_provider_settings(payload: ProviderSettingsUpdate, db: Session = Depends(get_db)):
    try:
        return update_provider_settings(db, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/settings/providers/test", response_model=ProviderTestResult)
def test_provider(payload: ProviderTestRequest, db: Session = Depends(get_db)):
    try:
        provider = build_translator(db) if payload.provider == "llm" else build_ocr(db)
        ok, message, latency = provider.test()
        return ProviderTestResult(ok=ok, message=message, latency_ms=latency)
    except Exception as exc:
        return ProviderTestResult(ok=False, message=str(exc))


@app.get("/api/v1/memory", response_model=list[MemoryOut])
def list_memory(
    source_language: str | None = None,
    target_language: str | None = None,
    db: Session = Depends(get_db),
):
    query = select(TranslationMemory).order_by(TranslationMemory.confirmed_at.desc())
    if source_language:
        query = query.where(TranslationMemory.source_language == source_language)
    if target_language:
        query = query.where(TranslationMemory.target_language == target_language)
    return list(db.scalars(query.limit(500)))


@app.post("/api/v1/memory", response_model=MemoryOut)
def create_memory(payload: MemoryCreate, db: Session = Depends(get_db)):
    unit = upsert_memory(db, **payload.model_dump())
    db.commit()
    return unit


@app.delete("/api/v1/memory/{memory_id}", status_code=204)
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    unit = db.get(TranslationMemory, memory_id)
    if unit is None:
        raise HTTPException(404, "翻译记忆不存在")
    db.delete(unit)
    db.commit()


@app.post("/api/v1/terms")
def create_term(payload: TermCreate, db: Session = Depends(get_db)):
    row = TermEntry(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/api/v1/terms/{term_id}", status_code=204)
def delete_term(term_id: int, db: Session = Depends(get_db)):
    row = db.get(TermEntry, term_id)
    if row is None:
        raise HTTPException(404, "术语不存在")
    db.delete(row)
    db.commit()


@app.get("/api/v1/memory/export")
def export_memory(format: str = Query("csv", pattern="^(csv|tmx)$"), db: Session = Depends(get_db)):
    units = list(db.scalars(select(TranslationMemory).where(TranslationMemory.active.is_(True))))
    if format == "tmx":
        return Response(
            export_memory_tmx(units),
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="folio-memory.tmx"'},
        )
    return Response(
        "\ufeff" + export_memory_csv(units),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="folio-memory.csv"'},
    )


@app.post("/api/v1/memory/import")
async def import_memory(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "记忆文件超过 5MB")
    imported = 0
    try:
        if (file.filename or "").lower().endswith(".tmx"):
            root = ET.fromstring(data)
            for tu in root.findall(".//tu"):
                rows = []
                for tuv in tu.findall("tuv"):
                    language = tuv.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or tuv.attrib.get("xml:lang")
                    segment = tuv.findtext("seg")
                    if language and segment:
                        rows.append((language, segment))
                if len(rows) >= 2:
                    upsert_memory(
                        db,
                        source_language=rows[0][0],
                        target_language=rows[1][0],
                        source_text=rows[0][1],
                        target_text=rows[1][1],
                    )
                    imported += 1
        else:
            text = data.decode("utf-8-sig")
            for row in csv.DictReader(io.StringIO(text)):
                upsert_memory(
                    db,
                    source_language=row["source_language"],
                    target_language=row["target_language"],
                    source_text=row["source_text"],
                    target_text=row["target_text"],
                    context=row.get("context") or None,
                )
                imported += 1
        db.commit()
    except (ET.ParseError, UnicodeDecodeError, KeyError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, f"无法导入记忆文件：{exc}") from exc
    return {"imported": imported}
