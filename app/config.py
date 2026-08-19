from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="FOLIO_", extra="ignore", case_sensitive=False
    )

    env: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite:///./data/folio.db"
    data_dir: Path = Path("./data")
    redis_url: str = "redis://127.0.0.1:6379/0"
    queue_mode: Literal["auto", "rq", "inline"] = "auto"
    retention_days: int = 7
    max_file_mb: int = 50
    max_pages: int = 100
    max_page_pixels: int = 40_000_000
    ocr_api_version: str = "2024-11-30"
    secret_service_name: str = "folio-translator"

    @field_validator("host")
    @classmethod
    def refuse_public_bind_without_auth(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:
            raise ValueError("Folio has no authentication; bind to a loopback address only")
        return value

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def database_path(self) -> Path | None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url.removeprefix(prefix))
        return None

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
