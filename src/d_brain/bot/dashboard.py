"""Inline dashboard rendering and state for the Telegram bot."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any, Literal, TypedDict

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
)

from d_brain.bot.replies import send_text
from d_brain.clock import owner_today, owner_tz, to_owner
from d_brain.services.atomic_io import atomic_write_file_text
from d_brain.services.calendar_ics import CalendarEvent, fetch_day, format_brief_lines
from d_brain.services.compiled_briefs import BRIEF_TYPES
from d_brain.services.compiled_enrich_report import WeeklyReview, collect_weekly_review
from d_brain.services.conversation_learning import LearningLesson, list_lessons
from d_brain.services.decisions_queue import (
    CONFLICT_KIND,
    DUPLICATE_CANDIDATE_KIND,
    FACT_CHECK_REJECTED_KIND,
    VERIFY_REJECTED_KIND,
    QueueItem,
    format_queue_item_line,
    list_queue_items,
    partition_queue_items,
    queue_item_fingerprint,
    queue_kind_label,
)
from d_brain.services.file_browser import (
    TELEGRAM_MAX_DOCUMENT_BYTES,
    BrowserEntry,
    BrowserRoot,
    FileBrowserService,
)
from d_brain.services.frontmatter import parse_frontmatter_bytes
from d_brain.services.processor import (
    JOB_HEALTH_DISABLE_THRESHOLD,
    cycle_step_label,
    load_job_health,
    save_job_health,
)
from d_brain.services.reminders import list_pending as list_pending_reminders
from d_brain.services.session import SessionStore
from d_brain.services.telegram_markup import (
    TELEGRAM_TEXT_LIMIT,
    markdown_to_html,
    markdown_to_markdown_v2,
    normalize_markdown_input,
    truncate_plain_text_for_edit,
)
from d_brain.services.todoist_tasks import TodoistTask
from d_brain.services.vault_lock import vault_write_lock

DashboardScreen = Literal[
    "tasks",
    "home",
    "stats",
    "file_roots",
    "files",
    "queue",
    "queue_item",
    "brief_type",
    "weekly",
    "jobhealth",
    "summaries",
    "processing",
    "more",
    "help",
    "do_input",
    "brief_input",
    "learnings",
    "weekly_goal",
    "goal_input",
    "meeting_prep",
]
logger = logging.getLogger(__name__)
DASHBOARD_SESSIONS_RELATIVE_PATH = Path(".session") / "dashboard-sessions.json"
CAPTURE_TYPES: tuple[str, ...] = ("voice", "text", "photo", "document", "forward")
TYPE_META: tuple[tuple[str, str, str], ...] = (
    ("voice", "🎤", "Голосовые"),
    ("text", "💬", "Текстовые"),
    ("photo", "📷", "Фото"),
    ("document", "📄", "Документы"),
    ("forward", "↩️", "Пересланные"),
)
FILES_PAGE_SIZE = 8
QUEUE_PAGE_SIZE = 8
LEARNINGS_PAGE_SIZE = 6
WEEKLY_QUEUE_PREVIEW_SIZE = 5
MAX_HOME_REMINDERS = 5
MAX_MEETING_PREP_EVENTS = 8
MAX_MEETING_PREP_NOTES = 3

_BRIEF_TYPE_BUTTON_LABELS = {
    "decision": "Решение",
    "topic": "Тема",
    "project": "Проект",
}
_VALID_SCREENS: frozenset[str] = frozenset(
    {
        "tasks",
        "home",
        "stats",
        "file_roots",
        "files",
        "queue",
        "queue_item",
        "brief_type",
        "weekly",
        "jobhealth",
        "summaries",
        "processing",
        "more",
        "help",
        "do_input",
        "brief_input",
        "learnings",
        "weekly_goal",
        "goal_input",
        "meeting_prep",
    }
)
_ONE_BIG_THING_HEADING_RE = re.compile(
    r"(?im)^##\s*(?:one\s+big\s+thing|главное)\s*$"
)
_NEXT_HEADING_RE = re.compile(r"(?m)^#{1,6}\s|^---\s*$")
_PLACEHOLDER_STRIP_RE = re.compile(r"[-.…>*_()\[\]\s]")
_MEETING_TOKEN_RE = re.compile(r"[\w-]{3,}", re.UNICODE)
_MEETING_STOPWORDS = frozenset(
    {
        "встреча",
        "встречи",
        "звонок",
        "созвон",
        "синк",
        "дейли",
        "стендап",
        "митинг",
        "обсуждение",
        "планёрка",
        "планерка",
        "sync",
        "call",
        "meeting",
        "daily",
        "standup",
        "weekly",
        "review",
        "check",
        "chat",
        "with",
        "для",
        "про",
        "или",
        "при",
        "без",
        "под",
        "над",
    }
)


class _UsageStats(TypedDict):
    today_counts: dict[str, int]
    today_total: int
    week_counts: dict[str, int]
    week_total: int


@dataclass(slots=True)
class DashboardSession:
    """UI state bound to one chat (with minimal disk persistence)."""

    chat_id: int
    dashboard_message_id: int | None = None
    current_screen: DashboardScreen = "home"
    file_root_id: str | None = None
    file_current_dir: str = ""
    file_page: int = 0
    file_entries: list[BrowserEntry] = field(default_factory=list)
    queue_page: int = 0
    queue_items: list[QueueItem] = field(default_factory=list)
    weekly_review: WeeklyReview | None = None
    learning_page: int = 0
    learning_lessons: list[LearningLesson] = field(default_factory=list)
    task_items: list[TodoistTask] = field(default_factory=list)
    task_timezone: str = "UTC"
    task_page: int = 0
    task_busy: bool = False


_SESSIONS: dict[int, DashboardSession] = {}


def _sessions_file(vault_path: Path | str) -> Path:
    return Path(vault_path) / DASHBOARD_SESSIONS_RELATIVE_PATH


def _read_sessions_payload(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, dict)
    }


def _serialize_session(session: DashboardSession) -> dict[str, Any]:
    review_pick = (
        list(session.weekly_review.review_pick)
        if session.weekly_review and session.weekly_review.review_pick
        else None
    )
    return {
        "dashboard_message_id": session.dashboard_message_id,
        "current_screen": session.current_screen,
        "file_root_id": session.file_root_id,
        "file_current_dir": session.file_current_dir,
        "file_page": session.file_page,
        "file_entries": [
            {
                "name": entry.name,
                "relative_path": entry.relative_path,
                "is_directory": entry.is_directory,
                "size": entry.size,
            }
            for entry in session.file_entries
        ],
        "queue_page": session.queue_page,
        "queue_items": [
            {
                "kind": item.kind,
                "page": item.page,
                "summary": item.summary,
                "since": item.since,
                "conflict_row": list(item.conflict_row)
                if item.conflict_row is not None
                else None,
                "candidate_page": item.candidate_page,
                "source_path": item.source_path,
                "source_excerpt": item.source_excerpt,
                "max_updates": item.max_updates,
            }
            for item in session.queue_items
        ],
        "weekly_review_pick": review_pick,
        "learning_page": session.learning_page,
        "learning_lessons": [
            {
                "lesson_id": lesson.lesson_id,
                "rel_path": lesson.rel_path,
                "kind": lesson.kind,
                "created": lesson.created,
                "condition": lesson.condition,
                "text": lesson.text,
                "outcome": lesson.outcome,
                "source": lesson.source,
                "exceptions": list(lesson.exceptions),
            }
            for lesson in session.learning_lessons
        ],
    }


def _deserialize_session(chat_id: int, raw: dict[str, Any]) -> DashboardSession:
    raw_screen = str(raw.get("current_screen") or "home")
    screen: DashboardScreen = (
        raw_screen if raw_screen in _VALID_SCREENS else "home"  # type: ignore[assignment]
    )
    msg_id = raw.get("dashboard_message_id")
    dashboard_message_id = msg_id if isinstance(msg_id, int) else None
    file_root = raw.get("file_root_id")
    file_root_id = file_root if isinstance(file_root, str) else None

    file_entries: list[BrowserEntry] = []
    for entry in raw.get("file_entries") or []:
        if isinstance(entry, dict) and isinstance(entry.get("relative_path"), str):
            file_entries.append(
                BrowserEntry(
                    name=str(entry.get("name") or ""),
                    relative_path=str(entry["relative_path"]),
                    is_directory=bool(entry.get("is_directory")),
                    size=int(entry.get("size") or 0),
                )
            )

    queue_items: list[QueueItem] = []
    for item in raw.get("queue_items") or []:
        if not isinstance(item, dict):
            continue
        raw_row = item.get("conflict_row")
        conflict_row: tuple[str, str, str, str, str] | None = None
        if isinstance(raw_row, list) and len(raw_row) == 5:
            conflict_row = (
                str(raw_row[0]),
                str(raw_row[1]),
                str(raw_row[2]),
                str(raw_row[3]),
                str(raw_row[4]),
            )
        queue_items.append(
            QueueItem(
                kind=str(item.get("kind") or ""),
                page=str(item.get("page") or ""),
                summary=str(item.get("summary") or ""),
                since=str(item.get("since") or ""),
                conflict_row=conflict_row,
                candidate_page=str(item.get("candidate_page") or ""),
                source_path=str(item.get("source_path") or ""),
                source_excerpt=str(item.get("source_excerpt") or ""),
                max_updates=int(item.get("max_updates") or 3),
            )
        )

    weekly_review: WeeklyReview | None = None
    raw_pick = raw.get("weekly_review_pick")
    if isinstance(raw_pick, list) and len(raw_pick) == 2:
        today = owner_today()
        weekly_review = WeeklyReview(
            start=today - timedelta(days=6),
            end=today,
            queue_items=(),
            changes=(),
            revisit=(),
            review_pick=(str(raw_pick[0]), str(raw_pick[1])),
        )

    learning_lessons: list[LearningLesson] = []
    for lesson in raw.get("learning_lessons") or []:
        if isinstance(lesson, dict) and isinstance(lesson.get("lesson_id"), str):
            raw_exc = lesson.get("exceptions")
            exceptions = (
                tuple(str(x) for x in raw_exc) if isinstance(raw_exc, list) else ()
            )
            learning_lessons.append(
                LearningLesson(
                    lesson_id=str(lesson["lesson_id"]),
                    rel_path=str(lesson.get("rel_path") or ""),
                    kind=str(lesson.get("kind") or "user-correction"),
                    created=str(lesson.get("created") or ""),
                    condition=str(lesson.get("condition") or ""),
                    text=str(lesson.get("text") or ""),
                    outcome=str(lesson.get("outcome") or ""),
                    source=str(lesson.get("source") or ""),
                    exceptions=exceptions,
                )
            )

    return DashboardSession(
        chat_id=chat_id,
        dashboard_message_id=dashboard_message_id,
        current_screen=screen,
        file_root_id=file_root_id,
        file_current_dir=str(raw.get("file_current_dir") or ""),
        file_page=int(raw.get("file_page") or 0),
        file_entries=file_entries,
        queue_page=int(raw.get("queue_page") or 0),
        queue_items=queue_items,
        weekly_review=weekly_review,
        learning_page=int(raw.get("learning_page") or 0),
        learning_lessons=learning_lessons,
    )


def save_dashboard_session(vault_path: Path | str, session: DashboardSession) -> None:
    """Persist minimal serializable dashboard session state atomically."""
    resolved = Path(vault_path)
    if not resolved.exists():
        return
    path = _sessions_file(resolved)
    try:
        payload = _read_sessions_payload(path)
        payload[str(session.chat_id)] = _serialize_session(session)
        atomic_write_file_text(
            path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )
    except OSError as exc:
        logger.warning("Failed to persist dashboard session: %s", exc)


def load_dashboard_session(
    vault_path: Path | str, chat_id: int
) -> DashboardSession | None:
    """Load one chat's persisted dashboard session from disk, if present."""
    path = _sessions_file(vault_path)
    payload = _read_sessions_payload(path)
    raw = payload.get(str(chat_id))
    if raw is None:
        return None
    try:
        return _deserialize_session(chat_id, raw)
    except (TypeError, ValueError):
        return None


def get_dashboard_session(
    chat_id: int, *, vault_path: Path | str | None = None
) -> DashboardSession:
    """Return a mutable dashboard session, restoring from disk if needed."""
    session = _SESSIONS.get(chat_id)
    if session is None:
        if vault_path is not None:
            session = load_dashboard_session(vault_path, chat_id)
        if session is None:
            session = DashboardSession(chat_id=chat_id)
        _SESSIONS[chat_id] = session
    return session


def clear_dashboard_session(
    chat_id: int, *, vault_path: Path | str | None = None
) -> None:
    """Forget dashboard state for one chat (in memory and on disk if given)."""
    _SESSIONS.pop(chat_id, None)
    if vault_path is None:
        return
    path = _sessions_file(vault_path)
    if not path.exists():
        return
    try:
        payload = _read_sessions_payload(path)
        if payload.pop(str(chat_id), None) is not None:
            atomic_write_file_text(
                path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            )
    except OSError as exc:
        logger.warning("Failed to clear persisted dashboard session: %s", exc)


def build_help_text() -> str:
    """Build the plain help surface shown by /help and /start."""
    return (
        "**Как использовать d-brain**\n\n"
        "Отправляй голосовые, текст, фото, документы и пересланные сообщения. "
        "Бот сохранит их в vault и использует в ежедневной обработке.\n\n"
        "**Команды**\n"
        "/start — показать эту справку\n"
        "/menu — открыть или пересоздать рабочее меню\n"
        "/do — выполнить произвольный запрос\n"
        "/undo — отменить последнюю запись\n"
        "/why — почему компилированная страница так говорит\n"
        "/find — поиск по заметкам vault без ИИ\n"
        "/reminders — список ожидающих напоминаний\n"
        "/help — эта справка\n"
        "/stats — статистика по записям\n"
        "/files — файловый браузер vault\n"
        "/process — показать разбор без сохранения\n"
        "/process_full — полный цикл с записью\n"
        "\n"
        "_/start теперь только показывает справку, "
        "а само меню живёт отдельно в /menu._"
    )


def _today_reminder_lines(vault_path: Path | str) -> list[str]:
    """Pending reminders due today in the owner's timezone."""
    try:
        pending = list_pending_reminders(vault_path)
    except OSError:
        return []
    today = owner_today()
    due_today = [
        (to_owner(record.due_at), record.id, record.text)
        for record in pending
        if to_owner(record.due_at).date() == today
    ]
    due_today.sort(key=lambda item: (item[0], item[1]))
    lines = [
        f"- {due:%H:%M} — {text}"
        for due, _rid, text in due_today[:MAX_HOME_REMINDERS]
    ]
    if len(due_today) > MAX_HOME_REMINDERS:
        lines.append(f"- …и ещё {len(due_today) - MAX_HOME_REMINDERS}")
    return lines


def build_home_text(vault_path: Path | str, user_id: int) -> str:
    """Build the dashboard home summary."""
    stats = _collect_usage_stats(vault_path, user_id)

    if stats["today_total"] == 0:
        today_line = "Сегодня записей пока нет."
    else:
        compact = " · ".join(
            f"{emoji} {stats['today_counts'][entry_type]}"
            for entry_type, emoji, _label in TYPE_META
            if stats["today_counts"][entry_type] > 0
        )
        summary = f" ({compact})" if compact else ""
        today_line = f"Сегодня: **{stats['today_total']}**{summary}"

    parts = [
        "**d-brain**",
        "",
        "Сохраняй мысли голосом или текстом — просто отправь сообщение.",
        "",
        today_line,
    ]
    reminders = _today_reminder_lines(vault_path)
    if reminders:
        parts.extend(["", "**Напоминания сегодня:**", *reminders])
    parts.extend(["", "Выбери, что сделать."])
    return "\n".join(parts)


def build_stats_text(vault_path: Path | str, user_id: int) -> str:
    """Build a detailed stats view for command and dashboard screens."""
    stats = _collect_usage_stats(vault_path, user_id)
    today = owner_today().isoformat()

    lines = [
        "**Статистика**",
        "",
        f"**Сегодня:** {today}",
        f"Всего записей: **{stats['today_total']}**",
    ]
    lines.extend(_format_counts(stats["today_counts"]))
    lines.extend(
        [
            "",
            "**За 7 дней**",
            f"Всего записей: **{stats['week_total']}**",
        ]
    )
    lines.extend(_format_counts(stats["week_counts"]))
    return "\n".join(lines)


def build_home_keyboard() -> InlineKeyboardMarkup:
    """Build the main inline dashboard keyboard."""
    return _keyboard(
        [
            [("✨ Задать вопрос или поручение", "menu:do")],
            [("☑ Дела на сегодня", "menu:tasks")],
            [("📋 Сводки", "menu:summaries"), ("🗂 Требуют решения", "menu:queue")],
            [("📁 Мои файлы", "menu:files"), ("⚙️ Разобрать записи", "menu:processing")],
            [("⋯ Ещё", "menu:more")],
            [("Закрыть меню", "menu:close")],
        ]
    )


def navigation_row(back: str = "menu:home") -> list[tuple[str, str]]:
    rows = [("Назад", back)]
    if back != "menu:home":
        rows.append(("Главное меню", "menu:home"))
    rows.append(("Закрыть", "menu:close"))
    return rows


async def render_input(
    bot: Bot, *, chat_id: int, message_id: int, text: str,
    screen: Literal["do_input", "brief_input", "goal_input"], back: str = "menu:home",
    vault_path: Path | str | None = None,
) -> None:
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = screen
    await render_dashboard(
        bot, chat_id=chat_id, session=session, text=text,
        keyboard=_keyboard([navigation_row(back)]), preferred_message_id=message_id,
    )
    if vault_path is not None:
        save_dashboard_session(vault_path, session)


def build_section_keyboard(section: DashboardScreen) -> InlineKeyboardMarkup:
    rows = {
        "summaries": [
            [("📰 Сводка дня", "menu:digest")],
            [("🤝 К встречам дня", "menu:meetings")],
            [("📅 Обзор недели", "menu:weekly")],
            [("📝 Собрать справку", "menu:brief")],
        ],
        "processing": [
            [("🔎 Показать разбор", "menu:process")],
            [("⚙️ Обработать и сохранить", "menu:processfull")],
        ],
        "more": [
            [("🎯 Цель недели", "menu:goal")],
            [("🧠 Выученные уроки", "menu:learnings")],
            [("📊 Статистика", "menu:stats")],
            [("🗺 Карта знаний · HTML", "menu:vaultmap")],
            [("⚙️ Автообработка", "menu:jobhealth")],
            [("❓ Справка", "menu:help")],
        ],
        "help": [],
    }[section]
    back = "menu:more" if section == "help" else "menu:home"
    return _keyboard([*rows, navigation_row(back)])


async def render_section(
    bot: Bot, *, chat_id: int, section: DashboardScreen,
    preferred_message_id: int | None = None,
    vault_path: Path | str | None = None,
) -> None:
    text = {
        "summaries": (
            "**Сводки**\n\nПосмотри итоги дня или недели, "
            "подготовься к сегодняшним встречам или собери справку."
        ),
        "processing": (
            "**Разобрать записи**\n\n"
            "**Показать разбор** — без сохранения изменений.\n"
            "**Обработать и сохранить** — полный разбор "
            "с созданием задач и обновлением заметок."
        ),
        "more": (
            "**Ещё**\n\nЦель недели, выученные уроки, статистика записей, "
            "карта знаний, отключённые шаги автообработки и справка."
        ),
        "help": build_help_text(),
    }[section]
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = section
    await render_dashboard(
        bot, chat_id=chat_id, session=session, text=text,
        keyboard=build_section_keyboard(section),
        preferred_message_id=preferred_message_id,
    )
    if vault_path is not None:
        save_dashboard_session(vault_path, session)


def build_stats_keyboard() -> InlineKeyboardMarkup:
    """Build the stats screen keyboard."""
    return _keyboard(
        [
            [("🔄 Обновить", "menu:stats")],
            navigation_row("menu:more"),
        ]
    )


def _disabled_job_steps(vault_path: Path | str) -> dict[str, dict[str, Any]]:
    """Steps the scheduled cycle has self-disabled (see processor.py)."""
    health = load_job_health(Path(vault_path))
    return {
        name: entry
        for name, entry in health.items()
        if isinstance(entry, dict) and entry.get("disabled")
    }


def build_disabled_steps_text(vault_path: Path | str) -> str:
    """Build the screen listing scheduled-cycle steps that self-disabled."""
    disabled = _disabled_job_steps(vault_path)
    if not disabled:
        return (
            "**Отключённые шаги**\n\n"
            "Сейчас все шаги ночного цикла работают без сбоев."
        )

    lines = [
        "**Отключённые шаги**",
        "",
        f"Шаг отключается сам после {JOB_HEALTH_DISABLE_THRESHOLD} сбоев подряд "
        "и пропускается, пока его не включат снова.",
    ]
    for name, entry in sorted(disabled.items()):
        lines.append("")
        lines.append(f"⛔ **{cycle_step_label(name)}**")
        error = str(entry.get("last_error") or "").strip()
        if error:
            lines.append(f"`{error}`")
    return "\n".join(lines)


def build_disabled_steps_keyboard(vault_path: Path | str) -> InlineKeyboardMarkup:
    """Build the disabled-steps screen keyboard: one re-enable button per step."""
    disabled_names = sorted(_disabled_job_steps(vault_path))
    rows: list[list[tuple[str, str]]] = [
        [(f"▶️ Включить снова: {name}", f"menu:jobhealthenable:{name}")]
        for name in disabled_names
    ]
    rows.append([("🔄 Обновить", "menu:jobhealth")])
    rows.append(navigation_row("menu:more"))
    return _keyboard(rows)


def reenable_job_step(vault_path: Path | str, step_name: str) -> bool:
    """Reset one disabled step's failure streak; return True if it was disabled."""
    vault_path = Path(vault_path)
    # vault_write_lock (an exclusive file lock under vault/.locks/) covers the
    # whole read-modify-write sequence so this can't interleave with the
    # nightly cycle's own read-modify-write of the same job-health.json (see
    # processor.py's _record_job_step_health).
    with vault_write_lock(vault_path):
        health = load_job_health(vault_path)
        entry = health.get(step_name)
        if not isinstance(entry, dict) or not entry.get("disabled"):
            return False
        health[step_name] = {
            **entry,
            "disabled": False,
            "consecutive_failures": 0,
        }
        save_job_health(vault_path, health)
    return True


def build_file_roots_text(roots: list[BrowserRoot]) -> str:
    """Build the root picker description."""
    if not roots:
        return (
            "**Файлы**\n\n"
            "Доступных разделов пока нет."
        )

    root_labels = ", ".join(root.label for root in roots)
    return (
        "**Файлы**\n\n"
        "Быстрые разделы vault для скачивания и навигации.\n"
        f"_Доступно:_ {root_labels}"
    )


def build_file_roots_keyboard(roots: list[BrowserRoot]) -> InlineKeyboardMarkup:
    """Build the curated root picker keyboard."""
    rows: list[list[tuple[str, str]]] = [
        [(root.label, f"menu:filesroot:{root.id}")]
        for root in roots
    ]
    rows.append(navigation_row())
    return _keyboard(rows)


def build_file_directory_text(
    *,
    root: BrowserRoot,
    current_dir: str,
    total_entries: int,
    page: int,
    total_pages: int,
) -> str:
    """Build one file directory screen."""
    current_path = "/" if not current_dir else f"/{current_dir}"
    lines = [
        "**Файлы**",
        "",
        f"**Раздел:** {root.label}",
        f"**Путь:** `{current_path}`",
        "_Нажми на файл, чтобы скачать его в Telegram._",
    ]
    if total_entries == 0:
        lines.extend(["", "В этой папке пока пусто."])
    elif total_pages > 1:
        lines.extend(
            [
                "",
                (
                    f"_Страница {page + 1} из {total_pages}, "
                    f"элементов: {total_entries}_"
                ),
            ]
        )
    return "\n".join(lines)


def file_entries_fingerprint(entries: list[BrowserEntry]) -> str:
    """8-hex-char fingerprint of one rendered file list (paths in order).

    Same idea as ``queue_item_fingerprint``, but for a whole page: a
    ``menu:filesentry:`` button is honoured only while the session still
    holds the list it was drawn from. Paths only, so a file edited in place
    is not "stale" -- the current version is what gets sent.
    """
    joined = "\n".join(entry.relative_path for entry in entries)
    return hashlib.sha256(joined.encode()).hexdigest()[:8]


def build_file_directory_keyboard(
    *,
    current_dir: str,
    page_entries: list[BrowserEntry],
    page: int,
    total_entries: int,
) -> InlineKeyboardMarkup:
    """Build the paginated directory keyboard."""
    rows: list[list[tuple[str, str]]] = []
    # The index alone points at whatever list is on screen *now*: an older
    # message (second /menu, refreshed page) would send a different file.
    token = file_entries_fingerprint(page_entries)
    for index, entry in enumerate(page_entries):
        prefix = "📁" if entry.is_directory else "📄"
        rows.append(
            [
                (
                    f"{prefix} {entry.name}",
                    f"menu:filesentry:{index}:{token}",
                )
            ]
        )

    total_pages = max(1, (total_entries + FILES_PAGE_SIZE - 1) // FILES_PAGE_SIZE)
    if total_pages > 1:
        start = page * FILES_PAGE_SIZE + 1
        end = start + len(page_entries) - 1
        pagination_row: list[tuple[str, str]] = []
        if page > 0:
            pagination_row.append(("Предыдущие", f"menu:filespage:{page - 1}"))
        pagination_row.append((f"{start}-{end}/{total_entries}", "menu:noop"))
        if page + 1 < total_pages:
            pagination_row.append(("Следующие", f"menu:filespage:{page + 1}"))
        rows.append(pagination_row)

    rows.extend(
        [
            [("🔄 Обновить", "menu:filesrefresh")],
            navigation_row("menu:filesup" if current_dir else "menu:filesroots"),
        ]
    )
    return _keyboard(rows)


def queue_action_button_specs(item: QueueItem) -> tuple[tuple[str, str], ...]:
    """UI ``(action_id, button_label)`` pairs for one decisions-queue item."""
    if item.kind == FACT_CHECK_REJECTED_KIND:
        return (
            ("confirm", "Подтвердить: страница актуальна"),
            ("reject", "Архивировать страницу"),
            ("defer", "Отложить"),
        )
    if item.kind == CONFLICT_KIND and item.conflict_row is not None:
        _since, existing_claim, _existing_source, new_claim, _new_source = (
            item.conflict_row
        )
        return (
            ("keep_existing", f"«{existing_claim}»"),
            ("keep_new", f"«{new_claim}»"),
            ("defer", "Отложить"),
        )
    if item.kind == VERIFY_REJECTED_KIND:
        return (
            ("retry", "Повторить проверку"),
            ("reject", "Прекратить попытки"),
            ("defer", "Отложить"),
        )
    if item.kind == DUPLICATE_CANDIDATE_KIND:
        return (
            ("link", "Связать как похожие"),
            ("distinct", "Пометить как разные"),
            ("defer", "Отложить"),
        )
    return (
        ("reject", "Отклонить (удалить из очереди)"),
        ("defer", "Отложить"),
    )


def build_queue_text(
    *,
    items: list[QueueItem],
    page: int,
    total_pages: int,
    total_items: int,
) -> str:
    """Build one page of the decisions queue list (задача L, ТЗ 7.2)."""
    lines = ["**Требуют решения**", ""]
    if total_items == 0:
        lines.append("Очередь пуста — открытых пунктов нет.")
        return "\n".join(lines)
    if total_pages > 1:
        lines.append(
            f"_Страница {page + 1} из {total_pages}, всего пунктов: {total_items}_"
        )
        lines.append("")
    owner_items, technical_items = partition_queue_items(items)
    if owner_items:
        lines.append("**Решения владельца**")
        lines.extend(format_queue_item_line(entry) for entry in owner_items)
    if technical_items:
        if owner_items:
            lines.append("")
        lines.append("**Техническое внимание**")
        lines.extend(format_queue_item_line(entry) for entry in technical_items)
    return "\n".join(lines)


def build_queue_keyboard(
    *,
    items: list[QueueItem],
    page: int,
    total_items: int,
) -> InlineKeyboardMarkup:
    """Build the paginated queue keyboard -- one button per item, encoding
    its index within the current page plus ``queue_item_fingerprint`` (the
    path itself never fits Telegram's 64-byte ``callback_data`` limit; worst
    case here is prefix (15) + a 2-digit index + ":" + 8 hex chars = 26).

    The fingerprint is on the *open* button for the same reason the action
    buttons below carry one (code review): an index alone can resolve to a
    different but still-valid item. That happens whenever an older queue
    message keeps live buttons -- ``render_dashboard`` falls back to
    *sending* a new message when its edit fails for anything other than
    "message is not modified" -- while ``session.queue_items`` has since
    moved on to another page.
    """
    rows: list[list[tuple[str, str]]] = []
    for index, entry in enumerate(items):
        label = f"{queue_kind_label(entry.kind)}: {entry.page}"
        fingerprint = queue_item_fingerprint(entry)
        rows.append([(label[:60], f"menu:queueitem:{index}:{fingerprint}")])

    total_pages = max(1, (total_items + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    if total_pages > 1:
        start = page * QUEUE_PAGE_SIZE + 1
        end = start + len(items) - 1
        pagination_row: list[tuple[str, str]] = []
        if page > 0:
            pagination_row.append(("Предыдущие", f"menu:queuepage:{page - 1}"))
        pagination_row.append((f"{start}-{end}/{total_items}", "menu:noop"))
        if page + 1 < total_pages:
            pagination_row.append(("Следующие", f"menu:queuepage:{page + 1}"))
        rows.append(pagination_row)

    # "Обновить" redraws the page the owner is on, so it reuses the
    # pagination callback rather than the bare ``menu:queue`` the *entry
    # points* (home, weekly review) use -- that one always opens at page 0,
    # which silently threw an owner working through page 3 back to the top
    # (code review).
    rows.append([("🔄 Обновить", f"menu:queuepage:{page}")])
    rows.append(navigation_row())
    return _keyboard(rows)


def build_queue_item_text(item: QueueItem) -> str:
    """Build one queue item's detail view (задача L, ТЗ 7.2)."""
    lines = [
        "**Пункт очереди решений**",
        "",
        f"**Тип:** {queue_kind_label(item.kind)}",
        f"**Страница:** {item.page}",
        f"**Суть:** {item.summary}",
    ]
    if item.since:
        lines.append(f"**С какого момента:** {item.since}")
    return "\n".join(lines)


def build_queue_item_keyboard(
    *, index: int, item: QueueItem, page: int
) -> InlineKeyboardMarkup:
    """Build the action keyboard for one queue item -- labels come straight
    from ``action_button_specs`` (real claim wording for a conflict, never
    abstract labels; only reject/defer for an unsupported kind).

    Each button also carries ``queue_item_fingerprint(item)`` (задача L code
    review, defect 4) alongside the index, so the handler can detect a
    stale/reused index -- e.g. a duplicated tap arriving after a previous
    response already shortened ``session.queue_items`` -- before applying an
    action to the wrong item. Worst case for ``callback_data`` length: prefix
    (14) + a 2-digit index (2) + ":" + an 8-char fingerprint (8) + ":" + the
    longest action id across every kind = 39 bytes, well under Telegram's
    64-byte ``callback_data`` limit. Recomputed (задача N), not assumed,
    after adding ``duplicate-candidate``'s "link"/"distinct" ids: both are
    shorter than ``conflict``'s ``"keep_existing"`` (13 chars), which stays
    the longest action id and the worst case above.
    """
    fingerprint = queue_item_fingerprint(item)
    rows: list[list[tuple[str, str]]] = [
        [(label, f"menu:queueact:{index}:{fingerprint}:{action_id}")]
        for action_id, label in queue_action_button_specs(item)
    ]
    # Back to the page this item was opened from, not to page 0 -- same
    # reason as the "Обновить" button above.
    rows.append(navigation_row(f"menu:queuepage:{page}"))
    return _keyboard(rows)


def build_brief_type_text() -> str:
    """Build the brief type-picker screen text (задача L, ТЗ 7.6)."""
    return (
        "**Собрать справку**\n\n"
        "Выбери тип справки, затем отправь текстом запрос "
        "(название решения, темы или проекта)."
    )


def build_brief_type_keyboard() -> InlineKeyboardMarkup:
    """Build the 3 brief-type buttons (decision/topic/project, ТЗ 7.6)."""
    rows: list[list[tuple[str, str]]] = [
        [(_BRIEF_TYPE_BUTTON_LABELS[brief_type], f"menu:brieftype:{brief_type}")]
        for brief_type in BRIEF_TYPES
    ]
    rows.append(navigation_row("menu:summaries"))
    return _keyboard(rows)


_SCREEN_TRUNCATION_SUFFIX = "\n\n… (показаны первые строки, экран обрезан)"


def build_rich_menu(text: str, keyboard: InlineKeyboardMarkup) -> InputRichMessage:
    body = markdown_to_html(normalize_markdown_input(text)).replace("\n", "<br>")
    content = f"<p>{body}</p>"
    for row in keyboard.inline_keyboard:
        buttons = "".join(
            '<tg-button type="callback_data" '
            f'data="{escape(button.callback_data or "", quote=True)}" style="link">'
            f"{escape(button.text)}</tg-button>"
            for button in row
        )
        content += f"<tg-button-row>{buttons}</tg-button-row>"
    return InputRichMessage(html=content, skip_entity_detection=True)


async def render_dashboard(
    bot: Bot,
    *,
    chat_id: int,
    session: DashboardSession,
    text: str,
    keyboard: InlineKeyboardMarkup,
    preferred_message_id: int | None = None,
    notice: str | None = None,
    rich_message: InputRichMessage | None = None,
) -> None:
    """Render one dashboard screen by editing or sending a message."""
    target_message_id = preferred_message_id or session.dashboard_message_id
    payload_text = text
    if notice:
        payload_text = f"ℹ️ _{notice}_\n\n{text}"
    payload_markdown = normalize_markdown_input(payload_text)
    payload_message = markdown_to_markdown_v2(payload_markdown)
    if len(payload_message) > TELEGRAM_TEXT_LIMIT:
        # A screen this long (e.g. a weekly review with 40+ changes) blows
        # past Telegram's message limit. Left alone, the edit below fails
        # and the ``send_text`` fallback ships an HTML document with no
        # keyboard -- ``session.dashboard_message_id`` then points at that
        # document forever, and every later edit on this screen fails too.
        # MarkdownV2 escaping grows the text (every '.', '-', '(' becomes
        # two characters), so trimming the raw text to the limit once is
        # not enough: shrink the raw budget by the observed overshoot until
        # the escaped payload fits.
        source_text = payload_text
        raw_budget = TELEGRAM_TEXT_LIMIT
        for _ in range(8):
            overshoot = len(payload_message)
            raw_budget = max(1, raw_budget * TELEGRAM_TEXT_LIMIT // overshoot)
            if raw_budget <= len(_SCREEN_TRUNCATION_SUFFIX) + 200:
                # Nothing readable would survive; leave the oversized text
                # to the document fallback below instead of a bare suffix.
                break
            payload_text = truncate_plain_text_for_edit(
                source_text,
                max_length=raw_budget,
                suffix=_SCREEN_TRUNCATION_SUFFIX,
            )
            payload_markdown = normalize_markdown_input(payload_text)
            payload_message = markdown_to_markdown_v2(payload_markdown)
            if len(payload_message) <= TELEGRAM_TEXT_LIMIT:
                break

    rich_message = rich_message or build_rich_menu(payload_text, keyboard)
    try:
        if target_message_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=target_message_id,
                    rich_message=rich_message, reply_markup=None, parse_mode=None,
                )
                session.dashboard_message_id = target_message_id
                return
            except TelegramBadRequest as error:
                description = str(error).lower()
                if "message is not modified" in description:
                    session.dashboard_message_id = target_message_id
                    return
                if not any(reason in description for reason in (
                    "message to edit not found", "message can't be edited",
                    "message cannot be edited",
                )):
                    raise
        sent = await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message)
        session.dashboard_message_id = sent.message_id
        return
    except TelegramBadRequest:
        logger.warning("Rich menu rejected, using ordinary menu", exc_info=True)

    if target_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=target_message_id,
                text=payload_message,
                reply_markup=keyboard,
                parse_mode="MarkdownV2",
            )
            session.dashboard_message_id = target_message_id
            return
        except Exception as error:  # noqa: BLE001
            if "message is not modified" in str(error):
                session.dashboard_message_id = target_message_id
                return

    sent = await send_text(
        chat_id=chat_id,
        text=payload_text,
        bot=bot,
        reply_markup=keyboard,
    )
    session.dashboard_message_id = sent.message_id


async def render_home(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    user_id: int | None = None,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the dashboard home screen."""
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "home"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_home_text(vault_path, user_id or chat_id),
        keyboard=build_home_keyboard(),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_stats(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    user_id: int | None = None,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the stats dashboard screen."""
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "stats"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_stats_text(vault_path, user_id or chat_id),
        keyboard=build_stats_keyboard(),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_disabled_steps(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the self-disabled scheduled-cycle steps screen."""
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "jobhealth"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_disabled_steps_text(vault_path),
        keyboard=build_disabled_steps_keyboard(vault_path),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_file_roots(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the curated root picker for the file browser."""
    browser = FileBrowserService(vault_path)
    roots = browser.list_roots()
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "file_roots"
    session.file_root_id = None
    session.file_current_dir = ""
    session.file_page = 0
    session.file_entries = []
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_file_roots_text(roots),
        keyboard=build_file_roots_keyboard(roots),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_file_directory(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    root_id: str,
    current_dir: str = "",
    page: int = 0,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render one directory inside the file browser."""
    browser = FileBrowserService(vault_path)
    root, normalized_dir, entries = await asyncio.to_thread(
        browser.list_entries,
        root_id=root_id,
        current_dir=current_dir,
    )
    total_pages = max(1, (len(entries) + FILES_PAGE_SIZE - 1) // FILES_PAGE_SIZE)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * FILES_PAGE_SIZE
    end = (safe_page + 1) * FILES_PAGE_SIZE
    page_entries = entries[start:end]

    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "files"
    session.file_root_id = root.id
    session.file_current_dir = normalized_dir
    session.file_page = safe_page
    session.file_entries = page_entries

    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_file_directory_text(
            root=root,
            current_dir=normalized_dir,
            total_entries=len(entries),
            page=safe_page,
            total_pages=total_pages,
        ),
        keyboard=build_file_directory_keyboard(
            current_dir=normalized_dir,
            page_entries=page_entries,
            page=safe_page,
            total_entries=len(entries),
        ),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_queue(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    page: int = 0,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render one page of the decisions queue (задача L, ТЗ 7.2).

    Re-reads ``list_queue_items`` fresh on every render (same approach the
    file browser takes for a directory listing): cheap enough, and it means
    a response applied to one item is reflected the moment the list is
    reopened, with no separate cache to invalidate.
    """
    raw_items = await asyncio.to_thread(list_queue_items, Path(vault_path))
    owner_items, technical_items = partition_queue_items(raw_items)
    all_items = [*owner_items, *technical_items]
    total_items = len(all_items)
    total_pages = max(1, (total_items + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * QUEUE_PAGE_SIZE
    end = start + QUEUE_PAGE_SIZE
    page_items = all_items[start:end]

    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "queue"
    session.queue_page = safe_page
    session.queue_items = page_items

    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_queue_text(
            items=page_items,
            page=safe_page,
            total_pages=total_pages,
            total_items=total_items,
        ),
        keyboard=build_queue_keyboard(
            items=page_items, page=safe_page, total_items=total_items
        ),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_queue_item(
    bot: Bot,
    *,
    chat_id: int,
    index: int,
    vault_path: Path | str | None = None,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> QueueItem | None:
    """Render one queue item's detail + action view.

    ``index`` refers to ``session.queue_items`` (the *current page's* slice,
    same convention as ``send_browser_file``'s ``entry_index``) -- returns
    the resolved item for the caller to act on, or ``None`` if the index no
    longer resolves (the queue changed since the list was last rendered).
    """
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    if index < 0 or index >= len(session.queue_items):
        return None
    item = session.queue_items[index]
    session.current_screen = "queue_item"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_queue_item_text(item),
        keyboard=build_queue_item_keyboard(
            index=index, item=item, page=session.queue_page
        ),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    if vault_path is not None:
        save_dashboard_session(vault_path, session)
    return item


# --- Weekly review (задача N, missed acceptance criterion) ----------------
#
# On-demand "Сводка недели" screen: the half-hour ritual (ТЗ) -- work
# through the queue, see the week's changes, confirm one page as reviewed,
# see one forgotten page. Kept within budget by design: compact, no
# pagination, one action button, a link to the full "Очередь" screen for
# the rest.


def page_fingerprint(rel_path: str) -> str:
    """8-hex-char identity fingerprint of one page path (задача N) -- same
    principle as ``queue_item_fingerprint`` above, applied to a page that is
    not selected from an indexed list (the weekly screen's single "confirm
    reviewed" pick, ``WeeklyReview.review_pick``). The handler must refuse
    to act if the page currently held in session state does not match what
    was fingerprinted when the button was rendered (e.g. the screen was
    refreshed and the picked page changed between render and tap)."""
    return hashlib.sha256(rel_path.encode()).hexdigest()[:8]


def build_weekly_review_text(review: WeeklyReview) -> str:
    """Build the "Сводка недели" screen text (задача N).

    The queue section reuses ``format_queue_item_line`` (the same line the
    "Очередь" screen and the human-readable queue file render) and is
    capped at ``WEEKLY_QUEUE_PREVIEW_SIZE`` with a pointer to the full queue
    screen -- no separate pagination here.
    """
    lines = [
        "**Обзор недели**",
        "",
        f"**Период:** {review.start.isoformat()} — {review.end.isoformat()}",
        "",
        f"**Изменения за неделю ({len(review.changes)}):**",
    ]
    if not review.changes:
        lines.append("Изменений за неделю не было.")
    else:
        lines.extend(
            f"- [[{change.rel_path}]] — {change.what_added}"
            for change in review.changes
        )

    lines.append("")
    lines.append(f"**Очередь решений ({len(review.queue_items)}):**")
    if not review.queue_items:
        lines.append("Очередь пуста.")
    else:
        preview = review.queue_items[:WEEKLY_QUEUE_PREVIEW_SIZE]
        lines.extend(format_queue_item_line(item) for item in preview)
        remaining = len(review.queue_items) - len(preview)
        if remaining > 0:
            lines.append(f"…и ещё {remaining} — см. «Очередь» в меню.")

    lines.append("")
    if review.revisit:
        rel_path, title = review.revisit[0]
        lines.append(f"**Забытая страница:** [[{rel_path}]] ({title})")
    else:
        lines.append("**Забытая страница:** предложений на этой неделе нет.")

    lines.append("")
    if review.review_pick:
        rel_path, title = review.review_pick
        lines.append(f"**Подтвердить как просмотренную:** [[{rel_path}]] ({title})")
    else:
        lines.append(
            "**Подтвердить как просмотренную:** кандидатов на этой неделе нет."
        )
    return "\n".join(lines)


def build_weekly_review_keyboard(review: WeeklyReview) -> InlineKeyboardMarkup:
    """Build the weekly-review keyboard -- one action button (confirm
    reviewed, only when there is a pick), a link to the full queue screen,
    refresh/home (задача N)."""
    rows: list[list[tuple[str, str]]] = []
    if review.review_pick is not None:
        rel_path, _title = review.review_pick
        fingerprint = page_fingerprint(rel_path)
        rows.append(
            [("✅ Подтвердить просмотр", f"menu:weeklyreview:{fingerprint}")]
        )
    rows.append([("🎯 Цель недели", "menu:goal")])
    rows.append([("🗂 Требуют решения", "menu:queue")])
    rows.append([("🔄 Обновить", "menu:weekly")])
    rows.append(navigation_row("menu:summaries"))
    return _keyboard(rows)


async def render_weekly_review(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the "Сводка недели" screen (задача N, missed acceptance
    criterion). Collected fresh from ``collect_weekly_review`` on every
    render, same convention as ``render_queue``."""
    review = await asyncio.to_thread(
        collect_weekly_review, Path(vault_path), owner_today()
    )

    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "weekly"
    session.weekly_review = review

    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_weekly_review_text(review),
        keyboard=build_weekly_review_keyboard(review),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def render_brief_type(
    bot: Bot,
    *,
    chat_id: int,
    preferred_message_id: int | None = None,
    notice: str | None = None,
    vault_path: Path | str | None = None,
) -> None:
    """Render the brief type-picker screen (задача L, ТЗ 7.6)."""
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "brief_type"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_brief_type_text(),
        keyboard=build_brief_type_keyboard(),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    if vault_path is not None:
        save_dashboard_session(vault_path, session)


# --- Conversation learning transparency -----------------------------------


def lesson_fingerprint(lesson_id: str) -> str:
    """8-hex-char fingerprint for one conversation-learning lesson id."""
    return hashlib.sha256(lesson_id.encode()).hexdigest()[:8]


def build_learnings_text(
    *,
    lessons: list[LearningLesson],
    page: int,
    total_pages: int,
    total_lessons: int,
) -> str:
    """Build one page of the saved conversation-learning lessons screen."""
    lines = ["**Выученные уроки**", ""]
    if total_lessons == 0:
        lines.append("Сохранённых уроков пока нет.")
        return "\n".join(lines)
    if total_pages > 1:
        lines.append(
            f"_Страница {page + 1} из {total_pages}, всего уроков: {total_lessons}_"
        )
        lines.append("")
    for index, lesson in enumerate(lessons, start=1):
        badge = "Практика" if lesson.kind == "confirmed-practice" else "Исправление"
        lines.append(f"**{index}. [{badge} · {lesson.created}]**")
        if lesson.condition:
            lines.append(f"_Когда:_ {lesson.condition}")
        lines.append(lesson.text)
        if lesson.outcome:
            lines.append(f"_Результат:_ {lesson.outcome}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_learnings_keyboard(
    *,
    lessons: list[LearningLesson],
    page: int,
    total_lessons: int,
) -> InlineKeyboardMarkup:
    """Build keyboard with per-lesson delete buttons and pagination."""
    rows: list[list[tuple[str, str]]] = []
    for index, lesson in enumerate(lessons):
        fp = lesson_fingerprint(lesson.lesson_id)
        short = lesson.text[:36] + ("…" if len(lesson.text) > 36 else "")
        rows.append(
            [(f"🗑 Удалить {index + 1}: {short}", f"menu:learningdel:{index}:{fp}")]
        )

    total_pages = max(
        1, (total_lessons + LEARNINGS_PAGE_SIZE - 1) // LEARNINGS_PAGE_SIZE
    )
    if total_pages > 1:
        start = page * LEARNINGS_PAGE_SIZE + 1
        end = start + len(lessons) - 1
        pagination_row: list[tuple[str, str]] = []
        if page > 0:
            pagination_row.append(("Предыдущие", f"menu:learningspage:{page - 1}"))
        pagination_row.append((f"{start}-{end}/{total_lessons}", "menu:noop"))
        if page + 1 < total_pages:
            pagination_row.append(("Следующие", f"menu:learningspage:{page + 1}"))
        rows.append(pagination_row)

    rows.append([("🔄 Обновить", f"menu:learningspage:{page}")])
    rows.append(navigation_row("menu:more"))
    return _keyboard(rows)


async def render_learnings(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    page: int = 0,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the saved conversation-learning lessons screen."""
    all_lessons = await asyncio.to_thread(list_lessons, Path(vault_path))
    total_lessons = len(all_lessons)
    total_pages = max(
        1, (total_lessons + LEARNINGS_PAGE_SIZE - 1) // LEARNINGS_PAGE_SIZE
    )
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * LEARNINGS_PAGE_SIZE
    page_lessons = all_lessons[start : start + LEARNINGS_PAGE_SIZE]

    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "learnings"
    session.learning_page = safe_page
    session.learning_lessons = page_lessons

    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_learnings_text(
            lessons=page_lessons,
            page=safe_page,
            total_pages=total_pages,
            total_lessons=total_lessons,
        ),
        keyboard=build_learnings_keyboard(
            lessons=page_lessons, page=safe_page, total_lessons=total_lessons
        ),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


# --- Weekly ONE Big Thing view & edit -------------------------------------


def _is_goal_placeholder(text: str) -> bool:
    return not any(char.isalnum() for char in _PLACEHOLDER_STRIP_RE.sub("", text))


def read_weekly_one_big_thing(vault_path: Path | str) -> str | None:
    """Read the current ONE Big Thing from ``goals/3-weekly.md``."""
    weekly_path = Path(vault_path) / "goals" / "3-weekly.md"
    if not weekly_path.exists():
        return None
    try:
        content = weekly_path.read_text(encoding="utf-8")
    except OSError:
        return None
    heading_match = _ONE_BIG_THING_HEADING_RE.search(content)
    if heading_match is None:
        return None
    rest = content[heading_match.end() :]
    next_heading = _NEXT_HEADING_RE.search(rest)
    section = rest[: next_heading.start()] if next_heading else rest

    marker: str | None = None
    lines: list[str] = []
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if marker is None:
            if stripped[0] not in ">-":
                break
            marker = stripped[0]
        elif stripped[0] != marker:
            break
        line = stripped.lstrip(marker).strip()
        if not line or (line.startswith("**") and line.endswith(":**")):
            continue
        if _is_goal_placeholder(line):
            continue
        lines.append(line)
    return " ".join(lines).strip() or None


def update_weekly_one_big_thing(
    vault_path: Path | str,
    new_goal: str,
    *,
    timeout_seconds: float | None = None,
) -> Path:
    """Atomically replace the ONE Big Thing quote in ``goals/3-weekly.md``."""
    cleaned = " ".join(new_goal.split()).strip()
    if not cleaned:
        raise ValueError("Goal text must not be empty")

    resolved_vault = Path(vault_path)
    weekly_path = resolved_vault / "goals" / "3-weekly.md"
    quote_block = f"> **If I accomplish nothing else, I will:**\n> {cleaned}"

    with vault_write_lock(resolved_vault, timeout=timeout_seconds):
        if not weekly_path.exists():
            new_content = f"# Weekly Focus\n\n## ONE Big Thing\n\n{quote_block}\n"
        else:
            content = weekly_path.read_text(encoding="utf-8")
            heading_match = _ONE_BIG_THING_HEADING_RE.search(content)
            if heading_match is None:
                suffix = "\n\n" if content.strip() else ""
                new_content = (
                    f"{content.rstrip()}{suffix}## ONE Big Thing\n\n{quote_block}\n"
                )
            else:
                rest = content[heading_match.end() :]
                next_heading = _NEXT_HEADING_RE.search(rest)
                section = rest[: next_heading.start()] if next_heading else rest
                after_section = rest[next_heading.start() :] if next_heading else ""

                # Keep trailing non-marker lines (like HTML comments) in section.
                section_lines = section.splitlines()
                trailing: list[str] = []
                marker: str | None = None
                in_trailing = False
                for raw_line in section_lines:
                    stripped = raw_line.strip()
                    if in_trailing:
                        trailing.append(raw_line)
                        continue
                    if not stripped:
                        continue
                    if marker is None:
                        if stripped[0] in ">-":
                            marker = stripped[0]
                            continue
                        in_trailing = True
                        trailing.append(raw_line)
                    elif stripped[0] == marker:
                        continue
                    else:
                        in_trailing = True
                        trailing.append(raw_line)

                trailing_block = (
                    "\n\n" + "\n".join(trailing).strip()
                    if any(t.strip() for t in trailing)
                    else ""
                )
                separator = "\n\n" if after_section else "\n"
                new_content = (
                    content[: heading_match.end()]
                    + f"\n\n{quote_block}{trailing_block}{separator}"
                    + after_section.lstrip("\n")
                )
        atomic_write_file_text(weekly_path, new_content)
    return weekly_path


def build_weekly_goal_text(vault_path: Path | str) -> str:
    """Build the weekly ONE Big Thing view screen."""
    goal = read_weekly_one_big_thing(vault_path)
    lines = ["**Главная цель недели (ONE Big Thing)**", ""]
    if goal:
        lines.append(f"> {goal}")
    else:
        lines.append("Главная цель недели пока не задана.")
    lines.extend(
        [
            "",
            "_Файл: `goals/3-weekly.md`_",
        ]
    )
    return "\n".join(lines)


def build_weekly_goal_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for viewing/editing the weekly ONE Big Thing."""
    return _keyboard(
        [
            [("✏️ Изменить цель", "menu:goaledit")],
            [("📅 Обзор недели", "menu:weekly")],
            navigation_row("menu:more"),
        ]
    )


async def render_weekly_goal(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the weekly ONE Big Thing screen."""
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "weekly_goal"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=build_weekly_goal_text(vault_path),
        keyboard=build_weekly_goal_keyboard(),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


# --- Meeting prep (deterministic, no LLM) ---------------------------------


def _meeting_keywords(event: CalendarEvent) -> set[str]:
    raw = " ".join([event.title, *event.attendees])
    return {
        token
        for token in (m.group(0).casefold() for m in _MEETING_TOKEN_RE.finditer(raw))
        if token not in _MEETING_STOPWORDS and not token.isdigit()
    }


def _iter_meeting_context_files(vault_path: Path, day: date) -> list[Path]:
    files: list[Path] = []
    compiled_dir = vault_path / "compiled"
    if compiled_dir.is_dir():
        for path in sorted(compiled_dir.rglob("*.md")):
            rel_parts = path.relative_to(compiled_dir).parts
            if rel_parts and rel_parts[0] == "archive":
                continue
            files.append(path)
    for folder in ("business", "projects"):
        sub = vault_path / folder
        if sub.is_dir():
            files.extend(sorted(sub.glob("*.md")))
    daily_dir = vault_path / "daily"
    if daily_dir.is_dir():
        for offset in range(14):
            candidate = daily_dir / f"{(day - timedelta(days=offset)).isoformat()}.md"
            if candidate.is_file():
                files.append(candidate)
    return files


def _note_snippet_for_keywords(
    vault_path: Path, path: Path, keywords: set[str]
) -> tuple[int, str, str]:
    """Return ``(score, rel_path, snippet)`` for one vault markdown file."""
    rel = path.relative_to(vault_path).as_posix()
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8", errors="replace")
    except OSError:
        return 0, rel, ""

    description = ""
    try:
        fm = parse_frontmatter_bytes(raw_bytes)
        description = str(fm.fields.get("description") or "").strip()
    except Exception:  # noqa: BLE001
        description = ""

    rel_tokens = {
        m.group(0).casefold()
        for m in _MEETING_TOKEN_RE.finditer(path.stem.replace("-", " "))
    }
    text_lower = text.casefold()
    score = 0
    for kw in keywords:
        if kw in rel_tokens:
            score += 3
        elif kw in description.casefold():
            score += 2
        elif kw in text_lower:
            score += 1
    if score == 0:
        return 0, rel, ""

    # Slight boost for compiled/ briefings over raw daily logs.
    if rel.startswith("compiled/"):
        score += 1

    snippet = description
    if not snippet:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("---") or stripped.startswith("#"):
                continue
            lowered = stripped.casefold()
            if any(kw in lowered for kw in keywords):
                snippet = stripped
                break
    if len(snippet) > 120:
        snippet = snippet[:119].rstrip() + "…"
    return score, rel, snippet


def build_meeting_prep_text(
    vault_path: Path | str,
    events: tuple[CalendarEvent, ...] | list[CalendarEvent],
    *,
    day: date,
    calendar_error: bool = False,
    calendar_configured: bool = True,
) -> str:
    """Build deterministic context cards for today's calendar meetings."""
    resolved_vault = Path(vault_path)
    lines = [f"**К встречам дня — {day.isoformat()}**", ""]
    if not calendar_configured:
        lines.append(
            "Календарь не подключён: укажи `CALENDAR_ICS_URL` в `.env`, "
            "чтобы видеть встречи и контекст к ним."
        )
        return "\n".join(lines)
    if calendar_error:
        lines.append("Календарь сейчас недоступен. Попробуй обновить позже.")
        return "\n".join(lines)
    if not events:
        lines.append("На сегодня встреч в календаре нет.")
        return "\n".join(lines)

    ordered = list(events)[:MAX_MEETING_PREP_EVENTS]
    brief_lines = format_brief_lines(ordered, day, owner_tz())
    context_files = _iter_meeting_context_files(resolved_vault, day)

    for idx, event in enumerate(ordered):
        header = (
            brief_lines[idx].removeprefix("- ")
            if idx < len(brief_lines)
            else event.title
        )
        lines.append(f"**{header}**")
        if event.attendees:
            lines.append(f"_Участники:_ {', '.join(event.attendees)}")
        keywords = _meeting_keywords(event)
        matches: list[tuple[int, str, str]] = []
        if keywords:
            for file_path in context_files:
                score, rel, snippet = _note_snippet_for_keywords(
                    resolved_vault, file_path, keywords
                )
                if score > 0:
                    matches.append((score, rel, snippet))
        matches.sort(key=lambda item: (-item[0], item[1]))
        if matches:
            for _score, rel, snippet in matches[:MAX_MEETING_PREP_NOTES]:
                suffix = f" — {snippet}" if snippet else ""
                lines.append(f"- [[{rel}]]{suffix}")
        else:
            lines.append("- _Связанных заметок в vault не найдено._")
        lines.append("")

    if len(events) > MAX_MEETING_PREP_EVENTS:
        lines.append(f"_…и ещё {len(events) - MAX_MEETING_PREP_EVENTS} встреч._")
    return "\n".join(lines).rstrip()


def build_meeting_prep_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for the meeting-prep screen."""
    return _keyboard(
        [
            [("🔄 Обновить", "menu:meetings")],
            navigation_row("menu:summaries"),
        ]
    )


async def render_meeting_prep(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    calendar_ics_url: str,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Fetch today's meetings and render deterministic vault context cards."""
    today = owner_today()
    url = calendar_ics_url.strip()
    if not url:
        text = build_meeting_prep_text(
            vault_path, (), day=today, calendar_configured=False
        )
    else:
        result = await asyncio.to_thread(fetch_day, url, today)
        text = await asyncio.to_thread(
            build_meeting_prep_text,
            vault_path,
            result.events,
            day=today,
            calendar_error=bool(result.error),
            calendar_configured=True,
        )

    session = get_dashboard_session(chat_id, vault_path=vault_path)
    session.current_screen = "meeting_prep"
    await render_dashboard(
        bot,
        chat_id=chat_id,
        session=session,
        text=text,
        keyboard=build_meeting_prep_keyboard(),
        preferred_message_id=preferred_message_id,
        notice=notice,
    )
    save_dashboard_session(vault_path, session)


async def close_dashboard(
    bot: Bot,
    *,
    chat_id: int,
    preferred_message_id: int | None = None,
    vault_path: Path | str | None = None,
) -> None:
    """Delete the current dashboard message if possible and clear session state."""
    session = get_dashboard_session(chat_id, vault_path=vault_path)
    target_message_id = preferred_message_id or session.dashboard_message_id
    if target_message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=target_message_id)
        except Exception:  # noqa: BLE001
            pass
    clear_dashboard_session(chat_id, vault_path=vault_path)


def file_browser_parent_dir(current_dir: str) -> str:
    """Return the parent directory inside the current file browser root."""
    if not current_dir:
        return ""
    parts = current_dir.split("/")
    return "/".join(parts[:-1])


async def send_browser_file(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: Path | str,
    session: DashboardSession,
    entry_index: int,
) -> str:
    """Send one selected file to Telegram and return its display name."""
    if session.file_root_id is None:
        raise ValueError("File browser root is not selected")
    if entry_index < 0 or entry_index >= len(session.file_entries):
        raise IndexError(entry_index)

    entry = session.file_entries[entry_index]
    if entry.is_directory:
        raise IsADirectoryError(entry.relative_path)

    browser = FileBrowserService(vault_path)
    _root, path = browser.resolve_file(
        root_id=session.file_root_id,
        relative_path=entry.relative_path,
    )
    if path.stat().st_size > TELEGRAM_MAX_DOCUMENT_BYTES:
        raise ValueError("FILE_TOO_LARGE")

    await bot.send_document(
        chat_id=chat_id,
        document=FSInputFile(path, filename=path.name),
    )
    return entry.name


def _collect_usage_stats(vault_path: Path | str, user_id: int) -> _UsageStats:
    session = SessionStore(vault_path)
    today_entries = [
        entry
        for entry in session.get_today(user_id)
        if entry.get("type") in CAPTURE_TYPES
    ]

    today_counts = {entry_type: 0 for entry_type in CAPTURE_TYPES}
    for entry in today_entries:
        entry_type = str(entry.get("type"))
        today_counts[entry_type] += 1

    week_counts = {entry_type: 0 for entry_type in CAPTURE_TYPES}
    raw_week_counts = session.get_stats(user_id, days=7)
    for entry_type in CAPTURE_TYPES:
        week_counts[entry_type] = int(raw_week_counts.get(entry_type, 0))

    return {
        "today_counts": today_counts,
        "today_total": sum(today_counts.values()),
        "week_counts": week_counts,
        "week_total": sum(week_counts.values()),
    }


def _format_counts(counts: dict[str, int]) -> list[str]:
    return [
        f"{emoji} {label}: {counts[entry_type]}"
        for entry_type, emoji, label in TYPE_META
    ]
def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=callback_data)
                for text, callback_data in row
            ]
            for row in rows
        ]
    )
