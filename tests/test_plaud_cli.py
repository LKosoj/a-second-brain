import subprocess
from pathlib import Path

import pytest

from d_brain.services import plaud_cli
from d_brain.services.plaud import PlaudAuthError, PlaudRecordingNotFoundError
from d_brain.services.plaud_cli import PlaudCliClient


def test_cli_keeps_legacy_archive_ids_and_new_ids(monkeypatch):
    old_id = "1" * 32
    new_id = "f_" + "2" * 32
    client = PlaudCliClient("plaud")

    def run(*args):
        if args[0] == "files":
            page = args[2]
            if page == "1":
                return f"Files on this page: 1\n  of_{old_id}  Old recording\n"
            return f"Files on this page: 1\n  {new_id}  New recording\n"
        assert args[1] == f"of_{old_id}"
        if args[0] == "file":
            return (
                f"  id:           of_{old_id}\n"
                "  name:         Test meeting\n"
                "  start_at:     2026-10-19T14:03:14\n"
                "  summary:      available\n"
            )
        Path(args[-1]).write_text(args[0], encoding="utf-8")
        return ""

    monkeypatch.setattr(client, "_run", run)
    assert [
        item["file_id"] for item in client.iter_recordings(limit=1, max_pages=2)
    ] == [old_id, new_id]
    assert client.get_recording(old_id) == {
        "file_id": old_id,
        "title": "Test meeting",
        "record_time": "2026-10-19T14:03:14+00:00",
        "ai_content": "summary",
        "trans_result": "transcript",
    }


def test_cli_auth_error_is_not_an_empty_page(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 2, "", "[AUTH_FAILED] Token invalid or expired."
        ),
    )
    with pytest.raises(PlaudAuthError, match="plaud login"):
        list(PlaudCliClient("plaud").iter_recordings(limit=100, max_pages=1))


def test_cli_marks_missing_recording_separately_from_transport_errors(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, "", "[NOT_FOUND] File not found: of_recording"
        ),
    )
    with pytest.raises(PlaudRecordingNotFoundError):
        PlaudCliClient("plaud").get_recording("recording")


def test_cli_does_not_double_prefix_of_id(monkeypatch):
    prefixed_id = "of_" + "3" * 32
    seen_ids: list[str] = []
    client = PlaudCliClient("plaud")

    def run(*args):
        seen_ids.append(args[1])
        if args[0] == "file":
            return (
                f"  id:           {prefixed_id}\n"
                "  name:         Already prefixed\n"
                "  start_at:     2026-10-19T14:03:14\n"
                "  summary:      none\n"
                "  transcript:   available\n"
            )
        Path(args[-1]).write_text("hello", encoding="utf-8")
        return ""

    monkeypatch.setattr(client, "_run", run)
    recording = client.get_recording(prefixed_id)

    assert seen_ids == [prefixed_id, prefixed_id]
    assert recording["file_id"] == prefixed_id
    assert recording["trans_result"] == "hello"
    assert recording["ai_content"] == ""


def test_cli_falls_back_to_created_at_when_start_at_is_dash(monkeypatch):
    rec_id = "4" * 32
    client = PlaudCliClient("plaud")

    def run(*args):
        if args[0] == "file":
            return (
                f"  id:           of_{rec_id}\n"
                "  created_at:   2026-10-20T09:15:00\n"
                "  start_at:     -\n"
            )
        Path(args[-1]).write_text("transcript text", encoding="utf-8")
        return ""

    monkeypatch.setattr(client, "_run", run)
    recording = client.get_recording(rec_id)

    assert recording["record_time"] == "2026-10-20T09:15:00+00:00"
    assert recording["title"] == ""
    assert recording["ai_content"] == ""
    assert recording["trans_result"] == "transcript text"


@pytest.mark.parametrize(
    "file_output",
    [
        "  id:           of_" + "5" * 32 + "\n  name:         No dates\n",
        "  id:           of_" + "5" * 32 + "\n  start_at:     -\n",
        "  id:           of_" + "5" * 32 + "\n  start_at:     not-a-date\n",
    ],
)
def test_cli_raises_runtime_error_on_incomplete_or_invalid_metadata(
    monkeypatch, file_output: str
):
    rec_id = "5" * 32
    client = PlaudCliClient("plaud")
    monkeypatch.setattr(client, "_run", lambda *args: file_output)

    with pytest.raises(RuntimeError, match="Unexpected Plaud CLI file output"):
        client.get_recording(rec_id)


@pytest.mark.parametrize("status", ["none", "pending", "unavailable", "-"])
def test_cli_skips_transcript_when_not_ready(monkeypatch, status: str):
    rec_id = "6" * 32
    calls: list[tuple[str, ...]] = []
    client = PlaudCliClient("plaud")

    def run(*args):
        calls.append(args)
        if args[0] == "file":
            return (
                f"  id:           of_{rec_id}\n"
                "  name:         Pending transcript\n"
                "  start_at:     2026-10-20T10:00:00\n"
                f"  transcript:   {status}\n"
                "  summary:      none\n"
            )
        raise AssertionError(f"Unexpected call: {args}")

    monkeypatch.setattr(client, "_run", run)
    recording = client.get_recording(rec_id)

    assert [call[0] for call in calls] == ["file"]
    assert recording["trans_result"] == ""
    assert recording["ai_content"] == ""


def test_cli_handles_transcript_not_found_without_failing(monkeypatch):
    rec_id = "7" * 32
    monkeypatch.setattr(plaud_cli, "sleep", lambda _: None)

    def fake_run(cmd, **kwargs):
        subcmd = cmd[1]
        if subcmd == "file":
            return subprocess.CompletedProcess(
                cmd,
                0,
                (
                    f"  id:           of_{rec_id}\n"
                    "  name:         Missing transcript\n"
                    "  start_at:     2026-10-20T10:00:00\n"
                    "  transcript:   available\n"
                    "  summary:      none\n"
                ),
                "",
            )
        if subcmd == "transcript":
            return subprocess.CompletedProcess(
                cmd, 1, "", f"[NOT_FOUND] Transcript not found: of_{rec_id}"
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    recording = PlaudCliClient("plaud").get_recording(rec_id)

    assert recording["title"] == "Missing transcript"
    assert recording["trans_result"] == ""


def test_cli_falls_back_to_polished_transcript(monkeypatch):
    rec_id = "8" * 32
    transcript_calls: list[tuple[str, ...]] = []
    client = PlaudCliClient("plaud")

    def run(*args):
        if args[0] == "file":
            return (
                f"  id:           of_{rec_id}\n"
                "  name:         Polished only\n"
                "  start_at:     2026-10-20T10:00:00\n"
                "  transcript:   available\n"
                "  summary:      none\n"
            )
        if args[0] == "transcript":
            transcript_calls.append(args)
            if "--polished" in args:
                Path(args[-1]).write_text("polished text", encoding="utf-8")
            return ""
        raise AssertionError(f"Unexpected call: {args}")

    monkeypatch.setattr(client, "_run", run)
    recording = client.get_recording(rec_id)

    assert len(transcript_calls) == 2
    assert "--polished" not in transcript_calls[0]
    assert "--polished" in transcript_calls[1]
    assert recording["trans_result"] == "polished text"


def test_cli_retries_once_on_429_and_succeeds(monkeypatch):
    sleeps: list[float] = []
    attempts = 0
    monkeypatch.setattr(plaud_cli, "sleep", lambda seconds: sleeps.append(seconds))

    def fake_run(cmd, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "429 Too Many Requests")
        return subprocess.CompletedProcess(cmd, 0, "Files on this page: 0\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = PlaudCliClient("plaud")

    assert list(client.iter_recordings(limit=10, max_pages=1)) == []
    assert attempts == 2
    assert len(sleeps) == 2
    assert sleeps[1] > 0


def test_cli_raises_after_second_429(monkeypatch):
    monkeypatch.setattr(plaud_cli, "sleep", lambda _: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1, "", "429 Too Many Requests"
        ),
    )

    with pytest.raises(RuntimeError, match="429 Too Many Requests"):
        list(PlaudCliClient("plaud").iter_recordings(limit=10, max_pages=1))


def test_cli_propagates_timeout_expired(monkeypatch):
    monkeypatch.setattr(plaud_cli, "sleep", lambda _: None)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=75)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        list(PlaudCliClient("plaud").iter_recordings(limit=10, max_pages=1))
