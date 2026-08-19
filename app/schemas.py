from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

LANGUAGES = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "it": "Italiano",
    "nl": "Nederlands",
    "pl": "Polski",
}


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class JobCreated(BaseModel):
    id: str
    token: str
    url: str


class IssueOut(ORMModel):
    id: int
    segment_id: int | None
    code: str
    severity: str
    message: str
    resolved: bool
    acknowledged: bool


class ArtifactOut(ORMModel):
    id: int
    kind: str
    size_bytes: int
    created_at: datetime


class JobOut(ORMModel):
    id: str
    source_filename: str
    target_language: str
    output_modes: str
    status: str
    stage: str
    progress: float
    current_page: int
    page_count: int
    warning: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    unresolved_issues: int = 0
    artifacts: list[ArtifactOut] = Field(default_factory=list)


class SegmentOut(ORMModel):
    id: int
    segment_key: str
    kind: str
    reading_order: int
    bbox: list[float]
    source_language: str | None
    source_text: str
    target_text: str | None
    structure: dict[str, Any] | None = None
    confidence: float | None
    status: str
    confirmed: bool
    ignored: bool
    issues: list[IssueOut] = Field(default_factory=list)


class PageOut(BaseModel):
    page_number: int
    width: float
    height: float
    rotation: int
    page_type: str
    preview_url: str | None
    segments: list[SegmentOut]


class SegmentUpdate(BaseModel):
    target_text: str | None = None
    confirmed: bool | None = None
    ignored: bool | None = None
    remember: bool = False
    acknowledge_issue_ids: list[int] = Field(default_factory=list)


class RenderRequest(BaseModel):
    mode: Literal["translated", "bilingual"]
    final: bool = False


class ProviderSettings(BaseModel):
    llm_base_url: str = ""
    llm_model: str = ""
    llm_extra_json: str = "{}"
    has_llm_api_key: bool = False
    azure_endpoint: str = ""
    azure_api_version: str = "2024-11-30"
    has_azure_api_key: bool = False


class ProviderSettingsUpdate(BaseModel):
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str | None = None
    llm_extra_json: str = "{}"
    azure_endpoint: str = ""
    azure_api_version: str = "2024-11-30"
    azure_api_key: str | None = None


class ProviderTestRequest(BaseModel):
    provider: Literal["llm", "ocr"]


class ProviderTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None


class TermCreate(BaseModel):
    source_language: str
    target_language: str
    source_term: str = Field(min_length=1, max_length=512)
    target_term: str = Field(min_length=1, max_length=512)
    case_sensitive: bool = False
    notes: str | None = None


class MemoryCreate(BaseModel):
    source_language: str
    target_language: str
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    context: str | None = None


class MemoryOut(ORMModel):
    id: int
    source_language: str
    target_language: str
    source_text: str
    target_text: str
    context: str | None
    version: int
    active: bool
    confirmed_at: datetime


class FuzzySuggestion(BaseModel):
    id: int
    source_text: str
    target_text: str
    score: float


class HealthOut(BaseModel):
    status: str
    database: str
    queue: str
    version: str
