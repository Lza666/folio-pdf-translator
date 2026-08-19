from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    uploaded = "uploaded"
    validating = "validating"
    extracting = "extracting"
    ocr = "ocr"
    translating = "translating"
    review_required = "review_required"
    rendering = "rendering"
    completed = "completed"
    paused = "paused"
    failed = "failed"
    canceled = "canceled"
    expired = "expired"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    output_modes: Mapped[str] = mapped_column(String(64), default="translated,bilingual")
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.uploaded.value, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="等待处理")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_page: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    warning: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    control_requested: Mapped[str | None] = mapped_column(String(16))
    is_encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    has_signature: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    pages: Mapped[list[Page]] = relationship(back_populates="job", cascade="all, delete-orphan")
    segments: Mapped[list[Segment]] = relationship(back_populates="job", cascade="all, delete-orphan")
    issues: Mapped[list[QualityIssue]] = relationship(back_populates="job", cascade="all, delete-orphan")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("job_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    page_type: Mapped[str] = mapped_column(String(16), default="native")
    preview_path: Mapped[str | None] = mapped_column(Text)
    extraction_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")

    job: Mapped[Job] = relationship(back_populates="pages")
    segments: Mapped[list[Segment]] = relationship(back_populates="page", cascade="all, delete-orphan")


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("job_id", "segment_key"), Index("ix_segment_job_page", "job_id", "page_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    segment_key: Mapped[str] = mapped_column(String(96))
    kind: Mapped[str] = mapped_column(String(32), default="paragraph")
    reading_order: Mapped[int] = mapped_column(Integer, default=0)
    bbox_json: Mapped[str] = mapped_column(Text)
    polygon_json: Mapped[str | None] = mapped_column(Text)
    source_language: Mapped[str | None] = mapped_column(String(16))
    source_text: Mapped[str] = mapped_column(Text)
    target_text: Mapped[str | None] = mapped_column(Text)
    structure_json: Mapped[str | None] = mapped_column(Text)
    font_name: Mapped[str | None] = mapped_column(String(128))
    font_size: Mapped[float | None] = mapped_column(Float)
    font_color: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    translation_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship(back_populates="segments")
    page: Mapped[Page] = relationship(back_populates="segments")
    issues: Mapped[list[QualityIssue]] = relationship(back_populates="segment")


class QualityIssue(Base):
    __tablename__ = "quality_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="issues")
    segment: Mapped[Segment | None] = relationship(back_populates="issues")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("job_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="artifacts")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TermEntry(Base):
    __tablename__ = "term_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_language: Mapped[str] = mapped_column(String(16))
    target_language: Mapped[str] = mapped_column(String(16), index=True)
    source_term: Mapped[str] = mapped_column(String(512))
    target_term: Mapped[str] = mapped_column(String(512))
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TranslationMemory(Base):
    __tablename__ = "translation_memory"
    __table_args__ = (
        UniqueConstraint("source_language", "target_language", "normalized_source"),
        Index("ix_tm_language_pair", "source_language", "target_language"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_language: Mapped[str] = mapped_column(String(16))
    target_language: Mapped[str] = mapped_column(String(16))
    source_text: Mapped[str] = mapped_column(Text)
    normalized_source: Mapped[str] = mapped_column(Text)
    target_text: Mapped[str] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    source_job_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list[TranslationMemoryVersion]] = relationship(
        back_populates="unit", cascade="all, delete-orphan"
    )


class TranslationMemoryVersion(Base):
    __tablename__ = "translation_memory_versions"
    __table_args__ = (UniqueConstraint("unit_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("translation_memory.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    target_text: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    unit: Mapped[TranslationMemory] = relationship(back_populates="versions")
