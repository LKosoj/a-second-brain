"""Deterministic "напомни ..." reminder parsing and storage (аудит, пункт 13).

No external services and no NLP: a fixed set of regexes recognizes a small
number of Russian time expressions (relative offsets, weekdays, explicit
dates, plain clock times) and returns ``None`` for anything else.

Storage is a plain append-only JSONL journal at
``vault/.session/reminders.jsonl``, mirroring the convention already used by
``SessionStore.append`` and ``decisions_queue.py``'s response journal: one
JSON object per line. ``add_reminder`` only ever appends; ``mark_sent``
rewrites the whole (small) file to reflect the new ``sent_at`` -- the
simpler of the two options the task allows for marking an entry sent -- but
does it via a tempfile-in-the-same-directory + ``os.replace``, so a crash
mid-write leaves either the old or the new full file, never a half-written
one.

Every read-modify-write (and the append) runs under ``_reminders_lock``, a
``flock`` on a sibling ``.lock`` file: without it a reminder appended between
``mark_sent``'s read and its ``os.replace`` was silently dropped.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from d_brain.clock import owner_now
from d_brain.services.atomic_io import atomic_write_file_text

logger = logging.getLogger(__name__)

REMINDERS_RELATIVE_PATH = Path(".session") / "reminders.jsonl"


# Only the imperative ("напомни"), infinitive ("напомнить") and colloquial
# ("напомни-ка") forms trigger a reminder. The ``\b`` right after the
# optional suffixes is what excludes past-tense forms like "напомнила"/
# "напомнили"/"напомнил" -- those continue the word instead of ending it, so
# there is no boundary there and the regex does not match.
_TRIGGER_RE = re.compile(r"^напомни(?:ть)?(?:-ка)?\b\s*", re.IGNORECASE)
# Everyday phrasing often puts a comma/colon and "пожалуйста" right after the
# trigger ("напомни, пожалуйста, завтра ..."); strip that filler so the time
# expression that follows is still recognized.
_LEADING_FILLER_RE = re.compile(
    r"^(?:[,:]\s*|пожалуйста,?\s*|мне\b,?\s*)*", re.IGNORECASE
)

_RELATIVE_RE = re.compile(r"^через\s+(\d+)\s+(\S+)\s*", re.IGNORECASE)

_DAY_OFFSETS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_DAY_WORD_RE = re.compile(
    r"^(сегодня|завтра|послезавтра)\b\s*(?:в\s+(\d{1,2})(?::(\d{2}))?)?\s*",
    re.IGNORECASE,
)

_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_DATE_RE = re.compile(
    r"^(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\b\s*(?:в\s+(\d{1,2})(?::(\d{2}))?)?\s*",
    re.IGNORECASE,
)

_WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "четверг": 3,
    "пятницу": 4,
    "субботу": 5,
    "воскресенье": 6,
}
_WEEKDAY_RE = re.compile(
    r"^во?\s+(" + "|".join(_WEEKDAYS) + r")\b\s*(?:в\s+(\d{1,2})(?::(\d{2}))?)?\s*",
    re.IGNORECASE,
)

_CLOCK_RE = re.compile(r"^в\s+(\d{1,2})(?::(\d{2}))?\b\s*", re.IGNORECASE)

_DEFAULT_HOUR = 9


def is_reminder_request(text: str) -> bool:
    """True if ``text`` opens with a reminder trigger word.

    Matches "напомни", "напомнить" and "напомни-ка" (any casing), but not
    past-tense forms like "напомнила"/"напомнили"/"напомнил" -- see
    ``_TRIGGER_RE`` for why. Used both by ``parse_reminder`` below and by the
    bot router's message filter, so the two never drift apart.
    """
    return _TRIGGER_RE.match(text.strip()) is not None


@dataclass
class Reminder:
    """One parsed reminder: when it is due, and the task text for it."""

    due_at: datetime
    text: str


@dataclass
class ReminderRecord:
    """One stored line from ``reminders.jsonl``."""

    id: str
    chat_id: int
    due_at: datetime
    text: str
    created_at: datetime
    sent_at: datetime | None


def _clock(hour_group: str | None, minute_group: str | None) -> tuple[int, int]:
    """Read an optional "в HH(:MM)" capture, defaulting to _DEFAULT_HOUR:00."""
    hour = int(hour_group) if hour_group else _DEFAULT_HOUR
    minute = int(minute_group) if minute_group else 0
    return hour, minute


def parse_reminder(text: str, now: datetime) -> Reminder | None:
    """Parse a Russian "напомни ..." phrase into a due time and task text.

    Returns ``None`` when the phrase does not start with a reminder trigger
    (see ``is_reminder_request``), no supported time expression follows it,
    that time expression is out of range (e.g. "в 25:70"), or nothing is
    left to remind about once the time expression is removed. Recognized
    shapes: "через N минут|часов|дней", "сегодня/завтра/послезавтра
    [в HH(:MM)]", "D месяц [в HH(:MM)]", "в день_недели [в HH(:MM)]", and a
    bare "в HH(:MM)" (rolled to tomorrow if that time has already passed
    today).
    """
    trigger_match = _TRIGGER_RE.match(text.strip())
    if trigger_match is None:
        return None
    remainder = text.strip()[trigger_match.end() :]
    remainder = _LEADING_FILLER_RE.sub("", remainder, count=1)

    for pattern, parser in _PARSERS:
        match = pattern.match(remainder)
        if match is None:
            continue
        result = parser(remainder, match, now)
        if result is None or not result.text:
            return None
        return result

    return None


def _parse_relative(
    remainder: str, match: re.Match[str], now: datetime
) -> Reminder | None:
    amount = int(match.group(1))
    unit_word = match.group(2).lower()
    if unit_word.startswith("минут"):
        delta = timedelta(minutes=amount)
    elif unit_word.startswith("час"):
        delta = timedelta(hours=amount)
    elif unit_word.startswith("д"):
        delta = timedelta(days=amount)
    else:
        return None
    return Reminder(due_at=now + delta, text=remainder[match.end() :].strip())


def _parse_day_word(
    remainder: str, match: re.Match[str], now: datetime
) -> Reminder | None:
    offset = _DAY_OFFSETS[match.group(1).lower()]
    hour, minute = _clock(match.group(2), match.group(3))
    due_date = (now + timedelta(days=offset)).date()
    try:
        due_at = datetime(
            due_date.year, due_date.month, due_date.day, hour, minute, tzinfo=now.tzinfo
        )
    except ValueError:
        return None
    # "сегодня" (with or without a time) can already lie in the past, e.g.
    # "напомни сегодня купить молоко" said at 15:00 defaults to 09:00. The
    # ticker would fire it on its next tick, which is not what was asked, and
    # rolling it to tomorrow would contradict the explicit "сегодня" -- so
    # ask for a clearer time instead.
    if due_at <= now:
        return None
    return Reminder(due_at=due_at, text=remainder[match.end() :].strip())


def _parse_date(remainder: str, match: re.Match[str], now: datetime) -> Reminder | None:
    day = int(match.group(1))
    month = _MONTHS[match.group(2).lower()]
    hour, minute = _clock(match.group(3), match.group(4))
    try:
        due_at = datetime(now.year, month, day, hour, minute, tzinfo=now.tzinfo)
    except ValueError:
        return None
    if due_at <= now:
        due_at = due_at.replace(year=now.year + 1)
    return Reminder(due_at=due_at, text=remainder[match.end() :].strip())


def _parse_weekday(
    remainder: str, match: re.Match[str], now: datetime
) -> Reminder | None:
    target_weekday = _WEEKDAYS[match.group(1).lower()]
    hour, minute = _clock(match.group(2), match.group(3))
    days_ahead = (target_weekday - now.weekday()) % 7 or 7
    due_date = (now + timedelta(days=days_ahead)).date()
    try:
        due_at = datetime(
            due_date.year, due_date.month, due_date.day, hour, minute, tzinfo=now.tzinfo
        )
    except ValueError:
        return None
    return Reminder(due_at=due_at, text=remainder[match.end() :].strip())


def _parse_clock(
    remainder: str, match: re.Match[str], now: datetime
) -> Reminder | None:
    hour, minute = _clock(match.group(1), match.group(2))
    try:
        due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if due_at <= now:
        due_at += timedelta(days=1)
    return Reminder(due_at=due_at, text=remainder[match.end() :].strip())


_PARSERS: list[
    tuple[re.Pattern[str], Callable[[str, re.Match[str], datetime], Reminder | None]]
] = [
    (_RELATIVE_RE, _parse_relative),
    (_DAY_WORD_RE, _parse_day_word),
    (_DATE_RE, _parse_date),
    (_WEEKDAY_RE, _parse_weekday),
    (_CLOCK_RE, _parse_clock),
]


def _reminders_path(vault_path: Path | str) -> Path:
    return Path(vault_path) / REMINDERS_RELATIVE_PATH


def _record_to_dict(record: ReminderRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "chat_id": record.chat_id,
        "due_at": record.due_at.isoformat(),
        "text": record.text,
        "created_at": record.created_at.isoformat(),
        "sent_at": record.sent_at.isoformat() if record.sent_at else None,
    }


def _record_from_dict(raw: dict[str, object]) -> ReminderRecord:
    sent_at_raw = raw.get("sent_at")
    return ReminderRecord(
        id=str(raw["id"]),
        chat_id=int(raw["chat_id"]),  # type: ignore[call-overload]
        due_at=datetime.fromisoformat(str(raw["due_at"])),
        text=str(raw["text"]),
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        sent_at=datetime.fromisoformat(str(sent_at_raw)) if sent_at_raw else None,
    )


def _read_all(path: Path) -> list[ReminderRecord]:
    """Read every reminder line, skipping and logging any corrupted ones."""
    if not path.exists():
        return []
    records: list[ReminderRecord] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(_record_from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
            logger.warning(
                "Skipping corrupted reminder entry in %s at line %s",
                path,
                line_number,
            )
    return records


@contextmanager
def _reminders_lock(path: Path) -> Iterator[None]:
    """Exclusive lock for the journal, taken on a separate file.

    The journal itself cannot carry the lock: ``os.replace`` swaps its inode,
    so a lock held on the old file would no longer protect anything.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def add_reminder(
    vault_path: Path | str,
    chat_id: int,
    reminder: Reminder,
    *,
    now: datetime | None = None,
) -> ReminderRecord:
    """Append one pending reminder to reminders.jsonl, creating .session if needed."""
    path = _reminders_path(vault_path)
    record = ReminderRecord(
        id=str(uuid.uuid4()),
        chat_id=chat_id,
        due_at=reminder.due_at,
        text=reminder.text,
        created_at=now or owner_now(),
        sent_at=None,
    )
    with _reminders_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(_record_to_dict(record), ensure_ascii=False) + "\n"
            )
    return record


def list_pending(vault_path: Path | str) -> list[ReminderRecord]:
    """Return every reminder not yet marked sent."""
    return [
        record
        for record in _read_all(_reminders_path(vault_path))
        if record.sent_at is None
    ]


def _write_all(path: Path, records: list[ReminderRecord]) -> None:
    """Rewrite the whole journal atomically (mode and group are kept), so a
    crash mid-write never leaves a half-written file.
    """
    atomic_write_file_text(
        path,
        "".join(
            json.dumps(_record_to_dict(record), ensure_ascii=False) + "\n"
            for record in records
        ),
    )


def is_pending_at(
    vault_path: Path | str, reminder_id: str, due_at: datetime
) -> bool:
    """True while the reminder exists, is unsent and still has this ``due_at``.

    Read under the lock so a cancel/postpone that already landed is seen; the
    ticker calls it right before sending a reminder it listed earlier.
    """
    path = _reminders_path(vault_path)
    with _reminders_lock(path):
        return any(
            record.id == reminder_id
            and record.sent_at is None
            and record.due_at == due_at
            for record in _read_all(path)
        )


def mark_sent(
    vault_path: Path | str,
    reminder_id: str,
    *,
    now: datetime | None = None,
    due_at: datetime | None = None,
) -> None:
    """Mark one reminder sent by atomically rewriting the (small) journal.

    With ``due_at`` only a record that still has that time is marked: one
    postponed after it was listed has not been delivered at its new time.
    """
    path = _reminders_path(vault_path)
    sent_at = now or owner_now()
    with _reminders_lock(path):
        records = _read_all(path)
        updated = False
        for record in records:
            if record.id == reminder_id and (due_at is None or record.due_at == due_at):
                record.sent_at = sent_at
                updated = True
        if not updated:
            return
        _write_all(path, records)


def cancel_reminder(vault_path: Path | str, reminder_id: str) -> bool:
    """Drop one reminder from the journal; False when the id is unknown.

    Removed rather than marked sent, so a cancelled reminder is never
    mistaken for a delivered one.
    """
    path = _reminders_path(vault_path)
    with _reminders_lock(path):
        records = _read_all(path)
        kept = [record for record in records if record.id != reminder_id]
        if len(kept) == len(records):
            return False
        _write_all(path, kept)
    return True


def postpone_reminder(
    vault_path: Path | str,
    reminder_id: str,
    *,
    now: datetime | None = None,
) -> ReminderRecord | None:
    """Push a pending reminder back by a day, keeping its time of day.

    A reminder still in the future moves one day past its own due date, so the
    button never makes it fire earlier. An overdue one moves to tomorrow. The
    clock time is read in ``now``'s timezone. None when the id is unknown or
    the reminder was already sent.
    """
    path = _reminders_path(vault_path)
    now = now or owner_now()
    with _reminders_lock(path):
        records = _read_all(path)
        for record in records:
            if record.id != reminder_id:
                continue
            if record.sent_at is not None:
                return None
            local = record.due_at.astimezone(now.tzinfo)
            base = local.date() if record.due_at > now else now.date()
            record.due_at = datetime.combine(
                base + timedelta(days=1), local.timetz()
            )
            _write_all(path, records)
            return record
    return None
