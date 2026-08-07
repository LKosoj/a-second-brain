"""Handler for /why command - provenance for one compiled page (задача L, ТЗ 7.3/7.4).

Pure read-only lookup (``compiled_why.build_why``/``build_why_for_path``
never call a model and never write to the vault). Input is text only -- no
voice, per ТЗ -- and the flow is modeled directly on ``bot/handlers/do.py``.
When the target is ambiguous, the owner is asked to pick -- one button per
candidate -- rather than have the bot guess (ТЗ 7.3 "не гадай, спроси").
"""

import hashlib
import logging
from pathlib import Path
from typing import cast

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from d_brain.bot.replies import answer_rich_text, answer_text
from d_brain.bot.states import WhyCommandState
from d_brain.config import get_settings
from d_brain.services.compiled_why import (
    WhyChoice,
    WhyResult,
    build_why,
    build_why_for_path,
)
from d_brain.services.qmd import QmdService

router = Router(name="why")
logger = logging.getLogger(__name__)


async def start_why_flow(message: Message, state: FSMContext) -> None:
    """Prompt the user for the next /why query."""
    await state.set_state(WhyCommandState.waiting_for_input)
    await answer_text(
        message,
        "🔍 **Почему страница так говорит?**\n\n"
        "Отправь текстом название решения, темы, проекта или страницы.\n"
        "Повтори `/why` или отправь `-`, чтобы отменить.",
    )


@router.message(Command("why"))
async def cmd_why(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Handle /why command."""
    current_state = await state.get_state()

    if (
        not command.args
        and current_state == WhyCommandState.waiting_for_input.state
    ):
        await state.clear()
        await answer_text(message, "❌ **/why отменён**")
        return

    if command.args:
        await state.clear()
        await process_why_request(message, command.args, state)
        return

    await start_why_flow(message, state)


@router.message(WhyCommandState.waiting_for_input)
async def handle_why_input(message: Message, state: FSMContext) -> None:
    """Handle input after /why command -- text only (ТЗ: no voice for /why)."""
    if not message.text:
        await answer_text(
            message,
            "❌ `/why` понимает только текст. "
            "Отправь текстовый запрос или `-` для отмены.",
        )
        return  # stay in waiting_for_input -- owner can just retry with text

    text = message.text.strip()
    if text == "-":
        await state.clear()
        await answer_text(message, "❌ **/why отменён**")
        return

    await state.clear()
    await process_why_request(message, text, state)


def why_choices_token(rel_paths: list[str]) -> str:
    """Short fingerprint of one disambiguation set (code review).

    ``callback_data`` cannot carry the paths themselves (Telegram's 64-byte
    limit), and an index alone is not enough: FSM state holds exactly one
    ``why_choices`` list, so a second ambiguous /why overwrites the first
    one's. Tapping the older message's button then silently resolved that
    index against the *newer* query's candidates and answered about a page
    the owner never picked -- an in-range index, so the existing bounds
    check could not catch it. The token makes that mismatch visible.
    """
    digest = hashlib.sha256("\n".join(rel_paths).encode("utf-8"))
    return digest.hexdigest()[:8]


def build_why_choice_keyboard(
    choices: tuple[WhyChoice, ...], token: str
) -> InlineKeyboardMarkup:
    """One disambiguation button per candidate -- ``callback_data`` encodes
    the disambiguation set's ``token`` plus an index.

    The choices themselves live in FSM state (see ``process_why_request``),
    so a long vault-relative path never has to fit inside Telegram's
    64-byte ``callback_data`` limit.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{choice.title} ({choice.domain})",
                    callback_data=f"why:choice:{token}:{index}",
                )
            ]
            for index, choice in enumerate(choices)
        ]
    )


def build_why_ambiguous_text(query: str, count: int) -> str:
    """Text shown when the candidates are too close to guess (ТЗ 7.3).

    Counts instead of saying "две" (code review): an exact slug hit lands in
    as many of the six ``COMPILED_BRIEFING_DOMAINS`` as happen to use that
    slug, so the number of buttons is not always two. Phrased with the
    number last so no Russian declension has to be picked at runtime.
    """
    return f"🤔 По запросу «{query}» нашлось похожих страниц: {count} — уточни:"


async def process_why_request(message: Message, query: str, state: FSMContext) -> None:
    """Resolve one /why query and render the answer (задача L, ТЗ 7.3/7.4)."""
    settings = get_settings()
    vault_path = Path(settings.vault_path)
    try:
        outcome = build_why(vault_path, query)
    except Exception:
        # ``build_why`` re-reads every ``compiled/**`` page, which the
        # nightly pass can be rewriting or archiving underneath it. Letting
        # that escape leaves the owner with no reply at all -- the flow this
        # module is modeled on (``do.py``) always answers, even when the work
        # itself crashed.
        logger.exception("Failed to build /why answer for %r", query)
        await answer_text(message, "❌ Не удалось собрать ответ на /why.")
        return

    if outcome.status == "not_found":
        await answer_text(message, f"🤷 Не нашёл страницу по запросу «{query}».")
        return

    if outcome.status == "ambiguous":
        token = why_choices_token([choice.rel_path for choice in outcome.choices])
        await state.update_data(
            why_choices=[
                {"rel_path": choice.rel_path, "title": choice.title}
                for choice in outcome.choices
            ],
            why_choices_token=token,
        )
        await state.set_state(WhyCommandState.waiting_for_input)
        await answer_text(
            message,
            build_why_ambiguous_text(query, len(outcome.choices)),
            reply_markup=build_why_choice_keyboard(outcome.choices, token),
        )
        return

    assert outcome.result is not None
    await deliver_why_result(message, vault_path, outcome.result)


async def deliver_why_result(
    message: Message, vault_path: Path, result: WhyResult
) -> None:
    """Send the /why answer and touch the resolved page -- the same
    best-effort ``QmdService.touch_notes`` promotion ``run_compiled_brief.py``
    already applies to a brief's source page (задача L)."""
    try:
        QmdService(vault_path).touch_notes([result.rel_path])
    except Exception as exc:  # pragma: no cover - best-effort touch
        logger.warning("Failed to touch /why source %s: %s", result.rel_path, exc)
    try:
        await answer_rich_text(message, result.markdown)
    except Exception:
        # The parity this module claims in its own header -- "modeled
        # directly on ``do.py``" -- was missing exactly here (code review):
        # ``do.py`` wraps its final reply, this did not. ``answer_rich_text``
        # only falls back on ``TelegramBadRequest`` (a payload Telegram
        # refuses); a network error or a 5xx on the send itself escaped into
        # the dispatcher, which has no error handler, so both entry points
        # -- the direct query and the disambiguation button -- ended with the
        # owner's FSM already cleared and no answer at all. Nothing left to
        # tell them with, so this only records it.
        logger.exception("Failed to send /why answer for %s", result.rel_path)


@router.callback_query(F.data.startswith("why:choice:"))
async def handle_why_choice(query: CallbackQuery, state: FSMContext) -> None:
    """Handle the owner picking one of the ambiguous /why candidates."""
    if query.message is None:
        await query.answer()
        return
    message = cast(Message, query.message)

    data = await state.get_data()
    raw_choices = data.get("why_choices") or []
    token, _, raw_index = (query.data or "").removeprefix("why:choice:").rpartition(":")
    try:
        index = int(raw_index)
    except ValueError:
        index = -1

    # The token alone is not enough (code review): ``start_do_flow`` and
    # ``start_brief_flow`` switch state without clearing data, so the
    # ``why_choices_token`` of an abandoned ambiguous /why outlives its
    # flow. Tapping that old button then matched, fell through to the
    # ``state.clear()`` below, and wiped the /do or /brief the owner had
    # since started -- their next message went to the plain capture
    # handlers instead, with nothing said about it either way.
    if await state.get_state() != WhyCommandState.waiting_for_input.state:
        await query.answer("Устарело, отправь /why ещё раз.", show_alert=True)
        return

    # A stale button from an earlier ambiguous /why carries an in-range
    # index against a different candidate set -- see ``why_choices_token``.
    if token != (data.get("why_choices_token") or ""):
        await query.answer("Устарело, отправь /why ещё раз.", show_alert=True)
        return

    if index < 0 or index >= len(raw_choices):
        await query.answer("Устарело, отправь /why ещё раз.", show_alert=True)
        return

    await state.clear()
    rel_path = str(raw_choices[index]["rel_path"])
    settings = get_settings()
    vault_path = Path(settings.vault_path)
    # Acknowledged before the read, not after (code review): the read below
    # can block or blow up, and until this call returns the button keeps its
    # spinner. ``query.answer()`` only closes that spinner -- the answer
    # itself is a separate message, so nothing is lost by conceding it early.
    await query.answer()
    try:
        result = build_why_for_path(vault_path, rel_path)
    except Exception:
        # Same guard, for the same reason, as ``process_why_request``'s
        # around ``build_why``: both re-read ``compiled/**`` pages the
        # nightly pass can be rewriting or archiving underneath them. This
        # one had none, and since the FSM was already cleared two lines up,
        # the owner was left with a dead button and no reply at all -- the
        # one outcome ТЗ 7.3's "не гадай, спроси" flow must never end in.
        logger.exception("Failed to build /why answer for %s", rel_path)
        await answer_text(message, "❌ Не удалось собрать ответ на /why.")
        return
    if result is None:
        await answer_text(message, "🤷 Страница больше не существует.")
        return
    await deliver_why_result(message, vault_path, result)
