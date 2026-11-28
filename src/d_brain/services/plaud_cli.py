"""Read recordings through the official Plaud CLI."""

import os
import re
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any

from d_brain.services.plaud import PlaudAuthError, PlaudRecordingNotFoundError

_FILE_ROW = re.compile(r"^  ((?:of_|f_)[a-f0-9]{32}) +", re.MULTILINE)
_FILE_FIELD = re.compile(
    r"^  (id|name|created_at|start_at|duration|"
    r"serial_number|audio|transcript|summary): +",
    re.MULTILINE,
)


class _PlaudArtifactNotFoundError(RuntimeError):
    """Requested recording artifact (e.g. transcript) is not available yet."""


class PlaudCliClient:
    def __init__(self, executable: str) -> None:
        self.executable = (
            str(Path(executable).expanduser())
            if ("/" in executable or executable.startswith("~"))
            else executable
        )
        self.env = {**os.environ, "NO_COLOR": "1"}
        # The legacy d-brain region is a hostname prefix, not the CLI region.
        self.env.pop("PLAUD_REGION", None)
        self._next_request = 0.0

    def _run(self, *args: str) -> str:
        for attempt in range(2):
            sleep(max(0, self._next_request - monotonic()))
            self._next_request = monotonic() + 1.1
            result = subprocess.run(
                [self.executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=self.env,
                timeout=75,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            if result.returncode == 2 and "[AUTH_FAILED]" in result.stderr:
                raise PlaudAuthError("Plaud CLI: войдите в аккаунт через plaud login")
            if args[0] == "file" and "[NOT_FOUND]" in result.stderr:
                raise PlaudRecordingNotFoundError(args[1])
            if args[0] == "transcript" and "[NOT_FOUND]" in result.stderr:
                raise _PlaudArtifactNotFoundError(args[1])
            if "429 Too Many Requests" not in result.stderr:
                raise RuntimeError(
                    f"Plaud CLI {args[0]} failed (exit {result.returncode})"
                )
            if attempt == 0:
                self._next_request = monotonic() + 30
        raise RuntimeError(f"Plaud CLI {args[0]}: 429 Too Many Requests")

    def iter_recordings(
        self, *, limit: int, max_pages: int | None
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while max_pages is None or page <= max_pages:
            output = self._run("files", "--page", str(page), "--page-size", str(limit))
            count = re.search(r"^Files on this page: (\d+)$", output, re.MULTILINE)
            ids = _FILE_ROW.findall(output)
            if not count or int(count[1]) != len(ids):
                raise RuntimeError("Unexpected Plaud CLI files output")
            for file_id in ids:
                yield {"file_id": file_id.removeprefix("of_")}
            if len(ids) < limit:
                break
            page += 1

    def get_recording(self, file_id: str) -> dict[str, Any]:
        cli_id = file_id if file_id.startswith(("f_", "of_")) else f"of_{file_id}"
        output = self._run("file", cli_id)
        _, *fields = _FILE_FIELD.split(output)
        metadata = {
            key: value.strip()
            for key, value in zip(fields[::2], fields[1::2], strict=True)
        }
        if metadata.get("id") != cli_id:
            raise RuntimeError("Unexpected Plaud CLI file output")
        raw_date = metadata.get("start_at", "")
        if not raw_date or raw_date == "-":
            raw_date = metadata.get("created_at", "")
        if not raw_date or raw_date == "-":
            raise RuntimeError(
                f"Unexpected Plaud CLI file output: missing recording date for {cli_id}"
            )
        try:
            recorded_at = datetime.fromisoformat(raw_date)
        except ValueError as exc:
            raise RuntimeError(
                f"Unexpected Plaud CLI file output: "
                f"invalid date {raw_date!r} for {cli_id}"
            ) from exc
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)

        transcript_status = metadata.get("transcript", "").lower()
        should_fetch_transcript = (
            not transcript_status or transcript_status == "available"
        )

        with TemporaryDirectory(prefix="d-brain-plaud-") as directory:
            summary_file = Path(directory) / "summary.md"
            transcript_file = Path(directory) / "transcript.txt"
            if metadata.get("summary", "").lower() == "available":
                self._run("summary", cli_id, "-o", str(summary_file))
            if should_fetch_transcript:
                try:
                    self._run("transcript", cli_id, "-o", str(transcript_file))
                except _PlaudArtifactNotFoundError:
                    pass
                if not transcript_file.is_file():
                    try:
                        self._run(
                            "transcript",
                            cli_id,
                            "--polished",
                            "-o",
                            str(transcript_file),
                        )
                    except _PlaudArtifactNotFoundError:
                        pass
            return {
                "file_id": file_id,
                "title": metadata.get("name", ""),
                "record_time": recorded_at.isoformat(),
                "ai_content": summary_file.read_text(encoding="utf-8")
                if summary_file.is_file()
                else "",
                "trans_result": transcript_file.read_text(encoding="utf-8")
                if transcript_file.is_file()
                else "",
            }
