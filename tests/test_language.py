from app.language import detect_language, is_translatable, normalize_text


def test_non_translatable_content():
    assert not is_translatable("https://example.com")
    assert not is_translatable("hello@example.com")
    assert not is_translatable("123,456.00")
    assert not is_translatable("x = a + b")
    assert is_translatable("A meaningful sentence.")


def test_language_heuristics():
    assert detect_language("这是中文段落")[0] == "zh-Hans"
    assert detect_language("這是繁體中文翻譯頁面")[0] == "zh-Hant"
    assert detect_language("これは日本語です")[0] == "ja"
    assert detect_language("한국어 문장입니다")[0] == "ko"
    assert normalize_text("  Hello   WORLD ") == "hello world"
