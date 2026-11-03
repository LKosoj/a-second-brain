"""Network request dispatch and durable job state."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from d_brain.clock import owner_now
from d_brain.config import Settings
from d_brain.services.link_summary import (
    LinkSummaryService,
    format_link_summary_message,
)
from d_brain.services.processor import (
    TEXT_INTENT_ACTION,
    TEXT_INTENT_QUESTION,
    CliProcessor,
)
from d_brain.services.secrets import scrub_secrets
from d_brain.services.session import SessionStore
from d_brain.services.source_links import SourceInfo
from d_brain.services.storage import VaultStorage

logger = logging.getLogger(__name__)
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class NetworkRequestError(ValueError):
    """Raised when a network request is invalid."""


class NetworkJobStore:
    """Persist one JSON document per network job."""

    def __init__(self, vault_path: Path) -> None:
        self.path = vault_path / ".session" / "network-jobs"
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            existing = self.find_by_request_id(request_id)
            if existing is not None:
                return existing
            job = {
                "job_id": uuid.uuid4().hex,
                "request_id": request_id,
                "status": "queued",
                "created_at": owner_now().isoformat(),
            }
            self._write(job)
            return job

    def find_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        for path in self.path.glob("*.json"):
            try:
                job = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipping unreadable network job %s", path)
                continue
            if job.get("request_id") == request_id:
                return job
        return None

    def get(self, job_id: str) -> dict[str, Any] | None:
        if _JOB_ID_RE.fullmatch(job_id) is None:
            return None
        path = self.path / f"{job_id}.json"
        if not path.is_file():
            return None
        try:
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read network job %s", job_id)
            return None

    def list(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in self.path.glob("*.json"):
            job = self.get(path.stem)
            if job is not None:
                jobs.append(job)
        return jobs

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise NetworkRequestError(f"Unknown job: {job_id}")
            job.update(changes)
            job["updated_at"] = owner_now().isoformat()
            self._write(job)
            return job

    def _write(self, job: dict[str, Any]) -> None:
        path = self.path / f"{job['job_id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(job, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


class NetworkRequestService:
    """Route network requests through the existing d-brain workflows."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs = NetworkJobStore(settings.vault_path)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="network")
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._resume_pending_jobs()

    def submit(self, request_id: str, text: str, client: str = "") -> dict[str, Any]:
        request_id = request_id.strip()
        text = text.strip()
        if not request_id:
            raise NetworkRequestError("request_id is required")
        if not text:
            raise NetworkRequestError("text is required")
        if len(text) > 100_000:
            raise NetworkRequestError("text is too long")
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        with self._lock:
            job = self.jobs.create(request_id)
            existing_digest = job.get("text_sha256")
            if not isinstance(existing_digest, str):
                existing_text = job.get("text")
                if isinstance(existing_text, str):
                    existing_digest = hashlib.sha256(
                        existing_text.encode("utf-8")
                    ).hexdigest()
            if existing_digest is not None and existing_digest != text_digest:
                raise NetworkRequestError(
                    "request_id reused with different text"
                )
            if job["status"] not in {"queued", "running"}:
                return job
            self.jobs.update(
                job["job_id"],
                text=text,
                text_sha256=text_digest,
                client=client.strip(),
            )
            self._condition.notify_all()
            self._schedule(str(job["job_id"]), text)
            return self.jobs.get(job["job_id"]) or job

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def wait_for(self, job_id: str, timeout: float) -> dict[str, Any] | None:
        """Wait for a queued/running job to finish or until timeout."""
        timeout = max(0.0, min(timeout, 30.0))
        with self._condition:
            end = time.monotonic() + timeout
            while True:
                job = self.jobs.get(job_id)
                if job is None or job.get("status") not in {"queued", "running"}:
                    return job
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return job
                self._condition.wait(timeout=remaining)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _resume_pending_jobs(self) -> None:
        for job in self.jobs.list():
            if job.get("status") not in {"queued", "running"}:
                continue
            if job.get("side_effect_started_at") and not job.get("result"):
                self.jobs.update(
                    str(job["job_id"]),
                    status="failed",
                    error="interrupted",
                    retryable=False,
                    completed_at=owner_now().isoformat(),
                )
                continue
            text = job.get("text")
            if not isinstance(text, str) or not text.strip():
                self.jobs.update(
                    str(job["job_id"]),
                    status="failed",
                    error="Job has no request text",
                )
                continue
            self.jobs.update(str(job["job_id"]), status="queued")
            self._schedule(str(job["job_id"]), text)

    def _schedule(self, job_id: str, text: str) -> None:
        if job_id in self._futures and not self._futures[job_id].done():
            return
        self._futures[job_id] = self.executor.submit(self._run, job_id, text)

    def _run(self, job_id: str, text: str) -> None:
        self.jobs.update(job_id, status="running", started_at=owner_now().isoformat())
        with self._condition:
            self._condition.notify_all()
        try:
            result = self._dispatch(text, job_id)
        except Exception as exc:  # pragma: no cover - final safety boundary
            logger.exception("Network request %s failed", job_id)
            result = {"error": str(exc), "processed_entries": 0}
        status = "failed" if "error" in result else "completed"
        self.jobs.update(
            job_id,
            status=status,
            completed_at=owner_now().isoformat(),
            result=result,
        )
        with self._condition:
            self._condition.notify_all()

    def _processor(self) -> CliProcessor:
        settings = self.settings
        return CliProcessor(
            settings.vault_path,
            settings.todoist_api_key,
            settings.ai_cli,
            settings.owner_full_name,
            settings.content_language,
            settings.openai_api_key,
            settings.openai_base_url,
            settings.openai_model,
            settings.tavily_api_key,
            0,
            settings.recall_planner_disable_thinking,
            settings.classification_model,
        )

    def _dispatch(self, text: str, job_id: str) -> dict[str, Any]:
        processor = self._processor()
        intent = processor.classify_text_intent(text)
        source = SourceInfo(kind="network", ref=f"network:{job_id}", label="LacEditor")
        if intent["intent"] == TEXT_INTENT_QUESTION:
            result = processor.answer_question(text, self.settings.owner_telegram_id)
        elif intent["intent"] == TEXT_INTENT_ACTION:
            self._mark_side_effect_started(job_id)
            result = processor.execute_prompt(text, self.settings.owner_telegram_id)
        else:
            self._mark_side_effect_started(job_id)
            result = self._capture(text, source)
        SessionStore(self.settings.vault_path).append(
            self.settings.owner_telegram_id,
            "network",
            text=text,
            request_id=job_id,
            intent=intent.get("intent", ""),
            workflow_name=intent.get("workflow", ""),
            client="LacEditor",
        )
        return result

    def _mark_side_effect_started(self, job_id: str) -> None:
        if self.jobs.get(job_id) is not None:
            self.jobs.update(
                job_id,
                side_effect_started_at=owner_now().isoformat(),
            )
            with self._condition:
                self._condition.notify_all()

    def _capture(
        self, text: str, source: SourceInfo, job_id: str | None = None
    ) -> dict[str, Any]:
        if job_id is None and source.kind == "network":
            job_id = source.ref.removeprefix("network:")
        settings = self.settings
        timestamp = owner_now()
        service = LinkSummaryService(
            str(settings.vault_path),
            settings.ai_cli,
            settings.content_language,
            tavily_api_key=settings.tavily_api_key,
            jina_api_key=settings.jina_api_key,
            zai_api_key=settings.zai_api_key,
            proxy_url=settings.proxy_url,
        )
        enriched = service.enrich_text(
            text,
            timestamp=timestamp,
            source=source,
            refresh_qmd=False,
        )
        content, _ = scrub_secrets(enriched.content)
        VaultStorage(settings.vault_path, settings.content_language).append_to_daily(
            content,
            timestamp,
            "[network]",
            source=source,
            job_id=job_id,
            refresh_qmd=False,
        )
        reply = "✓ Сохранено"
        summary = format_link_summary_message(
            enriched.summaries,
            language=settings.content_language,
            youtube_urls={item.url for item in enriched.youtube_summaries},
        )
        if summary:
            reply += "\n\n" + summary
        return {"report": reply, "processed_entries": 1, "artifact_paths": []}


__all__ = ["NetworkJobStore", "NetworkRequestError", "NetworkRequestService"]
