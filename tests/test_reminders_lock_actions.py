"""Lock around reminders.jsonl and the cancel/postpone actions (review C3, G3)."""

import threading
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone

from d_brain.services import reminders
from d_brain.services.reminders import (
    Reminder,
    add_reminder,
    cancel_reminder,
    is_pending_at,
    list_pending,
    mark_sent,
    postpone_reminder,
)

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def _add(vault, text="задача", due=None, chat_id=1):
    due = due or NOW + timedelta(hours=5)
    return add_reminder(vault, chat_id, Reminder(due_at=due, text=text), now=NOW)


def _race_add_during_mark_sent(monkeypatch, tmp_path, *, locked: bool):
    """Run add_reminder("B") in a thread while mark_sent("A") is between its
    read and its write; return the journal's pending texts afterwards.

    ``settled`` fires when the worker either finished its append (no lock to
    wait on) or reached the lock and is about to block on it.
    """
    a = _add(tmp_path, "A")
    original_read = reminders._read_all
    real_flock = reminders.fcntl.flock
    settled = threading.Event()
    started = {"done": False}
    worker_ref: list[threading.Thread] = []

    def worker():
        _add(tmp_path, "B")
        settled.set()

    def patched_read(path):
        records = original_read(path)
        if not started["done"]:
            started["done"] = True
            thread = threading.Thread(target=worker)
            worker_ref.append(thread)
            thread.start()
            assert settled.wait(timeout=5)
        return records

    def patched_flock(fd, op):
        if worker_ref and threading.current_thread() is worker_ref[0]:
            settled.set()
        return real_flock(fd, op)

    monkeypatch.setattr(reminders, "_read_all", patched_read)
    monkeypatch.setattr(reminders.fcntl, "flock", patched_flock)
    if not locked:
        monkeypatch.setattr(reminders, "_reminders_lock", lambda path: nullcontext())

    mark_sent(tmp_path, a.id, now=NOW)
    worker_ref[0].join(timeout=5)
    monkeypatch.undo()
    return [r.text for r in list_pending(tmp_path)]


def test_add_reminder_is_not_lost_when_mark_sent_rewrites_concurrently(
    monkeypatch, tmp_path
):
    assert _race_add_during_mark_sent(monkeypatch, tmp_path, locked=True) == ["B"]


def test_race_scenario_loses_the_new_reminder_without_the_lock(monkeypatch, tmp_path):
    # Guards the test above: with the lock disabled the same scenario must
    # lose "B", otherwise it would pass without proving anything.
    assert _race_add_during_mark_sent(monkeypatch, tmp_path, locked=False) == []


def test_concurrent_add_and_mark_sent_keep_every_record(tmp_path):
    seeded = [_add(tmp_path, f"seed{i}") for i in range(10)]
    threads = [
        threading.Thread(target=_add, args=(tmp_path, f"new{i}")) for i in range(30)
    ] + [
        threading.Thread(target=mark_sent, args=(tmp_path, r.id), kwargs={"now": NOW})
        for r in seeded
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    path = reminders._reminders_path(tmp_path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 40
    assert len(list_pending(tmp_path)) == 30


def test_lock_file_is_created_next_to_journal(tmp_path):
    _add(tmp_path)
    journal = reminders._reminders_path(tmp_path)
    assert (journal.parent / "reminders.jsonl.lock").exists()
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1


def test_cancel_reminder_removes_record_from_file(tmp_path):
    a = _add(tmp_path, "A")
    b = _add(tmp_path, "B")
    assert cancel_reminder(tmp_path, a.id) is True
    path = reminders._reminders_path(tmp_path)
    assert a.id not in path.read_text(encoding="utf-8")
    assert [r.id for r in list_pending(tmp_path)] == [b.id]

    before = path.read_bytes()
    assert cancel_reminder(tmp_path, a.id) is False
    assert cancel_reminder(tmp_path, "unknown") is False
    assert path.read_bytes() == before


def test_postpone_reminder_moves_to_tomorrow_same_clock_time(tmp_path):
    due = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    rec = _add(tmp_path, "A", due=due)

    moved = postpone_reminder(tmp_path, rec.id, now=NOW)

    assert moved is not None
    assert moved.due_at == datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
    assert (moved.id, moved.text) == (rec.id, rec.text)
    assert moved.created_at == rec.created_at
    assert list_pending(tmp_path)[0].due_at == moved.due_at
    assert postpone_reminder(tmp_path, "unknown", now=NOW) is None

    sent = _add(tmp_path, "S", due=due)
    mark_sent(tmp_path, sent.id, now=NOW)
    assert postpone_reminder(tmp_path, sent.id, now=NOW) is None


def test_postpone_reminder_keeps_timezone_of_now(tmp_path):
    tz = timezone(timedelta(hours=3))
    now = datetime(2026, 10, 25, 10, 0, tzinfo=tz)
    # Overdue 02:30 UTC is 05:30 local; "tomorrow" is the 26th local.
    rec = _add(tmp_path, "A", due=datetime(2026, 10, 25, 2, 30, tzinfo=UTC))

    moved = postpone_reminder(tmp_path, rec.id, now=now)

    assert moved is not None
    assert moved.due_at == datetime(2026, 10, 26, 5, 30, tzinfo=tz)


def test_postpone_reminder_adds_a_day_to_a_future_due(tmp_path):
    # Pressing the button must never move a reminder earlier than it was.
    due = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    rec = _add(tmp_path, "A", due=due)

    moved = postpone_reminder(tmp_path, rec.id, now=NOW)

    assert moved is not None
    assert moved.due_at == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def test_postpone_reminder_overdue_goes_to_tomorrow_same_time(tmp_path):
    due = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    rec = _add(tmp_path, "A", due=due)

    moved = postpone_reminder(tmp_path, rec.id, now=NOW)

    assert moved is not None
    assert moved.due_at == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def test_mark_sent_ignores_a_record_whose_due_at_moved(tmp_path):
    due = NOW + timedelta(hours=1)
    rec = _add(tmp_path, "A", due=due)
    postpone_reminder(tmp_path, rec.id, now=NOW)

    mark_sent(tmp_path, rec.id, now=NOW, due_at=due)

    assert [r.id for r in list_pending(tmp_path)] == [rec.id]


def test_mark_sent_with_matching_due_at_marks_it(tmp_path):
    due = NOW + timedelta(hours=1)
    rec = _add(tmp_path, "A", due=due)

    mark_sent(tmp_path, rec.id, now=NOW, due_at=due)

    assert list_pending(tmp_path) == []


def test_is_pending_at_tells_cancelled_moved_and_sent_apart(tmp_path):
    due = NOW + timedelta(hours=1)
    keep = _add(tmp_path, "keep", due=due)
    gone = _add(tmp_path, "gone", due=due)
    moved = _add(tmp_path, "moved", due=due)
    sent = _add(tmp_path, "sent", due=due)
    cancel_reminder(tmp_path, gone.id)
    postpone_reminder(tmp_path, moved.id, now=NOW)
    mark_sent(tmp_path, sent.id, now=NOW)

    assert is_pending_at(tmp_path, keep.id, due) is True
    assert is_pending_at(tmp_path, gone.id, due) is False
    assert is_pending_at(tmp_path, moved.id, due) is False
    assert is_pending_at(tmp_path, sent.id, due) is False
