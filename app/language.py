from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from app.schemas import LANGUAGES

URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.I)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
NUMBER_RE = re.compile(r"^[\s\d.,:;/%+\-()$€£¥]+$")
CODE_RE = re.compile(r"(?:[{};]|=>|</?\w+>|\b(?:def|class|function|const|let|var)\b)")
MATH_RE = re.compile(r"^[\s\dA-Za-z=+\-*/^_{}()[\]<>∑∫√παβγδθλμσ]+$")


LINGUA_MAP = {
    "CHINESE": "zh-Hans",
    "ENGLISH": "en",
    "JAPANESE": "ja",
    "KOREAN": "ko",
    "FRENCH": "fr",
    "GERMAN": "de",
    "SPANISH": "es",
    "PORTUGUESE": "pt",
    "ITALIAN": "it",
    "DUTCH": "nl",
    "POLISH": "pl",
}

# High-signal characters that differ in common Simplified/Traditional prose.
TRADITIONAL_MARKERS = set(
    "體臺灣譯語頁號檔開關門間國學時會點線圖書車馬風雲龍廣東萬與為這個來說後發現"
    "處問題確認實際應該讓達別節奏層級視覺編輯復核讀寫導輸標識終稿"
)


@lru_cache
def _detector():
    try:
        from lingua import Language, LanguageDetectorBuilder

        languages = [
            Language.CHINESE,
            Language.ENGLISH,
            Language.JAPANESE,
            Language.KOREAN,
            Language.FRENCH,
            Language.GERMAN,
            Language.SPANISH,
            Language.PORTUGUESE,
            Language.ITALIAN,
            Language.DUTCH,
            Language.POLISH,
        ]
        return LanguageDetectorBuilder.from_languages(*languages).build()
    except ImportError:
        return None


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.casefold().split())


def is_translatable(text: str) -> bool:
    value = text.strip()
    if len(value) < 2 or URL_RE.match(value) or EMAIL_RE.match(value) or NUMBER_RE.match(value):
        return False
    if CODE_RE.search(value):
        return False
    if MATH_RE.match(value) and any(symbol in value for symbol in "=+*/^∑∫√"):
        return False
    return any(character.isalpha() or "\u3400" <= character <= "\u9fff" for character in value)


def detect_language(text: str) -> tuple[str | None, float]:
    value = text.strip()
    if not value:
        return None, 0.0
    if re.search(r"[\u3040-\u30ff]", value):
        return "ja", 0.99
    if re.search(r"[\uac00-\ud7af]", value):
        return "ko", 0.99
    if re.search(r"[\u3400-\u9fff]", value):
        han = [character for character in value if "\u3400" <= character <= "\u9fff"]
        traditional_hits = sum(character in TRADITIONAL_MARKERS for character in han)
        if traditional_hits >= max(1, len(han) // 12):
            return "zh-Hant", 0.90
        return "zh-Hans", 0.88
    detector = _detector()
    if detector is None:
        return "en", 0.50
    language = detector.detect_language_of(value)
    if language is None:
        return None, 0.0
    confidences = detector.compute_language_confidence_values(value)
    score = next((item.value for item in confidences if item.language == language), 0.5)
    return LINGUA_MAP.get(language.name), float(score)


def same_target_language(source: str | None, target: str) -> bool:
    if source is None:
        return False
    if target in {"zh-Hans", "zh-Hant"} and source in {"zh-Hans", "zh-Hant"}:
        return source == target
    return source == target


def target_language_name(code: str) -> str:
    return LANGUAGES.get(code, code)
