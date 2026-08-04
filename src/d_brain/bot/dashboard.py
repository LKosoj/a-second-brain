"""Inline dashboard rendering and state for the Telegram bot."""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from d_brain.bot.replies import send_text
from d_brain.services.file_browser import (
    TELEGRAM_MAX_DOCUMENT_BYTES,
    BrowserEntry,
    BrowserRoot,
    FileBrowserService,
)
from d_brain.services.session import SessionStore
from d_brain.services.telegram_markup import (
    markdown_to_markdown_v2,
    normalize_markdown_input,
)

DashboardScreen = Literal["home", "stats", "file_roots", "files"]
CAPTURE_TYPES: tuple[str, ...] = ("voice", "text", "photo", "document", "forward")
TYPE_META: tuple[tuple[str, str, str], ...] = (
    ("voice", "🎤", "Голосовые"),
    ("text", "💬", "Текстовые"),
    ("photo", "📷", "Фото"),
    ("document", "📄", "Документы"),
    ("forward", "↩️", "Пересланные"),
)
FILES_PAGE_SIZE = 8


class _UsageStats(TypedDict):
    today_counts: dict[str, int]
    today_total: int
    week_counts: dict[str, int]
    week_total: int


@dataclass(slots=True)
class DashboardSession:
    """Ephemeral UI state bound to one chat."""

    chat_id: int
    dashboard_message_id: int | None = None
    current_screen: DashboardScreen = "home"
    file_root_id: str | None = None
    file_current_dir: str = ""
    file_page: int = 0
    file_entries: list[BrowserEntry] = field(default_factory=list)


_SESSIONS: dict[int, DashboardSession] = {}


def get_dashboard_session(chat_id: int) -> DashboardSession:
    """Return a mutable dashboard session for one chat."""
    session = _SESSIONS.get(chat_id)
    if session is None:
        session = DashboardSession(chat_id=chat_id)
        _SESSIONS[chat_id] = session
    return session


def clear_dashboard_session(chat_id: int) -> None:
    """Forget dashboard state for one chat."""
    _SESSIONS.pop(chat_id, None)


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
        "/help — эта справка\n"
        "/stats — статистика по записям\n"
        "/files — файловый браузер vault\n"
        "/process — быстрый preview без записи\n"
        "/process_full — полный цикл с записью\n"
        "\n"
        "_/start теперь только показывает справку, "
        "а само меню живёт отдельно в /menu._"
    )


def build_home_text(vault_path: Path | str, user_id: int) -> str:
    """Build the dashboard home summary."""
    stats = _collect_usage_stats(vault_path, user_id)
    today = date.today().isoformat()

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

    return (
        "**d-brain**\n\n"
        f"**Дата:** {today}\n"
        f"{today_line}\n"
        f"**За 7 дней:** {stats['week_total']}\n\n"
        "Выбери действие ниже.\n"
        "_/help — команды и правила использования._"
    )


def build_stats_text(vault_path: Path | str, user_id: int) -> str:
    """Build a detailed stats view for command and dashboard screens."""
    stats = _collect_usage_stats(vault_path, user_id)
    today = date.today().isoformat()

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
            [
                ("📊 Статистика", "menu:stats"),
                ("📁 Файлы", "menu:files"),
            ],
            [
                ("🔎 Превью", "menu:process"),
                ("⚙️ Обработать", "menu:processfull"),
            ],
            [
                ("✨ Запрос", "menu:do"),
                ("❌ Закрыть меню", "menu:close"),
            ],
        ]
    )


def build_stats_keyboard() -> InlineKeyboardMarkup:
    """Build the stats screen keyboard."""
    return _keyboard(
        [
            [("🔄 Обновить", "menu:stats")],
            [("🏠 Домой", "menu:home")],
        ]
    )


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
    rows.append([("🏠 Домой", "menu:home")])
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


def build_file_directory_keyboard(
    *,
    current_dir: str,
    page_entries: list[BrowserEntry],
    page: int,
    total_entries: int,
) -> InlineKeyboardMarkup:
    """Build the paginated directory keyboard."""
    rows: list[list[tuple[str, str]]] = []
    if current_dir:
        rows.append([("⬆️ Вверх", "menu:filesup")])

    for index, entry in enumerate(page_entries):
        prefix = "📁" if entry.is_directory else "📄"
        rows.append(
            [
                (
                    f"{prefix} {entry.name}",
                    f"menu:filesentry:{index}",
                )
            ]
        )

    total_pages = max(1, (total_entries + FILES_PAGE_SIZE - 1) // FILES_PAGE_SIZE)
    if total_pages > 1:
        start = page * FILES_PAGE_SIZE + 1
        end = start + len(page_entries) - 1
        pagination_row: list[tuple[str, str]] = []
        if page > 0:
            pagination_row.append(("⬅️", f"menu:filespage:{page - 1}"))
        pagination_row.append((f"{start}-{end}/{total_entries}", "menu:noop"))
        if page + 1 < total_pages:
            pagination_row.append(("➡️", f"menu:filespage:{page + 1}"))
        rows.append(pagination_row)

    rows.extend(
        [
            [("🔄 Обновить", "menu:filesrefresh")],
            [
                ("🗂 Разделы", "menu:filesroots"),
                ("🏠 Домой", "menu:home"),
            ],
        ]
    )
    return _keyboard(rows)


async def render_dashboard(
    bot: Bot,
    *,
    chat_id: int,
    session: DashboardSession,
    text: str,
    keyboard: InlineKeyboardMarkup,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render one dashboard screen by editing or sending a message."""
    target_message_id = preferred_message_id or session.dashboard_message_id
    payload_text = text
    if notice:
        payload_text = f"ℹ️ _{notice}_\n\n{text}"
    payload_markdown = normalize_markdown_input(payload_text)
    payload_message = markdown_to_markdown_v2(payload_markdown)

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
    session = get_dashboard_session(chat_id)
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
    session = get_dashboard_session(chat_id)
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
    session = get_dashboard_session(chat_id)
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
    root, normalized_dir, entries = browser.list_entries(
        root_id=root_id,
        current_dir=current_dir,
    )
    total_pages = max(1, (len(entries) + FILES_PAGE_SIZE - 1) // FILES_PAGE_SIZE)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * FILES_PAGE_SIZE
    end = (safe_page + 1) * FILES_PAGE_SIZE
    page_entries = entries[start:end]

    session = get_dashboard_session(chat_id)
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


async def close_dashboard(
    bot: Bot,
    *,
    chat_id: int,
    preferred_message_id: int | None = None,
) -> None:
    """Delete the current dashboard message if possible and clear session state."""
    session = get_dashboard_session(chat_id)
    target_message_id = preferred_message_id or session.dashboard_message_id
    if target_message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=target_message_id)
        except Exception:  # noqa: BLE001
            pass
    clear_dashboard_session(chat_id)


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
