import json
import sys
from pathlib import Path
from types import SimpleNamespace

from d_brain import run_compiled_wiki_care


def _patch_settings(monkeypatch, vault: Path, *, tavily_api_key: str = "") -> None:
    monkeypatch.setattr(
        run_compiled_wiki_care,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault,
            content_language="ru",
            ai_cli="claude",
            tavily_api_key=tavily_api_key,
        ),
    )


def _stub_run_weekly_wiki_care(monkeypatch, *, result: dict, calls: list[dict]):
    def _fake(vault_path, **kwargs):  # noqa: ANN001, ANN003
        calls.append({"vault_path": vault_path, **kwargs})
        return dict(result)

    monkeypatch.setattr(run_compiled_wiki_care, "run_weekly_wiki_care", _fake)


def test_default_mode_passes_no_limit_false(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    calls: list[dict] = []
    _stub_run_weekly_wiki_care(
        monkeypatch,
        result={"status": "no-work", "errors": []},
        calls=calls,
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_wiki_care.main()

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["vault_path"] == vault
    assert calls[0]["no_limit"] is False
    assert calls[0]["content_language"] == "ru"
    assert calls[0]["ai_cli"] == "claude"
    assert calls[0]["tavily_api_key"] == ""
    out = json.loads(capsys.readouterr().out)
    assert out == {"status": "no-work", "errors": []}


def test_no_limit_flag_passes_no_limit_true(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault, tavily_api_key="tvly-secret")
    calls: list[dict] = []
    _stub_run_weekly_wiki_care(
        monkeypatch,
        result={"status": "ok", "errors": []},
        calls=calls,
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--no-limit"])

    exit_code = run_compiled_wiki_care.main()

    assert exit_code == 0
    assert calls[0]["no_limit"] is True
    assert calls[0]["tavily_api_key"] == "tvly-secret"


def test_exit_code_reflects_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    _stub_run_weekly_wiki_care(
        monkeypatch,
        result={"status": "failed", "errors": ["boom"]},
        calls=[],
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_wiki_care.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == ["boom"]


def test_answer_question_closure_builds_processor_from_settings(
    tmp_path: Path, monkeypatch
) -> None:
    """The CLI's own ``_answer_question`` closure is passed through to
    ``run_weekly_wiki_care`` as the ``answer_question`` callable; this test
    invokes it directly to confirm it builds ``CliProcessor`` from the
    resolved settings and forwards the question text."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    captured_calls: list[dict] = []

    class FakeProcessor:
        def __init__(self, vault_path, *, ai_cli, content_language):  # noqa: ANN001
            captured_calls.append(
                {
                    "vault_path": vault_path,
                    "ai_cli": ai_cli,
                    "content_language": content_language,
                }
            )

        def answer_question(self, question):  # noqa: ANN001
            return {
                "filed_artifact_path": "summaries/answers/2026-08-05.md",
                "question": question,
            }

    monkeypatch.setattr(run_compiled_wiki_care, "CliProcessor", FakeProcessor)

    answer_question_calls: list[dict] = []
    _stub_run_weekly_wiki_care(
        monkeypatch,
        result={"status": "ok", "errors": []},
        calls=answer_question_calls,
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    run_compiled_wiki_care.main()

    assert len(answer_question_calls) == 1
    answer_question = answer_question_calls[0]["answer_question"]
    result = answer_question("Что нового по проекту?")

    assert captured_calls == [
        {"vault_path": vault, "ai_cli": "claude", "content_language": "ru"}
    ]
    assert result["question"] == "Что нового по проекту?"


def test_stdout_is_valid_json_matching_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    result = {
        "status": "ok",
        "links_added": 2,
        "pages_created": 1,
        "questions_answered": 1,
        "articles_imported": 0,
        "changed_paths": ["compiled/topics/a.md"],
        "errors": [],
        "model_calls_used": 5,
        "compiled_index_written": True,
    }
    _stub_run_weekly_wiki_care(monkeypatch, result=result, calls=[])
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_wiki_care.main()

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == result


def test_exception_is_reported_as_json_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _patch_settings(monkeypatch, tmp_path / "vault")

    def _boom(vault_path, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("wiki care exploded")

    monkeypatch.setattr(run_compiled_wiki_care, "run_weekly_wiki_care", _boom)
    monkeypatch.setattr(sys, "argv", ["prog"])

    exit_code = run_compiled_wiki_care.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {"errors": ["RuntimeError: wiki care exploded"]}
