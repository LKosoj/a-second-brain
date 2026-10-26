import json
import subprocess
from pathlib import Path

import pytest

from d_brain import run_qmd
from d_brain.services.compiled_index import (
    build_question_catalog,
    is_overview_question,
    project_scope_paths,
)
from d_brain.services.conversation_learning import ConversationLearningService
from d_brain.services.processor import CliProcessor
from d_brain.services.qmd import QmdService
from d_brain.services.recall_planner import (
    RecallPlan,
    RecallPlannerConfig,
    build_qmd_recall_block,
)


def _project(vault: Path, name: str = "Аврора", slug: str = "aurora") -> Path:
    path = vault / f"compiled/projects/{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\ndescription: "Поставка оборудования {name}"\n'
        f"freshness_state: fresh\n---\n# Проект {name}\n\n"
        "Содержимое только для полного чтения.\n\n## Sources\n"
        "- [[daily/2026-10-01.md]]\n"
    )
    source = vault / "daily/2026-10-01.md"
    source.parent.mkdir(exist_ok=True)
    source.write_text("Источник проекта")
    return path


def test_overview_offers_catalog_without_claiming_pages_were_read(
    tmp_path, write_vault_manifest, monkeypatch
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _project(vault)
    processor = CliProcessor(vault)

    block = processor._build_compiled_briefings_block("Дай обзор проекта Аврора")

    assert "COMPILED CATALOG" in block
    assert "compiled/projects/aurora" in block
    assert "Поставка оборудования" in block
    assert "Содержимое только для полного чтения" not in block
    assert "qmd get" in block
    assert processor._question_provenance_candidates == ()
    monkeypatch.setattr(
        processor, "_build_auto_recall_block",
        lambda *args, **kwargs: pytest.fail("overview must read the catalog first"),
    )
    prompt = processor._inject_question_context_blocks(
        "USER QUESTION:", "Дай обзор проекта Аврора"
    )
    assert "COMPILED CATALOG" in prompt

    block = processor._build_compiled_briefings_block("Какой статус проекта Аврора?")
    assert "Содержимое только для полного чтения" in block
    assert processor._question_provenance_candidates


@pytest.mark.parametrize(
    "question", ["Какой статус?", "Когда срок?", "Обзор истории проекта"]
)
def test_status_and_exact_questions_keep_existing_retrieval(question):
    assert not is_overview_question(question)


def test_project_scope_uses_explicit_name_sources_and_rejects_ambiguity(
    tmp_path, write_vault_manifest
):
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    _project(vault)
    assert project_scope_paths(vault, "Что решили по Аврора?") == (
        "compiled/projects/aurora.md",
        "daily/2026-10-01.md",
    )
    assert project_scope_paths(vault, "Что решили по проектам?") == ()
    _project(vault, "Борей", "borey")
    assert project_scope_paths(vault, "Сравни Аврора и Борей") == ()


@pytest.mark.parametrize("remote", [False, True])
def test_qmd_scope_reaches_backend_for_local_and_remote_models(
    tmp_path, write_vault_manifest, monkeypatch, remote
):
    write_vault_manifest(tmp_path)
    (tmp_path / "thoughts/learnings").mkdir(parents=True)
    qmd = QmdService(tmp_path)
    monkeypatch.setattr(qmd, "_uses_remote_embeddings", lambda: remote)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr("d_brain.services.qmd.run_in_process_group", run)
    qmd.recall("доклад начальнику", scope=("thoughts/learnings",))
    assert len(calls) == 1
    assert calls[0][0] == "node"
    assert "--scope-uri" in calls[0]
    assert "qmd://thoughts/learnings" in calls[0]
    with pytest.raises(ValueError):
        qmd.query("test", scope=("../outside",))


def test_scoped_cli_preserves_query_and_multiple_paths(
    tmp_path, write_vault_manifest, monkeypatch, capsys
):
    write_vault_manifest(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    calls = []

    def recall(self, query, **kwargs):
        calls.append((query, kwargs))
        return {"results": []}

    monkeypatch.setattr(QmdService, "recall", recall)
    assert (
        run_qmd.main(
            [
                "recall",
                "срок проекта",
                "--scope",
                "compiled/projects/aurora.md",
                "--scope",
                "daily/2026-10-01.md",
                "--json",
            ]
        )
        == 0
    )
    assert calls == [
        (
            "срок проекта",
            {
                "deep": False,
                "limit": 5,
                "scope": ["compiled/projects/aurora.md", "daily/2026-10-01.md"],
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"results": []}


@pytest.mark.parametrize("scoped_count, history", [(1, False), (5, False), (5, True)])
def test_auto_recall_expands_only_when_project_sources_are_insufficient(
    tmp_path, write_vault_manifest, monkeypatch, scoped_count, history
):
    write_vault_manifest(tmp_path)
    _project(tmp_path)
    calls = []

    def recall(self, query, *, deep=False, limit=5, scope=()):
        calls.append(scope)
        return {
            "query": query,
            "results": [
                {
                    "file": f"qmd://daily/{index}.md",
                    "rel_path": f"daily/{index}.md",
                    "score": 0.9,
                    "effective_score": 0.9,
                    "confidence": 0.9,
                }
                for index in range(scoped_count)
            ],
        }

    monkeypatch.setattr(QmdService, "recall", recall)
    monkeypatch.setattr(
        "d_brain.services.recall_planner.plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True, query="Аврора",
            history_scope="full" if history else "latest",
        ),
    )
    build_qmd_recall_block(
        tmp_path,
        task="Что решили по Аврора?",
        purpose="question_answer",
        config=RecallPlannerConfig(language="ru"),
    )
    assert calls[0] == ("compiled/projects/aurora.md", "daily/2026-10-01.md")
    assert (() in calls) == (scoped_count < 5 or history)


def test_semantic_lesson_recall_handles_paraphrase_without_accepting_other_notes(
    tmp_path, write_vault_manifest, monkeypatch
):
    write_vault_manifest(tmp_path)
    root = tmp_path / "thoughts/learnings"
    root.mkdir(parents=True)
    name = "thoughts/learnings/2026-10-01-user-correction-1-2.md"
    (tmp_path / name).write_text(
        '---\ncondition: "презентация руководству"\n'
        'lesson: "Сначала вывод"\nsource: "telegram:1:2"\n'
        'exceptions: ["Не применять к техническому приложению"]\n---\n'
    )
    db_path = QmdService(tmp_path).db_path
    db_path.parent.mkdir(parents=True)
    db_path.touch()

    def recall(self, query, **kwargs):
        assert query == "доклад начальнику"
        assert kwargs["scope"] == ("thoughts/learnings",)
        return {
            "results": [
                {"rel_path": name, "confidence": 0.9},
                {"rel_path": "thoughts/untrusted.md", "confidence": 1},
            ]
        }

    monkeypatch.setattr(QmdService, "recall", recall)
    block = ConversationLearningService(tmp_path).relevant_lessons("доклад начальнику")
    assert "Сначала вывод" in block
    assert "telegram:1:2" in block
    assert "Не применять к техническому приложению" in block
    assert "untrusted" not in block

    monkeypatch.setattr(
        QmdService, "recall",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("index unavailable")),
    )
    block = ConversationLearningService(tmp_path).relevant_lessons(
        "презентация руководству"
    )
    assert "Сначала вывод" in block


def test_empty_catalog_does_not_invent_pages(tmp_path):
    assert build_question_catalog(tmp_path, "Какие проекты?") == ""
