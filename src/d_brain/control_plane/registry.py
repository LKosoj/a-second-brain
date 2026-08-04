"""Canonical control-plane workflow registry."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

from d_brain.control_plane.contracts import (
    QUESTION_CONTEXT_COMPILED,
    QUESTION_CONTEXT_RECALL,
    WORKFLOW_KIND_CAPTURE,
    WORKFLOW_KIND_INTEGRATION,
    WORKFLOW_KIND_MAINTENANCE,
    WORKFLOW_KIND_QUESTION,
    QuestionRoutingStrategy,
    WorkflowSpec,
)

_PLANNING_ROUTE_CUES = (
    "приоритет",
    "приорит",
    "фокус",
    "сфокус",
    "что делать",
    "что мне делать",
    "что делать дальше",
    "на эту неделю",
    "на неделе",
    "на сегодня",
    "план",
    "план на",
    "следующий шаг",
    "next step",
    "следующие шаги",
)
_STATUS_HISTORY_ROUTE_CUES = (
    "что по ",
    "что сейчас",
    "статус",
    "что мы решили",
    "что решили",
    "раньше",
    "ранее",
    "история",
    "что изменилось",
    "как дела с",
    "с начала",
)
_RELATIONSHIP_ROUTE_CUES = (
    "кому",
    "челов",
    "контакт",
    "клиент",
    "партнер",
    "партнёр",
    "обещал",
    "договорились с",
)
_FACT_LOOKUP_PREFIXES = (
    "когда ",
    "где ",
    "сколько ",
    "какой ",
    "какая ",
    "какие ",
    "какое ",
    "which ",
    "when ",
    "where ",
    "how many ",
)

_QUESTION_ENTRYPOINT = "d_brain.services.processor.CliProcessor.answer_question"

CONTROL_PLANE_REGISTRY: tuple[WorkflowSpec, ...] = (
    WorkflowSpec(
        name="capture.daily-entry",
        kind=WORKFLOW_KIND_CAPTURE,
        entrypoint="d_brain.services.storage.VaultStorage.append_to_daily",
        owner="storage",
        display_name="Daily Entry Capture",
        intents=("capture",),
        required_context=("daily",),
        allowed_writes=("daily", "qmd", "compiled"),
        fallback_behavior="Persist raw entry and keep indexes best-effort.",
        notes="Primary text/voice/photo/forward capture path into daily notes.",
    ),
    WorkflowSpec(
        name="question.planning",
        kind=WORKFLOW_KIND_QUESTION,
        entrypoint=_QUESTION_ENTRYPOINT,
        owner="processor",
        display_name="Question Planning",
        intents=("question",),
        required_context=("core", "compiled", "recall"),
        allowed_writes=("answer-artifact",),
        fallback_behavior=(
            "Fall back to core-first answer flow when no stronger planning "
            "signal exists."
        ),
        notes="Questions about priorities, focus, weekly plan, and next steps.",
        question_strategy=QuestionRoutingStrategy(
            route="planning",
            contains_any=_PLANNING_ROUTE_CUES,
            instructions=(
                "Use curated core context first: MEMORY, goals, business/projects "
                "indexes, and live Todoist state.",
                "Use compiled briefings only for directly relevant projects, people, "
                "or ongoing threads.",
                "Use archive recall only when core context and compiled briefings do "
                "not explain the current workload, constraints, or commitments.",
            ),
            context_blocks=(
                QUESTION_CONTEXT_COMPILED,
                QUESTION_CONTEXT_RECALL,
            ),
        ),
    ),
    WorkflowSpec(
        name="question.relationship",
        kind=WORKFLOW_KIND_QUESTION,
        entrypoint=_QUESTION_ENTRYPOINT,
        owner="processor",
        display_name="Question Relationship",
        intents=("question",),
        required_context=("compiled", "core", "recall"),
        allowed_writes=("answer-artifact",),
        fallback_behavior=(
            "Escalate to archive recall when relationship history spans multiple "
            "days or meetings."
        ),
        notes=(
            "Questions about people, commitments, contacts, and recurring "
            "relationship context."
        ),
        question_strategy=QuestionRoutingStrategy(
            route="relationship",
            contains_any=_RELATIONSHIP_ROUTE_CUES,
            instructions=(
                "Start from relevant people or project briefings when available.",
                "Verify promises, commitments, and changes against curated core "
                "context and archive evidence.",
                "Escalate to archive recall when the relationship history spans "
                "multiple days or meetings.",
            ),
            context_blocks=(
                QUESTION_CONTEXT_COMPILED,
                QUESTION_CONTEXT_RECALL,
            ),
        ),
    ),
    WorkflowSpec(
        name="question.status-history",
        kind=WORKFLOW_KIND_QUESTION,
        entrypoint=_QUESTION_ENTRYPOINT,
        owner="processor",
        display_name="Question Status History",
        intents=("question",),
        required_context=("compiled", "core", "recall"),
        allowed_writes=("answer-artifact",),
        fallback_behavior=(
            "Use archive recall to reconstruct longer timelines, conflicting "
            "evidence, or missing history."
        ),
        notes="Questions about current status, earlier decisions, and topic history.",
        question_strategy=QuestionRoutingStrategy(
            route="status_history",
            contains_any=_STATUS_HISTORY_ROUTE_CUES,
            instructions=(
                "Start from compiled briefings when they match the topic.",
                "Verify status and history against curated core context.",
                "Use archive recall to reconstruct longer timelines, conflicting "
                "evidence, or missing history.",
            ),
            context_blocks=(
                QUESTION_CONTEXT_COMPILED,
                QUESTION_CONTEXT_RECALL,
            ),
        ),
    ),
    WorkflowSpec(
        name="question.fact-lookup",
        kind=WORKFLOW_KIND_QUESTION,
        entrypoint=_QUESTION_ENTRYPOINT,
        owner="processor",
        display_name="Question Fact Lookup",
        intents=("question",),
        required_context=("recall", "compiled"),
        allowed_writes=("answer-artifact",),
        fallback_behavior="Prefer exact evidence even when compiled briefings exist.",
        notes="Questions about exact dates, names, IDs, links, and point facts.",
        question_strategy=QuestionRoutingStrategy(
            route="fact_lookup",
            startswith_any=_FACT_LOOKUP_PREFIXES,
            instructions=(
                "Use exact or archival evidence first.",
                "Prefer qmd recall/get and rg for precise facts, dates, names, IDs, "
                "or links.",
                "Use compiled briefings only as supporting context, not as the sole "
                "source of truth.",
            ),
            context_blocks=(
                QUESTION_CONTEXT_RECALL,
                QUESTION_CONTEXT_COMPILED,
            ),
        ),
    ),
    WorkflowSpec(
        name="question.general",
        kind=WORKFLOW_KIND_QUESTION,
        entrypoint=_QUESTION_ENTRYPOINT,
        owner="processor",
        display_name="Question General",
        intents=("question",),
        required_context=("compiled", "core", "recall"),
        allowed_writes=("answer-artifact",),
        fallback_behavior=(
            "Default route when no more specific question strategy matches."
        ),
        notes="General question-answer workflow.",
        question_strategy=QuestionRoutingStrategy(
            route="general",
            instructions=(
                "Start from compiled briefings when they match the topic.",
                "Verify status and history against curated core context.",
                "Use archive recall to reconstruct longer timelines, conflicting "
                "evidence, or missing history.",
            ),
            context_blocks=(
                QUESTION_CONTEXT_COMPILED,
                QUESTION_CONTEXT_RECALL,
            ),
        ),
    ),
    WorkflowSpec(
        name="maintenance.compiled-nightly",
        kind=WORKFLOW_KIND_MAINTENANCE,
        entrypoint=(
            "d_brain.services.processor.CliProcessor."
            "_run_compiled_nightly_maintenance"
        ),
        owner="processor",
        display_name="Compiled Maintenance",
        triggers=("scheduled-post",),
        allowed_writes=("compiled", "qmd"),
        fallback_behavior=(
            "Best-effort nightly drain, lint, and source-aware backfill for "
            "compiled briefings."
        ),
        notes="Scheduled compiled maintenance after daily and periodic cycles.",
    ),
    WorkflowSpec(
        name="maintenance.vault-health",
        kind=WORKFLOW_KIND_MAINTENANCE,
        entrypoint="d_brain.services.processor.CliProcessor._run_vault_health_cycle",
        owner="processor",
        display_name="Vault Health",
        triggers=("scheduled-post",),
        allowed_writes=("graph", "moc", "links"),
        fallback_behavior=(
            "Safe health pass with analysis first and narrow repair only on low "
            "score."
        ),
        notes="Scheduled vault health check after compiled maintenance.",
    ),
    WorkflowSpec(
        name="maintenance.scheduled-cycle",
        kind=WORKFLOW_KIND_MAINTENANCE,
        entrypoint="d_brain.services.processor.CliProcessor.run_scheduled_cycle",
        owner="processor",
        display_name="Scheduled Cycle",
        triggers=("scheduled-root",),
        allowed_writes=(
            "daily",
            "thoughts",
            "tasks",
            "summaries",
            "compiled",
            "qmd",
        ),
        fallback_behavior="Daily scheduled stack with mandatory final qmd refresh.",
        notes="Top-level scheduled processing workflow.",
    ),
    WorkflowSpec(
        name="integration.documents.archive",
        kind=WORKFLOW_KIND_INTEGRATION,
        entrypoint="d_brain.services.documents.DocumentArchiveService.archive_document",
        owner="documents",
        display_name="Document Archive",
        allowed_writes=("imports/documents", "daily", "qmd", "compiled"),
        fallback_behavior=(
            "Persist original and extracted note even when follow-up indexing is "
            "best-effort."
        ),
        notes="Archive one uploaded document into searchable vault artifacts.",
    ),
    WorkflowSpec(
        name="integration.web.archive",
        kind=WORKFLOW_KIND_INTEGRATION,
        entrypoint="d_brain.services.web_archive.WebArchiveService.archive_page",
        owner="web",
        display_name="Web Source Archive",
        allowed_writes=("imports/web", "daily", "qmd", "compiled"),
        fallback_behavior=(
            "Persist extracted source text and metadata even when summary or "
            "follow-up indexing is unavailable."
        ),
        notes="Archive one generic web source into immutable vault artifacts.",
    ),
    WorkflowSpec(
        name="integration.youtube.archive",
        kind=WORKFLOW_KIND_INTEGRATION,
        entrypoint="d_brain.services.youtube_transcript.YouTubeArchiveService.archive_transcript",
        owner="youtube",
        display_name="YouTube Archive",
        allowed_writes=("imports/youtube", "daily", "qmd", "compiled"),
        fallback_behavior=(
            "Persist transcript note and keep qmd/compiled sync best-effort."
        ),
        notes="Archive one fetched YouTube transcript into vault notes.",
    ),
    WorkflowSpec(
        name="integration.plaud.sync",
        kind=WORKFLOW_KIND_INTEGRATION,
        entrypoint="d_brain.services.plaud.PlaudSyncService.sync",
        owner="plaud",
        display_name="PLAUD Sync",
        allowed_writes=(
            "imports/plaud",
            "daily",
            "todoist",
            "qmd",
            "compiled",
        ),
        fallback_behavior=(
            "Best-effort PLAUD sync with durable state locking and follow-up "
            "indexing."
        ),
        notes="Hourly or manual PLAUD synchronization workflow.",
    ),
)


def iter_workflows(
    *,
    kind: str | None = None,
    trigger: str | None = None,
) -> Iterator[WorkflowSpec]:
    """Yield workflow specs, optionally filtered by kind."""
    for workflow in CONTROL_PLANE_REGISTRY:
        if kind is not None and workflow.kind != kind:
            continue
        if trigger is not None and trigger not in workflow.triggers:
            continue
        yield workflow


def get_workflow(name: str) -> WorkflowSpec:
    """Return one workflow by canonical name."""
    for workflow in CONTROL_PLANE_REGISTRY:
        if workflow.name == name:
            return workflow
    raise KeyError(name)


def get_question_workflow(route: str) -> WorkflowSpec:
    """Return one registered question workflow by route name."""
    for workflow in iter_workflows(kind=WORKFLOW_KIND_QUESTION):
        strategy = workflow.question_strategy
        if strategy is not None and strategy.route == route:
            return workflow
    raise KeyError(route)


def resolve_entrypoint(entrypoint: str) -> object:
    """Resolve a dotted entrypoint path to a live Python object."""
    parts = entrypoint.split(".")
    last_error: Exception | None = None
    for index in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:index])
        try:
            obj: object = importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
            continue
        try:
            for attr in parts[index:]:
                obj = getattr(obj, attr)
        except AttributeError as exc:
            last_error = exc
            continue
        return obj
    if last_error is not None:
        raise last_error
    raise ImportError(f"Unable to resolve entrypoint: {entrypoint}")


def validate_control_plane_registry() -> list[str]:
    """Return registry validation errors, if any."""
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_routes: set[str] = set()
    for workflow in CONTROL_PLANE_REGISTRY:
        if workflow.name in seen_names:
            errors.append(f"duplicate workflow name: {workflow.name}")
        seen_names.add(workflow.name)

        try:
            resolve_entrypoint(workflow.entrypoint)
        except (AttributeError, ImportError) as exc:
            errors.append(
                f"entrypoint unresolved for {workflow.name}: "
                f"{workflow.entrypoint} ({exc})"
            )

        strategy = workflow.question_strategy
        if workflow.kind == WORKFLOW_KIND_QUESTION:
            if strategy is None:
                errors.append(f"question workflow missing strategy: {workflow.name}")
                continue
            if strategy.route in seen_routes:
                errors.append(f"duplicate question route: {strategy.route}")
            seen_routes.add(strategy.route)
            if not strategy.instructions:
                errors.append(
                    f"question workflow missing instructions: {workflow.name}"
                )
            invalid_blocks = [
                value
                for value in strategy.context_blocks
                if value not in {QUESTION_CONTEXT_COMPILED, QUESTION_CONTEXT_RECALL}
            ]
            if invalid_blocks:
                errors.append(
                    f"invalid context blocks for {workflow.name}: {invalid_blocks}"
                )
    return errors
