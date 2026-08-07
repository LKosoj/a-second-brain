"""Bot FSM states."""

from aiogram.fsm.state import State, StatesGroup


class DoCommandState(StatesGroup):
    """States for /do command flow."""

    waiting_for_input = State()  # Waiting for voice or text after /do


class WhyCommandState(StatesGroup):
    """States for /why command flow (задача L, ТЗ 7.3)."""

    # Waiting for a text query after /why, or for a button tap disambiguating
    # two close candidates (see bot/handlers/why.py's why_choices FSM data).
    waiting_for_input = State()


class BriefCommandState(StatesGroup):
    """States for the dashboard's "Собрать бриф" sub-flow (задача L, ТЗ 7.6)."""

    # Waiting for the query text after a brief-type button was tapped.
    waiting_for_query = State()
