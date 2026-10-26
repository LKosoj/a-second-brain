"""Owner's calendar day: one place that decides what "today" and "now" mean.

``TIMEZONE`` (an IANA name such as ``Europe/Moscow``) sets the zone of the
owner's day. Without it every function here behaves exactly like the plain
``date.today()`` / ``datetime.now().astimezone()`` calls it replaces: the zone
of the operating system, including its daylight-saving rules.

Only values that become a calendar date, an ``HH:MM`` or a folder name belong
here. Technical timestamps (heartbeats, durations, mtimes) stay as they are.
This module must not import the rest of the project: ``config`` imports it.
"""

import os
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_configured = False
_owner_zone: tzinfo | None = None


def resolve_timezone(name: str) -> tzinfo | None:
    """Return the zone for ``name``; ``None`` (system zone) for an empty name.

    Raises ``ValueError`` for a name that is not a known IANA zone.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            f"TIMEZONE must be an IANA name such as Europe/Moscow, got {name!r}"
        ) from exc


def set_owner_timezone(name: str) -> None:
    """Fix the owner's zone; called by ``get_settings()`` once at start."""
    global _configured, _owner_zone
    _owner_zone = resolve_timezone(name)
    _configured = True


def _reset_for_tests() -> None:
    global _configured, _owner_zone
    _configured = False
    _owner_zone = None


def owner_tz() -> tzinfo | None:
    """Owner's zone, ``None`` when the system zone is used."""
    global _configured, _owner_zone
    if not _configured:
        # Scripts that never called get_settings() still honour the variable.
        # A bad name falls back to the system zone: doctor reports it.
        try:
            _owner_zone = resolve_timezone(os.environ.get("TIMEZONE", ""))
        except ValueError:
            _owner_zone = None
        _configured = True
    return _owner_zone


def _utcnow() -> datetime:
    """The only place that reads the clock."""
    return datetime.now(UTC)


def owner_now() -> datetime:
    """Aware current moment in the owner's zone."""
    return _utcnow().astimezone(owner_tz())


def owner_today() -> date:
    return owner_now().date()


def to_owner(moment: datetime) -> datetime:
    """Convert ``moment`` to the owner's zone (naive values count as system)."""
    return moment.astimezone(owner_tz())


def system_timezone_name() -> str:
    """Best-effort name of the operating system zone, for doctor only."""
    env_tz = os.environ.get("TZ", "").strip().lstrip(":")
    if env_tz:
        return env_tz
    try:
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    marker = "zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1]
    return "unknown"


__all__ = [
    "owner_now",
    "owner_today",
    "owner_tz",
    "resolve_timezone",
    "set_owner_timezone",
    "system_timezone_name",
    "to_owner",
]
