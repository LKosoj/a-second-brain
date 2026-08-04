"""LLM-assisted owner-facing digest over completed daily reflection results."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from d_brain.services.cli_runner import CliRunner
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import normalize_language, prompt_language_name
from d_brain.services.telegram_markup import (
    markdown_to_plain_text,
    normalize_markdown_input,
)

REFLECTION_DIGEST_TIMEOUT = 180


class ReflectionDigestService:
    """Generate short owner-facing takeaways from a completed reflect run."""

    def __init__(
        self,
        vault_path: Path | str,
        *,
        ai_cli: str = "claude",
        content_language: str = "ru",
    ) -> None:
        self.vault_path = Path(vault_path)
        self.runner = CliRunner(self.vault_path, ai_cli)
        self.content_language = normalize_language(content_language)

    @staticmethod
    def _compact_execute_payload(payload: dict[str, Any]) -> dict[str, Any]:
        tasks = [
            {
                "content": str(item.get("content") or "").strip(),
                "priority": item.get("priority"),
                "due": str(item.get("due") or item.get("due_hint") or "").strip(),
            }
            for item in payload.get("tasks_created", [])
            if isinstance(item, dict)
        ]
        thoughts = [
            {
                "title": str(item.get("title") or "").strip(),
                "category": str(item.get("category") or "").strip(),
            }
            for item in payload.get("thoughts_saved", [])
            if isinstance(item, dict)
        ]
        crm = [
            {
                "path": str(item.get("path") or "").strip(),
                "change": str(item.get("change") or "").strip(),
            }
            for item in payload.get("crm_updated", [])
            if isinstance(item, dict)
        ]
        process_goals = payload.get("process_goals")
        observations = payload.get("observations")
        return {
            "tasks_created": tasks,
            "thoughts_saved": thoughts,
            "crm_updated": crm,
            "process_goals": process_goals if isinstance(process_goals, dict) else {},
            "observations": observations if isinstance(observations, list) else [],
        }

    def _build_prompt(
        self,
        *,
        day: date,
        report_markdown: str,
        execute_payload: dict[str, Any],
    ) -> str:
        language_name = prompt_language_name(self.content_language)
        clean_report = " ".join(markdown_to_plain_text(report_markdown).split())
        compact_payload = self._compact_execute_payload(execute_payload)
        return (
            "Summarize one completed daily reflection for the owner-facing Telegram "
            "digest.\n"
            f"Write in {language_name}.\n"
            "Use ONLY the provided report and execute payload. "
            "Do not invent facts or hidden motives.\n\n"
            "Return strict JSON:\n"
            '{\n  "takeaways": ["...", "..."]\n}\n\n'
            "Rules:\n"
            "- Return an empty array when there is nothing noteworthy beyond raw "
            "counts.\n"
            "- Include every genuinely noteworthy takeaway from the provided "
            "artifacts.\n"
            "- Prefer decisions, real risks, meaningful insights, "
            "and next-step-worthy findings.\n"
            "- Ignore low-signal implementation detail unless it changes "
            "what the owner should know.\n"
            "- Each takeaway must fit one short sentence.\n"
            "- Output JSON only. No markdown fences.\n\n"
            f"[DAY]\n{day.isoformat()}\n\n"
            f"[REPORT]\n{clean_report}\n\n"
            "[EXECUTE_JSON]\n"
            f"{json.dumps(compact_payload, ensure_ascii=False, indent=2)}\n"
        )

    def summarize(
        self,
        *,
        day: date,
        report_markdown: str,
        execute_payload: dict[str, Any],
    ) -> list[str]:
        """Return 0-3 owner-facing takeaways or raise if the LLM contract breaks."""
        normalized_report = normalize_markdown_input(report_markdown)
        output = self.runner.run(
            self._build_prompt(
                day=day,
                report_markdown=normalized_report,
                execute_payload=execute_payload,
            ),
            timeout=REFLECTION_DIGEST_TIMEOUT,
        )
        payload = extract_first_json_dict(
            output,
            error_context="reflection digest output",
        )
        items = payload.get("takeaways")
        if not isinstance(items, list):
            return []
        takeaways: list[str] = []
        for item in items:
            text = " ".join(str(item or "").split())
            if not text:
                continue
            takeaways.append(text)
        return takeaways


__all__ = ["ReflectionDigestService"]
