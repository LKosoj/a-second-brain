"""CLI-backed processing service."""

import fcntl
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from d_brain.control_plane.registry import (
    get_workflow,
    iter_workflows,
    resolve_entrypoint,
)
from d_brain.control_plane.router import (
    build_question_route_block as build_control_plane_question_route_block,
)
from d_brain.control_plane.router import (
    classify_question_route as classify_control_plane_question_route,
)
from d_brain.control_plane.router import (
    iter_question_context_blocks,
    resolve_text_workflow,
)
from d_brain.manifest import (
    VaultManifest,
    load_manifest_for_vault,
)
from d_brain.services.cli_runner import CliExecutionError, CliRunner
from d_brain.services.compiled_briefings import (
    QUESTION_CONTEXT_LIMIT,
    CompiledBriefingCandidate,
    CompiledBriefingService,
)
from d_brain.services.compiled_enrich_report import (
    build_daily_digest,
    describe_budget_exhausted,
    digest_path,
    read_pass_status,
    render_digest_note,
)
from d_brain.services.compiled_fact_check import run_monthly_fact_check
from d_brain.services.compiled_question_provenance import build_question_provenance
from d_brain.services.context_pack import ContextPackBuilder, select_yearly_goals_name
from d_brain.services.decisions_queue import write_queue_document

from d_brain.services.daily_workflow import (  # isort: skip
    INTERACTIVE_MODE as INTERACTIVE_MODE,
    SCHEDULED_MODE as SCHEDULED_MODE,
    DailyWorkflow,
    run_json_phase,
)
from d_brain.services.entry_status import (
    ENTRY_STATUS_ALREADY_PROCESSED,
    normalize_entry_type,
    parse_daily_entry_statuses,
)
from d_brain.services.frontmatter import (
    DuplicateKeyError,
    FrontmatterError,
    UnsafeVaultPathError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import normalize_language, prompt_language_name
from d_brain.services.memory_audit import MemoryAuditService
from d_brain.services.qmd import QmdService
from d_brain.services.recall_planner import (
    RecallPlannerConfig,
    build_qmd_recall_block,
)
from d_brain.services.session import SessionStore
from d_brain.services.source_links import collapse_to_single_line
from d_brain.services.storage import VaultStorage
from d_brain.services.telegram_delivery import send_telegram_text_sync
from d_brain.services.telegram_markup import normalize_markdown_input
from d_brain.services.todoist_projects import (
    TodoistProjectCatalog,
    TodoistProjectRouter,
)
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 2400  # 40 minutes
DAILY_ENTRY_HEADER_RE = re.compile(r"^##\s+\d{2}:\d{2}\s+\[[^\]]+\]\s*$", re.MULTILINE)
REFLECT_DAILY_START_MARKER = "<!-- d-brain:reflect:start -->"
REFLECT_DAILY_END_MARKER = "<!-- d-brain:reflect:end -->"
TEXT_INTENT_CAPTURE = "capture"
TEXT_INTENT_QUESTION = "question"
VAULT_HEALTH_LOW_SCORE_THRESHOLD = 80.0
VAULT_HEALTH_REPORT_SECTION_RE = re.compile(
    r"^#{1,6}[ \t]+(?:(?:📊|🩺)[ \t]+)?"
    r"(?:Здоровье[ \t]+(?:хранилища|vault)|Vault[ \t]+Health)[ \t]*\n"
    r".*?(?=^#{1,6}[ \t]+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
HANDOFF_SECTION_ORDER = (
    "Last Session",
    "Key Decisions",
    "In Progress",
    "Next Steps",
    "Observations",
)
HandoffRevision = tuple[int, int, int, int, bytes]
HANDOFF_BULLET_SECTIONS = frozenset(HANDOFF_SECTION_ORDER[1:])
HANDOFF_EMPTY_SECTION = {
    "Last Session": "(none)",
    "Key Decisions": "- (none)",
    "In Progress": "- (none)",
    "Next Steps": "- (none)",
    "Observations": "- (none)",
}
HANDOFF_SECTION_RE = re.compile(
    r"^##\s+(Last Session|Key Decisions|In Progress|Next Steps|Observations)\s*$",
    re.MULTILINE,
)
HANDOFF_FRONTMATTER_RE = re.compile(r"\A\ufeff?---\n.*?\n---\n*", re.DOTALL)
THOUGHT_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
PROCESS_AUDIT_STATE_TTL_DAYS = 7
# Block-boundary splitter for _insert_after_first_paragraph (ТЗ 7.4 code
# review defect 1): keeps the blank-line separators so the text can be
# reassembled byte-for-byte around the inserted warning.
QUESTION_ANSWER_BLOCK_SPLIT_RE = re.compile(r"(\n[ \t]*\n+)")
QUESTION_ANSWER_HEADING_RE = re.compile(r"^#{1,6}[ \t]+")
QUESTION_ANSWER_TABLE_ROW_RE = re.compile(r"^[ \t]*\|")


class ProcessAlreadyRunningError(RuntimeError):
    """Raised when a duplicate full processing run is attempted."""


class CliProcessor:
    """Service for triggering one of the supported coding CLIs."""

    def __init__(
        self,
        vault_path: Path,
        todoist_api_key: str = "",
        ai_cli: str = "claude",
        owner_full_name: str = "",
        content_language: str = "ru",
        openai_api_key: str = "",
        openai_base_url: str = "",
        openai_model: str = "",
    ) -> None:
        self.vault_path = Path(vault_path)
        self.todoist_api_key = todoist_api_key
        self.ai_cli = ai_cli
        self.owner_full_name = owner_full_name.strip()
        self.content_language = normalize_language(content_language)
        self._openai_api_key = openai_api_key.strip()
        self._openai_base_url = openai_base_url.strip()
        self._openai_model = openai_model.strip()
        self._recall_planner_config = RecallPlannerConfig(
            model=self._openai_model,
            api_key=self._openai_api_key,
            base_url=self._openai_base_url,
            language=self.content_language,
        )
        self._project_runner = CliRunner(self.vault_path.parent, ai_cli)
        self._assistant_runner = CliRunner(self.vault_path, ai_cli)
        self._project_catalog = TodoistProjectCatalog(
            self.vault_path,
            todoist_api_key=todoist_api_key,
        )
        self._project_router = TodoistProjectRouter(
            self.vault_path,
            ai_cli=ai_cli,
            todoist_api_key=todoist_api_key,
            catalog=self._project_catalog,
        )
        self._scheduled_cycle_lock_held = False
        self._daily_workflow = DailyWorkflow(self)
        # Compiled-page candidates ranked for the question currently being
        # answered (ТЗ 7.4 code-review defect 2): set fresh by
        # `_build_compiled_briefings_block` before every model call, and
        # consumed by `_append_question_provenance` after it -- never left
        # over from a previous question (see `answer_question`).
        self._question_provenance_candidates: tuple[CompiledBriefingCandidate, ...] = ()

    def _load_phase_content(self, phase_name: str) -> str:
        """Load phase instructions from the project skill tree."""
        phase_path = (
            self.vault_path.parent
            / "skills/dbrain-processor/phases"
            / f"{phase_name}.md"
        )
        if phase_path.exists():
            return phase_path.read_text(encoding="utf-8")
        return ""

    def _load_dbrain_reference(self, reference_name: str) -> str:
        """Load a reference file from the dbrain processor skill tree."""
        ref_path = (
            self.vault_path.parent
            / "skills/dbrain-processor/references"
            / f"{reference_name}.md"
        )
        if not ref_path.exists():
            return ""
        return ref_path.read_text(encoding="utf-8")

    def _load_todoist_reference(self) -> str:
        """Load Todoist reference for inclusion in prompt."""
        return self._load_dbrain_reference("todoist")

    def _load_todoist_project_routing_reference(self) -> str:
        """Load Todoist project routing rules for task-producing prompts."""
        return self._load_dbrain_reference("todoist-project-routing")

    def _load_ownership_reference(self) -> str:
        """Load shared ownership rules used by all task-producing prompt paths."""
        reference = self._load_dbrain_reference("ownership")
        if not reference:
            return ""
        return reference.replace(
            "{OWNER_FULL_NAME}",
            self.owner_full_name or "assistant owner",
        )

    def _load_intake_intent_reference(self) -> str:
        """Load plain-text intake intent routing rules."""
        return self._load_dbrain_reference("intake-intent")

    def _load_question_answer_reference(self) -> str:
        """Load answer-now rules for plain-text questions."""
        return self._load_dbrain_reference("question-answer")

    def _load_vault_retrieval_skill(self) -> str:
        """Load the single runtime retrieval contract for vault-aware prompts."""
        skill_path = self.vault_path.parent / "skills/vault-retrieval/SKILL.md"
        try:
            content = skill_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Vault retrieval skill is required but missing: {skill_path}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Vault retrieval skill is unreadable: {skill_path}"
            ) from exc
        if not content.strip():
            raise RuntimeError(f"Vault retrieval skill is empty: {skill_path}")
        return content

    def _get_session_context(self, user_id: int) -> str:
        """Get today's session context for the active CLI."""
        if user_id == 0:
            return ""

        session = SessionStore(self.vault_path)
        today_entries = session.get_today(user_id)
        if not today_entries:
            return ""

        lines = ["=== TODAY'S SESSION ==="]
        for entry in today_entries:
            ts = entry.get("ts", "")[11:16]
            entry_type = entry.get("type", "unknown")
            text = entry.get("text", "")
            if text:
                lines.append(f"{ts} [{entry_type}] {text}")
        lines.append("=== END SESSION ===\n")
        return "\n".join(lines)

    def _language_instruction(self) -> str:
        """Shared language contract for all human-readable generated output."""
        language_name = prompt_language_name(self.content_language)
        if self.content_language == "ru":
            lines = [
                "ПРАВИЛО ЯЗЫКА:",
                (
                    "- Весь человекочитаемый текст, который ты сохраняешь или "
                    f"возвращаешь, должен быть на {language_name}."
                ),
                "- Машинные JSON-ключи и имена полей схемы не переименовывай.",
            ]
        else:
            lines = [
                "LANGUAGE RULE:",
                (
                    "- All human-readable content you save or return must be in "
                    f"{language_name}."
                ),
                "- Keep machine-oriented JSON keys and schema field names unchanged.",
            ]
        return "\n".join(lines) + "\n"

    def _run_prompt(self, prompt: str) -> str:
        """Execute prompt via the configured CLI from the project root."""
        extra_env = self._cli_extra_env()
        return self._project_runner.run(
            prompt,
            timeout=DEFAULT_TIMEOUT,
            extra_env=extra_env,
        )

    def _run_vault_prompt(self, prompt: str) -> str:
        """Execute prompt via the configured CLI from the vault root."""
        extra_env = {
            **self._cli_extra_env(),
            "D_BRAIN_CONTEXT_PACK_MODE": "runtime",
        }
        return self._assistant_runner.run(
            prompt,
            timeout=DEFAULT_TIMEOUT,
            extra_env=extra_env,
        )

    def _run_assistant_prompt(self, prompt: str) -> str:
        """Execute a vault-scoped assistant prompt via the configured CLI."""
        return self._run_vault_prompt(prompt)

    @staticmethod
    def _inject_prompt_block(prompt: str, marker: str, block: str) -> str:
        """Insert one context block before the first marker occurrence."""
        if not block or marker not in prompt:
            return prompt
        return prompt.replace(marker, f"{block}\n\n{marker}", 1)

    def _build_auto_recall_block(self, task: str, *, purpose: str) -> str:
        """Best-effort auto recall block built before the main CLI run."""
        try:
            return build_qmd_recall_block(
                self.vault_path,
                task=task,
                purpose=purpose,
                config=self._recall_planner_config,
            )
        except Exception as exc:
            logger.warning("Auto recall planner failed: %s", exc)
            return ""

    def _cli_extra_env(self) -> dict[str, str]:
        """Environment shared by project and vault CLI runs."""
        extra_env: dict[str, str] = {}
        if self.todoist_api_key:
            extra_env["TODOIST_API_KEY"] = self.todoist_api_key
        if self._openai_api_key:
            extra_env["OPENAI_API_KEY"] = self._openai_api_key
        if self._openai_base_url:
            extra_env["BASE_URL"] = self._openai_base_url
            extra_env["OPENAI_BASE_URL"] = self._openai_base_url
        if self._openai_model:
            extra_env["MODEL"] = self._openai_model
            extra_env["OPENAI_MODEL"] = self._openai_model

        mcp_config_path = (self.vault_path.parent / "mcp-config.json").resolve()
        if mcp_config_path.exists():
            extra_env["MCP_CONFIG_PATH"] = str(mcp_config_path)

        return extra_env

    def _todoist_cli_rules(self, *, needs_completed_tasks: bool = False) -> str:
        """Common Todoist access rules shared by all supported CLIs."""
        if self.content_language == "ru":
            lines = [
                "ПРАВИЛА РАБОТЫ С TODOIST:",
                (
                    "- Для Todoist используй Bash + mcp-cli. Не опирайся на "
                    "CLI-специфичные имена MCP-инструментов."
                ),
                (
                    "- Сначала проверь доступность Todoist командой: "
                    "mcp-cli call todoist user-info '{}'"
                ),
                (
                    "- Если команда падает, сделай до 3 повторов с короткими "
                    "паузами, прежде чем считать интеграцию сломанной."
                ),
                (
                    "- Не заявляй, что Todoist недоступен, пока не сделал "
                    "реальные попытки вызова."
                ),
                (
                    "- Если после повторов команда всё ещё падает, включи в отчёт "
                    "точный текст ошибки."
                ),
            ]
        else:
            lines = [
                "TODOIST EXECUTION RULES:",
                (
                    "- Use Bash + mcp-cli for Todoist. "
                    "Do not rely on CLI-specific MCP tool names."
                ),
                "- First verify Todoist with: mcp-cli call todoist user-info '{}'",
                (
                    "- If the command fails, retry up to 3 times "
                    "with short waits before concluding it is broken."
                ),
                "- Never say Todoist is unavailable before actual command attempts.",
                (
                    "- If a command still fails after retries, "
                    "include the exact error text in the report."
                ),
            ]
        if needs_completed_tasks:
            lines.append(
                (
                    "- Для завершённых задач используй mcp-cli call todoist "
                    "find-completed-tasks с нужными фильтрами."
                )
                if self.content_language == "ru"
                else (
                    "- For completed tasks, use mcp-cli call "
                    "todoist find-completed-tasks with the needed filters."
                )
            )
        else:
            lines.append(
                (
                    "- Для создания и обновления задач используй mcp-cli call "
                    "todoist add-tasks / update-tasks / complete-tasks."
                )
                if self.content_language == "ru"
                else (
                    "- For task creation and updates, use mcp-cli call "
                    "todoist add-tasks / update-tasks / complete-tasks."
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _assistant_scope_rules() -> str:
        """Rules for Telegram /do requests."""
        return "\n".join(
            [
                "ASSISTANT SCOPE RULES:",
                "- Current working directory is the vault root. Stay inside it.",
                "- Read/write only files in the current directory tree.",
                "- Use Bash + mcp-cli for Todoist when task actions are needed.",
                (
                    "- Never access ../, absolute paths, .env, .git, src/, scripts/, "
                    "deploy/, docs/, install/setup files, or system configuration."
                ),
                "- Never change repository code or infrastructure from /do.",
                (
                    "- If the request requires code, deployment, or system changes, "
                    "refuse briefly and tell the user to do it outside Telegram /do."
                ),
            ]
        )

    @staticmethod
    def _telegram_markdown_output_rules(*, opening_line: str) -> str:
        """Shared markdown output contract for Telegram-facing prompts."""
        return "\n".join(
            [
                "CRITICAL OUTPUT FORMAT:",
                "- Return ONLY markdown for Telegram.",
                "- Use plain markdown only: headings, bullets, numbered lists, "
                "inline code, bold, italic, links.",
                "- Do not return HTML tags.",
                "- No markdown fences unless a real code block is necessary.",
                f"- {opening_line}",
                "- Default to a complete, well-structured answer with enough "
                "context, reasoning, evidence, caveats, and next actions to fully "
                "satisfy the request.",
                "- Be brief only when the request is a simple factual question "
                "that can be answered completely in a few sentences, or when the "
                "user explicitly asks for brevity.",
                "- Do not shorten complex, analytical, status/history, planning, "
                "comparison, or synthesis answers merely because the output is for "
                "Telegram; runtime will handle delivery.",
            ]
        )

    @staticmethod
    def _trim_owner_markdown_preamble(text: str) -> str:
        """Drop generic lead-in prose before the first structured markdown line."""
        value = str(text or "").strip()
        if not value:
            return value
        lines = value.splitlines()
        structured_patterns = (
            r"^#{1,6}\s+",
            r"^[-*]\s+",
            r"^\d+\.\s+",
            r"^•\s+",
            r"^>",
        )
        for index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line:
                continue
            if any(re.match(pattern, line) for pattern in structured_patterns):
                return "\n".join(lines[index:]).strip()
            if any(token in line for token in ("**", "`", "<b>", "<i>", "<code>")):
                return "\n".join(lines[index:]).strip()
            if re.match(r"^[^\w\s]", line):
                return "\n".join(lines[index:]).strip()
        return value

    def _owner_report_defaults(self) -> dict[str, str]:
        """Localized owner-facing fallback copy."""
        if self.content_language == "ru":
            return {
                "weekly_digest_title": "Недельный дайджест",
                "system_reflection_title": "Системная рефлексия",
                "monthly_review_title": "Месячный обзор",
                "yearly_review_title": "Годовой обзор",
                "key_takeaways_title": "Ключевые выводы",
                "graph_health_title": "Здоровье графа",
                "watch_next_week_title": "Что смотреть на следующей неделе",
                "carry_forward_title": "Наблюдения на перенос",
                "no_system_signals": "Новых системных сигналов нет.",
                "no_system_signals_daily": (
                    "Системная рефлексия: новых системных сигналов нет."
                ),
                "reflection_created": "Создана заметка:",
                "retained_observations": (
                    "Новых сгруппированных выводов нет; наблюдения оставлены"
                    " на следующий недельный проход."
                ),
                "retained_daily_log": (
                    "Системная рефлексия: нерешённые наблюдения "
                    "оставлены на следующий проход."
                ),
                "reflection_daily_log": "Системная рефлексия:",
                "processed_observations_label": "Обработано наблюдений",
                "carry_forward_label": "Перенесено наблюдений",
                "reflection_note_description": "Недельная системная рефлексия за",
                "reflection_title_fallback": "Системная рефлексия",
            }
        return {
            "weekly_digest_title": "Weekly Digest",
            "system_reflection_title": "System Reflection",
            "monthly_review_title": "Monthly Review",
            "yearly_review_title": "Yearly Review",
            "key_takeaways_title": "Key Takeaways",
            "graph_health_title": "Graph Health",
            "watch_next_week_title": "Watch Next Week",
            "carry_forward_title": "Carry-forward Observations",
            "no_system_signals": "No new system signals.",
            "no_system_signals_daily": (
                "Weekly system reflection: no new system signals."
            ),
            "reflection_created": "Created note:",
            "retained_observations": (
                "No grouped reflection was created; unresolved observations were "
                "kept for the next weekly pass."
            ),
            "retained_daily_log": (
                "Weekly system reflection: unresolved observations were kept "
                "for the next pass."
            ),
            "reflection_daily_log": "Weekly system reflection:",
            "processed_observations_label": "Processed observations",
            "carry_forward_label": "Carry forward",
            "reflection_note_description": "Weekly system reflection for",
            "reflection_title_fallback": "Weekly system reflection",
        }

    @staticmethod
    def _string_list(payload: Any) -> list[str]:
        """Normalize an arbitrary JSON field into a compact string list."""
        if not isinstance(payload, list):
            return []
        items: list[str] = []
        for item in payload:
            value = " ".join(str(item or "").split())
            if value:
                items.append(value)
        return items

    @staticmethod
    def _graph_health_entry_day(entry: Any) -> date | None:
        """Calendar day of one graph-health history entry, when it has one."""
        if not isinstance(entry, dict):
            return None
        raw = str(entry.get("date") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None

    @classmethod
    def _graph_health_window(
        cls,
        graph_history: list[Any],
        *,
        start: date,
        end: date,
    ) -> list[Any]:
        """Graph-health entries recorded inside one closed date window.

        ``.graph/health-history.json`` keeps up to 90 runs, so the raw list
        spans weeks. A weekly reflection must compare the week it reports on,
        not the whole retained tail.
        """
        window: list[Any] = []
        for entry in graph_history:
            entry_day = cls._graph_health_entry_day(entry)
            if entry_day is None or not (start <= entry_day <= end):
                continue
            window.append(entry)
        return window

    def _graph_health_delta_line(self, graph_history: list[Any]) -> str:
        """Build one compact graph-health delta summary when enough history exists."""
        if len(graph_history) < 2:
            return ""
        start = graph_history[0] if isinstance(graph_history[0], dict) else {}
        end = graph_history[-1] if isinstance(graph_history[-1], dict) else {}
        try:
            start_score = float(cast(str, start.get("health_score")))
            end_score = float(cast(str, end.get("health_score")))
            start_links = int(cast(str, start.get("total_links")))
            end_links = int(cast(str, end.get("total_links")))
            start_orphans = int(cast(str, start.get("orphans")))
            end_orphans = int(cast(str, end.get("orphans")))
            start_weak = int(cast(str, start.get("weakly_connected")))
            end_weak = int(cast(str, end.get("weakly_connected")))
        except (TypeError, ValueError):
            return ""

        delta_score = end_score - start_score
        delta_links = end_links - start_links
        start_day = self._graph_health_entry_day(start)
        end_day = self._graph_health_entry_day(end)
        period = ""
        if start_day is not None and end_day is not None:
            period = f"{start_day.isoformat()} → {end_day.isoformat()}: "
        if self.content_language == "ru":
            return (
                f"{period}{start_score:.1f} → {end_score:.1f} "
                f"({delta_score:+.1f}); "
                f"связи {delta_links:+d}, орфаны {start_orphans}→{end_orphans}, "
                f"слабосвязанные {start_weak}→{end_weak}"
            )
        return (
            f"{period}{start_score:.1f} → {end_score:.1f} ({delta_score:+.1f}); "
            f"links {delta_links:+d}, orphans {start_orphans}→{end_orphans}, "
            f"weakly-connected {start_weak}→{end_weak}"
        )

    def _build_weekly_system_report_markdown(
        self,
        *,
        title: str,
        highlights: list[str],
        watch_items: list[str],
        graph_history: list[Any],
        carry_forward_count: int,
    ) -> str:
        """Render one deterministic owner-facing weekly system report."""
        copy = self._owner_report_defaults()
        lines = [f"## 🛠 {title}"]

        if highlights:
            lines.extend(["", f"### {copy['key_takeaways_title']}"])
            lines.extend(f"- {item}" for item in highlights)

        graph_line = self._graph_health_delta_line(graph_history)
        if graph_line:
            lines.extend(
                [
                    "",
                    f"### {copy['graph_health_title']}",
                    f"- {graph_line}",
                ]
            )

        if watch_items:
            lines.extend(["", f"### {copy['watch_next_week_title']}"])
            lines.extend(f"- {item}" for item in watch_items)

        lines.extend(
            [
                "",
                f"### {copy['carry_forward_title']}",
                f"- {carry_forward_count}",
            ]
        )
        return "\n".join(lines)

    def _normalize_owner_report_markdown(self, text: str) -> str:
        """Normalize one owner-facing LLM output into markdown only."""
        value = normalize_markdown_input(str(text or "").strip())
        return self._trim_owner_markdown_preamble(value)

    @staticmethod
    def _strip_periodic_persistence_claims(markdown_body: str) -> str:
        """Remove LLM claims about saving/updating files; runtime owns persistence."""
        kept_lines: list[str] = []
        for raw_line in str(markdown_body or "").splitlines():
            compact = " ".join(raw_line.replace("`", "").split())
            lower = compact.casefold()
            mentions_runtime_paths = any(
                token in compact
                for token in (
                    "summaries/",
                    "MOC-weekly.md",
                    "MEMORY.md",
                    "handoff.md",
                )
            )
            mentions_persistence = any(
                token in lower
                for token in (
                    "сохран",
                    "обновл",
                    "saved",
                    "updated",
                    "written",
                    "persist",
                )
            )
            if mentions_runtime_paths and mentions_persistence:
                continue
            kept_lines.append(raw_line)
        cleaned = "\n".join(kept_lines).strip()
        return re.sub(r"\n{3,}", "\n\n", cleaned)

    def _save_weekly_summary(self, report_markdown: str, week_date: date) -> Path:
        """Save weekly summary to vault/summaries/YYYY-WXX-summary.md."""
        year, week, _ = week_date.isocalendar()
        filename = f"{year}-W{week:02d}-summary.md"
        summary_path = self.vault_path / "summaries" / filename
        self._write_periodic_summary(
            summary_path,
            report_markdown,
            date_value=week_date,
            period_key="week",
            period_value=f"{year}-W{week:02d}",
            summary_type="weekly-summary",
        )
        logger.info("Weekly summary saved to %s", summary_path)
        return summary_path

    def _save_monthly_summary(self, report_markdown: str, month_date: date) -> Path:
        """Save monthly summary to vault/summaries/YYYY-MM-summary.md."""
        filename = f"{month_date.strftime('%Y-%m')}-summary.md"
        summary_path = self.vault_path / "summaries" / filename
        self._write_periodic_summary(
            summary_path,
            report_markdown,
            date_value=month_date,
            period_key="month",
            period_value=month_date.strftime("%Y-%m"),
            summary_type="monthly-summary",
        )
        logger.info("Monthly summary saved to %s", summary_path)
        return summary_path

    def _save_yearly_summary(self, report_markdown: str, year_date: date) -> Path:
        """Save yearly summary to vault/summaries/YYYY-summary.md."""
        filename = f"{year_date.year}-summary.md"
        summary_path = self.vault_path / "summaries" / filename
        self._write_periodic_summary(
            summary_path,
            report_markdown,
            date_value=year_date,
            period_key="year",
            period_value=str(year_date.year),
            summary_type="yearly-summary",
        )
        logger.info("Yearly summary saved to %s", summary_path)
        return summary_path

    def _write_periodic_summary(
        self,
        path: Path,
        report_markdown: str,
        *,
        date_value: date,
        period_key: str,
        period_value: str,
        summary_type: str,
    ) -> None:
        """Write a searchable periodic summary through the derived profile."""
        description = f"{summary_type.replace('-', ' ')} for {period_value}"
        content = str(report_markdown or "").strip()
        rendered = (
            "---\n"
            f"date: {date_value.isoformat()}\n"
            f"type: {summary_type}\n"
            f"description: {description}\n"
            f"{period_key}: {period_value}\n"
            f"last_accessed: {date_value.isoformat()}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            f"{content}\n"
        )
        self._write_vault_markdown(path, rendered)

    def _log_periodic_summary(
        self,
        *,
        timestamp: datetime,
        label: str,
        summary_path: Path,
        refresh_qmd: bool,
        extra_lines: list[str] | None = None,
    ) -> None:
        """Append one concise periodic-review entry to today's daily."""
        summary_rel_path = summary_path.relative_to(self.vault_path).as_posix()
        lines = [f"{label}: [[{summary_rel_path}|{summary_path.stem}]]"]
        if extra_lines:
            lines.extend(
                f"- {' '.join(str(line).split())}"
                for line in extra_lines
                if str(line).strip()
            )
        VaultStorage(
            self.vault_path,
            self.content_language,
        ).append_to_daily(
            "\n".join(lines),
            timestamp,
            "[text]",
            refresh_qmd=refresh_qmd,
        )

    def _vision_period_years(self) -> tuple[int, int] | None:
        """Read the active 3-year vision period when available."""
        path = self.vault_path / "goals" / "0-vision-3y.md"
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        match = re.search(r"^period:\s*(\d{4})-(\d{4})\s*$", content, re.MULTILINE)
        if not match:
            return None
        start_year = int(match.group(1))
        end_year = int(match.group(2))
        if end_year < start_year:
            return None
        return start_year, end_year

    def _three_year_rollover_due(self, year_date: date) -> bool:
        """Whether the current yearly review closes the active 3-year horizon."""
        period = self._vision_period_years()
        if period is None:
            return False
        _, end_year = period
        return year_date.year >= end_year

    @staticmethod
    def _iso_week_label(target_day: date) -> str:
        """Canonical YYYY-Www label for one calendar day."""
        iso_year, iso_week, _ = target_day.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    @staticmethod
    def _iso_weeks_in_year(year: int) -> int:
        """ISO week count for one year."""
        return date(year, 12, 28).isocalendar().week

    @staticmethod
    def _next_iso_week_start(target_day: date) -> date:
        """Monday that starts the next ISO week after the supplied day."""
        return target_day + timedelta(days=7 - target_day.weekday())

    @classmethod
    def _weekly_goals_target_start(cls, target_day: date) -> date:
        """Week start appropriate for a scheduled rollover on the supplied day."""
        if target_day.weekday() == 6:
            return cls._next_iso_week_start(target_day)
        return target_day - timedelta(days=target_day.weekday())

    @staticmethod
    def _extract_next_week_focus(content: str) -> str | None:
        """Return the first blockquoted Next Week Focus sentence if present."""
        section_match = re.search(
            r"(?ms)^#{2,3} (?:Next Week Focus|Фокус следующей недели)\s*\n"
            r"(?P<section>.*?)(?=^---\s*$|\Z)",
            content,
        )
        if section_match is None:
            return None
        quote_lines: list[str] = []
        for line in section_match.group("section").splitlines():
            if not line.startswith(">"):
                if quote_lines:
                    break
                continue
            quote_lines.append(line.removeprefix(">").strip())
        focus = " ".join(line for line in quote_lines if line).strip()
        return focus or None

    @classmethod
    def _promote_next_week_focus(cls, content: str) -> str:
        """Promote Next Week Focus into ONE Big Thing when it is available."""
        focus = cls._extract_next_week_focus(content)
        if focus is None:
            return content

        sections = (
            (
                re.compile(
                    r"(?ms)^## ONE Big Thing\s*\n\n"
                    r"> \*\*If I accomplish nothing else, I will:\*\*\s*\n"
                    r"> .+?(?=\n\n<!--|\n\n---|\Z)"
                ),
                "## ONE Big Thing\n\n"
                "> **If I accomplish nothing else, I will:**\n"
                f"> {focus}",
            ),
            (
                re.compile(
                    r"(?ms)^## Главное\s*\n\n"
                    r"> \*\*Если я выполню только одно, то:\*\*\s*\n"
                    r"> .+?(?=\n\n<!--|\n\n---|\Z)"
                ),
                f"## Главное\n\n> **Если я выполню только одно, то:**\n> {focus}",
            ),
        )
        for pattern, replacement in sections:
            updated, count = pattern.subn(replacement, content, count=1)
            if count:
                return updated
        return content

    def _weekly_goals_rollover_due(self, target_day: date) -> bool:
        """Whether an existing weekly goals file is behind its target week."""
        weekly_path = self.vault_path / "goals" / "3-weekly.md"
        if not weekly_path.exists():
            return False
        content = weekly_path.read_text(encoding="utf-8")
        match = re.search(
            r"^week:\s*([0-9]{4}-W[0-9]{2})\s*$",
            content,
            re.MULTILINE,
        )
        if match is None:
            return False
        target_week = self._iso_week_label(self._weekly_goals_target_start(target_day))
        return match.group(1) < target_week

    def rollover_weekly_goals(
        self,
        *,
        day: date | None = None,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Switch goals/3-weekly.md to the scheduled target ISO week."""
        today = day or date.today()
        weekly_path = self.vault_path / "goals" / "3-weekly.md"
        if not weekly_path.exists():
            return {
                "error": "Weekly goals rollover failed: goals/3-weekly.md is missing.",
                "processed_entries": 0,
            }

        try:
            original_bytes = weekly_path.read_bytes()
        except FileNotFoundError:
            return {
                "error": "Weekly goals rollover failed: goals/3-weekly.md is missing.",
                "processed_entries": 0,
            }
        content = original_bytes.decode("utf-8")
        target_week_start = self._weekly_goals_target_start(today)
        target_week = self._iso_week_label(target_week_start)
        previous_week = self._iso_week_label(target_week_start - timedelta(days=7))
        match = re.search(r"^week:\s*([0-9]{4}-W[0-9]{2})\s*$", content, re.MULTILINE)
        if match is None:
            return {
                "error": (
                    "Weekly goals rollover failed: goals/3-weekly.md has no week field."
                ),
                "processed_entries": 0,
            }

        actual_week = match.group(1)
        if actual_week >= target_week:
            return {
                "report": (
                    f"🔁 **{self._cycle_label('weekly_goals_rollover')}**\n\n"
                    f"`goals/3-weekly.md` уже на `{actual_week}`; "
                    f"целевая неделя — `{target_week}`."
                ),
                "processed_entries": 0,
                "skipped": True,
                "from_week": actual_week,
                "to_week": target_week,
                "searchable_write": False,
            }

        iso_year, iso_week, _ = target_week_start.isocalendar()
        total_weeks = self._iso_weeks_in_year(iso_year)
        updated = today.isoformat()
        replacements = [
            (
                r"^updated:\s*\d{4}-\d{2}-\d{2}\s*$",
                f"updated: {updated}",
            ),
            (
                r"^last_accessed:\s*\d{4}-\d{2}-\d{2}\s*$",
                f"last_accessed: {updated}",
            ),
            (r"^week:\s*[0-9]{4}-W[0-9]{2}\s*$", f"week: {target_week}"),
            (
                r"^\*\*Week:\*\*\s*\d+\s+of\s+\d+\s*$",
                f"**Week:** {iso_week} of {total_weeks}",
            ),
            (
                r"^\*\*Неделя:\*\*\s*\d+\s+из\s+\d+\s*$",
                f"**Неделя:** {iso_week} из {total_weeks}",
            ),
            (
                r"^- Previous:\s*[0-9]{4}-W[0-9]{2}\s*$",
                f"- Previous: {previous_week}",
            ),
            (
                r"^- Предыдущая неделя:\s*[0-9]{4}-W[0-9]{2}\s*$",
                f"- Предыдущая неделя: {previous_week}",
            ),
            (
                r"^\*Week Started:\s*\d{4}-\d{2}-\d{2}\*\s*$",
                f"*Week Started: {target_week_start.isoformat()}*",
            ),
            (
                r"^\*Неделя началась:\s*\d{4}-\d{2}-\d{2}\*\s*$",
                f"*Неделя началась: {target_week_start.isoformat()}*",
            ),
        ]
        for pattern, replacement in replacements:
            content = re.sub(pattern, replacement, content, count=1, flags=re.MULTILINE)

        content = self._promote_next_week_focus(content)
        content = self._ensure_goal_write_metadata(content, updated)
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = weekly_path.read_bytes()
            except FileNotFoundError:
                current_bytes = None
            if current_bytes != original_bytes:
                return {
                    "error": (
                        "Weekly goals rollover aborted: goals/3-weekly.md "
                        "changed during rollover; retry."
                    ),
                    "processed_entries": 0,
                }
            self._write_vault_markdown(weekly_path, content, lock=lock)
        self._touch_memory_paths("goals/3-weekly.md")
        self._log_periodic_summary(
            timestamp=datetime.now().astimezone(),
            label=self._cycle_label("weekly_goals_rollover"),
            summary_path=weekly_path,
            refresh_qmd=False,
            extra_lines=[f"{actual_week} -> {target_week}"],
        )
        if refresh_qmd:
            self._refresh_qmd_index()

        return {
            "report": (
                f"🔁 **{self._cycle_label('weekly_goals_rollover')}**\n\n"
                f"`goals/3-weekly.md`: `{actual_week}` → `{target_week}`."
            ),
            "processed_entries": 1,
            "goal_path": "goals/3-weekly.md",
            "from_week": actual_week,
            "to_week": target_week,
            "searchable_write": True,
        }

    @staticmethod
    def _ensure_goal_write_metadata(content: str, updated: str) -> str:
        """Fill mandatory goal metadata when rollover repairs a legacy note."""
        document = parse_frontmatter_bytes(content.encode("utf-8"))
        updates = {
            key: value
            for key, value in {
                "type": "weekly",
                "description": "Current weekly goals and focus",
                "last_accessed": updated,
                "relevance": 1.0,
                "tier": "active",
            }.items()
            if document.fields.get(key) in (None, "", [])
        }
        if not updates:
            return content
        return patch_frontmatter_bytes(content.encode("utf-8"), updates).decode("utf-8")

    def _weekly_rollover_guard(self, target_day: date) -> str | None:
        """Return a localized blocking error when goals/3-weekly.md is stale."""
        weekly_path = self.vault_path / "goals" / "3-weekly.md"
        expected_week = self._iso_week_label(target_day)
        if not weekly_path.exists():
            return (
                "Weekly digest заблокирован: отсутствует goals/3-weekly.md."
                if self.content_language == "ru"
                else "Weekly digest blocked: goals/3-weekly.md is missing."
            )

        content = weekly_path.read_text(encoding="utf-8")
        match = re.search(r"^week:\s*([0-9]{4}-W[0-9]{2})\s*$", content, re.MULTILINE)
        if match is None:
            return (
                "Weekly digest заблокирован: в goals/3-weekly.md нет поля week."
                if self.content_language == "ru"
                else "Weekly digest blocked: goals/3-weekly.md has no week field."
            )

        actual_week = match.group(1)
        if actual_week == expected_week:
            return None

        if self.content_language == "ru":
            return (
                "Weekly digest заблокирован: нужен rollover goals/3-weekly.md "
                f"с {actual_week} на {expected_week}."
            )
        return (
            "Weekly digest blocked: goals/3-weekly.md needs rollover "
            f"from {actual_week} to {expected_week}."
        )

    def _build_weekly_digest_prompt(
        self,
        *,
        today: date,
        yearly_goals_name: str,
    ) -> str:
        """Localized weekly digest prompt without mixed-language framing."""
        if self.content_language == "ru":
            return f"""Сегодня {today}. Подготовь недельный дайджест для владельца.

КОНТЕКСТ:
- Рабочая директория: корень проекта ({self.vault_path.parent})
- Корень vault: {self.vault_path}

{self._language_instruction()}

{self._todoist_cli_rules(needs_completed_tasks=True)}

ПРАВИЛА НЕДЕЛЬНОГО РАЗБОРА:
- Прочитай `MEMORY.md`, `goals/3-weekly.md`, `goals/2-monthly.md`
  и `goals/{yearly_goals_name}`.
- Прочитай daily-файлы за релевантную ISO-неделю.
- Если это добавляет сигнал о трении или риске переноса, прочитай `.session/handoff.md`.
- Используй завершённые задачи как подтверждение, а не вместо осмысленного вывода.
- Если риск переноса неясен, проверь открытые и ближайшие задачи командой
  `mcp-cli call todoist find-tasks-by-date '{{"startDate": "today", "daysCount": 7}}'`.
- Предпочитай синтез простому перечислению.
- Говори прямо, если неделя была в основном операционной.
- Не создавай и не редактируй файлы сам. Канонический weekly summary и обновление
  `MOC-weekly.md` делает Python runtime после твоего ответа.
- Не переписывай goal-файлы автоматически.
- Верни ТОЛЬКО markdown, не HTML.
- Не добавляй вводных фраз вроде "Сейчас соберу дайджест" или "Вот готовый дайджест".
- Не упоминай пути сохранения и не утверждай, что файлы уже обновлены или записаны.
- Начни с одного короткого markdown-заголовка.
- Держи отчёт owner-facing и конкретным: прогресс, что тянуло вниз, риск переноса,
  фокус следующей недели.

ПОРЯДОК РАБОТЫ:
1. Собери недельные сигналы из goals, daily-файлов и завершённых задач.
2. Проверь, сдвинулся ли главный фокус недели и какие цели остались \
тихими или застывшими.
3. Выдели победы, сопротивление, риск переноса и фокус следующей недели.
4. Верни один короткий owner-facing markdown-отчёт.
"""

        return f"""Today is {today}. Generate the weekly owner digest.

CONTEXT:
- Working directory: project root ({self.vault_path.parent})
- Vault root: {self.vault_path}

{self._language_instruction()}

{self._todoist_cli_rules(needs_completed_tasks=True)}

WEEKLY REVIEW RULES:
- Read `MEMORY.md`, `goals/3-weekly.md`, `goals/2-monthly.md`,
  and `goals/{yearly_goals_name}`.
- Read the daily files for the relevant ISO week.
- If it adds signal about friction or carry-over risk, read `.session/handoff.md`.
- Use completed tasks as evidence, not as a substitute for synthesis.
- If carry-over risk is unclear, inspect upcoming/open work with
  `mcp-cli call todoist find-tasks-by-date '{{"startDate": "today", "daysCount": 7}}'`.
- Prefer synthesis over raw enumeration.
- Say directly when the week was mostly operational.
- Do not create or edit files yourself. The Python runtime saves the canonical
  weekly summary and updates `MOC-weekly.md` after your response.
- Do not rewrite goal files automatically.
- Return ONLY markdown, not HTML.
- Do not add meta preambles like "Now I have the full picture",
  "Let me compile the digest", or "Вот готовый дайджест".
- Do not mention save paths or claim that files were updated/persisted.
- Start with one short markdown heading for the digest.
- Keep the report owner-facing and concrete: progress, drag, carry-over risk,
  next-week focus.

WORKFLOW:
1. Gather weekly evidence from goals, daily files, and completed tasks.
2. Check whether `ONE Big Thing` moved and which goals stayed quiet/stale.
3. Identify wins, drag, carry-over risk, and next-week focus.
4. Return one concise owner-facing markdown report.
"""

    def _weekly_system_reflection_path(self, week_date: date) -> Path:
        """Path for the weekly system reflection note."""
        year, week, _ = week_date.isocalendar()
        return (
            self.vault_path
            / "thoughts"
            / "reflections"
            / f"{year}-W{week:02d}-system-reflection.md"
        )

    def _save_weekly_system_reflection(
        self,
        *,
        week_date: date,
        title: str,
        markdown_body: str,
    ) -> Path:
        """Persist the weekly system reflection note."""
        year, week, _ = week_date.isocalendar()
        note_path = self._weekly_system_reflection_path(week_date)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        description = self._owner_report_defaults()["reflection_note_description"]
        content = (
            f"---\n"
            f"date: {week_date.isoformat()}\n"
            "type: reflection\n"
            f"description: {description} {year}-W{week:02d}\n"
            f"week: {year}-W{week:02d}\n"
            "tags: [system, weekly-reflection]\n"
            f"created: {week_date.isoformat()}\n"
            f"updated: {week_date.isoformat()}\n"
            f"last_accessed: {week_date.isoformat()}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            f"# {title}\n\n"
            "#system\n\n"
            f"{markdown_body.strip()}\n"
        )
        self._write_vault_markdown(note_path, content)
        logger.info("Weekly system reflection saved to %s", note_path)
        return note_path

    def _load_weekly_reflection_rule(self) -> str:
        """Load the weekly system reflection rule text."""
        path = self.vault_path / ".claude" / "rules" / "weekly-reflection.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _load_graph_health_history(self) -> list[Any]:
        """Load graph health history for weekly system reflection."""
        path = self.vault_path / ".graph" / "health-history.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid graph health history at %s", path)
            return []
        return payload if isinstance(payload, list) else []

    def _log_weekly_system_reflection(
        self,
        *,
        timestamp: datetime,
        note_path: Path | None,
        title: str,
        processed_observations: int,
        carry_forward_observations: int,
        refresh_qmd: bool,
    ) -> None:
        """Append one concise weekly system reflection entry to today's daily."""
        copy = self._owner_report_defaults()
        carry_forward_line = (
            f"- {copy['carry_forward_label']}: {carry_forward_observations}"
        )
        if note_path is not None:
            note_rel_path = note_path.relative_to(self.vault_path).as_posix()
            body = (
                f"{copy['reflection_daily_log']} [[{note_rel_path}|{title}]]\n"
                f"- {copy['processed_observations_label']}:"
                f" {processed_observations}\n"
                f"{carry_forward_line}"
            )
        elif carry_forward_observations > 0:
            body = f"{copy['retained_daily_log']}\n{carry_forward_line}"
        else:
            body = f"{copy['no_system_signals_daily']}\n{carry_forward_line}"
        VaultStorage(
            self.vault_path,
            self.content_language,
        ).append_to_daily(
            body,
            timestamp,
            "[text]",
            refresh_qmd=refresh_qmd,
        )

    def _update_weekly_moc(self, summary_path: Path) -> None:
        """Add link to new summary in MOC-weekly.md."""
        moc_path = self.vault_path / "MOC" / "MOC-weekly.md"
        if not moc_path.exists():
            return
        with vault_write_lock(self.vault_path) as lock:
            content = moc_path.read_text(encoding="utf-8")
            link = f"- [[summaries/{summary_path.name}|{summary_path.stem}]]"
            if summary_path.stem in content:
                return
            marker_re = re.compile(r"^(## Previous Weeks\s*)$", re.MULTILINE)
            match = marker_re.search(content)
            if match:
                insert_pos = match.end()
                content = content[:insert_pos] + f"\n\n{link}\n" + content[insert_pos:]
            else:
                content = content.rstrip() + f"\n\n## Previous Weeks\n\n{link}\n"
            content = self._ensure_weekly_moc_metadata(content)
            self._write_vault_markdown(moc_path, content, lock=lock)
            logger.info("Updated MOC-weekly.md with link to %s", summary_path.stem)

    @staticmethod
    def _ensure_weekly_moc_metadata(content: str) -> str:
        """Fill the index profile fields without changing the MOC body."""
        document = parse_frontmatter_bytes(content.encode("utf-8"))
        today = date.today().isoformat()
        updates: dict[str, object] = (
            {"type": "index"} if document.fields.get("type") != "index" else {}
        )
        metadata_defaults: dict[str, object] = {
            "description": "Index of weekly summaries and prior weekly reviews",
            "last_accessed": today,
            "relevance": 1.0,
            "tier": "active",
        }
        updates.update(
            {
                key: value
                for key, value in metadata_defaults.items()
                if document.fields.get(key) in (None, "", [])
            }
        )
        if not updates:
            return content
        return patch_frontmatter_bytes(content.encode("utf-8"), updates).decode("utf-8")

    def _get_daily_file(self, day: date) -> Path:
        """Path to the daily file for the requested date."""
        return self.vault_path / "daily" / f"{day.isoformat()}.md"

    def _ensure_daily_file(self, day: date) -> Path:
        """Create today's daily file if it does not exist."""
        daily_file = VaultStorage(
            self.vault_path,
            self.content_language,
        ).ensure_daily_file(day)
        if daily_file.stat().st_size <= len(f"# {day.isoformat()}\n"):
            logger.info("Created empty daily file for %s", day)
        return daily_file

    @contextmanager
    def _scheduled_process_lock(self) -> Iterator[None]:
        """Prevent overlapping write-heavy full processing runs."""
        lock_dir = self.vault_path / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "full-process.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProcessAlreadyRunningError(
                    "Full processing is already running"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_handoff_file(self) -> None:
        """Create handoff stub expected by the reflect phase."""
        handoff_path = self._handoff_path()
        with vault_write_lock(self.vault_path) as lock:
            if handoff_path.exists():
                return
            self._write_vault_markdown(
                handoff_path,
                self._compact_handoff_text(""),
                lock=lock,
            )
        logger.info("Created handoff stub at %s", handoff_path)

    def _handoff_path(self) -> Path:
        """Return the rolling handoff file path."""
        return self._session_dir() / "handoff.md"

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[str, str]:
        """Split YAML frontmatter from the markdown body when present."""
        match = HANDOFF_FRONTMATTER_RE.match(text)
        if not match:
            return "", text
        return match.group(0), text[match.end() :]

    @classmethod
    def _split_thought_frontmatter(cls, text: str) -> tuple[str, str]:
        """Recover one thought-note frontmatter block when closing --- is missing."""
        frontmatter, body = cls._split_frontmatter(text)
        if frontmatter:
            return frontmatter, body
        if not text.startswith("---\n"):
            return "", text

        lines = text.splitlines()
        if len(lines) < 2:
            return "", text

        body_start: int | None = None
        for index in range(1, len(lines)):
            line = lines[index]
            if line == "---":
                body_start = index + 1
                break
            if re.match(r"^#(?:\s|$)", line):
                body_start = index
                break

        if body_start is None or body_start <= 1:
            return "", text

        frontmatter_lines = lines[:body_start]
        if frontmatter_lines[-1] != "---":
            frontmatter_lines.append("---")
        body_lines = lines[body_start:]
        frontmatter = "\n".join(frontmatter_lines).rstrip() + "\n"
        body = "\n".join(body_lines)
        if body and text.endswith("\n"):
            body += "\n"
        return frontmatter, body

    @staticmethod
    def _parse_handoff_sections(body: str) -> dict[str, list[str]]:
        """Collect all canonical handoff sections in encounter order."""
        sections: dict[str, list[str]] = {title: [] for title in HANDOFF_SECTION_ORDER}
        matches = list(HANDOFF_SECTION_RE.finditer(body))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            title = match.group(1)
            content = body[match.end() : end].strip()
            sections[title].append(content)
        return sections

    @staticmethod
    def _normalize_handoff_last_session(chunks: list[str]) -> str:
        """Collapse the latest last-session block into one compact paragraph."""
        for chunk in reversed(chunks):
            lines = [line.strip() for line in chunk.splitlines() if line.strip()]
            if lines:
                return " ".join(lines)
        return HANDOFF_EMPTY_SECTION["Last Session"]

    @staticmethod
    def _coerce_bullet_lines(content: str) -> list[str]:
        """Normalize arbitrary lines into flat markdown bullets."""
        bullets: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line in {"(none)", "- (none)"}:
                continue
            line = re.sub(r"^\d+\.\s+", "", line)
            if not line.startswith("- "):
                line = f"- {line.lstrip('-').strip()}"
            bullets.append(line)
        return bullets

    @classmethod
    def _normalize_handoff_bullet_section(
        cls,
        chunks: list[str],
    ) -> str:
        """Keep the latest non-empty bullet section in canonical form."""
        for chunk in reversed(chunks):
            bullets = cls._coerce_bullet_lines(chunk)
            if not bullets:
                continue
            deduped = list(dict.fromkeys(bullets))
            return "\n".join(deduped)
        return "- (none)"

    @classmethod
    def _normalize_handoff_observations(cls, chunks: list[str]) -> str:
        """Deduplicate observations without dropping older surviving items."""
        collected: list[str] = []
        for chunk in chunks:
            collected.extend(cls._coerce_bullet_lines(chunk))
        if not collected:
            return HANDOFF_EMPTY_SECTION["Observations"]

        deduped_reversed: list[str] = []
        seen: set[str] = set()
        for bullet in reversed(collected):
            if bullet in seen:
                continue
            seen.add(bullet)
            deduped_reversed.append(bullet)
        deduped = list(reversed(deduped_reversed))[-10:]
        return "\n".join(deduped)

    @staticmethod
    def _default_handoff_frontmatter() -> str:
        """Frontmatter for a freshly initialized rolling handoff file."""
        today = date.today().isoformat()
        return (
            "---\n"
            "type: note\n"
            f"last_accessed: {today}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
        )

    def _compact_handoff_text(self, text: str) -> str:
        """Render handoff into one canonical rolling document."""
        frontmatter, normalized_sections = self._normalized_handoff_sections(text)
        return self._render_handoff_sections(frontmatter, normalized_sections)

    def _normalized_handoff_sections(self, text: str) -> tuple[str, dict[str, str]]:
        """Parse and normalize all canonical handoff sections."""
        frontmatter, body = self._split_frontmatter(text)
        sections = self._parse_handoff_sections(body)
        if not frontmatter:
            frontmatter = self._default_handoff_frontmatter()

        normalized_sections = {
            "Last Session": self._normalize_handoff_last_session(
                sections["Last Session"]
            ),
            "Key Decisions": self._normalize_handoff_bullet_section(
                sections["Key Decisions"]
            ),
            "In Progress": self._normalize_handoff_bullet_section(
                sections["In Progress"]
            ),
            "Next Steps": self._normalize_handoff_bullet_section(
                sections["Next Steps"]
            ),
            "Observations": self._normalize_handoff_observations(
                sections["Observations"]
            ),
        }
        return frontmatter, normalized_sections

    @staticmethod
    def _render_handoff_sections(frontmatter: str, sections: dict[str, str]) -> str:
        """Render one canonical handoff document from normalized sections."""

        lines = [frontmatter.rstrip(), "# Передача сессии", ""]
        for title in HANDOFF_SECTION_ORDER:
            lines.append(f"## {title}")
            lines.append(sections[title])
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _compact_handoff_file(self) -> None:
        """Normalize handoff into its rolling compact form."""
        handoff_path = self._handoff_path()
        with vault_write_lock(self.vault_path) as lock:
            if not handoff_path.exists():
                return
            compacted = self._compact_handoff_text(
                handoff_path.read_text(encoding="utf-8")
            )
            self._write_vault_markdown(handoff_path, compacted, lock=lock)

    def _current_handoff_observations(self) -> list[str]:
        """Return unresolved handoff observations as canonical bullets."""
        self._ensure_handoff_file()
        handoff_path = self._handoff_path()
        _, sections = self._normalized_handoff_sections(
            handoff_path.read_text(encoding="utf-8")
        )
        return self._coerce_bullet_lines(sections["Observations"])

    @staticmethod
    def _handoff_revision(path: Path, content: bytes) -> HandoffRevision:
        """Identify one exact atomic handoff version, including same-byte rewrites."""
        file_stat = path.stat()
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mtime_ns,
            file_stat.st_size,
            sha256(content).digest(),
        )

    def _handoff_observation_snapshot(
        self,
    ) -> tuple[list[str], HandoffRevision]:
        """Read observations and their exact handoff revision under a short lock."""
        self._ensure_handoff_file()
        handoff_path = self._handoff_path()
        with vault_write_lock(self.vault_path):
            content = handoff_path.read_bytes()
            revision = self._handoff_revision(handoff_path, content)
        _, sections = self._normalized_handoff_sections(content.decode("utf-8"))
        observations = self._coerce_bullet_lines(sections["Observations"])
        return observations, revision

    def _write_handoff_observations(self, bullets: list[str]) -> None:
        """Replace the rolling handoff observations section."""
        self._ensure_handoff_file()
        handoff_path = self._handoff_path()
        with vault_write_lock(self.vault_path) as lock:
            frontmatter, sections = self._normalized_handoff_sections(
                handoff_path.read_text(encoding="utf-8")
            )
            normalized = self._coerce_bullet_lines("\n".join(bullets))
            deduped = list(dict.fromkeys(normalized))
            sections["Observations"] = (
                "\n".join(deduped) if deduped else HANDOFF_EMPTY_SECTION["Observations"]
            )
            self._write_vault_markdown(
                handoff_path,
                self._render_handoff_sections(frontmatter, sections),
                lock=lock,
            )

    def _merge_handoff_observation_snapshot(
        self,
        snapshot: list[str],
        carry_forward: list[str],
        snapshot_revision: HandoffRevision,
    ) -> tuple[list[str], int]:
        """Apply an unchanged snapshot or conservatively retain ambiguous bullets."""
        snapshot_bullets = self._coerce_bullet_lines("\n".join(snapshot))
        carried = set(self._coerce_bullet_lines("\n".join(carry_forward)))
        processed = {bullet for bullet in snapshot_bullets if bullet not in carried}

        self._ensure_handoff_file()
        handoff_path = self._handoff_path()
        with vault_write_lock(self.vault_path) as lock:
            current_bytes = handoff_path.read_bytes()
            revision_matches = (
                self._handoff_revision(handoff_path, current_bytes) == snapshot_revision
            )
            frontmatter, sections = self._normalized_handoff_sections(
                current_bytes.decode("utf-8")
            )
            current = self._coerce_bullet_lines(sections["Observations"])
            if not revision_matches:
                return current, 0
            merged = [bullet for bullet in current if bullet not in processed]
            processed_count = len(processed)
            sections["Observations"] = (
                "\n".join(merged) if merged else HANDOFF_EMPTY_SECTION["Observations"]
            )
            self._write_vault_markdown(
                handoff_path,
                self._render_handoff_sections(frontmatter, sections),
                lock=lock,
            )
        return merged, processed_count

    def _log_graph_age_warning(self) -> None:
        """Warn if the vault graph looks stale."""
        graph_path = self.vault_path / ".graph" / "vault-graph.json"
        if not graph_path.exists():
            return

        graph_date = date.fromtimestamp(graph_path.stat().st_mtime)
        graph_age_days = (date.today() - graph_date).days
        if graph_age_days > 7:
            logger.warning("vault-graph.json is %s days old", graph_age_days)

    def _daily_has_processable_entries(self, daily_file: Path) -> bool:
        """Check whether the daily file contains at least one canonical entry header."""
        if not daily_file.exists():
            return False
        content = daily_file.read_text(encoding="utf-8")
        return bool(DAILY_ENTRY_HEADER_RE.search(content))

    def _get_yearly_goals_name(self) -> str:
        """Select the latest yearly goals file."""
        return select_yearly_goals_name(self.vault_path)

    def _build_injected_context(self, *, consumer: str, target_day: date) -> str:
        """Build and touch the single eager context pack for one vault prompt."""
        pack = ContextPackBuilder(self.vault_path).build(target_day)
        self._touch_memory_paths(*pack.loaded_paths)
        logger.info(
            "Injected vault context consumer=%s bytes=%s budget=%s collapsed=%s "
            "over_budget=%s",
            consumer,
            pack.byte_count,
            pack.budget_bytes,
            ",".join(pack.collapsed_sections) or "none",
            pack.over_budget,
        )
        return pack.text

    def _run_json_phase(
        self,
        prompt: str,
        *,
        phase_name: str,
        retry_on_parse_error: bool = True,
        persist_raw_output: bool = True,
    ) -> dict[str, Any]:
        """Run a JSON-returning phase prompt and optionally retry parse failure."""
        return run_json_phase(
            prompt,
            phase_name=phase_name,
            retry_on_parse_error=retry_on_parse_error,
            run_prompt=self._run_vault_prompt,
            raw_output_writer=(
                self._write_session_text if persist_raw_output else None
            ),
        )

    _SESSION_PHASE_ARTIFACTS = (
        "capture.json",
        "capture-raw-output.txt",
        "capture-retry-raw-output.txt",
        "execute.json",
        "execute-raw-output.txt",
        "execute-retry-raw-output.txt",
        "creative-recall.txt",
        "question-creative-recall.txt",
        "memory-audit.md",
    )
    _SESSION_PHASE_ARTIFACT_GLOBS = (
        "audit-*-raw-output.txt",
        "audit-*-retry-raw-output.txt",
    )

    def _session_dir(self) -> Path:
        """Path where phase artifacts are stored."""
        session_dir = self.vault_path / ".session"
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _manifest(self) -> VaultManifest:
        """Load the required project manifest before every Markdown write."""
        return load_manifest_for_vault(self.vault_path)

    def _write_vault_markdown(
        self,
        path: Path,
        content: str,
        *,
        lock: VaultWriteLock | None = None,
        expected_full_sha256: str | None = None,
    ) -> None:
        """Validate and atomically persist a vault Markdown writer result."""
        if path == self._handoff_path():
            document = parse_frontmatter_bytes(content.encode("utf-8"))
            handoff_defaults: dict[str, object] = {
                "type": "note",
                "last_accessed": date.today().isoformat(),
                "relevance": 1.0,
                "tier": "active",
            }
            handoff_updates = {
                key: value
                for key, value in handoff_defaults.items()
                if document.fields.get(key) in (None, "", [])
            }
            if handoff_updates:
                content = patch_frontmatter_bytes(
                    content.encode("utf-8"), handoff_updates
                ).decode("utf-8")
        write_validated_vault_markdown(
            self.vault_path,
            path,
            content.encode("utf-8"),
            manifest=self._manifest(),
            existing_lock=lock,
            expected_full_sha256=expected_full_sha256,
        )

    def _clear_session_phase_artifacts(self) -> None:
        """Remove stale phase artifacts so a new run starts clean."""
        session_dir = self._session_dir()
        for name in self._SESSION_PHASE_ARTIFACTS:
            artifact = session_dir / name
            if artifact.exists():
                artifact.unlink()
                logger.debug("Removed stale session artifact: %s", name)
        for pattern in self._SESSION_PHASE_ARTIFACT_GLOBS:
            for artifact in session_dir.glob(pattern):
                if artifact.is_file():
                    artifact.unlink()
                    logger.debug("Removed stale session artifact: %s", artifact.name)

    def _write_session_json(self, file_name: str, payload: dict[str, Any]) -> Path:
        """Persist a phase artifact to the session directory."""
        path = self._session_dir() / file_name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_session_text(self, file_name: str, content: str) -> Path:
        """Persist a text artifact to the session directory."""
        path = self._session_dir() / file_name
        rendered = content.rstrip() + "\n"
        if file_name == "memory-audit.md":
            today = date.today().isoformat()
            rendered = (
                "---\n"
                "type: technical\n"
                f"last_accessed: {today}\n"
                "relevance: 1.0\n"
                "tier: active\n"
                "---\n\n" + rendered
            )
            self._write_vault_markdown(path, rendered)
        else:
            path.write_text(rendered, encoding="utf-8")
        return path

    _UV_SCRIPT_TIMEOUT_SECONDS = 3600

    def _run_uv_script(self, *args: str) -> None:
        """Run a uv-managed helper script from the vault root."""
        command_args = self._project_skill_script_args(args)
        uv_bin = os.environ.get("UV_BIN", "uv").strip() or "uv"
        try:
            result = subprocess.run(
                [uv_bin, "run", *command_args],
                cwd=self.vault_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._UV_SCRIPT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise CliExecutionError(f"uv executable not found: {uv_bin}") from exc
        except subprocess.TimeoutExpired:
            logger.warning(
                "Command timed out after %ss: %s",
                self._UV_SCRIPT_TIMEOUT_SECONDS,
                " ".join([uv_bin, "run", *args]),
            )
            return
        if result.returncode != 0:
            logger.warning(
                "Command failed: %s (%s)",
                " ".join(result.args),
                (result.stderr or result.stdout).strip(),
            )

    def _run_uv_script_capture(self, *args: str) -> str:
        """Run a uv-managed helper script and return stdout on success."""
        command_args = self._project_skill_script_args(args)
        uv_bin = os.environ.get("UV_BIN", "uv").strip() or "uv"
        try:
            result = subprocess.run(
                [uv_bin, "run", *command_args],
                cwd=self.vault_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._UV_SCRIPT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise CliExecutionError(f"uv executable not found: {uv_bin}") from exc
        except subprocess.TimeoutExpired:
            logger.warning(
                "Command timed out after %ss: %s",
                self._UV_SCRIPT_TIMEOUT_SECONDS,
                " ".join([uv_bin, "run", *args]),
            )
            return ""
        if result.returncode != 0:
            logger.warning(
                "Command failed: %s (%s)",
                " ".join(result.args),
                (result.stderr or result.stdout).strip(),
            )
            return ""
        return result.stdout

    def _project_skill_script_args(self, args: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve a project skill script while preserving vault-root arguments."""
        if not args or not args[0].startswith("skills/"):
            return args
        script = self.vault_path.parent / args[0]
        return (str(script), *args[1:])

    # NOTE: _vault_working_directory was removed — callers now use absolute
    # paths instead of changing the process-global cwd (not thread-safe).

    def _load_memory_engine_module(self) -> Any:
        """Load the legacy memory-engine script in-process for hot paths."""
        script_path = (
            self.vault_path.parent / "skills/agent-memory/scripts/memory-engine.py"
        )
        if not script_path.exists():
            raise FileNotFoundError(f"memory engine script not found: {script_path}")

        spec = importlib.util.spec_from_file_location(
            "d_brain_memory_engine_runtime",
            script_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"unable to load memory engine module: {script_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "TODAY"):
            cast(Any, module).TODAY = date.today()
        return module

    def _todoist_project_catalog_snapshot(
        self,
        *,
        force_refresh: bool,
    ) -> dict[str, Any]:
        """Fetch or load the current Todoist project catalog for prompt context."""
        if not self.todoist_api_key:
            return {"fetched_at": None, "inbox_project_id": "", "projects": []}
        result = self._project_catalog.get_catalog(force_refresh=force_refresh)
        if not result["available"] and result["errors"]:
            logger.warning(
                "Todoist project catalog unavailable: %s",
                " | ".join(result["errors"]),
            )
        return cast(dict[str, Any], result["catalog"])

    def _rebuild_graph(self) -> None:
        """Refresh vault graph artifacts."""
        self._run_uv_script("skills/graph-builder/scripts/analyze.py")

    def _load_graph_stats(self) -> dict[str, Any]:
        """Load the latest graph stats from disk."""
        graph_path = self.vault_path / ".graph" / "vault-graph.json"
        if not graph_path.exists():
            return {}
        try:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _refresh_qmd_index(self) -> None:
        """Refresh the local qmd retrieval index after searchable vault writes."""
        result = QmdService(self.vault_path).refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
            return
        if result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))

    def _create_todoist_tasks(
        self,
        tasks: list[dict[str, Any]],
        *,
        source_context: str = "",
    ) -> list[dict[str, str]]:
        """Create routed Todoist tasks and return created ids with titles."""
        if not tasks or not self.todoist_api_key:
            return []

        catalog_result = self._project_catalog.get_catalog(force_refresh=True)
        catalog = catalog_result["catalog"] if catalog_result["available"] else None
        payload: dict[str, list[dict[str, Any]]] = {"tasks": []}
        normalized_tasks: list[dict[str, Any]] = []

        for task in tasks:
            content = " ".join(str(task.get("content") or "").split())
            if not content:
                continue
            priority = int(task.get("priority") or 1)
            due_hint = " ".join(str(task.get("due_hint") or "").split())
            route = self._project_router.route_task(
                task,
                source_context=source_context,
                catalog=catalog,
            )
            payload_task: dict[str, Any] = {
                "content": content,
                "priority": f"p{max(1, min(priority, 4))}",
            }
            if due_hint:
                payload_task["dueString"] = due_hint
            if route.get("project_id"):
                payload_task["projectId"] = route["project_id"]
            payload["tasks"].append(payload_task)
            normalized_tasks.append({"content": content})

        if not payload["tasks"]:
            return []

        command = [
            "mcp-cli",
            "call",
            "todoist",
            "add-tasks",
            json.dumps(payload, ensure_ascii=False),
        ]
        errors: list[str] = []
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2 * attempt)
            try:
                result = subprocess.run(
                    command,
                    cwd=self.vault_path,
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**os.environ, **self._cli_extra_env()},
                    timeout=3600,
                )
            except subprocess.TimeoutExpired:
                errors.append("timeout after 3600s")
                continue
            if result.returncode == 0:
                try:
                    parsed = extract_first_json_dict(
                        result.stdout,
                        error_context="Processor Todoist MCP output",
                    )
                except ValueError:
                    return []
                task_items = parsed.get("tasks")
                if not isinstance(task_items, list):
                    structured = parsed.get("structuredContent")
                    if isinstance(structured, dict):
                        task_items = structured.get("tasks") or structured.get(
                            "created"
                        )
                created: list[dict[str, str]] = []
                for index, item in enumerate(task_items or []):
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    fallback = (
                        normalized_tasks[index]["content"]
                        if index < len(normalized_tasks)
                        else ""
                    )
                    created.append(
                        {
                            "id": str(item["id"]),
                            "content": str(item.get("content") or fallback).strip()
                            or fallback,
                        }
                    )
                return created
            errors.append(result.stderr.strip() or result.stdout.strip())
        logger.warning("Todoist creation failed after retries: %s", " | ".join(errors))
        return []

    def _process_audit_state_path(self) -> Path:
        """Cache path for short-lived dedupe of auto-created audit tasks."""
        return self.vault_path / ".sync" / "process-audits.json"

    def _load_process_audit_state(self) -> dict[str, Any]:
        """Load the audit dedupe cache."""
        path = self._process_audit_state_path()
        if not path.exists():
            return {"issues": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid process audit state at %s", path)
            return {"issues": {}}
        return payload if isinstance(payload, dict) else {"issues": {}}

    def _save_process_audit_state(self, payload: dict[str, Any]) -> None:
        """Persist the audit dedupe cache."""
        path = self._process_audit_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _audit_issue_fingerprint(cycle_name: str, issue: dict[str, Any]) -> str:
        """Stable fingerprint for one process-audit finding."""
        payload = {
            "cycle": cycle_name,
            "title": " ".join(str(issue.get("title") or "").split()),
            "action": " ".join(str(issue.get("action") or "").split()),
            "evidence": " ".join(str(issue.get("evidence") or "").split()),
        }
        return sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _filter_new_audit_issues(
        self,
        cycle_name: str,
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Skip very recent duplicate findings to avoid Todoist task spam."""
        state = self._load_process_audit_state()
        issues_state = state.get("issues")
        if not isinstance(issues_state, dict):
            issues_state = {}

        now = datetime.now().astimezone()
        kept: list[dict[str, Any]] = []
        refreshed_state: dict[str, Any] = {}

        for fingerprint, raw_item in issues_state.items():
            if not isinstance(raw_item, dict):
                continue
            created_at = str(raw_item.get("created_at") or "").strip()
            try:
                created_dt = datetime.fromisoformat(created_at)
            except ValueError:
                continue
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=now.tzinfo)
            if (now - created_dt).days < PROCESS_AUDIT_STATE_TTL_DAYS:
                refreshed_state[str(fingerprint)] = raw_item

        for issue in issues:
            fingerprint = self._audit_issue_fingerprint(cycle_name, issue)
            if fingerprint in refreshed_state:
                continue
            kept.append(issue)
            refreshed_state[fingerprint] = {
                "created_at": now.isoformat(),
                "cycle": cycle_name,
                "title": issue.get("title", ""),
                "action": issue.get("action", ""),
            }

        self._save_process_audit_state({"issues": refreshed_state})
        return kept

    def _run_memory_decay(self) -> None:
        """Refresh memory tiers and relevance scores."""
        self._run_uv_script(
            "skills/agent-memory/scripts/memory-engine.py",
            "decay",
            ".",
        )

    def _touch_memory_paths(self, *paths: str) -> None:
        """Best-effort touch for core notes that are explicitly read in prompts."""
        try:
            memory_engine = self._load_memory_engine_module()
        except Exception as exc:
            logger.warning("Failed to load memory engine for touch: %s", exc)
            return

        for path in paths:
            if not path:
                continue
            output = io.StringIO()
            abs_path = self.vault_path / path
            try:
                with redirect_stdout(output):
                    config = memory_engine.load_config(abs_path.parent)
                    memory_engine.cmd_touch(str(abs_path), config)
            except SystemExit as exc:
                logger.warning(
                    "Memory touch failed for %s: exit=%s output=%s",
                    path,
                    exc.code,
                    output.getvalue().strip(),
                )
            except Exception as exc:
                logger.warning("Memory touch failed for %s: %s", path, exc)

    def _capture_creative_recall(
        self,
        sample_size: int = 3,
        *,
        file_name: str = "creative-recall.txt",
    ) -> None:
        """Capture a small creative-recall artifact for the reflect phase."""
        try:
            memory_engine = self._load_memory_engine_module()
        except Exception as exc:
            logger.warning("Failed to load memory engine for creative recall: %s", exc)
            return

        output_buffer = io.StringIO()
        try:
            with redirect_stdout(output_buffer):
                config = memory_engine.load_config(self.vault_path)
                memory_engine.cmd_creative(sample_size, self.vault_path, config)
        except SystemExit as exc:
            logger.warning(
                "Creative recall failed: exit=%s output=%s",
                exc.code,
                output_buffer.getvalue().strip(),
            )
            return
        except Exception as exc:
            logger.warning("Creative recall failed: %s", exc)
            return

        output = output_buffer.getvalue().strip()
        if output:
            self._write_session_text(file_name, output)

    def _capture_memory_audit(self) -> None:
        """Persist a duplicate/overlap audit for MEMORY.md."""
        report = MemoryAuditService(self.vault_path).render()
        self._write_session_text("memory-audit.md", report)

    def _run_vault_health_maintenance(self) -> None:
        """Run deterministic vault maintenance before reflect."""
        self._rebuild_graph()
        stats = self._load_graph_stats()

        self._run_uv_script("skills/vault-health/scripts/generate_moc.py")
        self._run_uv_script(
            "skills/vault-health/scripts/add_descriptions.py",
            "--apply",
        )

        if stats.get("orphan_count") or stats.get("weakly_connected_count"):
            self._run_uv_script(
                "skills/vault-health/scripts/connect_orphans.py",
                "--apply",
            )

        if stats.get("broken_link_count"):
            self._run_uv_script(
                "skills/vault-health/scripts/fix_links.py",
                "--apply",
            )

        self._rebuild_graph()

    def _count_processed_entries(self, capture_data: dict[str, Any]) -> int:
        """Best-effort processed entry count for reports and tests."""
        entries = capture_data.get("entries")
        if isinstance(entries, list):
            return sum(
                1
                for entry in entries
                if isinstance(entry, dict) and entry.get("classification") != "skip"
            )

        stats = capture_data.get("stats")
        if isinstance(stats, dict):
            total_entries = stats.get("total_entries")
            if isinstance(total_entries, int):
                return total_entries

        return 0

    @staticmethod
    def _recompute_capture_stats(capture_data: dict[str, Any]) -> None:
        """Keep capture stats aligned after deterministic runtime rewrites."""
        entries = capture_data.get("entries")
        if not isinstance(entries, list):
            return

        classification_counts = {
            "tasks": 0,
            "thoughts": 0,
            "crm_updates": 0,
            "skipped": 0,
        }
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            classification = str(entry.get("classification") or "").strip().lower()
            if classification == "task":
                classification_counts["tasks"] += 1
            elif classification in {"idea", "reflection", "learning", "project"}:
                classification_counts["thoughts"] += 1
            elif classification == "crm_update":
                classification_counts["crm_updates"] += 1
            elif classification == "skip":
                classification_counts["skipped"] += 1

        capture_data["stats"] = {
            "total_entries": len(entries),
            **classification_counts,
        }

    def _apply_entry_status_guardrails(
        self,
        *,
        day: date,
        capture_data: dict[str, Any],
    ) -> None:
        """Apply deterministic entry-status rules before execute sees capture.json."""
        entries = capture_data.get("entries")
        if not isinstance(entries, list) or not entries:
            return

        daily_file = self._get_daily_file(day)
        if not daily_file.exists():
            return

        raw_entries = parse_daily_entry_statuses(daily_file.read_text(encoding="utf-8"))
        changed = False
        used_raw_indices: set[int] = set()
        entry_to_raw: dict[int, int] = {}

        # Pass 1: claim raw entries by exact (time, type) match.
        for entry_pos, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            entry_time = str(entry.get("time") or "").strip()
            entry_type = normalize_entry_type(str(entry.get("type") or ""))
            if not entry_time or not entry_type:
                continue
            for candidate_index, raw_entry in enumerate(raw_entries):
                if candidate_index in used_raw_indices:
                    continue
                if raw_entry.time == entry_time and raw_entry.entry_type == entry_type:
                    used_raw_indices.add(candidate_index)
                    entry_to_raw[entry_pos] = candidate_index
                    break

        # Pass 2: fall back to positional order for the rest.
        next_raw_index = 0
        for entry_pos, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry_pos in entry_to_raw:
                continue
            while next_raw_index in used_raw_indices and next_raw_index < len(
                raw_entries
            ):
                next_raw_index += 1
            if next_raw_index < len(raw_entries):
                used_raw_indices.add(next_raw_index)
                entry_to_raw[entry_pos] = next_raw_index
                next_raw_index += 1

        for entry_pos, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            raw_index = entry_to_raw.get(entry_pos)
            statuses = (
                list(raw_entries[raw_index].statuses) if raw_index is not None else []
            )
            if statuses:
                entry["entry_statuses"] = statuses
            if ENTRY_STATUS_ALREADY_PROCESSED not in statuses:
                continue

            entry["classification"] = "skip"
            entry["skip_reason"] = "entry_status:already_processed"
            for key in tuple(entry):
                if not str(key).startswith("task_"):
                    continue
                entry.pop(key, None)
            changed = True

        if changed:
            self._recompute_capture_stats(capture_data)

    def _build_capture_prompt(self, day: date) -> str:
        """Prompt for the shared capture phase."""
        core_context = self._build_injected_context(consumer="capture", target_day=day)
        phase_content = self._load_phase_content("capture")
        about_reference = self._load_dbrain_reference("about")
        classification_reference = self._load_dbrain_reference("classification")
        goals_reference = self._load_dbrain_reference("goals")
        ownership_reference = self._load_ownership_reference()
        return (
            f"Today is {day}. "
            "Read skills/dbrain-processor/phases/capture.md "
            "and execute Phase 1.\n\n"
            f"{self._language_instruction()}\n"
            f"{core_context}\n"
            "=== PHASE INSTRUCTIONS ===\n"
            f"{phase_content}\n"
            "=== END PHASE ===\n\n"
            "=== ABOUT REFERENCE ===\n"
            f"{about_reference}\n"
            "=== END ABOUT REFERENCE ===\n\n"
            "=== CLASSIFICATION REFERENCE ===\n"
            f"{classification_reference}\n"
            "=== END CLASSIFICATION REFERENCE ===\n\n"
            "=== GOALS REFERENCE ===\n"
            f"{goals_reference}\n"
            "=== END GOALS REFERENCE ===\n\n"
            "=== OWNERSHIP REFERENCE ===\n"
            f"{ownership_reference}\n"
            "=== END OWNERSHIP REFERENCE ===\n\n"
            "Use the injected core context to classify each entry. Return ONLY JSON."
        )

    def _build_preview_prompt(self, day: date, capture_data: dict[str, Any]) -> str:
        """Prompt for the fast interactive preview."""
        core_context = self._build_injected_context(consumer="preview", target_day=day)
        phase_content = self._load_phase_content("preview")
        capture_json = json.dumps(capture_data, ensure_ascii=False, indent=2)
        return (
            f"Today is {day}. "
            "Read skills/dbrain-processor/phases/preview.md "
            "and execute the interactive preview mode.\n\n"
            f"{self._language_instruction()}\n"
            f"{core_context}\n"
            "=== PHASE INSTRUCTIONS ===\n"
            f"{phase_content}\n"
            "=== END PHASE ===\n\n"
            "INPUT CAPTURE JSON:\n"
            f"{capture_json}\n\n"
            "Return ONLY markdown for Telegram."
        )

    def _build_execute_prompt(self, day: date) -> str:
        """Prompt for the scheduled execute phase."""
        core_context = self._build_injected_context(consumer="execute", target_day=day)
        phase_content = self._load_phase_content("execute")
        vault_retrieval_skill = self._load_vault_retrieval_skill()
        goals_reference = self._load_dbrain_reference("goals")
        links_reference = self._load_dbrain_reference("links")
        ownership_reference = self._load_ownership_reference()
        process_goals_reference = self._load_dbrain_reference("process-goals")
        routing_reference = self._load_todoist_project_routing_reference()
        todoist_reference = self._load_todoist_reference()
        project_catalog = self._todoist_project_catalog_snapshot(force_refresh=True)
        return (
            f"Today is {day}. "
            "Read skills/dbrain-processor/phases/execute.md "
            "and execute Phase 2.\n\n"
            f"{self._language_instruction()}\n"
            f"{core_context}\n"
            "=== PHASE INSTRUCTIONS ===\n"
            f"{phase_content}\n"
            "=== END PHASE ===\n\n"
            "=== VAULT RETRIEVAL SKILL ===\n"
            f"{vault_retrieval_skill}\n"
            "=== END VAULT RETRIEVAL SKILL ===\n\n"
            "=== GOALS REFERENCE ===\n"
            f"{goals_reference}\n"
            "=== END GOALS REFERENCE ===\n\n"
            "=== LINKS REFERENCE ===\n"
            f"{links_reference}\n"
            "=== END LINKS REFERENCE ===\n\n"
            "=== OWNERSHIP REFERENCE ===\n"
            f"{ownership_reference}\n"
            "=== END OWNERSHIP REFERENCE ===\n\n"
            "=== PROCESS GOALS REFERENCE ===\n"
            f"{process_goals_reference}\n"
            "=== END PROCESS GOALS REFERENCE ===\n\n"
            "=== TODOIST PROJECT ROUTING ===\n"
            f"{routing_reference}\n"
            "=== END TODOIST PROJECT ROUTING ===\n\n"
            "=== TODOIST REFERENCE ===\n"
            f"{todoist_reference}\n"
            "=== END TODOIST REFERENCE ===\n\n"
            "[TODOIST_PROJECT_CATALOG]\n"
            f"{json.dumps(project_catalog, ensure_ascii=False, indent=2)}\n\n"
            f"{self._todoist_cli_rules()}\n\n"
            "Read .session/capture.json for input data.\n"
            "Read business/crm.md, business/network.md, business/events.md, "
            "projects/clients.md, "
            "projects/leads.md, and projects/projects.md for context.\n"
            "Create tasks in Todoist, save thoughts, update CRM. "
            "Return ONLY JSON."
        )

    def _build_text_intent_prompt(self, text: str) -> str:
        """Prompt for routing plain text between capture and answer-now."""
        intent_reference = self._load_intake_intent_reference()
        return (
            "Route one Telegram text message.\n\n"
            f"{self._language_instruction()}\n"
            "=== ROUTING REFERENCE ===\n"
            f"{intent_reference}\n"
            "=== END ROUTING REFERENCE ===\n\n"
            "Message:\n"
            f"{text}\n\n"
            "Return ONLY JSON like:\n"
            "{\n"
            '  "intent": "capture|question",\n'
            '  "confidence": "high|medium|low",\n'
            '  "reason": "short explanation"\n'
            "}\n"
        )

    def _build_question_answer_prompt(self, question: str, user_id: int) -> str:
        """Prompt for direct plain-text answers without capture."""
        core_context = self._build_injected_context(
            consumer="question", target_day=date.today()
        )
        todoist_ref = self._load_todoist_reference()
        question_reference = self._load_question_answer_reference()
        vault_retrieval_skill = self._load_vault_retrieval_skill()
        question_route = self._classify_question_route(question)
        source_footer_policy = (
            "REQUIRED"
            if question_route in {"fact_lookup", "status_history"}
            else "OPTIONAL"
        )
        session_context = self._get_session_context(user_id)
        intro = (
            "Ты - персональный ассистент d-brain. "
            "Ответь на прямой вопрос пользователя сейчас, "
            "без сохранения этого сообщения как заметки."
        )
        return f"""{intro}

CONTEXT:
- Working directory: vault root ({self.vault_path})

{core_context}

{session_context}=== QUESTION ANSWER REFERENCE ===
{question_reference}
=== END QUESTION ANSWER REFERENCE ===

=== VAULT RETRIEVAL SKILL ===
{vault_retrieval_skill}
=== END VAULT RETRIEVAL SKILL ===

=== TODOIST REFERENCE ===
{todoist_ref}
=== END REFERENCE ===

{self._todoist_cli_rules(needs_completed_tasks=True)}

{self._assistant_scope_rules()}

{self._language_instruction()}

If `.session/question-creative-recall.txt` exists and is useful, read it too.

If a QUESTION ROUTE block is provided, follow its read order and escalation
rules. That route overrides the generic defaults below when they conflict.

If a COMPILED BRIEFINGS block is provided, use it before semantic recall.
Treat it as the fast operational briefing layer.
For status/history questions, start from that compiled layer, then verify against
curated core context and qmd when needed.

If an AUTO ARCHIVE RECALL block is provided, use it first.
Treat it as a starting context, not a restriction.
If that block declares a history scope or explicit start point, you MUST follow
it when deciding how far back to analyze the question.
For status/history questions about a project or other long-running topic, you
MUST retrieve and synthesize the history of the topic/question, not only the
latest snapshot.
If the topic spans weeks or months, answer with enough depth to cover:
- current status
- what changed recently
- key milestones or turning points over time
- blockers/risks
- next steps
Do not collapse a long-running project into a very short answer if important
history exists.
SOURCE FOOTER POLICY: {source_footer_policy}
For REQUIRED answers, finish with exactly this markdown section:
Источники:
- [[vault-relative/path.md]]

List 2-5 фактически использованных vault-relative paths. Cite the source note,
not a search-result snippet. Never cite a file you did not read. If fewer than
two confirming sources exist, list only the real source(s) and state the evidence
gap briefly; не выдумывай ссылку. Mark conclusions that go beyond the sources as
an inference. For OPTIONAL answers, add the same section when vault evidence
materially supports the answer, but omit it for purely conversational guidance.

USER QUESTION:
{question}

If the answer depends on current operational state, inspect Todoist
or recent vault notes before answering instead of guessing.

{
            self._telegram_markdown_output_rules(
                opening_line=(
                    "Start with the actual answer in the first sentence. "
                    "Do not put an emoji or heading before that sentence."
                )
            )
        }
"""

    def _build_reflect_prompt(self, day: date) -> str:
        """Prompt for the scheduled reflect phase."""
        core_context = self._build_injected_context(consumer="reflect", target_day=day)
        phase_content = self._load_phase_content("reflect")
        vault_retrieval_skill = self._load_vault_retrieval_skill()
        goals_reference = self._load_dbrain_reference("goals")
        links_reference = self._load_dbrain_reference("links")
        return (
            f"Today is {day}. "
            "Read skills/dbrain-processor/phases/reflect.md "
            "and execute Phase 3.\n\n"
            f"{self._language_instruction()}\n"
            f"{core_context}\n"
            "=== PHASE INSTRUCTIONS ===\n"
            f"{phase_content}\n"
            "=== END PHASE ===\n\n"
            "=== VAULT RETRIEVAL SKILL ===\n"
            f"{vault_retrieval_skill}\n"
            "=== END VAULT RETRIEVAL SKILL ===\n\n"
            "=== GOALS REFERENCE ===\n"
            f"{goals_reference}\n"
            "=== END GOALS REFERENCE ===\n\n"
            "=== LINKS REFERENCE ===\n"
            f"{links_reference}\n"
            "=== END LINKS REFERENCE ===\n\n"
            "Read .session/capture.json and .session/execute.json for input data.\n"
            "Read .graph/health-history.json, .session/creative-recall.txt and "
            ".session/memory-audit.md "
            "when they exist.\n"
            "Do not write to daily/{DATE}.md directly from this phase; "
            "the Python runtime records the bounded daily summary block itself.\n"
            "Generate one markdown report, update MEMORY, deduplicate "
            "overlapping memory rules when needed, "
            "record observations.\n"
            "Return ONLY markdown for Telegram."
        )

    @staticmethod
    def _json_dict_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Return a typed list of dict items from one JSON phase payload."""
        items = payload.get(key)
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _thought_description_from_body(title: str, body: str) -> str:
        """Derive one retrieval-style snippet when description is blank."""
        for raw_line in body.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line or line.startswith("#"):
                continue
            if line.startswith(("- ", "* ", "1. ")):
                continue
            return line
        return title

    @staticmethod
    def _valid_thought_date(value: object, fallback: date) -> str:
        """Keep generated thought dates valid for profile validation."""
        candidate = str(value or "").strip().strip("\"'")
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return fallback.isoformat()

    @staticmethod
    def _normalized_thought_tags(
        value: object,
        *,
        category: str,
        reflection: bool,
    ) -> list[str]:
        """Return 2-5 normalized tags from structured or scalar metadata."""
        raw_values = value if isinstance(value, list) else [value]
        tags: list[str] = []
        for raw_value in raw_values:
            if isinstance(raw_value, str):
                tags.extend(
                    re.findall(
                        r"[a-z0-9]+(?:-[a-z0-9]+)*",
                        raw_value.casefold(),
                    )
                )
        defaults = ["reflection", "system"] if reflection else [category, "memory"]
        for tag in defaults:
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag):
                tags.append(tag)
        unique = list(dict.fromkeys(tags))[:5]
        while len(unique) < 2:
            unique.append("memory")
            unique = list(dict.fromkeys(unique))
        return unique

    @staticmethod
    def _normalized_thought_relevance(value: object) -> float:
        """Clamp malformed generated relevance to the manifest range."""
        try:
            relevance = float(str(value).strip().strip("\"'"))
        except (TypeError, ValueError):
            relevance = 1.0
        if not 0 <= relevance <= 1:
            relevance = 1.0
        return relevance

    def _repair_malformed_saved_thought_note(
        self,
        path: Path,
        original: str,
        *,
        day: date,
    ) -> bytes | None:
        """Keep the narrow legacy repair for YAML that cannot be losslessly parsed."""
        frontmatter, body = self._split_thought_frontmatter(original)
        if not frontmatter:
            return None

        inner_lines = frontmatter.strip().splitlines()[1:-1]
        scalar_fields: dict[str, str] = {}
        list_fields: dict[str, list[str]] = {}
        multiline_fields: dict[str, list[str]] = {}
        stray_lines: list[str] = []
        current_key: tuple[str, str] | None = None

        for raw_line in inner_lines:
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            match = THOUGHT_FRONTMATTER_KEY_RE.match(stripped)
            if match and not raw_line.startswith(("  ", "\t")):
                key = match.group(1)
                value = (match.group(2) or "").strip()
                if value == ">-":
                    multiline_fields.setdefault(key, [])
                    current_key = ("multiline", key)
                elif not value:
                    list_fields.setdefault(key, [])
                    current_key = ("list", key)
                else:
                    scalar_fields[key] = value
                    current_key = None
                continue

            if current_key is not None:
                mode, key = current_key
                if mode == "multiline" and raw_line.startswith(("  ", "\t")):
                    multiline_fields.setdefault(key, []).append(stripped)
                    continue
                if mode == "list" and stripped.startswith("- "):
                    list_fields.setdefault(key, []).append(stripped[2:].strip())
                    continue

            stray_lines.append(stripped)

        title_match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        description_parts = multiline_fields.get("description", [])
        if description_parts:
            description = " ".join(
                part.strip() for part in description_parts if part.strip()
            )
        elif stray_lines:
            description = " ".join(stray_lines)
        else:
            description = self._thought_description_from_body(title, body)
        description = " ".join(description.split())

        reflection = path.parent.name == "reflections"
        category = path.parent.name.rstrip("s") or "thought"
        tags = self._normalized_thought_tags(
            scalar_fields.get("tags", ""),
            category=category,
            reflection=reflection,
        )
        status = scalar_fields.get("status", "").strip().strip("\"'")
        if status not in {"active", "draft", "pending", "done", "inactive"}:
            status = "active"
        tier = scalar_fields.get("tier", "").strip().strip("\"'")
        if tier not in {"core", "active", "warm", "cold", "archive"}:
            tier = "active"
        relevance = self._normalized_thought_relevance(
            scalar_fields.get("relevance", "1.0")
        )
        related_items = list_fields.get("related", [])
        if not related_items:
            related_items = ["[]"]

        normalized_frontmatter = [
            "---",
            (
                "type: "
                f"{'reflection' if reflection else scalar_fields.get('type') or 'note'}"
            ),
            "description: >-",
            f"  {description or title}",
            f"tags: [{', '.join(tags)}]",
            f"source: {scalar_fields.get('source') or f'daily/{day.isoformat()}.md'}",
            f"status: {status}",
            (
                "created: "
                f"{self._valid_thought_date(scalar_fields.get('created', ''), day)}"
            ),
            f"updated: {day.isoformat()}",
            f"last_accessed: {day.isoformat()}",
            f"relevance: {relevance}",
            f"tier: {tier}",
            "related:" if related_items != ["[]"] else "related: []",
        ]
        if reflection:
            normalized_frontmatter.insert(4, f"date: {day.isoformat()}")
        if related_items != ["[]"]:
            normalized_frontmatter.extend(f"  - {item}" for item in related_items)
        normalized = "\n".join(normalized_frontmatter) + "\n---\n\n" + body.lstrip()
        return normalized.encode("utf-8")

    @classmethod
    def _legacy_thought_repair_allowed(
        cls,
        original: str,
        error: FrontmatterError,
    ) -> bool:
        """Recognize only the two historical thought-frontmatter defects."""
        if isinstance(error, DuplicateKeyError):
            return False
        if str(error) == "frontmatter is missing a closing '---'":
            frontmatter, body = cls._split_thought_frontmatter(original)
            if not frontmatter or not body or not original.startswith("---\n"):
                return False
            try:
                parse_frontmatter_bytes(frontmatter.encode("utf-8"))
            except FrontmatterError:
                return False
            return True

        frontmatter, _body = cls._split_frontmatter(original)
        if not frontmatter:
            return False
        lines = frontmatter.splitlines(keepends=True)
        if not any(line.strip() == "description: >-" for line in lines):
            return False

        closing_index = len(lines) - 1
        while closing_index > 0 and not lines[closing_index].strip():
            closing_index -= 1
        if lines[closing_index].strip() != "---":
            return False
        stray_indexes: list[int] = []
        current_mode: str | None = None
        for index, raw_line in enumerate(lines[1:closing_index], start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = THOUGHT_FRONTMATTER_KEY_RE.match(stripped)
            if match and not raw_line.startswith(("  ", "\t")):
                value = (match.group(2) or "").strip()
                if value == ">-":
                    current_mode = "multiline"
                elif not value:
                    current_mode = "list"
                else:
                    current_mode = None
                continue
            if (current_mode == "multiline" and raw_line.startswith(("  ", "\t"))) or (
                current_mode == "list"
                and raw_line.startswith(("  ", "\t"))
                and stripped.startswith("- ")
            ):
                continue
            if raw_line.startswith(("  ", "\t")):
                return False
            stray_indexes.append(index)

        if len(stray_indexes) != 1:
            return False
        cleaned = "".join(
            line for index, line in enumerate(lines) if index != stray_indexes[0]
        )
        try:
            parse_frontmatter_bytes(cleaned.encode("utf-8"))
        except FrontmatterError:
            return False
        return True

    def _normalize_saved_thought_note(self, path: Path, *, day: date) -> bool:
        """Losslessly normalize targeted fields in one generated thought note."""
        try:
            original_bytes = path.read_bytes()
        except FileNotFoundError:
            return False
        try:
            original = original_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return False

        try:
            document = parse_frontmatter_bytes(original_bytes)
        except DuplicateKeyError:
            return False
        except FrontmatterError as exc:
            if not self._legacy_thought_repair_allowed(original, exc):
                return False
            candidate = self._repair_malformed_saved_thought_note(
                path,
                original,
                day=day,
            )
            if candidate is None:
                return False
        else:
            if not document.has_frontmatter:
                return False
            body = document.body.decode("utf-8")
            fields = document.fields
            title_match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else path.stem

            raw_description = fields.get("description")
            description = (
                " ".join(raw_description.split())
                if isinstance(raw_description, str)
                else ""
            )
            if not description:
                for key, value in fields.items():
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                        continue
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        suffix = "" if value is None else f": {value}"
                        description = f"{key}{suffix}"
                        break
            if not description:
                description = self._thought_description_from_body(title, body)
            description = " ".join(description.split()) or title

            reflection = path.parent.name == "reflections"
            category = path.parent.name.rstrip("s") or "thought"
            raw_type = fields.get("type")
            thought_type = (
                "reflection"
                if reflection
                else (
                    raw_type.strip()
                    if isinstance(raw_type, str) and raw_type.strip()
                    else "note"
                )
            )
            tags = self._normalized_thought_tags(
                fields.get("tags"),
                category=category,
                reflection=reflection,
            )
            raw_status = fields.get("status")
            status = raw_status.strip() if isinstance(raw_status, str) else ""
            if status not in {"active", "draft", "pending", "done", "inactive"}:
                status = "active"
            raw_tier = fields.get("tier")
            tier = raw_tier.strip() if isinstance(raw_tier, str) else ""
            if tier not in {"core", "active", "warm", "cold", "archive"}:
                tier = "active"
            related_value = fields.get("related")
            related = (
                list(related_value)
                if isinstance(related_value, list)
                and all(isinstance(item, str) for item in related_value)
                else []
            )

            targets: dict[str, object] = {
                "type": thought_type,
                "description": description,
                "tags": tags,
                "status": status,
                "created": self._valid_thought_date(fields.get("created", ""), day),
                "updated": day.isoformat(),
                "last_accessed": day.isoformat(),
                "relevance": self._normalized_thought_relevance(
                    fields.get("relevance", 1.0)
                ),
                "tier": tier,
                "related": related,
            }
            if reflection:
                targets["date"] = day.isoformat()
            updates = {
                key: value for key, value in targets.items() if fields.get(key) != value
            }
            candidate = patch_frontmatter_bytes(original_bytes, updates)

        if candidate == original_bytes:
            return False
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = path.read_bytes()
            except FileNotFoundError:
                return False
            if current_bytes != original_bytes:
                return False
            try:
                self._write_vault_markdown(
                    path,
                    candidate.decode("utf-8"),
                    lock=lock,
                    expected_full_sha256=sha256(original_bytes).hexdigest(),
                )
            except UnsafeVaultPathError as exc:
                if (
                    str(exc)
                    != "atomic write source does not match expected_full_sha256"
                ):
                    raise
                logger.warning(
                    "Thought note normalization conflict for %s; skipped",
                    path,
                )
                return False
        return True

    def _normalize_saved_thoughts(
        self,
        execute_data: dict[str, Any],
        *,
        day: date,
    ) -> None:
        """Best-effort cleanup for thought notes created by the execute phase."""
        normalized_count = 0
        for item in self._json_dict_list(execute_data, "thoughts_saved"):
            rel_path = str(item.get("path") or "").strip()
            if not rel_path.startswith("thoughts/"):
                continue
            try:
                changed = self._normalize_saved_thought_note(
                    self.vault_path / rel_path,
                    day=day,
                )
            except Exception as exc:
                logger.warning(
                    "Thought note normalization failed for %s: %s",
                    rel_path,
                    exc,
                )
                continue
            if changed:
                normalized_count += 1
        if normalized_count:
            logger.info("Normalized %s thought note(s) after execute", normalized_count)

    def _write_reflect_daily_block(
        self,
        day: date,
        execute_data: dict[str, Any],
    ) -> None:
        """Persist the reflect summary block via the single locked daily writer."""
        timestamp = datetime.now().astimezone()
        tasks_created = self._json_dict_list(execute_data, "tasks_created")
        thoughts_saved = self._json_dict_list(execute_data, "thoughts_saved")
        crm_updated = self._json_dict_list(execute_data, "crm_updated")

        # Not "[text]": that is an own-entry marker (OWN_ENTRY_MARK_RE in
        # compiled_briefings.py), and this block is not the owner typing.
        # Its task contents, note titles and CRM descriptions are what the
        # execute phase derived from the day's entries -- forwarded ones
        # included -- so marking it own let someone else's words reach
        # CONSEQUENTIAL_ACTION_TRUST_LEVELS and silently supersede a fact on
        # the owner's page. Nothing is lost by rating this summary lower:
        # the entry it summarizes is still in the same file at full trust.
        lines = [
            f"\n## {timestamp:%H:%M} [d-brain]",
            REFLECT_DAILY_START_MARKER,
            "d-brain processing",
            "",
            f"**Tasks created:** {len(tasks_created)}",
        ]
        # Every value below is execute-phase output, i.e. what a model read
        # off the day's entries -- forwarded ones included -- and each lands
        # in a one-line construct here. collapse_to_single_line, not strip():
        # a value that keeps a newline gets a line of its own inside the
        # block, where upsert_daily_block cannot tell it from the block's own
        # markers, and a forged or duplicated marker there wedges every
        # later write for the day.
        for task in tasks_created:
            content = collapse_to_single_line(task.get("content")) or "Untitled task"
            details: list[str] = []
            task_id = collapse_to_single_line(task.get("id"))
            if task_id:
                details.append(f"id: {task_id}")
            priority = task.get("priority")
            if priority not in {None, ""}:
                details.append(f"priority: {collapse_to_single_line(priority)}")
            due_value = collapse_to_single_line(
                task.get("due") or task.get("due_hint")
            )
            if due_value:
                details.append(f"due: {due_value}")
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f'- "{content}"{suffix}')

        lines.extend(["", f"**Thoughts saved:** {len(thoughts_saved)}"])
        for thought in thoughts_saved:
            path = collapse_to_single_line(thought.get("path"))
            title = collapse_to_single_line(thought.get("title")) or path or (
                "Untitled note"
            )
            category = collapse_to_single_line(thought.get("category"))
            link = f"[[{path}|{title}]]" if path else title
            suffix = f" — {category}" if category else ""
            lines.append(f"- {link}{suffix}")

        lines.extend(["", f"**CRM updated:** {len(crm_updated)}"])
        for change in crm_updated:
            path = collapse_to_single_line(change.get("path")) or "business/crm.md"
            description = collapse_to_single_line(change.get("change"))
            line = f"- [[{path}]]"
            if description:
                line += f" — {description}"
            lines.append(line)

        lines.extend([REFLECT_DAILY_END_MARKER, ""])
        block = "\n".join(lines)
        VaultStorage(self.vault_path, self.content_language).upsert_daily_block(
            day=day,
            start_marker=REFLECT_DAILY_START_MARKER,
            end_marker=REFLECT_DAILY_END_MARKER,
            block=block,
            refresh_qmd=False,
        )

    def classify_text_intent(self, text: str) -> dict[str, str]:
        """Classify plain text as capture or direct question."""
        try:
            payload = self._run_json_phase(
                self._build_text_intent_prompt(text),
                phase_name="text-intent",
            )
        except Exception as exc:
            logger.warning("Text intent classification failed: %s", exc)
            return {
                "intent": TEXT_INTENT_CAPTURE,
                "confidence": "low",
                "reason": "classifier failed",
            }

        intent = payload.get("intent")
        confidence = payload.get("confidence")
        reason = payload.get("reason", "")
        if intent not in {TEXT_INTENT_CAPTURE, TEXT_INTENT_QUESTION}:
            intent = TEXT_INTENT_CAPTURE
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if intent == TEXT_INTENT_QUESTION and confidence != "high":
            intent = TEXT_INTENT_CAPTURE
        workflow = resolve_text_workflow(
            text,
            intent=str(intent),
            confidence=str(confidence),
        )
        return {
            "intent": str(intent),
            "confidence": str(confidence),
            "reason": str(reason),
            "workflow": workflow.name,
            "workflow_kind": workflow.kind,
        }

    def _classify_question_route(self, question: str) -> str:
        """Classify direct questions into a small set of routing strategies."""
        return classify_control_plane_question_route(question)

    def _build_question_route_block(self, question: str) -> str:
        """Describe how the assistant should prioritize context for this question."""
        return build_control_plane_question_route_block(question)

    def _inject_question_context_blocks(self, prompt: str, question: str) -> str:
        """Inject route-aware context blocks before the user question."""
        compiled_block = self._build_compiled_briefings_block(question)
        recall_block = self._build_auto_recall_block(
            question,
            purpose="question_answer",
        )
        block_by_name = {
            "compiled": compiled_block,
            "recall": recall_block,
        }
        block_names = tuple(
            name
            for name in iter_question_context_blocks(question)
            if block_by_name.get(name, "")
        )
        if "compiled" not in block_names:
            # This route's block order dropped "compiled" (or ranking found
            # nothing): no compiled page actually reached the prompt, so no
            # candidates survive for provenance either (ТЗ 7.4 code-review
            # defect 3).
            self._question_provenance_candidates = ()
        blocks = [self._build_question_route_block(question)]
        blocks.extend(block_by_name[name] for name in block_names)

        for block in blocks:
            prompt = self._inject_prompt_block(prompt, "USER QUESTION:", block)
        return prompt

    def answer_question(self, question: str, user_id: int = 0) -> dict[str, Any]:
        """Answer a plain-text question directly without capturing it as a note."""
        try:
            self._capture_creative_recall(
                sample_size=2,
                file_name="question-creative-recall.txt",
            )
            prompt = self._build_question_answer_prompt(question, user_id)
            prompt = self._inject_question_context_blocks(prompt, question)
            output = self._run_assistant_prompt(prompt)
            normalized = self._normalize_owner_report_markdown(output)
            normalized = self._append_question_provenance(normalized, question)
            self._file_output_artifact_if_useful(
                request=question,
                output_markdown=normalized,
                artifact_type="question-answer",
            )
            return {
                "report": normalized,
                "processed_entries": 1,
            }
        except TimeoutError:
            logger.error("%s question answering timed out", self.ai_cli)
            return {"error": "Question answering timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("%s CLI not found", self.ai_cli)
            return {"error": f"{self.ai_cli} CLI not installed", "processed_entries": 0}
        except CliExecutionError as exc:
            logger.error("%s question answering failed: %s", self.ai_cli, exc)
            return {"error": str(exc), "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during question answering")
            return {"error": str(exc), "processed_entries": 0}

    def _build_compiled_briefings_block(self, question: str) -> str:
        """Build a small compiled-briefing context block for direct answers.

        Also freezes the exact ranked candidates behind this block onto
        ``self`` (ТЗ 7.4 code-review defect 2): ``_append_question_provenance``
        reuses them instead of re-ranking the vault after the model has
        already run, when a background enrichment job or the model's own
        write could have changed a page's frontmatter underneath it.
        """
        service = CompiledBriefingService(
            self.vault_path,
            content_language=self.content_language,
            ai_cli=self.ai_cli,
        )
        self._question_provenance_candidates = tuple(
            service._rank_candidates(question, limit=QUESTION_CONTEXT_LIMIT)
        )
        # Hand the frozen list straight through: ranking again inside
        # ``build_question_context`` would be a second live scan of
        # ``compiled/**``, so a background enrichment landing in between
        # would put one set of pages into the prompt and cite another in the
        # footnote -- the exact drift this freeze exists to prevent.
        return service.build_question_context(
            question, ranked=self._question_provenance_candidates
        )

    @staticmethod
    def _is_real_answer_paragraph(block: str) -> bool:
        """True when ``block`` is prose the provenance warning may follow --
        not a heading, a table row, or a fenced code block (ТЗ 7.4
        code-review defect 1: those must never get the warning wedged in
        front of the answer's actual first sentence)."""
        stripped = block.strip()
        if not stripped:
            return False
        if stripped.startswith("```") or stripped.startswith("~~~"):
            return False
        first_line = stripped.splitlines()[0]
        if QUESTION_ANSWER_HEADING_RE.match(first_line):
            return False
        if QUESTION_ANSWER_TABLE_ROW_RE.match(first_line):
            return False
        return True

    @staticmethod
    def _fence_state_after(block: str, in_fence: bool) -> bool:
        """Whether a fenced code block is still open once ``block`` ends.

        ``QUESTION_ANSWER_BLOCK_SPLIT_RE`` splits on blank lines, and a
        fenced code block is free to contain them -- so
        ``_is_real_answer_paragraph``'s "does this block start with a
        fence" test only ever recognizes the block that *opens* the fence.
        Every later block of the same fence looks like ordinary prose to
        it, which is how the warning ended up wedged inside the code
        (rendered literally, not as a callout). Toggling on each fence
        delimiter line carries that state across the blank-line splits.
        """
        for line in block.splitlines():
            if line.strip().startswith(("```", "~~~")):
                in_fence = not in_fence
        return in_fence

    @staticmethod
    def _split_at_first_fence(block: str) -> tuple[str, str]:
        """Split ``block`` into the prose before its first fence delimiter
        line and the rest, or ``(block, "")`` when it opens no fence."""
        lines = block.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip().startswith(("```", "~~~")):
                return "".join(lines[:index]), "".join(lines[index:])
        return block, ""

    @classmethod
    def _insert_after_first_paragraph(cls, markdown: str, warning: str) -> str:
        """Insert ``warning`` right after the answer's first real paragraph
        (ТЗ 7.4 code-review defect 1), skipping any opening heading, table,
        or fenced code block. Falls back to putting ``warning`` first when
        no real paragraph exists at all (e.g. an empty model response, or a
        never-closed fence) -- there is nothing safe to follow in that case.
        """
        if not warning:
            return markdown
        parts = QUESTION_ANSWER_BLOCK_SPLIT_RE.split(markdown)
        in_fence = False
        for index in range(0, len(parts), 2):
            block = parts[index]
            if not in_fence and cls._is_real_answer_paragraph(block):
                # A block may *start* as prose and open a fence further down
                # with no blank line between them ("Вот пример:" immediately
                # followed by ```python -- a very ordinary model answer).
                # Appending to such a block drops the warning inside the code;
                # skipping the block instead sends an answer that is nothing
                # but prose+code to the warning-first fallback, i.e. straight
                # back into the ТЗ 7.4 defect. Splitting the block is the only
                # placement that is both outside the fence and after the
                # answer's real first sentence.
                prose, fenced = cls._split_at_first_fence(block)
                parts[index] = (
                    f"{prose.rstrip()}\n\n{warning}\n\n{fenced}"
                    if fenced
                    else f"{block}\n\n{warning}"
                )
                return "".join(parts)
            in_fence = cls._fence_state_after(block, in_fence)
        return f"{warning}\n\n{markdown}" if markdown else warning

    def _append_question_provenance(self, markdown: str, question: str) -> str:
        """Append code-determined provenance to a direct-question answer
        (ТЗ 7.4, 4.4): trust level and open-conflict count for the compiled
        pages ``_build_compiled_briefings_block`` actually ranked for this
        question when it built the prompt (ТЗ 7.4 code-review defect 2) --
        see ``compiled_question_provenance`` for the "ranked" vs "used"
        caveat.

        The warning callout is inserted right after the answer's first real
        paragraph, never before it and never inside an opening heading,
        table, or fenced code block (ТЗ 7.4 code-review defect 1): the
        prompt separately instructs the model to open with the actual
        answer, and a test locks that in. The full per-page block is
        appended at the end. Best-effort, like the neighboring
        ``_file_output_artifact_if_useful``: a failure here must never break
        an answer already produced for the owner.
        """
        try:
            provenance = build_question_provenance(
                self.vault_path,
                question,
                candidates=self._question_provenance_candidates,
            )
            if not provenance.block:
                return markdown
            result = self._insert_after_first_paragraph(markdown, provenance.warning)
            result = f"{result}\n\n{provenance.block}"
            self._touch_memory_paths(*provenance.touched_paths)
            return result
        except Exception as exc:
            logger.warning("Question provenance skipped: %s", exc)
            return markdown

    def _file_output_artifact_if_useful(
        self,
        *,
        request: str,
        output_markdown: str,
        artifact_type: str,
    ) -> str | None:
        """Persist reusable assistant outputs back into searchable summaries."""
        try:
            return CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
                ai_cli=self.ai_cli,
            ).file_output_artifact(
                request=request,
                output_markdown=output_markdown,
                artifact_type=artifact_type,
            )
        except Exception as exc:
            logger.warning("Output filing skipped: %s", exc)
            return None

    def _run_compiled_nightly_maintenance(self) -> dict[str, Any]:
        """Drain queue and run deterministic compiled-note maintenance."""
        try:
            result = CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
                ai_cli=self.ai_cli,
            ).run_nightly_maintenance()
        except Exception as exc:
            logger.warning("Compiled nightly maintenance failed: %s", exc)
            return {"error": str(exc), "processed_entries": 0}
        # ТЗ 5.5 inv 7: "факт исчерпания бюджета попадает в дайджест" --
        # ``run_nightly_maintenance``'s own return value has no
        # ``budget_exhausted`` key (only the pass journal
        # ``_write_pass_journal`` writes does), so read it back the same way
        # the compile digest itself does (``compiled_enrich_report``) rather
        # than leaving this combined report silent about it.
        budget_exhausted = read_pass_status(self.vault_path).budget_exhausted
        if self.content_language == "ru":
            report_lines = [
                "## 🧩 Поддержка compiled-слоя",
                "",
                f"- Очередь обработана: {int(result.get('queued_drained') or 0)}",
                f"- Консолидации: {len(result.get('consolidations', []))}",
                (
                    "- Архивировано устаревших заметок: "
                    f"{len(result.get('archived', []))}"
                ),
                f"- Проблемы проверки: {len(result.get('lint_issues', []))}",
                (f"- Карточки к переоценке: {len(result.get('freshness_issues', []))}"),
            ]
            if result.get("queue_busy"):
                worker_pid = int(result.get("queue_worker_pid") or 0)
                if worker_pid > 0:
                    report_lines.append(
                        f"- Очередь уже дренируется фоновым воркером (pid {worker_pid})"
                    )
                else:
                    report_lines.append("- Очередь уже дренируется фоновым воркером")
            elif result.get("queue_errors"):
                report_lines.append(
                    f"- Ошибки очереди: {len(result.get('queue_errors', []))}"
                )
        else:
            report_lines = [
                "## 🧩 Compiled Maintenance",
                "",
                f"- Queue drained: {int(result.get('queued_drained') or 0)}",
                f"- Consolidations: {len(result.get('consolidations', []))}",
                f"- Archived stale notes: {len(result.get('archived', []))}",
                f"- Lint issues: {len(result.get('lint_issues', []))}",
                (
                    "- Briefings needing review: "
                    f"{len(result.get('freshness_issues', []))}"
                ),
            ]
            if result.get("queue_busy"):
                worker_pid = int(result.get("queue_worker_pid") or 0)
                if worker_pid > 0:
                    report_lines.append(
                        "- Queue is already draining in a background worker "
                        f"(pid {worker_pid})"
                    )
                else:
                    report_lines.append(
                        "- Queue is already draining in a background worker"
                    )
            elif result.get("queue_errors"):
                report_lines.append(
                    f"- Queue errors: {len(result.get('queue_errors', []))}"
                )
        if budget_exhausted:
            if self.content_language == "ru":
                report_lines.append(
                    "- Бюджет прохода исчерпан: "
                    + "; ".join(describe_budget_exhausted(budget_exhausted))
                )
            else:
                report_lines.append(
                    "- Pass budget exhausted: " + ", ".join(budget_exhausted)
                )
        result["report"] = "\n".join(report_lines)
        result["processed_entries"] = len(result.get("archived", []))
        return result

    def _run_compiled_fact_check_cycle(self) -> dict[str, Any]:
        """Monthly-cadence deterministic re-check of stale compiled pages (ТЗ 6.6).

        Zero model calls; only patches ``last_verified``/``confidence``.
        Separate from ``_run_compiled_nightly_maintenance`` -- see
        ``compiled_fact_check.run_monthly_fact_check`` for why this is its
        own process with its own budget and journal.
        """
        try:
            result = run_monthly_fact_check(self.vault_path)
        except Exception as exc:
            logger.warning("Compiled fact-check failed: %s", exc)
            return {"error": str(exc), "processed_entries": 0}
        if self.content_language == "ru":
            report_lines = [
                "## 🔎 Проверка фактов compiled",
                "",
                f"- Страниц проверено: {int(result.get('pages_checked') or 0)}",
                f"- Дата подтверждена: {int(result.get('pages_patched') or 0)}",
                (
                    "- Отправлено на решение владельца: "
                    f"{int(result.get('pages_flagged') or 0)}"
                ),
                (
                    "- Утверждений проверено: "
                    f"{int(result.get('claims_checked') or 0)}, "
                    f"не подтвердилось: {int(result.get('claims_failed') or 0)}, "
                    f"непроверяемых: {int(result.get('claims_unverifiable') or 0)}"
                ),
            ]
            if result.get("queue_evictions"):
                report_lines.append(
                    "- Вытеснено из очереди решений: "
                    f"{int(result.get('queue_evictions') or 0)}"
                )
            if result.get("errors"):
                report_lines.append(
                    f"- Ошибки записи: {len(result.get('errors', []))}"
                )
        else:
            report_lines = [
                "## 🔎 Compiled Fact-Check",
                "",
                f"- Pages checked: {int(result.get('pages_checked') or 0)}",
                f"- Date confirmed: {int(result.get('pages_patched') or 0)}",
                (
                    "- Sent to owner for a decision: "
                    f"{int(result.get('pages_flagged') or 0)}"
                ),
                (
                    "- Claims checked: "
                    f"{int(result.get('claims_checked') or 0)}, "
                    f"failed: {int(result.get('claims_failed') or 0)}, "
                    f"unverifiable: {int(result.get('claims_unverifiable') or 0)}"
                ),
            ]
            if result.get("queue_evictions"):
                report_lines.append(
                    "- Evicted from the decisions queue: "
                    f"{int(result.get('queue_evictions') or 0)}"
                )
            if result.get("errors"):
                report_lines.append(
                    f"- Write errors: {len(result.get('errors', []))}"
                )
        result["report"] = "\n".join(report_lines)
        result["processed_entries"] = int(result.get("pages_patched") or 0)
        return result

    def _run_vault_health_cycle(self) -> dict[str, Any]:
        """Rebuild graph stats and apply a safe repair pass when health is low."""
        try:
            self._rebuild_graph()
            initial = self._load_graph_stats()
            initial_score = float(initial.get("health_score") or 0.0)
            initial_broken = int(initial.get("broken_link_count") or 0)
            initial_orphans = int(initial.get("orphan_count") or 0)
            initial_weak = int(initial.get("weakly_connected_count") or 0)
            initial_malformed_daily = int(initial.get("malformed_daily_count") or 0)
            repair_applied = False

            if initial_score < VAULT_HEALTH_LOW_SCORE_THRESHOLD and initial_broken > 0:
                self._run_uv_script(
                    "skills/vault-health/scripts/fix_links.py",
                    "--apply",
                )
                repair_applied = True
                self._rebuild_graph()

            final = self._load_graph_stats()
            final_score = float(final.get("health_score", initial_score))
            final_broken = int(final.get("broken_link_count", initial_broken))
            final_orphans = int(final.get("orphan_count", initial_orphans))
            final_weak = int(final.get("weakly_connected_count", initial_weak))
            final_malformed_daily = int(
                final.get("malformed_daily_count", initial_malformed_daily)
            )
        except Exception as exc:
            logger.warning("Vault health maintenance failed: %s", exc)
            return {"error": str(exc), "processed_entries": 0}

        if self.content_language == "ru":
            report_lines = [
                "## 🩺 Здоровье vault",
                "",
                f"- Оценка: {final_score:.1f}/100",
                f"- Битые ссылки: {final_broken}",
                f"- Сироты: {final_orphans}",
                f"- Слабо связанные заметки: {final_weak}",
                f"- Нарушения структуры daily: {final_malformed_daily}",
            ]
        else:
            report_lines = [
                "## 🩺 Vault Health",
                "",
                f"- Score: {final_score:.1f}/100",
                f"- Broken links: {final_broken}",
                f"- Orphans: {final_orphans}",
                f"- Weakly connected: {final_weak}",
                f"- Malformed daily notes: {final_malformed_daily}",
            ]
        if repair_applied:
            report_lines.append(
                "- Repair applied: fix_links (--apply), score was "
                f"{initial_score:.1f}/100"
                if self.content_language != "ru"
                else (
                    "- Применено автоисправление: fix_links (--apply), "
                    f"исходная оценка была {initial_score:.1f}/100"
                )
            )
        elif initial_score < VAULT_HEALTH_LOW_SCORE_THRESHOLD:
            report_lines.append(
                "- Repair skipped: health low, but no safe automatic "
                "broken-link fix was available"
                if self.content_language != "ru"
                else (
                    "- Автоисправление не применялось: оценка низкая, но "
                    "безопасного исправления битых ссылок не нашлось"
                )
            )

        return {
            "report": "\n".join(report_lines),
            "processed_entries": 1 if repair_applied else 0,
            "health_score": final_score,
            "broken_link_count": final_broken,
            "orphan_count": final_orphans,
            "weakly_connected_count": final_weak,
            "malformed_daily_count": final_malformed_daily,
            "repair_applied": repair_applied,
            "searchable_write": repair_applied,
        }

    def _run_compiled_digest_cycle(self) -> dict[str, Any]:
        """Build, write, and deliver the ТЗ 7.1 owner digest for the
        compiled-enrichment layer as the last of the ``scheduled-post``
        maintenance workflows (registry.py: it reads the pass journal
        ``maintenance.compiled-nightly`` just wrote and the decisions queue
        ``maintenance.compiled-fact-check`` may have just added to, so it
        must run after both).

        Unlike this cycle's siblings, the digest text itself is delivered
        to the owner as its own Telegram message (``send_telegram_text_sync``)
        instead of being folded into the combined scheduled report -- it is
        a decision-focused artifact, not routine cycle status, so ``report``
        is left empty here (nothing to add to the combined report) and the
        written file's path is surfaced via ``summary_path`` instead, which
        ``run_daily_process.py``'s periodic-cycle line renderer already
        picks up. This means the owner receives two Telegram messages at
        21:00 on a day with something to report -- accepted deliberately so
        the digest stays a distinct message instead of being buried inside
        the general summary.

        A vault with no project manifest yet (e.g. a fresh vault before its
        first nightly pass, or a temp vault in a test that does not build
        one) has nothing safe to write to; that is treated exactly like a
        quiet night -- skipped, no exception, no write attempt.
        """
        try:
            manifest = load_manifest_for_vault(self.vault_path)
        except Exception:
            return {
                "report": "",
                "processed_entries": 0,
                "skipped": True,
                "searchable_write": False,
            }

        today = date.today()
        try:
            pass_status = read_pass_status(self.vault_path)
            digest = build_daily_digest(self.vault_path, today, pass_status=pass_status)
        except Exception as exc:
            logger.warning("Compiled digest cycle failed: %s", exc)
            return {"error": str(exc), "processed_entries": 0}

        if digest is None:
            return {
                "report": "",
                "processed_entries": 0,
                "skipped": True,
                "searchable_write": False,
            }

        path = digest_path(self.vault_path, today)
        try:
            with vault_write_lock(self.vault_path) as lock:
                write_validated_vault_markdown(
                    self.vault_path,
                    path,
                    render_digest_note(today, digest),
                    manifest=manifest,
                    existing_lock=lock,
                )
                # Nightly safety net (задача N): regenerates the
                # human-readable decisions-queue mirror file even if no
                # response handler ran today. Only reached past the
                # ``digest is None`` quiet-day return above, and
                # ``write_queue_document`` is itself best-effort (catches
                # and logs), so it cannot turn a successful digest write
                # into a reported failure.
                write_queue_document(
                    self.vault_path, manifest=manifest, existing_lock=lock
                )
        except Exception as exc:
            logger.warning("Failed to write compiled digest: %s", exc)
            return {"error": str(exc), "processed_entries": 0}

        try:
            send_telegram_text_sync(digest, rich=True)
        except Exception as exc:  # pragma: no cover - notification boundary
            logger.warning("Failed to send compiled digest: %s", exc)

        return {
            "report": "",
            "processed_entries": 1,
            "summary_path": path.relative_to(self.vault_path).as_posix(),
            "searchable_write": True,
        }

    def process_daily(
        self,
        day: date | None = None,
        *,
        mode: str = INTERACTIVE_MODE,
    ) -> dict[str, Any]:
        """Process daily notes in interactive or scheduled mode."""
        try:
            return self._daily_workflow.run(day, mode=mode)
        except ProcessAlreadyRunningError as exc:
            logger.warning("%s", exc)
            return {
                "error": str(exc),
                "processed_entries": 0,
            }
        except TimeoutError:
            logger.error("%s processing timed out", self.ai_cli)
            return {
                "error": "Processing timed out",
                "processed_entries": 0,
            }
        except FileNotFoundError as exc:
            logger.error(
                "Required file or command not found during processing: %s",
                exc,
            )
            return {
                "error": str(exc) or "Required file or command not found",
                "processed_entries": 0,
            }
        except CliExecutionError as exc:
            logger.error("%s processing failed: %s", self.ai_cli, exc)
            return {
                "error": str(exc),
                "processed_entries": 0,
            }
        except Exception as exc:
            logger.exception("Unexpected error during processing")
            return {
                "error": str(exc),
                "processed_entries": 0,
            }

    def execute_prompt(self, user_prompt: str, user_id: int = 0) -> dict[str, Any]:
        """Execute arbitrary prompt with the configured CLI."""
        today = date.today()
        core_context = self._build_injected_context(consumer="do", target_day=today)
        todoist_ref = self._load_todoist_reference()
        vault_retrieval_skill = self._load_vault_retrieval_skill()
        session_context = self._get_session_context(user_id)

        prompt = f"""Ты - персональный ассистент d-brain.

CONTEXT:
- Working directory: vault root ({self.vault_path})

{core_context}

{session_context}=== TODOIST REFERENCE ===
{todoist_ref}
=== END REFERENCE ===

=== VAULT RETRIEVAL SKILL ===
{vault_retrieval_skill}
=== END VAULT RETRIEVAL SKILL ===

{self._todoist_cli_rules()}

{self._assistant_scope_rules()}

{self._language_instruction()}

If that block declares a history scope or explicit start point, follow it when
deciding how far back to analyze the topic.
If the request asks for status/history of a long-running topic, synthesize the
history of that topic/question, not only the latest snapshot.
If the topic spans weeks or months, include current state, recent changes, key
milestones, blockers/risks, and next steps with enough depth to be useful.
Do not collapse a long-running project into a very short answer if important
history exists.

USER REQUEST:
{user_prompt}

{
            self._telegram_markdown_output_rules(
                opening_line="Start with emoji and a short heading."
            )
        }

EXECUTION:
1. Analyze the request
2. Work only inside the vault workspace and Todoist
3. Refuse requests that need code, deploy, or system changes
4. Return a markdown status report with results"""

        try:
            prompt = self._inject_prompt_block(
                prompt,
                "USER REQUEST:",
                self._build_auto_recall_block(
                    user_prompt,
                    purpose="assistant_request",
                ),
            )
            output = self._run_assistant_prompt(prompt)
            normalized = self._normalize_owner_report_markdown(output)
            self._file_output_artifact_if_useful(
                request=user_prompt,
                output_markdown=normalized,
                artifact_type="assistant-request",
            )
            return {
                "report": normalized,
                "processed_entries": 1,
            }
        except TimeoutError:
            logger.error("%s execution timed out", self.ai_cli)
            return {"error": "Execution timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("%s CLI not found", self.ai_cli)
            return {"error": f"{self.ai_cli} CLI not installed", "processed_entries": 0}
        except CliExecutionError as exc:
            logger.error("%s execution failed: %s", self.ai_cli, exc)
            return {"error": str(exc), "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during execution")
            return {"error": str(exc), "processed_entries": 0}

    def _cycle_label(self, cycle_name: str) -> str:
        """Localized display label for one scheduled review cycle."""
        copy = self._owner_report_defaults()
        labels_ru = {
            "daily": "Ежедневная обработка",
            "weekly_digest": copy["weekly_digest_title"],
            "weekly_system_reflection": copy["system_reflection_title"],
            "weekly_goals_rollover": "Переключение недельного фокуса",
            "monthly": copy["monthly_review_title"],
            "yearly": copy["yearly_review_title"],
            "maintenance.compiled-nightly": "Поддержка compiled-слоя",
            "maintenance.vault-health": "Здоровье vault",
            "maintenance.compiled-fact-check": "Проверка фактов compiled",
            "maintenance.compiled-digest": "Дайджест обогащения compiled",
        }
        labels_en = {
            "daily": "Daily Processing",
            "weekly_digest": copy["weekly_digest_title"],
            "weekly_system_reflection": copy["system_reflection_title"],
            "weekly_goals_rollover": "Weekly Goals Rollover",
            "monthly": copy["monthly_review_title"],
            "yearly": copy["yearly_review_title"],
            "maintenance.compiled-nightly": "Compiled Maintenance",
            "maintenance.vault-health": "Vault Health",
            "maintenance.compiled-fact-check": "Compiled Fact Check",
            "maintenance.compiled-digest": "Compiled Digest",
        }
        labels = labels_ru if self.content_language == "ru" else labels_en
        return labels.get(cycle_name, cycle_name)

    def _build_control_plane_cycle_record(
        self,
        workflow_name: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one periodic cycle record from the canonical control-plane registry."""
        workflow = get_workflow(workflow_name)
        return {
            "name": workflow.name,
            "label": self._cycle_label(workflow.name),
            "result": result,
        }

    def _run_control_plane_maintenance_workflow(
        self,
        workflow_name: str,
    ) -> dict[str, Any]:
        """Execute one registry-declared maintenance workflow against this processor."""
        workflow = get_workflow(workflow_name)
        method_name = workflow.entrypoint.rsplit(".", 1)[-1]
        runner = getattr(self, method_name, None)
        if callable(runner):
            result = runner()
        else:
            runner = resolve_entrypoint(workflow.entrypoint)
            if not callable(runner):
                raise TypeError(
                    f"Workflow entrypoint is not callable: {workflow.entrypoint}"
                )
            result = runner(self)
        if not isinstance(result, dict):
            raise TypeError(
                f"Workflow entrypoint returned non-dict result: {workflow.entrypoint}"
            )
        return result

    @staticmethod
    def _combine_reports(*reports: str) -> str:
        """Join multiple owner-facing markdown blocks into one Telegram report."""
        parts = [
            str(report or "").strip() for report in reports if str(report or "").strip()
        ]
        return "\n\n".join(parts)

    @staticmethod
    def _strip_vault_health_report_section(report: str) -> str:
        """Remove pre-maintenance health data from one generated daily report."""
        cleaned = VAULT_HEALTH_REPORT_SECTION_RE.sub("", str(report or "")).strip()
        return re.sub(r"\n{3,}", "\n\n", cleaned)

    @staticmethod
    def _is_month_end(day: date) -> bool:
        """Whether the supplied date closes the current month."""
        return (day + timedelta(days=1)).month != day.month

    @staticmethod
    def _is_year_end(day: date) -> bool:
        """Whether the supplied date closes the current year."""
        return day.month == 12 and day.day == 31

    def _scheduled_cycle_names_for_day(self, day: date) -> list[str]:
        """Periodic review layers that should run after the daily cycle."""
        cycles: list[str] = []
        if day.weekday() == 4:
            cycles.append("weekly_digest")
        if day.weekday() == 6:
            cycles.append("weekly_system_reflection")
        if self._is_month_end(day):
            cycles.append("monthly")
        if self._is_year_end(day):
            cycles.append("yearly")
        return cycles

    def _audit_artifact_paths(
        self,
        *,
        cycle_name: str,
        day: date,
        result: dict[str, Any],
    ) -> list[str]:
        """Relevant files the audit pass may inspect for one finished cycle."""
        paths = [f"daily/{day.isoformat()}.md", ".session/handoff.md"]
        if cycle_name == "daily":
            paths.extend(
                [".session/capture.json", ".session/execute.json", "MEMORY.md"]
            )
        if cycle_name == "weekly_digest":
            paths.extend(
                [
                    "goals/3-weekly.md",
                    "goals/2-monthly.md",
                    f"goals/{self._get_yearly_goals_name()}",
                ]
            )
        if cycle_name == "weekly_system_reflection":
            paths.append(".graph/health-history.json")
        if cycle_name == "weekly_goals_rollover":
            paths.append("goals/3-weekly.md")
        if cycle_name == "monthly":
            paths.extend(
                [
                    "goals/2-monthly.md",
                    f"goals/{self._get_yearly_goals_name()}",
                    "goals/0-vision-3y.md",
                ]
            )
        if cycle_name == "yearly":
            paths.extend(
                [
                    f"goals/{self._get_yearly_goals_name()}",
                    "goals/0-vision-3y.md",
                ]
            )
        for key in ("summary_path", "note_path"):
            value = " ".join(str(result.get(key) or "").split())
            if value:
                paths.append(value)
        deduped: list[str] = []
        for path in paths:
            if path not in deduped:
                deduped.append(path)
        return deduped

    def _build_process_audit_prompt(
        self,
        *,
        cycle_name: str,
        day: date,
        result: dict[str, Any],
    ) -> str:
        """Prompt for one post-run audit over a completed workflow result."""
        artifact_lines = "\n".join(
            f"- {path}"
            for path in self._audit_artifact_paths(
                cycle_name=cycle_name,
                day=day,
                result=result,
            )
        )
        return (
            f"Today is {day}. Audit one completed d-brain workflow result.\n\n"
            f"{self._language_instruction()}\n"
            "AUDIT SCOPE:\n"
            f"- Workflow: {self._cycle_label(cycle_name)}\n"
            f"- Vault root: {self.vault_path}\n"
            "- The audit runs after the workflow itself. qmd refresh for later "
            "periodic writes may still happen after this audit; do not flag "
            "deferred indexing by itself as a problem.\n\n"
            "RESULT JSON:\n"
            f"{json.dumps(result, ensure_ascii=False, indent=2)}\n\n"
            "FILES YOU MAY INSPECT:\n"
            f"{artifact_lines}\n\n"
            "AUDIT RULES:\n"
            "- Look for concrete process problems: failed phases, malformed notes, "
            "missing expected artifacts, contradictory status reporting, wrong paths, "
            "broken owner-facing output, or quality regressions that deserve "
            "follow-up.\n"
            "- Ignore normal lack of user activity and acceptable no-op outcomes.\n"
            "- Do not invent issues without evidence from the result JSON or "
            "inspected files.\n"
            "- `action` must be a short concrete engineering follow-up task title "
            "suitable for Todoist.\n"
            "- If no actionable problems are found, return an empty `issues` list.\n\n"
            "Return exactly one JSON object:\n"
            "{\n"
            '  "summary": "Short audit summary",\n'
            '  "issues": [\n'
            "    {\n"
            '      "title": "What is wrong",\n'
            '      "severity": "high|medium|low",\n'
            '      "evidence": "Concrete evidence",\n'
            '      "action": "Concrete follow-up task",\n'
            '      "project_hint": "Inbox"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )

    @staticmethod
    def _normalize_audit_issues(payload: dict[str, Any]) -> list[dict[str, str]]:
        """Normalize one audit JSON payload into compact structured findings."""
        raw_items = payload.get("issues")
        if not isinstance(raw_items, list):
            return []
        issues: list[dict[str, str]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())
            action = " ".join(str(item.get("action") or "").split())
            if not title or not action:
                continue
            severity = str(item.get("severity") or "medium").strip().lower()
            if severity not in {"high", "medium", "low"}:
                severity = "medium"
            issues.append(
                {
                    "title": title,
                    "severity": severity,
                    "evidence": " ".join(str(item.get("evidence") or "").split()),
                    "action": action,
                    "project_hint": " ".join(
                        str(item.get("project_hint") or "").split()
                    ),
                }
            )
        return issues

    def audit_cycle_result(
        self,
        *,
        cycle_name: str,
        day: date,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one separate CLI audit for a completed workflow result."""
        label = self._cycle_label(cycle_name)
        fallback_payload: dict[str, Any] | None = None
        try:
            payload = self._run_json_phase(
                self._build_process_audit_prompt(
                    cycle_name=cycle_name,
                    day=day,
                    result=result,
                ),
                phase_name=f"audit-{cycle_name}",
            )
        except Exception as exc:
            logger.warning("Post-run audit failed for %s: %s", cycle_name, exc)
            if "error" not in result:
                return {
                    "cycle_name": cycle_name,
                    "label": label,
                    "summary": "",
                    "issues": [],
                    "task_candidates": [],
                    "tasks_created": [],
                    "error": str(exc),
                }
            fallback_payload = {
                "summary": str(result.get("error") or ""),
                "issues": [
                    {
                        "title": f"{label}: workflow failed",
                        "severity": "high",
                        "evidence": str(result.get("error") or ""),
                        "action": (
                            f"Разобрать сбой workflow: {label}"
                            if self.content_language == "ru"
                            else f"Investigate workflow failure: {label}"
                        ),
                        "project_hint": "Inbox",
                    }
                ],
            }
            payload = fallback_payload

        issues = self._normalize_audit_issues(payload)
        new_issues = self._filter_new_audit_issues(cycle_name, issues)
        task_candidates = [
            {
                "content": issue["action"],
                "priority": {"high": 4, "medium": 3, "low": 2}[issue["severity"]],
                "due_hint": "today" if issue["severity"] == "high" else "",
                "project_hint": issue["project_hint"] or "Inbox",
            }
            for issue in new_issues
        ]
        created_tasks = self._create_todoist_tasks(
            task_candidates,
            source_context=(
                f"{label}\n{json.dumps(result, ensure_ascii=False, indent=2)}"
            ),
        )
        return {
            "cycle_name": cycle_name,
            "label": label,
            "summary": " ".join(str(payload.get("summary") or "").split()),
            "issues": issues,
            "task_candidates": [task["content"] for task in task_candidates],
            "tasks_created": created_tasks,
            **({"fallback": True} if fallback_payload is not None else {}),
        }

    def _safe_audit_cycle_result(
        self,
        *,
        cycle_name: str,
        day: date,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """``audit_cycle_result`` for the scheduled stack, degraded on error.

        The audit guards only its own model phase; everything after it --
        Todoist project routing (another CLI call) and task creation -- can
        still raise. ``_run_scheduled_cycle_locked`` calls the audit outside
        the try/except that wraps each workflow, so such a raise used to
        escape the whole nightly cycle: no owner report for work that had
        already succeeded, and no maintenance workflow afterwards. An audit
        is a helper, never the deliverable -- record the failure the same way
        a failed audit phase does and let the cycle continue.
        """
        try:
            return self.audit_cycle_result(
                cycle_name=cycle_name,
                day=day,
                result=result,
            )
        except Exception as exc:
            logger.exception("Post-run audit crashed for %s", cycle_name)
            return {
                "cycle_name": cycle_name,
                "label": self._cycle_label(cycle_name),
                "summary": "",
                "issues": [],
                "task_candidates": [],
                "tasks_created": [],
                "error": str(exc),
            }

    def generate_weekly_digest(
        self,
        *,
        day: date | None = None,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Generate the owner-facing weekly digest."""
        today = day or date.today()
        copy = self._owner_report_defaults()
        yearly_goals_name = self._get_yearly_goals_name()
        rollover_error = self._weekly_rollover_guard(today)
        if rollover_error is not None:
            return {
                "error": rollover_error,
                "processed_entries": 0,
                "rollover_required": True,
            }
        self._touch_memory_paths(
            "MEMORY.md",
            "goals/3-weekly.md",
            "goals/2-monthly.md",
            f"goals/{yearly_goals_name}",
        )

        prompt = self._build_weekly_digest_prompt(
            today=today,
            yearly_goals_name=yearly_goals_name,
        )

        try:
            summary_markdown = self._normalize_owner_report_markdown(
                self._run_prompt(prompt)
            )
            summary_markdown = self._strip_periodic_persistence_claims(summary_markdown)
            report_markdown = summary_markdown or (
                f"📅 **{copy['weekly_digest_title']}**"
            )

            summary_path: Path | None = None
            try:
                summary_path = self._save_weekly_summary(summary_markdown, today)
                self._update_weekly_moc(summary_path)
                self._log_periodic_summary(
                    timestamp=datetime.now().astimezone(),
                    label=copy["weekly_digest_title"],
                    summary_path=summary_path,
                    refresh_qmd=False,
                )
                if refresh_qmd:
                    self._refresh_qmd_index()
            except Exception as exc:
                logger.warning("Failed to save weekly summary: %s", exc)

            result: dict[str, Any] = {
                "report": report_markdown,
                "processed_entries": 1,
                "searchable_write": summary_path is not None,
            }
            if summary_path is not None:
                result["summary_path"] = summary_path.relative_to(
                    self.vault_path
                ).as_posix()
            return result
        except TimeoutError:
            logger.error("%s weekly digest timed out", self.ai_cli)
            return {"error": "Weekly digest timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("%s CLI not found", self.ai_cli)
            return {"error": f"{self.ai_cli} CLI not installed", "processed_entries": 0}
        except CliExecutionError as exc:
            logger.error("%s weekly digest failed: %s", self.ai_cli, exc)
            return {"error": str(exc), "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during weekly digest")
            return {"error": str(exc), "processed_entries": 0}

    def generate_weekly(self) -> dict[str, Any]:
        """Backward-compatible alias for the weekly digest."""
        return self.generate_weekly_digest()

    def generate_weekly_system_reflection(
        self,
        *,
        day: date | None = None,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Generate one weekly system reflection from handoff observations."""
        today = day or date.today()
        copy = self._owner_report_defaults()
        observations, observation_revision = self._handoff_observation_snapshot()
        if not observations:
            return {
                "report": (
                    f"🛠 **{copy['system_reflection_title']}**\n\n"
                    f"{copy['no_system_signals']}"
                ),
                "processed_entries": 0,
                "skipped": True,
                "created_reflection": False,
                "carry_forward_observations": [],
                "processed_observations": 0,
                "searchable_write": False,
            }

        iso_year, iso_week, _ = today.isocalendar()
        week_label = f"{iso_year}-W{iso_week:02d}"
        week_start = today - timedelta(days=today.weekday())
        graph_history = self._graph_health_window(
            self._load_graph_health_history(),
            start=week_start,
            end=today,
        )
        rule_text = self._load_weekly_reflection_rule()
        prompt = f"""Today is {today}. Generate the weekly system reflection.

CONTEXT:
- Working directory: vault root ({self.vault_path})
- Current ISO week: {week_label} ({week_start} .. {today})

{self._language_instruction()}

WEEKLY SYSTEM REFLECTION RULE:
{rule_text}

CURRENT OBSERVATIONS (exact unresolved bullets):
{json.dumps(observations, ensure_ascii=False, indent=2)}

GRAPH HEALTH HISTORY (this ISO week only):
{json.dumps(graph_history, ensure_ascii=False, indent=2)}

WEEKLY SYSTEM REFLECTION RULES:
- Work only from the supplied observations and graph health history.
- Focus on recurring friction, repeated patterns, and smallest useful fixes.
- If there is not enough signal for a real reflection note, set
  `create_reflection` to false.
- `carry_forward_observations` must contain only the exact bullets that should
  remain in handoff after this weekly reflection.
- If a created reflection note already captures the pattern, prefer clearing
  the original observation bullets instead of carrying them all forward again.
- Only keep carry-forward bullets that remain genuinely unresolved for the next
  week. Do not keep all original bullets by default.
- If you create a note, `reflection_markdown` must be markdown without
  frontmatter and without a top-level title line.
- `report_highlights` must be 1-3 short owner-facing bullets with the key
  meaning of this reflection.
- `watch_next_week` must be 0-2 short bullets for risks, follow-up, or
  monitoring items.

Return exactly one JSON object:
{{
  "create_reflection": true,
  "title": "Short title",
  "report_highlights": ["Main pattern resolved ..."],
  "watch_next_week": ["Watch one broken link"],
  "reflection_markdown": "## Friction Patterns\\n...",
  "carry_forward_observations": ["- [pattern] ..."]
}}
"""

        try:
            payload = self._run_json_phase(
                prompt,
                phase_name="weekly-system-reflection",
            )
        except Exception as exc:
            logger.exception("Weekly system reflection failed")
            return {"error": str(exc), "processed_entries": 0}

        create_reflection = bool(payload.get("create_reflection"))
        title = " ".join(str(payload.get("title") or "").split())
        if not title:
            title = f"{copy['reflection_title_fallback']} {week_label}"
        report_highlights = self._string_list(payload.get("report_highlights"))
        watch_next_week = self._string_list(payload.get("watch_next_week"))
        report_markdown = self._normalize_owner_report_markdown(
            str(payload.get("report_markdown") or payload.get("report_html") or "")
        )
        markdown_body = str(payload.get("reflection_markdown") or "").strip()
        carry_forward_raw = payload.get("carry_forward_observations", [])
        if not isinstance(carry_forward_raw, list):
            carry_forward_raw = []
        carry_forward = [
            str(item).strip() for item in carry_forward_raw if str(item).strip()
        ]
        carry_forward = self._coerce_bullet_lines("\n".join(carry_forward))

        if not create_reflection and not carry_forward:
            carry_forward = observations

        note_path: Path | None = None
        if create_reflection and markdown_body:
            note_path = self._save_weekly_system_reflection(
                week_date=today,
                title=title,
                markdown_body=markdown_body,
            )
        if note_path is not None and carry_forward == observations:
            carry_forward = []

        carry_forward, processed_observations = (
            self._merge_handoff_observation_snapshot(
                observations,
                carry_forward,
                observation_revision,
            )
        )
        self._log_weekly_system_reflection(
            timestamp=datetime.now().astimezone(),
            note_path=note_path,
            title=title,
            processed_observations=processed_observations,
            carry_forward_observations=len(carry_forward),
            refresh_qmd=False,
        )
        if refresh_qmd:
            self._refresh_qmd_index()

        owner_report = ""
        if report_highlights or watch_next_week:
            owner_report = self._build_weekly_system_report_markdown(
                title=title,
                highlights=report_highlights,
                watch_items=watch_next_week,
                graph_history=graph_history,
                carry_forward_count=len(carry_forward),
            )
        if not owner_report and report_markdown:
            owner_report = report_markdown
        if not owner_report:
            if note_path is None:
                message = (
                    copy["retained_observations"]
                    if carry_forward
                    else copy["no_system_signals"]
                )
                owner_report = f"🛠 **{copy['system_reflection_title']}**\n\n{message}"
            else:
                note_rel_path = note_path.relative_to(self.vault_path).as_posix()
                owner_report = (
                    f"🛠 **{copy['system_reflection_title']}**\n\n"
                    f"{copy['reflection_created']} `{note_rel_path}`"
                )

        result: dict[str, Any] = {
            "report": owner_report,
            "processed_entries": processed_observations,
            "skipped": note_path is None,
            "created_reflection": note_path is not None,
            "carry_forward_observations": carry_forward,
            "processed_observations": processed_observations,
            "searchable_write": bool(observations),
        }
        if note_path is not None:
            result["note_path"] = note_path.relative_to(self.vault_path).as_posix()
            result["title"] = title
        return result

    def run_monthly_cycle(
        self,
        *,
        day: date | None = None,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Generate the end-of-month owner review."""
        today = day or date.today()
        copy = self._owner_report_defaults()
        yearly_goals_name = self._get_yearly_goals_name()
        month_label = today.strftime("%Y-%m")
        self._touch_memory_paths(
            "MEMORY.md",
            "goals/0-vision-3y.md",
            f"goals/{yearly_goals_name}",
            "goals/2-monthly.md",
            "goals/3-weekly.md",
        )

        prompt = f"""Today is {today}. Generate the monthly owner review.

CONTEXT:
- Working directory: project root ({self.vault_path.parent})
- Vault root: {self.vault_path}
- Current month: {month_label}

{self._language_instruction()}

{self._todoist_cli_rules(needs_completed_tasks=True)}

MONTHLY REVIEW RULES:
- Read `MEMORY.md`, `goals/0-vision-3y.md`, `goals/{yearly_goals_name}`,
  `goals/2-monthly.md`, and `goals/3-weekly.md`.
- Read the daily files for the current calendar month.
- Read weekly summaries for the same month when they exist in `summaries/`.
- Use completed Todoist tasks as supporting evidence.
- Prefer synthesis over raw enumeration.
- Be explicit about what moved, what drifted, what should carry into next month,
  and what to cut.
- Do not create or edit files yourself. The Python runtime saves the canonical
  monthly summary after your response.
- Do not rewrite goal files automatically.
- Return ONLY markdown, not HTML.
- Do not mention save paths or claim that files were updated/persisted.
- Start with one short markdown heading for the review.
- Keep the report owner-facing and concrete.
"""

        try:
            summary_markdown = self._normalize_owner_report_markdown(
                self._run_prompt(prompt)
            )
            summary_markdown = self._strip_periodic_persistence_claims(summary_markdown)
            report_markdown = summary_markdown or (
                f"🗓 **{copy['monthly_review_title']}**"
            )

            summary_path: Path | None = None
            try:
                summary_path = self._save_monthly_summary(summary_markdown, today)
                self._log_periodic_summary(
                    timestamp=datetime.now().astimezone(),
                    label=copy["monthly_review_title"],
                    summary_path=summary_path,
                    refresh_qmd=False,
                )
                if refresh_qmd:
                    self._refresh_qmd_index()
            except Exception as exc:
                logger.warning("Failed to save monthly summary: %s", exc)

            result: dict[str, Any] = {
                "report": report_markdown,
                "processed_entries": 1,
                "searchable_write": summary_path is not None,
            }
            if summary_path is not None:
                result["summary_path"] = summary_path.relative_to(
                    self.vault_path
                ).as_posix()
            return result
        except TimeoutError:
            logger.error("%s monthly review timed out", self.ai_cli)
            return {"error": "Monthly review timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("%s CLI not found", self.ai_cli)
            return {"error": f"{self.ai_cli} CLI not installed", "processed_entries": 0}
        except CliExecutionError as exc:
            logger.error("%s monthly review failed: %s", self.ai_cli, exc)
            return {"error": str(exc), "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during monthly review")
            return {"error": str(exc), "processed_entries": 0}

    def run_yearly_cycle(
        self,
        *,
        day: date | None = None,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Generate the end-of-year owner review."""
        today = day or date.today()
        copy = self._owner_report_defaults()
        yearly_goals_name = self._get_yearly_goals_name()
        rollover_due = self._three_year_rollover_due(today)
        rollover_rule = (
            (
                f"- The active 3-year horizon ends with {today.year}. "
                "Call this out explicitly and name the next horizon that must be "
                f"drafted ({today.year + 1}-{today.year + 3})."
            )
            if rollover_due
            else (
                "- Mention the 3-year horizon only when it materially affects "
                "the annual review."
            )
        )
        self._touch_memory_paths(
            "MEMORY.md",
            "goals/0-vision-3y.md",
            f"goals/{yearly_goals_name}",
            "goals/2-monthly.md",
        )

        prompt = f"""Today is {today}. Generate the yearly owner review.

CONTEXT:
- Working directory: project root ({self.vault_path.parent})
- Vault root: {self.vault_path}
- Current year: {today.year}

{self._language_instruction()}

{self._todoist_cli_rules(needs_completed_tasks=True)}

YEARLY REVIEW RULES:
- Read `MEMORY.md`, `goals/0-vision-3y.md`, `goals/{yearly_goals_name}`,
  and `goals/2-monthly.md`.
- Read monthly summaries for the current year from `summaries/`.
- If monthly summaries are sparse, use weekly summaries or daily files only to fill
  critical gaps instead of enumerating everything.
- Use completed Todoist tasks as supporting evidence.
- Prefer synthesis over enumeration.
- Be explicit about what advanced the year, what stalled, what should carry into
  next year, and what strategic assumptions changed.
{rollover_rule}
- Do not create or edit files yourself. The Python runtime saves the canonical
  yearly summary after your response.
- Do not rewrite goal files automatically.
- Return ONLY markdown, not HTML.
- Do not mention save paths or claim that files were updated/persisted.
- Start with one short markdown heading for the review.
- Keep the report owner-facing and concrete.
"""

        try:
            summary_markdown = self._normalize_owner_report_markdown(
                self._run_prompt(prompt)
            )
            summary_markdown = self._strip_periodic_persistence_claims(summary_markdown)
            report_markdown = summary_markdown or (
                f"🧭 **{copy['yearly_review_title']}**"
            )

            summary_path: Path | None = None
            try:
                summary_path = self._save_yearly_summary(summary_markdown, today)
                extra_lines = []
                if rollover_due:
                    extra_lines.append(
                        (
                            "3-year horizon rollover due: "
                            f"{today.year + 1}-{today.year + 3}"
                        )
                        if self.content_language == "en"
                        else (
                            "Нужен rollover 3-year vision: "
                            f"{today.year + 1}-{today.year + 3}"
                        )
                    )
                self._log_periodic_summary(
                    timestamp=datetime.now().astimezone(),
                    label=copy["yearly_review_title"],
                    summary_path=summary_path,
                    refresh_qmd=False,
                    extra_lines=extra_lines,
                )
                if refresh_qmd:
                    self._refresh_qmd_index()
            except Exception as exc:
                logger.warning("Failed to save yearly summary: %s", exc)

            result: dict[str, Any] = {
                "report": report_markdown,
                "processed_entries": 1,
                "searchable_write": summary_path is not None,
                "vision_rollover_due": rollover_due,
            }
            if summary_path is not None:
                result["summary_path"] = summary_path.relative_to(
                    self.vault_path
                ).as_posix()
            return result
        except TimeoutError:
            logger.error("%s yearly review timed out", self.ai_cli)
            return {"error": "Yearly review timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("%s CLI not found", self.ai_cli)
            return {"error": f"{self.ai_cli} CLI not installed", "processed_entries": 0}
        except CliExecutionError as exc:
            logger.error("%s yearly review failed: %s", self.ai_cli, exc)
            return {"error": str(exc), "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during yearly review")
            return {"error": str(exc), "processed_entries": 0}

    def run_weekly_cycle(self, *, day: date | None = None) -> dict[str, Any]:
        """Run the weekly digest and system reflection back-to-back."""
        if day is None:
            digest_result = self.generate_weekly_digest(refresh_qmd=False)
        else:
            digest_result = self.generate_weekly_digest(day=day, refresh_qmd=False)
        if "error" in digest_result:
            return digest_result

        if day is None:
            system_result = self.generate_weekly_system_reflection(refresh_qmd=False)
        else:
            system_result = self.generate_weekly_system_reflection(
                day=day,
                refresh_qmd=False,
            )
        digest_report = str(digest_result.get("report", "")).strip()
        system_report = str(system_result.get("report", "")).strip()

        if "error" in system_result:
            system_report = "\n".join(
                [
                    "🛠 **Системная рефлексия**",
                    "",
                    f"`{str(system_result['error'])}`",
                ]
            )

        combined_report = digest_report
        if system_report:
            combined_report = (
                f"{combined_report.rstrip()}\n\n{system_report.lstrip()}"
                if combined_report
                else system_report
            )

        self._refresh_qmd_index()
        result: dict[str, Any] = {
            "report": combined_report,
            "processed_entries": int(digest_result.get("processed_entries") or 0)
            + int(system_result.get("processed_entries") or 0),
            "digest": digest_result,
            "system_reflection": system_result,
        }
        if "error" in system_result:
            result["system_reflection_error"] = system_result["error"]
        return result

    def run_scheduled_cycle(self, day: date | None = None) -> dict[str, Any]:
        """Run the full scheduled stack: daily plus any due periodic reviews."""
        try:
            with self._scheduled_process_lock():
                self._scheduled_cycle_lock_held = True
                try:
                    return self._run_scheduled_cycle_locked(day)
                finally:
                    self._scheduled_cycle_lock_held = False
        except ProcessAlreadyRunningError as exc:
            logger.warning("%s", exc)
            return {"error": str(exc), "processed_entries": 0}

    def _run_scheduled_cycle_locked(self, day: date | None = None) -> dict[str, Any]:
        """Run scheduled processing while the full-cycle lock is held."""
        today = day or date.today()
        daily_result = self.process_daily(today, mode=SCHEDULED_MODE)
        reports = [
            self._strip_vault_health_report_section(
                str(daily_result.get("report", "")).strip()
            )
        ]
        audits: list[dict[str, Any]] = [
            self._safe_audit_cycle_result(
                cycle_name="daily",
                day=today,
                result=daily_result,
            )
        ]
        periodic_cycles: list[dict[str, Any]] = []
        run_weekly_goals_rollover = (
            today.weekday() == 6 or self._weekly_goals_rollover_due(today)
        )

        if "error" not in daily_result:
            for cycle_name in self._scheduled_cycle_names_for_day(today):
                try:
                    if cycle_name == "weekly_digest":
                        cycle_result = self.generate_weekly_digest(
                            day=today,
                            refresh_qmd=False,
                        )
                    elif cycle_name == "weekly_system_reflection":
                        cycle_result = self.generate_weekly_system_reflection(
                            day=today,
                            refresh_qmd=False,
                        )
                    elif cycle_name == "monthly":
                        cycle_result = self.run_monthly_cycle(
                            day=today,
                            refresh_qmd=False,
                        )
                    else:
                        cycle_result = self.run_yearly_cycle(
                            day=today,
                            refresh_qmd=False,
                        )
                except Exception as exc:
                    logger.exception("Scheduled periodic cycle failed: %s", cycle_name)
                    cycle_result = {"error": str(exc), "processed_entries": 0}

                label = self._cycle_label(cycle_name)
                periodic_cycles.append(
                    {
                        "name": cycle_name,
                        "label": label,
                        "result": cycle_result,
                    }
                )
                audits.append(
                    self._safe_audit_cycle_result(
                        cycle_name=cycle_name,
                        day=today,
                        result=cycle_result,
                    )
                )

                cycle_report = str(cycle_result.get("report", "")).strip()
                if not cycle_report and "error" in cycle_result:
                    cycle_report = "\n".join(
                        [
                            f"❌ **{label}**",
                            "",
                            f"`{str(cycle_result['error'])}`",
                        ]
                    )
                reports.append(cycle_report)

        if run_weekly_goals_rollover:
            rollover_name = "weekly_goals_rollover"
            rollover_label = self._cycle_label(rollover_name)
            try:
                rollover_result = self.rollover_weekly_goals(
                    day=today,
                    refresh_qmd=False,
                )
            except Exception as exc:
                logger.exception("Scheduled weekly goals rollover failed")
                rollover_result = {"error": str(exc), "processed_entries": 0}

            periodic_cycles.append(
                {
                    "name": rollover_name,
                    "label": rollover_label,
                    "result": rollover_result,
                }
            )
            audits.append(
                self._safe_audit_cycle_result(
                    cycle_name=rollover_name,
                    day=today,
                    result=rollover_result,
                )
            )

            rollover_report = str(rollover_result.get("report", "")).strip()
            if not rollover_report and "error" in rollover_result:
                rollover_report = "\n".join(
                    [
                        f"❌ **{rollover_label}**",
                        "",
                        f"`{str(rollover_result['error'])}`",
                    ]
                )
            reports.append(rollover_report)

        # Maintenance workflows run regardless of daily processing outcome
        # so that compiled briefings, vault health, etc. stay fresh even when
        # the daily pipeline hits a transient LLM error.
        for workflow in iter_workflows(
            kind="maintenance",
            trigger="scheduled-post",
        ):
            try:
                cycle_result = self._run_control_plane_maintenance_workflow(
                    workflow.name
                )
            except Exception as exc:
                logger.exception(
                    "Scheduled maintenance workflow failed: %s", workflow.name
                )
                cycle_result = {"error": str(exc), "processed_entries": 0}
            periodic_cycles.append(
                self._build_control_plane_cycle_record(
                    workflow.name,
                    cycle_result,
                )
            )
            audits.append(
                self._safe_audit_cycle_result(
                    cycle_name=workflow.name,
                    day=today,
                    result=cycle_result,
                )
            )
            cycle_report = str(cycle_result.get("report", "")).strip()
            if not cycle_report and "error" in cycle_result:
                # Same fallback as the two loops above (code review): a
                # workflow that raised has no ``report`` of its own, so
                # appending it verbatim contributed an empty string and the
                # owner's nightly message came back looking like a clean
                # run. The traceback goes to the service log, which nobody
                # reads at 21:00 -- and these are exactly the workflows
                # whose silence is indistinguishable from "nothing to say".
                cycle_report = "\n".join(
                    [
                        f"❌ **{self._cycle_label(workflow.name)}**",
                        "",
                        f"`{str(cycle_result['error'])}`",
                    ]
                )
            reports.append(cycle_report)

        self._refresh_qmd_index()

        combined_report = self._combine_reports(*reports)
        task_candidates = [
            task
            for audit in audits
            for task in audit.get("task_candidates", [])
            if str(task).strip()
        ]
        tasks_created = [
            task
            for audit in audits
            for task in audit.get("tasks_created", [])
            if isinstance(task, dict) and str(task.get("content") or "").strip()
        ]

        result: dict[str, Any] = {
            **daily_result,
            "report": combined_report or str(daily_result.get("report", "")).strip(),
            "daily": daily_result,
            "periodic_cycles": periodic_cycles,
            "audits": audits,
            "audit_task_candidates": task_candidates,
            "audit_tasks_created": tasks_created,
            "processed_entries": int(daily_result.get("processed_entries") or 0)
            + sum(
                int(cycle["result"].get("processed_entries") or 0)
                for cycle in periodic_cycles
            ),
        }
        return result


# Backward-compatible alias for existing imports.
ClaudeProcessor = CliProcessor
