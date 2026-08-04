"""Localization helpers for saved generated content."""

from __future__ import annotations

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "ai_description": "AI-описание",
        "auto_subtitles": "автосубтитры",
        "extracted_text": "Извлечённый текст",
        "format": "Формат",
        "link_summary": "Саммари по ссылке",
        "no_summary_yet": "_Саммари пока нет._",
        "no_transcript_yet": "_Транскрипта пока нет._",
        "ocr": "OCR",
        "open_plaud": "Открыть в PLAUD",
        "open_telegram_message": "Открыть сообщение в Telegram",
        "original_file": "Исходный файл",
        "manual_subtitles": "ручные субтитры",
        "metadata": "Метаданные",
        "plaud_file_id": "PLAUD file_id",
        "raw_payload": "Исходный payload",
        "source": "Источник",
        "source_path": "Путь к источнику",
        "source_ref": "Идентификатор источника",
        "summary": "Саммари",
        "summary_unavailable": "Саммари недоступно.",
        "title": "Заголовок",
        "transcript": "Транскрипт",
        "transcript_source": "Источник",
        "transcript_unavailable": "Транскрипт недоступен.",
        "unknown_title": "Без заголовка",
        "url": "URL",
        "warnings": "Предупреждения",
        "youtube_summary": "Саммари YouTube",
        "youtube_transcript": "Транскрипт YouTube",
    },
    "en": {
        "ai_description": "AI description",
        "auto_subtitles": "auto subtitles",
        "extracted_text": "Extracted text",
        "format": "Format",
        "link_summary": "Link summary",
        "no_summary_yet": "_No summary yet._",
        "no_transcript_yet": "_No transcript yet._",
        "ocr": "OCR",
        "open_plaud": "Open in PLAUD",
        "open_telegram_message": "Open Telegram message",
        "original_file": "Original file",
        "manual_subtitles": "manual subtitles",
        "metadata": "Metadata",
        "plaud_file_id": "PLAUD file_id",
        "raw_payload": "Raw payload",
        "source": "Source",
        "source_path": "Source path",
        "source_ref": "Source ref",
        "summary": "Summary",
        "summary_unavailable": "Summary unavailable.",
        "title": "Title",
        "transcript": "Transcript",
        "transcript_source": "Source",
        "transcript_unavailable": "Transcript unavailable.",
        "unknown_title": "Unknown title",
        "url": "URL",
        "warnings": "Warnings",
        "youtube_summary": "YouTube summary",
        "youtube_transcript": "YouTube transcript",
    },
}

PROMPT_LANGUAGE_NAMES = {
    "ru": "Russian",
    "en": "English",
}


def normalize_language(language: str) -> str:
    """Normalize BCP-47-ish values to a supported primary language code."""

    cleaned = (language or "").strip().lower()
    if not cleaned:
        return "en"
    primary = cleaned.split("-", 1)[0]
    return primary if primary in TRANSLATIONS else "en"


def translate(language: str, key: str) -> str:
    """Return translated UI/save label with English fallback."""

    normalized = normalize_language(language)
    return TRANSLATIONS.get(normalized, TRANSLATIONS["en"]).get(
        key,
        TRANSLATIONS["en"].get(key, key),
    )


def prompt_language_name(language: str) -> str:
    """Human-readable language name for prompts."""

    normalized = normalize_language(language)
    return PROMPT_LANGUAGE_NAMES.get(normalized, language or "English")
