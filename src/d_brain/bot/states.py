"""Bot FSM states."""

import time

from aiogram.fsm.state import State, StatesGroup

# How long /why and the brief flow keep waiting for the owner's next message.
# Without a limit a forgotten prompt swallowed the next unrelated note.
WAIT_STATE_TTL_SECONDS = 600


def wait_started_at() -> float:
    """Timestamp to store in FSM data when a waiting state is entered."""
    return time.time()


def wait_expired(entered_at: object, *, now: float | None = None) -> bool:
    """True once a waiting state is older than the TTL.

    A missing or malformed stamp counts as "not expired": dropping a live
    request is worse than keeping a stale one for a little longer.
    """
    if isinstance(entered_at, bool) or not isinstance(entered_at, int | float):
        return False
    current = time.time() if now is None else now
    return current - entered_at > WAIT_STATE_TTL_SECONDS


class DoCommandState(StatesGroup):
    """States for /do command flow."""

    waiting_for_input = State()  # Waiting for voice or text after /do


class WhyCommandState(StatesGroup):
    """States for /why command flow (задача L, ТЗ 7.3)."""

    # Waiting for a text query after /why, or for a button tap disambiguating
    # two close candidates (see bot/handlers/why.py's why_choices FSM data).
    waiting_for_input = State()


class FindCommandState(StatesGroup):
    """States for /find command flow (G6)."""

    waiting_for_input = State()  # Waiting for a text query after bare /find


class BriefCommandState(StatesGroup):
    """States for the dashboard's "Собрать бриф" sub-flow (задача L, ТЗ 7.6)."""

    # Waiting for the query text after a brief-type button was tapped.
    waiting_for_query = State()
