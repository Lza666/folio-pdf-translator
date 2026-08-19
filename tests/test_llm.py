import pytest

from app.providers.base import ProviderError
from app.providers.llm import OpenAICompatibleTranslator


def test_structured_response_accepts_all_ids():
    text = '{"translations":[{"id":"a","text":"甲"},{"id":"b","text":"乙"}]}'
    assert OpenAICompatibleTranslator._parse_response(text, {"a", "b"}) == {"a": "甲", "b": "乙"}


def test_structured_response_rejects_missing_id():
    with pytest.raises(ProviderError, match="漏掉"):
        OpenAICompatibleTranslator._parse_response(
            '{"translations":[{"id":"a","text":"甲"}]}', {"a", "b"}
        )


def test_structured_response_rejects_duplicates():
    with pytest.raises(ProviderError, match="重复"):
        OpenAICompatibleTranslator._parse_response(
            '{"translations":[{"id":"a","text":"甲"},{"id":"a","text":"乙"}]}', {"a"}
        )


def test_structured_response_enforces_character_budget():
    with pytest.raises(ProviderError, match="超过字符预算"):
        OpenAICompatibleTranslator._parse_response(
            '{"translations":[{"id":"a","text":"超过预算"}]}',
            {"a"},
            {"a": 3},
        )
