"""Routing helpers built on top of the control-plane registry."""

from __future__ import annotations

import re

from d_brain.control_plane.contracts import (
    QUESTION_CONTEXT_COMPILED,
    QUESTION_CONTEXT_RECALL,
    WORKFLOW_KIND_CAPTURE,
    WORKFLOW_KIND_QUESTION,
    WorkflowSpec,
)
from d_brain.control_plane.registry import (
    get_question_workflow,
    get_workflow,
    iter_workflows,
)

_QUESTION_QUERY_RE = re.compile(r"\s+")


def normalize_question_query(question: str) -> str:
    """Normalize free-form question text for cheap lexical routing."""
    return _QUESTION_QUERY_RE.sub(" ", (question or "").strip().casefold())


def classify_question_workflow(question: str) -> WorkflowSpec:
    """Pick the canonical question workflow for one question."""
    query = normalize_question_query(question)
    if not query:
        return get_question_workflow("general")

    for workflow in iter_workflows(kind=WORKFLOW_KIND_QUESTION):
        strategy = workflow.question_strategy
        if strategy is None or strategy.route == "general":
            continue
        if strategy.contains_any and any(
            token in query for token in strategy.contains_any
        ):
            return workflow
        if strategy.startswith_any and query.startswith(strategy.startswith_any):
            return workflow
    return get_question_workflow("general")


def classify_question_route(question: str) -> str:
    """Return the route name for one question."""
    strategy = classify_question_workflow(question).question_strategy
    if strategy is None:
        return "general"
    return strategy.route


def build_question_route_block(question: str) -> str:
    """Render one user-facing routing block for prompt injection."""
    workflow = classify_question_workflow(question)
    strategy = workflow.question_strategy
    route = strategy.route if strategy is not None else "general"
    lines = ["=== QUESTION ROUTE ===", f"Route: {route}"]
    if strategy is not None:
        lines.extend(strategy.instructions)
    lines.append("=== END QUESTION ROUTE ===")
    return "\n".join(lines)


def iter_question_context_blocks(question: str) -> tuple[str, ...]:
    """Return the registry-defined block order for question answering."""
    workflow = classify_question_workflow(question)
    strategy = workflow.question_strategy
    if strategy is None:
        return (QUESTION_CONTEXT_COMPILED, QUESTION_CONTEXT_RECALL)
    return strategy.context_blocks


def resolve_text_workflow(
    text: str,
    *,
    intent: str,
    confidence: str = "",
) -> WorkflowSpec:
    """Resolve one top-level text workflow from the intent classifier output."""
    del confidence
    if intent == "question":
        return classify_question_workflow(text)
    capture_workflows = tuple(iter_workflows(kind=WORKFLOW_KIND_CAPTURE))
    if not capture_workflows:
        return get_workflow("capture.daily-entry")
    return capture_workflows[0]
