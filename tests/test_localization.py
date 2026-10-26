import pytest

from d_brain.services.localization import (
    TRANSLATIONS,
    normalize_language,
    prompt_language_name,
    translate,
)


def test_translations_have_the_same_keys_for_ru_and_en() -> None:
    assert set(TRANSLATIONS["ru"]) == set(TRANSLATIONS["en"])


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ru", "ru"),
        ("en", "en"),
        ("ru-RU", "ru"),
        ("EN-us", "en"),
        ("  RU  ", "ru"),
        ("", "en"),
        (None, "en"),
        ("   ", "en"),
        ("de", "en"),
        ("zh-CN", "en"),
    ],
)
def test_normalize_language_maps_to_a_supported_code(language, expected) -> None:
    assert normalize_language(language) == expected


def test_translate_uses_the_requested_language() -> None:
    assert translate("ru", "ai_description") == TRANSLATIONS["ru"]["ai_description"]
    assert translate("en", "ai_description") == TRANSLATIONS["en"]["ai_description"]
    assert translate("ru", "ai_description") != translate("en", "ai_description")


def test_translate_accepts_regional_codes() -> None:
    assert translate("ru-RU", "format") == TRANSLATIONS["ru"]["format"]


def test_translate_falls_back_to_english_for_an_unknown_language() -> None:
    assert translate("de", "format") == TRANSLATIONS["en"]["format"]
    assert translate("", "format") == TRANSLATIONS["en"]["format"]


def test_translate_returns_an_unknown_key_as_is() -> None:
    assert translate("ru", "no_such_label") == "no_such_label"
    assert translate("de", "no_such_label") == "no_such_label"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ru", "Russian"),
        ("ru-RU", "Russian"),
        ("en", "English"),
        ("", "English"),
        (None, "English"),
        # unsupported codes normalise to English before the name lookup
        ("de", "English"),
    ],
)
def test_prompt_language_name(language, expected) -> None:
    assert prompt_language_name(language) == expected
