"""Tests for owner-facing report formatting in bot/formatters.py."""

from d_brain.bot.formatters import format_process_report, inline_artifact_image_paths


def test_inline_artifact_image_paths_matches_by_trailing_ref() -> None:
    report_text = "Итог: ![chart](attachments/charts/2026-09-25-x.png)"
    artifact_paths = [
        "vault/attachments/charts/2026-09-25-x.png",
        "vault/attachments/report.pdf",
    ]

    assert inline_artifact_image_paths(report_text, artifact_paths) == {
        "vault/attachments/charts/2026-09-25-x.png"
    }


def test_inline_artifact_image_paths_matches_title_and_angle_bracket_refs() -> None:
    report_text = (
        '![a](attachments/charts/2026-09-25-x.png "Title") '
        "![b](<attachments/charts/2026-09-25-y.png>)"
    )
    artifact_paths = [
        "vault/attachments/charts/2026-09-25-x.png",
        "vault/attachments/charts/2026-09-25-y.png",
        "vault/attachments/report.pdf",
    ]

    assert inline_artifact_image_paths(report_text, artifact_paths) == {
        "vault/attachments/charts/2026-09-25-x.png",
        "vault/attachments/charts/2026-09-25-y.png",
    }


def test_inline_artifact_image_paths_empty_when_no_inline_images() -> None:
    report_text = "Просто текст без картинок"
    artifact_paths = ["vault/attachments/report.pdf"]

    assert inline_artifact_image_paths(report_text, artifact_paths) == set()


def test_format_process_report_omits_inline_chart_from_files_list() -> None:
    report = {
        "report": "Итог: ![chart](attachments/charts/2026-09-25-x.png)",
        "artifact_paths": [
            "/vault/attachments/charts/2026-09-25-x.png",
            "/vault/attachments/report.pdf",
        ],
    }

    formatted = format_process_report(report)

    assert "attachments/report.pdf" in formatted
    assert "attachments/charts/2026-09-25-x.png" not in formatted.split(
        "**Файлы:**"
    )[-1]
    assert "**Файлы:**" in formatted


def test_format_process_report_omits_titled_inline_chart_from_files_list() -> None:
    report = {
        "report": (
            'Итог: ![chart](attachments/charts/2026-09-25-x.png "Title")'
        ),
        "artifact_paths": [
            "/vault/attachments/charts/2026-09-25-x.png",
            "/vault/attachments/report.pdf",
        ],
    }

    formatted = format_process_report(report)

    assert "attachments/report.pdf" in formatted
    assert "attachments/charts/2026-09-25-x.png" not in formatted.split(
        "**Файлы:**"
    )[-1]
    assert "**Файлы:**" in formatted


def test_format_process_report_omits_files_section_when_only_inline_images() -> None:
    report = {
        "report": "Итог: ![chart](attachments/charts/2026-09-25-x.png)",
        "artifact_paths": ["/vault/attachments/charts/2026-09-25-x.png"],
    }

    formatted = format_process_report(report)

    assert "**Файлы:**" not in formatted
