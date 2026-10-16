"""Memory planning uses the same model priority as message classification."""

from types import SimpleNamespace

import httpx
import pytest

from d_brain.services.recall_planner import RecallPlannerConfig, plan_qmd_recall


@pytest.mark.parametrize("use_recall", [True, False])
def test_decision_model_takes_priority_over_chat_and_cli(monkeypatch, use_recall):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "answers": {
                    "use_recall": {"noul": 0.95 if use_recall else 0.05},
                    "history_scope": {"choice": "year"},
                    "deep": {"noul": 0.9},
                },
            },
        )

    monkeypatch.setattr("d_brain.services.recall_planner.httpx.post", post)
    client = SimpleNamespace(chat=None)
    plan = plan_qmd_recall(
        "Подготовь анализ проекта Example",
        purpose="assistant_request",
        config=RecallPlannerConfig(
            model="chat",
            classification_model="decision",
            api_key="test",
            base_url="https://gateway.example/v1/",
            language="ru",
        ),
        client=client,
        run_prompt=lambda prompt: pytest.fail("Decision model must take priority"),
    )
    assert plan.use_recall is use_recall
    assert plan.query
    assert plan.history_scope == "year"
    assert plan.deep is True
    assert calls[0][0] == "https://gateway.example/v1/decisions"
    assert calls[0][1]["json"]["model"] == "decision"


def test_chat_model_takes_priority_over_cli():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"use_recall":true,"query":"Example project"}',
                    )
                )
            ]
        )

    plan = plan_qmd_recall(
        "Example project",
        purpose="assistant_request",
        config=RecallPlannerConfig(model="chat", api_key="test"),
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        ),
        run_prompt=lambda prompt: pytest.fail("Chat model must take priority over CLI"),
    )
    assert plan.use_recall is True
    assert calls[0]["model"] == "chat"


def test_no_model_plans_recall_through_cli():
    calls = []

    def run_prompt(prompt):
        calls.append(prompt)
        return '{"use_recall":true,"query":"Example project","history_scope":"year"}'

    plan = plan_qmd_recall(
        "Example project",
        purpose="assistant_request",
        config=RecallPlannerConfig(),
        run_prompt=run_prompt,
    )
    assert plan.use_recall is True
    assert plan.history_scope == "year"
    assert "Example project" in calls[0]
    assert '"use_recall"' in calls[0]
