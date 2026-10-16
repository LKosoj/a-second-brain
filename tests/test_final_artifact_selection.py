"""Only files explicitly listed in the final response are delivered."""

from pathlib import Path

from d_brain.bot.formatters import format_process_report
from d_brain.services.processor import CliProcessor


def test_final_deck_and_pdf_exclude_all_working_files(
    tmp_path: Path, monkeypatch
) -> None:
    processor = CliProcessor(tmp_path)

    def execute(prompt: str) -> str:
        root = tmp_path / "attachments" / "presentation"
        root.mkdir(parents=True)
        for i in range(136):
            (root / f"work-{i}.png").write_bytes(b"preview")
        for name in [
            "final.pptx",
            "final.pdf",
            "final.pdf.bak",
            "build.py",
            "layout.json",
        ]:
            (root / name).write_bytes(b"test")
        return (
            "[PPTX](attachments/presentation/final.pptx) "
            "[PDF](attachments/presentation/final.pdf)"
        )

    monkeypatch.setattr(processor, "_run_assistant_prompt", execute)
    output, paths = processor._run_assistant_prompt_with_artifacts(
        "Create presentation"
    )

    assert paths == [
        str(tmp_path / "attachments/presentation/final.pdf"),
        str(tmp_path / "attachments/presentation/final.pptx"),
    ]
    report = format_process_report({"report": output, "artifact_paths": paths})
    assert "work-" not in report
    assert "build.py" not in report
    assert "layout.json" not in report
    assert ".bak" not in report


def test_unlisted_generated_file_is_not_delivered(tmp_path: Path, monkeypatch) -> None:
    processor = CliProcessor(tmp_path)

    def execute(prompt: str) -> str:
        root = tmp_path / "attachments"
        root.mkdir()
        (root / "draft.pdf").write_bytes(b"draft")
        return "Готово."

    monkeypatch.setattr(processor, "_run_assistant_prompt", execute)
    assert processor._run_assistant_prompt_with_artifacts("task")[1] == []


def test_existing_explicit_files_are_deduplicated_and_must_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    processor = CliProcessor(tmp_path)
    root = tmp_path / "attachments"
    root.mkdir()
    artifact = root / "result with spaces.xlsx"
    artifact.write_bytes(b"spreadsheet")
    output = (
        "[Result](<attachments/result with spaces.xlsx>) "
        f"`{artifact}` `attachments/missing.pdf`"
    )
    monkeypatch.setattr(processor, "_run_assistant_prompt", lambda prompt: output)

    assert processor._run_assistant_prompt_with_artifacts("task")[1] == [str(artifact)]
