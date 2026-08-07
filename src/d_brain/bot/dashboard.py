"""Inline dashboard rendering and state for the Telegram bot."""

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from d_brain.bot.replies import send_text
from d_brain.services.compiled_briefs import BRIEF_TYPES
from d_brain.services.compiled_enrich_report import WeeklyReview, collect_weekly_review
from d_brain.services.decisions_queue import (
    QueueItem,
    action_button_specs,
    format_queue_item_line,
    list_queue_items,
    queue_item_fingerprint,
    queue_kind_label,
)
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

DashboardScreen = Literal[
    "home",
    "stats",
    "file_roots",
    "files",
    "queue",
    "queue_item",
    "brief_type",
    "weekly",
]
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
WEEKLY_QUEUE_PREVIEW_SIZE = 5

_BRIEF_TYPE_BUTTON_LABELS = {
    "decision": "Решение",
    "topic": "Тема",
    "project": "Проект",
}


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
    queue_page: int = 0
    queue_items: list[QueueItem] = field(default_factory=list)
    weekly_review: WeeklyReview | None = None


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
        "/why — почему компилированная страница так говорит\n"
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
            [
                ("📰 Дайджест", "menu:digest"),
                ("🗂 Очередь", "menu:queue"),
            ],
            [
                ("📝 Бриф", "menu:brief"),
                ("📅 Сводка недели", "menu:weekly"),
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


def build_queue_text(
    *,
    items: list[QueueItem],
    page: int,
    total_pages: int,
    total_items: int,
) -> str:
    """Build one page of the decisions queue list (задача L, ТЗ 7.2)."""
    lines = ["**Очередь решений**", ""]
    if total_items == 0:
        lines.append("Очередь пуста — открытых пунктов нет.")
        return "\n".join(lines)
    if total_pages > 1:
        lines.append(
            f"_Страница {page + 1} из {total_pages}, всего пунктов: {total_items}_"
        )
        lines.append("")
    lines.extend(format_queue_item_line(entry) for entry in items)
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
            pagination_row.append(("⬅️", f"menu:queuepage:{page - 1}"))
        pagination_row.append((f"{start}-{end}/{total_items}", "menu:noop"))
        if page + 1 < total_pages:
            pagination_row.append(("➡️", f"menu:queuepage:{page + 1}"))
        rows.append(pagination_row)

    # "Обновить" redraws the page the owner is on, so it reuses the
    # pagination callback rather than the bare ``menu:queue`` the *entry
    # points* (home, weekly review) use -- that one always opens at page 0,
    # which silently threw an owner working through page 3 back to the top
    # (code review).
    rows.append([("🔄 Обновить", f"menu:queuepage:{page}"), ("🏠 Домой", "menu:home")])
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
        for action_id, label in action_button_specs(item)
    ]
    # Back to the page this item was opened from, not to page 0 -- same
    # reason as the "Обновить" button above.
    rows.append(
        [("⬅️ К очереди", f"menu:queuepage:{page}"), ("🏠 Домой", "menu:home")]
    )
    return _keyboard(rows)


def build_brief_type_text() -> str:
    """Build the brief type-picker screen text (задача L, ТЗ 7.6)."""
    return (
        "**Собрать бриф**\n\n"
        "Выбери тип брифа, затем отправь текстом запрос "
        "(название решения, темы или проекта)."
    )


def build_brief_type_keyboard() -> InlineKeyboardMarkup:
    """Build the 3 brief-type buttons (decision/topic/project, ТЗ 7.6)."""
    rows: list[list[tuple[str, str]]] = [
        [(_BRIEF_TYPE_BUTTON_LABELS[brief_type], f"menu:brieftype:{brief_type}")]
        for brief_type in BRIEF_TYPES
    ]
    rows.append([("🏠 Домой", "menu:home")])
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
    all_items = list_queue_items(Path(vault_path))
    total_items = len(all_items)
    total_pages = max(1, (total_items + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * QUEUE_PAGE_SIZE
    end = start + QUEUE_PAGE_SIZE
    page_items = all_items[start:end]

    session = get_dashboard_session(chat_id)
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


async def render_queue_item(
    bot: Bot,
    *,
    chat_id: int,
    index: int,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> QueueItem | None:
    """Render one queue item's detail + action view.

    ``index`` refers to ``session.queue_items`` (the *current page's* slice,
    same convention as ``send_browser_file``'s ``entry_index``) -- returns
    the resolved item for the caller to act on, or ``None`` if the index no
    longer resolves (the queue changed since the list was last rendered).
    """
    session = get_dashboard_session(chat_id)
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
        "**Сводка недели**",
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
    rows.append([("🗂 Очередь целиком", "menu:queue")])
    rows.append([("🔄 Обновить", "menu:weekly"), ("🏠 Домой", "menu:home")])
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
    review = collect_weekly_review(Path(vault_path), date.today())

    session = get_dashboard_session(chat_id)
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


async def render_brief_type(
    bot: Bot,
    *,
    chat_id: int,
    preferred_message_id: int | None = None,
    notice: str | None = None,
) -> None:
    """Render the brief type-picker screen (задача L, ТЗ 7.6)."""
    session = get_dashboard_session(chat_id)
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
