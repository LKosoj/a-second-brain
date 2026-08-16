"""OpenAI-backed helper for planning qmd archive recall."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from openai import OpenAI

from d_brain.services.localization import normalize_language
from d_brain.services.qmd import (
    DEFAULT_RECALL_LIMIT,
    RECALL_CONFIDENCE_THRESHOLD,
    QmdService,
)

logger = logging.getLogger(__name__)

_RECALL_FETCH_LIMIT = 100
_RECALL_MIN_RESULTS = 10
_RECALL_MAX_RESULTS = 20
_PLANNER_REQUEST_TIMEOUT = 30.0
_HISTORY_MIN_DISTINCT_MONTHS = 3
_HISTORY_MIN_OLDEST_AGE_DAYS = 90
_FULLTEXT_MIN_SOURCES = 5
_FULLTEXT_MAX_SOURCES = 7
_HISTORY_SCOPE_VALUES = {"latest", "recent", "quarter", "year", "full"}
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_./:-]{1,}")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_ISO_YEAR_RE = re.compile(r"^\d{4}$")

_PLANNER_SYSTEM_PROMPT = """You decide whether a local qmd archive recall
should run before a main assistant answers a task.

Return JSON only with this exact schema:
{
  "use_recall": true,
  "query": "space-separated keywords/entities only, in the user's language",
  "history_scope": "latest|recent|quarter|year|full",
  "history_start_hint": "YYYY-MM-DD or empty",
  "deep": false,
  "limit": 3,
  "reason": "short reason"
}

Rules:
- Prefer "use_recall": false when live files explicitly named in the task
  should be enough.
- Prefer recall for questions about prior decisions, history, carry-over,
  earlier notes, related meetings, or long-running context.
- Keep "query" short and concrete, ideally 2-6 keywords.
- "query" must be keyword-style, not a sentence.
- Prefer exact entities, acronyms, product names, code names, and unique nouns
  copied from the task.
- Avoid filler verbs, helper phrases, and broad framing words when more
  specific entities are available.
- Use the declared content language exactly. Do not switch languages.
- Prefer entity-bearing terms from the task over generic words like
  "context", "history", or "recent activities".
- Choose "history_scope" to tell the main assistant how far back it should
  analyze the topic:
  - "latest" = only the latest snapshot/current state
  - "recent" = last few weeks or current cycle
  - "quarter" = last few months / recent phase of a long-running topic
  - "year" = long-running project or topic with important milestones across months
  - "full" = origin/background across the full archived history
- Fill "history_start_hint" only when the task implies a meaningful start point.
  Use an ISO date when possible. Otherwise return an empty string.
- Use "deep": true only for older history, long-term trends, or when recent
  recall is unlikely to be enough.
- Keep "limit" between 1 and 20.
- Never answer the task itself.
"""

_STOPWORDS = {
    "ru": {
        "а",
        "без",
        "был",
        "были",
        "быть",
        "в",
        "во",
        "вопрос",
        "делал",
        "делали",
        "делать",
        "где",
        "для",
        "до",
        "если",
        "за",
        "и",
        "из",
        "или",
        "как",
        "какая",
        "какие",
        "какой",
        "когда",
        "ли",
        "мне",
        "мы",
        "на",
        "надо",
        "не",
        "недавно",
        "неделе",
        "недели",
        "неделя",
        "нужно",
        "о",
        "об",
        "по",
        "под",
        "после",
        "прошлой",
        "прошлую",
        "прошлый",
        "прошлые",
        "про",
        "раньше",
        "ранее",
        "решили",
        "решение",
        "с",
        "со",
        "сделай",
        "сделал",
        "сделали",
        "сейчас",
        "так",
        "то",
        "только",
        "тут",
        "ты",
        "у",
        "уже",
        "уточни",
        "что",
        "чтобы",
        "эта",
        "этим",
        "это",
        "эту",
        "я",
    },
    "en": {
        "a",
        "an",
        "and",
        "before",
        "do",
        "for",
        "from",
        "get",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "last",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "previous",
        "recent",
        "show",
        "that",
        "the",
        "this",
        "to",
        "week",
        "we",
        "what",
        "were",
        "when",
        "where",
        "with",
        "you",
    },
}
_GENERIC_QUERY_TOKENS = {
    "ru": {"контекст", "история", "недавно", "недавние", "активности", "активность"},
    "en": {"context", "history", "recent", "activities", "activity", "updates"},
}
_RECALL_CUES = {
    "ru": (
        "раньше",
        "ранее",
        "недавно",
        "до этого",
        "что мы решили",
        "что я делал",
        "что делали",
        "история",
        "контекст",
        "прошл",
        "последн",
    ),
    "en": (
        "earlier",
        "before",
        "recent",
        "history",
        "carry-over",
        "what did i",
        "what did we",
        "previous",
        "last ",
    ),
}
_RECENT_CUES = {
    "ru": ("недавно", "сегодня", "вчера", "на этой неделе", "последн"),
    "en": ("recent", "today", "yesterday", "this week", "last "),
}


@dataclass(frozen=True)
class RecallPlannerConfig:
    """Connection settings for the OpenAI-compatible recall planner."""

    model: str = ""
    api_key: str = ""
    base_url: str = ""
    language: str = "en"
    disable_thinking: bool = False

    @property
    def enabled(self) -> bool:
        """Whether planner calls can be attempted."""
        return bool(self.model.strip() and self.api_key.strip())


class _OpenAIClientKwargs(TypedDict):
    api_key: str
    base_url: NotRequired[str]


@dataclass(frozen=True)
class RecallPlan:
    """Structured plan for qmd recall."""

    use_recall: bool = False
    query: str = ""
    fallback_query: str = ""
    history_scope: str = "latest"
    history_start_hint: str = ""
    deep: bool = False
    limit: int = DEFAULT_RECALL_LIMIT
    reason: str = ""


def _compact_task_text(task: str) -> str:
    """Normalize planner input without truncating task text."""
    return re.sub(r"\s+", " ", task).strip()


def _extract_first_json_dict(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _task_keywords(task: str, language: str, *, limit: int = 6) -> list[str]:
    """Extract compact literal search terms from the task itself."""
    normalized_language = normalize_language(language)
    stopwords = _STOPWORDS.get(normalized_language, _STOPWORDS["en"])
    keywords: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(task):
        cleaned = token.strip("._-:/")
        lowered = cleaned.lower()
        if len(lowered) < 2 or lowered in stopwords:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        keyword = cleaned if any(char.isupper() for char in cleaned) else lowered
        keywords.append(keyword)
        if len(keywords) >= limit:
            break
    return keywords


def _build_literal_query(task: str, language: str) -> str:
    """Build a deterministic fallback query directly from task entities."""
    return " ".join(_task_keywords(task, language))


def _query_has_expected_language(query: str, task: str, language: str) -> bool:
    """Check whether planner query matches the configured content language."""
    normalized_language = normalize_language(language)
    if normalized_language == "ru" and _CYRILLIC_RE.search(task):
        if _CYRILLIC_RE.search(query):
            return True
        task_keywords = _task_keywords(task, language)
        if task_keywords and all(
            _LATIN_RE.search(keyword) for keyword in task_keywords
        ):
            return bool(_LATIN_RE.search(query))
        return False
    if normalized_language == "ru":
        return True
    if normalized_language == "en" and _LATIN_RE.search(task):
        return bool(_LATIN_RE.search(query))
    return True


def _query_is_generic(query: str, language: str) -> bool:
    """Detect planner queries that are too generic to retrieve useful notes."""
    normalized_language = normalize_language(language)
    generic = _GENERIC_QUERY_TOKENS.get(normalized_language, set())
    keywords = [token.lower() for token in _task_keywords(query, language)]
    if not keywords:
        return True
    return all(token in generic for token in keywords)


def _query_matches_task_entities(query: str, task: str, language: str) -> bool:
    """Check whether planner query still contains the task's main entities."""
    query_terms = {token.casefold() for token in _task_keywords(query, language)}
    task_terms = {token.casefold() for token in _task_keywords(task, language)}
    if not query_terms or not task_terms:
        return True
    return bool(query_terms & task_terms)


def _task_likely_needs_recall(task: str, purpose: str, language: str) -> bool:
    """Detect clear history/continuity questions even if planner under-fires."""
    if purpose not in {"question_answer", "assistant_request"}:
        return False
    lowered = task.casefold()
    normalized_language = normalize_language(language)
    return any(cue in lowered for cue in _RECALL_CUES.get(normalized_language, ()))


def _default_history_scope(
    task: str, *, purpose: str, deep: bool, language: str
) -> str:
    """Fallback history depth when planner does not provide one."""
    if deep:
        return "full"
    if _task_has_recent_bias(task, language):
        return "recent"
    if _task_likely_needs_recall(task, purpose, language):
        return "quarter"
    return "latest"


def _normalize_history_scope(
    value: Any, *, task: str, purpose: str, deep: bool, language: str
) -> str:
    """Sanitize planner-provided history depth into a stable enum."""
    raw = str(value or "").strip().lower()
    if raw in _HISTORY_SCOPE_VALUES:
        return raw
    return _default_history_scope(
        task,
        purpose=purpose,
        deep=deep,
        language=language,
    )


def _normalize_history_start_hint(value: Any) -> str:
    """Accept only stable ISO-like planner hints for history start."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if _ISO_DATE_RE.match(raw):
        return raw
    if _ISO_MONTH_RE.match(raw):
        return f"{raw}-01"
    if _ISO_YEAR_RE.match(raw):
        return f"{raw}-01-01"
    return ""


def _history_scope_lookback_days(scope: str) -> int:
    """Approximate minimum history window for each planner scope."""
    if scope == "latest":
        return 14
    if scope == "recent":
        return 60
    if scope == "quarter":
        return 120
    if scope == "year":
        return 400
    return 730


def _history_scope_min_months(scope: str) -> int:
    """Minimum distinct month coverage expected for each planner scope."""
    if scope == "latest":
        return 1
    if scope == "recent":
        return 2
    if scope == "quarter":
        return 3
    if scope == "year":
        return 6
    return 9


def _history_scope_requires_deep(scope: str) -> bool:
    """Whether planner scope is deep enough to merit immediate deep recall."""
    return scope in {"quarter", "year", "full"}


def _history_analysis_start(plan: RecallPlan) -> str:
    """Resolve one explicit start point to give the main assistant."""
    if plan.history_start_hint:
        return plan.history_start_hint
    if plan.history_scope == "full":
        return ""
    lookback_days = _history_scope_lookback_days(plan.history_scope)
    return (date.today() - timedelta(days=lookback_days)).isoformat()


def _normalize_plan(
    payload: dict[str, Any], *, task: str, purpose: str, language: str
) -> RecallPlan:
    """Sanitize planner output into a stable internal contract."""
    use_recall = bool(payload.get("use_recall"))
    query = str(payload.get("query", "")).strip()
    literal_query = _build_literal_query(task, language)
    compact_query = _build_literal_query(query, language)
    if compact_query:
        query = compact_query
    if (
        not query
        or _query_is_generic(query, language)
        or not _query_matches_task_entities(query, task, language)
        or not _query_has_expected_language(query, task, language)
    ):
        query = literal_query
    if (
        not use_recall
        and literal_query
        and _task_likely_needs_recall(task, purpose, language)
    ):
        use_recall = True
    if not query:
        use_recall = False
    deep = bool(payload.get("deep"))
    history_scope = _normalize_history_scope(
        payload.get("history_scope"),
        task=task,
        purpose=purpose,
        deep=deep,
        language=language,
    )
    history_start_hint = _normalize_history_start_hint(
        payload.get("history_start_hint")
    )
    limit_raw = payload.get("limit", DEFAULT_RECALL_LIMIT)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = DEFAULT_RECALL_LIMIT
    limit = min(max(limit, 1), 20)
    reason = str(payload.get("reason", "")).strip()
    return RecallPlan(
        use_recall=use_recall,
        query=query,
        fallback_query=(
            ""
            if literal_query.casefold() == query.casefold()
            else literal_query
        ),
        history_scope=history_scope,
        history_start_hint=history_start_hint,
        deep=deep,
        limit=limit,
        reason=reason,
    )


def _plan_queries(plan: RecallPlan) -> list[str]:
    """Build the ordered list of recall queries to execute."""
    queries: list[str] = []
    for query in (plan.query, plan.fallback_query):
        cleaned = query.strip()
        if not cleaned:
            continue
        if any(cleaned.casefold() == existing.casefold() for existing in queries):
            continue
        queries.append(cleaned)
    return queries


def _task_has_recent_bias(task: str, language: str) -> bool:
    """Detect whether the question is explicitly about fresh/recent context."""
    lowered = task.casefold()
    normalized_language = normalize_language(language)
    return any(cue in lowered for cue in _RECENT_CUES.get(normalized_language, ()))


def _literal_match_score(
    item: dict[str, Any], keywords: list[str]
) -> tuple[float, int]:
    """Compute a small lexical bonus from title/path/snippet matches."""
    title = str(item.get("title", "")).casefold()
    rel_path = str(item.get("rel_path", "")).casefold()
    snippet = str(item.get("snippet", "")).casefold()
    score = 0.0
    matches = 0
    for keyword in keywords:
        lowered = keyword.casefold()
        if lowered in title:
            score += 0.12
            matches += 1
        elif lowered in rel_path:
            score += 0.08
            matches += 1
        elif lowered in snippet:
            score += 0.04
            matches += 1
    return min(score, 0.28), matches


def _path_bias_score(item: dict[str, Any], *, recent_bias: bool) -> float:
    """Small path-aware bonus/penalty to suppress obvious noise."""
    rel_path = str(item.get("rel_path", ""))
    if rel_path == "MEMORY.md":
        return 0.08
    if rel_path.startswith("goals/"):
        return 0.06
    if recent_bias and rel_path.startswith("daily/"):
        return 0.10
    return 0.0


def _merge_recall_payloads(
    payloads: list[dict[str, Any]],
    *,
    task: str,
    language: str,
    limit: int,
) -> dict[str, Any]:
    """Merge multiple recall passes into one prompt-ready result set."""
    merged: dict[str, dict[str, Any]] = {}
    keywords = _task_keywords(task, language, limit=8)
    recent_bias = _task_has_recent_bias(task, language)

    for payload in payloads:
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue
            file_ref = str(item.get("file", ""))
            rel_path = str(item.get("rel_path", "")) or file_ref
            literal_bonus, literal_matches = _literal_match_score(item, keywords)
            score = float(
                item.get("effective_score", item.get("score", 0.0)) or 0.0
            )
            score += literal_bonus
            score += _path_bias_score(
                item,
                recent_bias=recent_bias,
            )
            stored = dict(item)
            stored["effective_score"] = round(score, 4)
            stored["confidence"] = max(0.0, min(float(stored["effective_score"]), 1.0))
            stored["_literal_matches"] = literal_matches
            current = merged.get(rel_path)
            if current is None or float(stored["effective_score"]) > float(
                current.get("effective_score", 0.0)
            ):
                merged[rel_path] = stored

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            float(item.get("effective_score", 0.0)),
            int(item.get("_literal_matches", 0)),
        ),
        reverse=True,
    )
    backends = sorted(
        {
            str(payload.get("backend", "none"))
            for payload in payloads
            if str(payload.get("backend", "none"))
        }
    )
    mode = payloads[0].get("mode", "recall") if payloads else "recall"
    joined_query = ", ".join(
        payload.get("query", "") for payload in payloads if payload.get("query")
    )
    confidence = (
        max(0.0, min(float(ranked[0].get("confidence", 0.0) or 0.0), 1.0))
        if ranked
        else 0.0
    )
    return {
        "query": joined_query,
        "backend": "+".join(backends) if backends else "none",
        "mode": mode,
        "confidence": round(confidence, 4),
        "results": ranked[:limit],
    }


def _source_family(rel_path: str) -> str:
    """Collapse vault paths into broad source families for follow-up reads."""
    if rel_path == "MEMORY.md":
        return "memory"
    if rel_path.startswith("goals/"):
        return "goals"
    if rel_path.startswith("business/"):
        return "business"
    if rel_path.startswith("projects/"):
        return "projects"
    if rel_path.startswith("thoughts/"):
        return "thoughts"
    if rel_path.startswith("daily/"):
        return "daily"
    if rel_path.startswith("imports/plaud/notes/"):
        return "plaud"
    if rel_path.startswith("imports/documents/notes/"):
        return "documents"
    if rel_path.startswith("imports/documents/forwarded/"):
        return "documents"
    if rel_path.startswith("imports/youtube/notes/"):
        return "youtube"
    if rel_path.startswith("summaries/"):
        return "summaries"
    if rel_path.startswith("MOC/"):
        return "moc"
    return "other"


def _followup_path(item: dict[str, Any]) -> str:
    """Prefer vault-relative paths for mandatory follow-up reads."""
    return str(item.get("rel_path", "") or item.get("file", "")).strip()


def _select_fulltext_followup_sources(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Choose a compact but diverse set of full-text reads after ranking."""
    ranked = [
        item
        for item in payload.get("results", [])
        if isinstance(item, dict) and _followup_path(item)
    ]
    desired_min = min(len(ranked), _FULLTEXT_MIN_SOURCES)
    desired_max = min(len(ranked), _FULLTEXT_MAX_SOURCES)
    selected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_families: set[str] = set()

    for item in ranked:
        rel_path = _followup_path(item)
        family = _source_family(rel_path)
        if rel_path in seen_paths or family in seen_families:
            continue
        selected.append(item)
        seen_paths.add(rel_path)
        seen_families.add(family)
        if len(selected) >= desired_max:
            return selected

    if len(selected) < desired_min:
        for item in ranked:
            rel_path = _followup_path(item)
            if rel_path in seen_paths:
                continue
            selected.append(item)
            seen_paths.add(rel_path)
            if len(selected) >= desired_min:
                break

    return selected


def _format_fulltext_followup(payload: dict[str, Any]) -> str:
    """Render mandatory follow-up file reads for the main assistant."""
    selected = _select_fulltext_followup_sources(payload)
    if not selected:
        return ""

    lines = [
        "FULLTEXT FOLLOW-UP:",
        "- Before answering, read every listed source file fully.",
        (
            "- Use those full reads to reconstruct the history of the topic "
            "and the key moments over time."
        ),
    ]
    for index, item in enumerate(selected, start=1):
        rel_path = _followup_path(item)
        metadata: list[str] = []
        record_date = str(item.get("record_date", "") or "")
        if record_date:
            metadata.append(f"date={record_date}")
        metadata.append(
            f"score={float(item.get('confidence', 0.0) or 0.0):.2f}"
        )
        lines.append(f"{index}. {rel_path} [{', '.join(metadata)}]")
    return "\n".join(lines)


def _select_prompt_results(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim merged recall to the prompt-sized slice with a low-score floor."""
    ranked = [
        item for item in payload.get("results", []) if isinstance(item, dict)
    ]
    available_count = len(ranked)
    confident_count = sum(
        1
        for item in ranked
        if float(item.get("confidence", 0.0) or 0.0) >= RECALL_CONFIDENCE_THRESHOLD
    )
    floor_count = min(available_count, _RECALL_MIN_RESULTS)
    selected_count = min(
        available_count,
        max(floor_count, confident_count),
        _RECALL_MAX_RESULTS,
    )
    return {
        **payload,
        "available_results": available_count,
        "confident_results": confident_count,
        "selected_results": selected_count,
        "results": ranked[:selected_count],
    }


def _needs_history_backfill(
    payloads: list[dict[str, Any]],
    *,
    purpose: str,
    deep: bool,
    history_scope: str,
    history_start: str,
) -> bool:
    """Detect when the current recall window is too recent to reconstruct history."""
    if deep or purpose not in {"question_answer", "assistant_request"}:
        return False
    if history_scope == "latest":
        return False

    results = [
        item
        for payload in payloads
        for item in payload.get("results", [])
        if isinstance(item, dict)
    ]
    if not results:
        return False

    dated = [
        item for item in results if str(item.get("record_date", "") or "").strip()
    ]
    if len(dated) < 3:
        return True

    distinct_months = {
        str(item.get("record_date", "") or "")[:7]
        for item in dated
        if len(str(item.get("record_date", "") or "")) >= 7
    }
    age_values = [
        int(item.get("age_days", 0) or 0)
        for item in dated
        if item.get("age_days") is not None
    ]
    if not age_values:
        return True
    oldest_age_days = max(age_values)
    required_oldest_age = max(
        _HISTORY_MIN_OLDEST_AGE_DAYS,
        _history_scope_lookback_days(history_scope),
    )
    required_months = max(
        _HISTORY_MIN_DISTINCT_MONTHS,
        _history_scope_min_months(history_scope),
    )
    if history_start:
        try:
            start_date = date.fromisoformat(history_start)
        except ValueError:
            start_date = None
        if start_date is not None:
            required_oldest_age = max(
                required_oldest_age,
                max(0, (date.today() - start_date).days),
            )
    return (
        len(distinct_months) < required_months
        or oldest_age_days < required_oldest_age
    )


def _build_client(config: RecallPlannerConfig) -> OpenAI:
    """Create an OpenAI-compatible SDK client."""
    kwargs = _OpenAIClientKwargs(api_key=config.api_key)
    if config.base_url.strip():
        kwargs["base_url"] = config.base_url.strip()
    return OpenAI(**kwargs)


def plan_qmd_recall(
    task: str,
    *,
    purpose: str,
    config: RecallPlannerConfig,
    client: Any | None = None,
) -> RecallPlan:
    """Ask a small helper model whether qmd recall should run."""
    if not config.enabled:
        return RecallPlan()

    normalized_task = _compact_task_text(task)
    if not normalized_task:
        return RecallPlan()

    planner_client = client or _build_client(config)
    create_kwargs: dict[str, Any] = {
        "model": config.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Content language: {normalize_language(config.language)}\n"
                    f"Purpose: {purpose}\n"
                    "Task:\n"
                    f"{normalized_task}\n"
                ),
            },
        ],
        "timeout": _PLANNER_REQUEST_TIMEOUT,
    }
    if config.disable_thinking:
        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    response = planner_client.chat.completions.create(**create_kwargs)
    content = response.choices[0].message.content or "{}"
    payload = _extract_first_json_dict(content)
    return _normalize_plan(
        payload,
        task=normalized_task,
        purpose=purpose,
        language=config.language,
    )


def build_qmd_recall_block(
    vault_path: Path,
    *,
    task: str,
    purpose: str,
    config: RecallPlannerConfig,
    client: Any | None = None,
) -> str:
    """Build a prompt-ready archive context block from planner + qmd."""
    plan = plan_qmd_recall(
        task,
        purpose=purpose,
        config=config,
        client=client,
    )
    if not plan.use_recall or not plan.query:
        return ""

    qmd = QmdService(vault_path)
    queries = _plan_queries(plan)
    payloads = [
        qmd.recall(
            query,
            deep=plan.deep,
            limit=_RECALL_FETCH_LIMIT,
        )
        for query in queries
    ]
    history_start = _history_analysis_start(plan)
    if _history_scope_requires_deep(plan.history_scope) and not plan.deep:
        history_query = next(iter(queries), "")
        if history_query:
            logger.info(
                (
                    "QMD auto-recall using planner history scope: purpose=%s "
                    "scope=%s start=%s query=%r"
                ),
                purpose,
                plan.history_scope,
                history_start or "earliest-relevant",
                history_query,
            )
            payloads.append(
                qmd.recall(
                    history_query,
                    deep=True,
                    limit=_RECALL_FETCH_LIMIT,
                )
            )
    elif _needs_history_backfill(
        payloads,
        purpose=purpose,
        deep=plan.deep,
        history_scope=plan.history_scope,
        history_start=history_start,
    ):
        history_query = next(iter(queries), "")
        if history_query:
            logger.info(
                (
                    "QMD auto-recall requesting deep history backfill: purpose=%s "
                    "scope=%s start=%s query=%r"
                ),
                purpose,
                plan.history_scope,
                history_start or "earliest-relevant",
                history_query,
            )
            payloads.append(
                qmd.recall(
                    history_query,
                    deep=True,
                    limit=_RECALL_FETCH_LIMIT,
                )
            )
    merged_payload = _merge_recall_payloads(
        payloads,
        task=task,
        language=config.language,
        limit=_RECALL_FETCH_LIMIT,
    )
    payload = _select_prompt_results(merged_payload)
    if not payload.get("results"):
        logger.info(
            "QMD auto-recall skipped: purpose=%s query=%r no results",
            purpose,
            plan.query,
        )
        return ""

    confidence = float(payload.get("confidence", 0.0) or 0.0)
    logger.info(
        (
            "QMD auto-recall decision: purpose=%s query=%r backend=%s "
            "available=%s selected=%s confident=%s top_confidence=%.2f "
            "threshold=%.2f action=%s"
        ),
        purpose,
        payload.get("query", plan.query),
        payload.get("backend", "none"),
        int(payload.get("available_results", 0) or 0),
        len(payload.get("results", [])),
        int(payload.get("confident_results", 0) or 0),
        confidence,
        RECALL_CONFIDENCE_THRESHOLD,
        (
            "send-threshold"
            if len(payload.get("results", []))
            == min(
                int(payload.get("confident_results", 0) or 0),
                _RECALL_MAX_RESULTS,
            )
            else "send-floor"
        ),
    )
    for index, item in enumerate(payload.get("results", []), start=1):
        logger.info(
            (
                "QMD auto-recall merged #%s path=%s score=%.2f "
                "confidence=%.2f age_days=%s date=%s"
            ),
            index,
            item.get("rel_path", "") or item.get("file", ""),
            float(item.get("effective_score", 0.0) or 0.0),
            float(item.get("confidence", 0.0) or 0.0),
            item.get("age_days"),
            item.get("record_date", ""),
        )

    reason = plan.reason or "planner requested archive recall"
    history_scope = plan.history_scope
    history_scope_line = (
        f"History scope: {history_scope}\n"
        if history_scope
        else ""
    )
    history_start_line = (
        f"Analyze history from: {history_start}\n"
        if history_start
        else (
            "Analyze history from: earliest relevant archived history\n"
            if history_scope == "full"
            else ""
        )
    )
    return (
        "=== AUTO ARCHIVE RECALL ===\n"
        f"Reason: {reason}\n"
        f"{history_scope_line}"
        f"{history_start_line}"
        f"{QmdService.format_recall(payload)}\n"
        f"{_format_fulltext_followup(payload)}\n"
        "HISTORY CHECKLIST:\n"
        "- Reconstruct the history of the question over time.\n"
        "- Call out key moments, decisions, blockers, and what changed.\n"
        "- Follow the declared history scope/start point when deciding how far "
        "back to analyze the topic.\n"
        "- Use the freshest evidence for current status, while preserving "
        "important older milestones when they explain the present state.\n"
        "Age rule: prefer newer evidence. Older notes are less valuable when "
        "they conflict with fresher results, unless the note is durable "
        "context such as MEMORY, goals, MOC, or business/projects indexes.\n"
        "Use this block first. Only run extra qmd commands if it is clearly "
        "insufficient.\n"
        "=== END AUTO ARCHIVE RECALL ==="
    )
