from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderError(RuntimeError):
    pass


class ProviderNotConfigured(ProviderError):
    pass


@dataclass(slots=True)
class TranslationItem:
    id: str
    text: str
    source_language: str | None = None
    context: str | None = None
    max_characters: int | None = None


@dataclass(slots=True)
class OCRItem:
    text: str
    polygon: list[float]
    confidence: float | None = None
    kind: str = "paragraph"


class TranslationProvider(Protocol):
    def translate(
        self, items: list[TranslationItem], target_language: str, terms: dict[str, str]
    ) -> dict[str, str]: ...

    def test(self) -> tuple[bool, str, int]: ...


class OCRProvider(Protocol):
    def analyze(self, content: bytes, content_type: str = "image/png") -> list[OCRItem]: ...

    def test(self) -> tuple[bool, str, int]: ...
