"""Typed contracts for the d-brain control plane."""

from __future__ import annotations

from dataclasses import dataclass, field

WORKFLOW_KIND_CAPTURE = "capture"
WORKFLOW_KIND_QUESTION = "question"
WORKFLOW_KIND_MAINTENANCE = "maintenance"
WORKFLOW_KIND_INTEGRATION = "integration"

QUESTION_CONTEXT_COMPILED = "compiled"
QUESTION_CONTEXT_RECALL = "recall"


@dataclass(frozen=True, slots=True)
class QuestionRoutingStrategy:
    """Registry contract for one question-routing strategy."""

    route: str
    contains_any: tuple[str, ...] = ()
    startswith_any: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    context_blocks: tuple[str, ...] = (
        QUESTION_CONTEXT_COMPILED,
        QUESTION_CONTEXT_RECALL,
    )


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """One canonical workflow entry in the control-plane registry."""

    name: str
    kind: str
    entrypoint: str
    owner: str
    display_name: str = ""
    intents: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    required_context: tuple[str, ...] = ()
    allowed_writes: tuple[str, ...] = ()
    fallback_behavior: str = ""
    notes: str = ""
    question_strategy: QuestionRoutingStrategy | None = field(default=None)
