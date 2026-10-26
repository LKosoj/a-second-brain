from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from d_brain import clock


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(clock, "_utcnow", lambda: moment)


def test_resolve_timezone_empty_valid_invalid() -> None:
    assert clock.resolve_timezone("") is None
    assert clock.resolve_timezone("  ") is None
    assert clock.resolve_timezone("Europe/Moscow") == ZoneInfo("Europe/Moscow")
    with pytest.raises(ValueError, match="IANA"):
        clock.resolve_timezone("Mars/Base")


def test_unset_timezone_matches_system_clock() -> None:
    assert clock.owner_tz() is None
    before = datetime.now().astimezone()
    got = clock.owner_now()
    after = datetime.now().astimezone()
    assert before <= got <= after
    assert clock.owner_today() in {before.date(), after.date()}


def test_late_utc_evening_is_next_day_in_moscow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock.set_owner_timezone("Europe/Moscow")
    _freeze(monkeypatch, datetime(2026, 3, 10, 22, 30, tzinfo=UTC))
    assert clock.owner_today().isoformat() == "2026-03-11"
    assert clock.owner_now().strftime("%H:%M") == "01:30"


def test_moscow_midnight_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    clock.set_owner_timezone("Europe/Moscow")
    _freeze(monkeypatch, datetime(2026, 3, 10, 20, 59, 59, tzinfo=UTC))
    assert clock.owner_today().isoformat() == "2026-03-10"
    _freeze(monkeypatch, datetime(2026, 3, 10, 21, 0, 0, tzinfo=UTC))
    assert clock.owner_today().isoformat() == "2026-03-11"


@pytest.mark.parametrize(
    ("moment", "offset_hours"),
    [
        (datetime(2026, 3, 28, 23, 30, tzinfo=UTC), 1),
        (datetime(2026, 3, 29, 23, 30, tzinfo=UTC), 2),
        (datetime(2026, 10, 24, 23, 30, tzinfo=UTC), 2),
        (datetime(2026, 10, 25, 23, 30, tzinfo=UTC), 1),
    ],
)
def test_berlin_dst_days(
    monkeypatch: pytest.MonkeyPatch, moment: datetime, offset_hours: int
) -> None:
    clock.set_owner_timezone("Europe/Berlin")
    _freeze(monkeypatch, moment)
    now = clock.owner_now()
    assert now.utcoffset().total_seconds() == offset_hours * 3600  # type: ignore[union-attr]
    # 23:30 UTC is always 00:30 or 01:30 of the following Berlin day.
    assert clock.owner_today() == (moment + (now.utcoffset() or moment - moment)).date()


def test_to_owner_aware_and_naive() -> None:
    clock.set_owner_timezone("Europe/Moscow")
    aware = clock.to_owner(datetime(2026, 3, 10, 22, 30, tzinfo=UTC))
    assert aware.date().isoformat() == "2026-03-11"
    assert aware.tzinfo == ZoneInfo("Europe/Moscow")
    naive = datetime(2026, 3, 10, 12, 0)
    assert clock.to_owner(naive).tzinfo == ZoneInfo("Europe/Moscow")


def test_to_owner_without_timezone_is_system_astimezone() -> None:
    moment = datetime(2026, 3, 10, 22, 30, tzinfo=UTC)
    assert clock.to_owner(moment) == moment.astimezone()
    assert clock.to_owner(moment).utcoffset() == moment.astimezone().utcoffset()


def test_env_fallback_without_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    assert clock.owner_tz() == ZoneInfo("Asia/Tokyo")


def test_bad_env_falls_back_to_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEZONE", "Mars/Base")
    assert clock.owner_tz() is None


def test_system_timezone_name_prefers_tz_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", ":Pacific/Auckland")
    assert clock.system_timezone_name() == "Pacific/Auckland"
