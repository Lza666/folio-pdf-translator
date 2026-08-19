from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting
from app.providers.azure_ocr import AzureLayoutOCR
from app.providers.llm import OpenAICompatibleTranslator
from app.schemas import ProviderSettings, ProviderSettingsUpdate
from app.security import secret_store

DEFAULTS = {
    "llm_base_url": "",
    "llm_model": "",
    "llm_extra_json": "{}",
    "azure_endpoint": "",
    "azure_api_version": get_settings().ocr_api_version,
}


def get_value(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else DEFAULTS.get(key, "")


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def get_provider_settings(db: Session) -> ProviderSettings:
    return ProviderSettings(
        llm_base_url=get_value(db, "llm_base_url"),
        llm_model=get_value(db, "llm_model"),
        llm_extra_json=get_value(db, "llm_extra_json"),
        has_llm_api_key=bool(secret_store.get("llm-api-key")),
        azure_endpoint=get_value(db, "azure_endpoint"),
        azure_api_version=get_value(db, "azure_api_version"),
        has_azure_api_key=bool(secret_store.get("azure-api-key")),
    )


def update_provider_settings(db: Session, payload: ProviderSettingsUpdate) -> ProviderSettings:
    try:
        extra = json.loads(payload.llm_extra_json or "{}")
        if not isinstance(extra, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("模型附加参数必须是 JSON 对象") from exc
    set_value(db, "llm_base_url", payload.llm_base_url.strip())
    set_value(db, "llm_model", payload.llm_model.strip())
    set_value(db, "llm_extra_json", json.dumps(extra, ensure_ascii=False))
    set_value(db, "azure_endpoint", payload.azure_endpoint.strip())
    set_value(db, "azure_api_version", payload.azure_api_version.strip())
    if payload.llm_api_key:
        secret_store.set("llm-api-key", payload.llm_api_key)
    if payload.azure_api_key:
        secret_store.set("azure-api-key", payload.azure_api_key)
    db.commit()
    return get_provider_settings(db)


def build_translator(db: Session) -> OpenAICompatibleTranslator:
    try:
        extra = json.loads(get_value(db, "llm_extra_json") or "{}")
    except json.JSONDecodeError:
        extra = {}
    return OpenAICompatibleTranslator(
        base_url=get_value(db, "llm_base_url"),
        api_key=secret_store.get("llm-api-key"),
        model=get_value(db, "llm_model"),
        extra=extra,
    )


def build_ocr(db: Session) -> AzureLayoutOCR:
    return AzureLayoutOCR(
        endpoint=get_value(db, "azure_endpoint"),
        api_key=secret_store.get("azure-api-key"),
        api_version=get_value(db, "azure_api_version"),
    )
