"""Image OCR and description via the configured agent CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from d_brain.services.cli_runner import CliRunner
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import normalize_language, prompt_language_name

IMAGE_ANALYSIS_TIMEOUT = 120


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


class ImageAnalysisService:
    """Analyze local images via the configured coding CLI."""

    def __init__(
        self,
        vault_path: Path | str,
        ai_cli: str = "claude",
        content_language: str = "ru",
    ) -> None:
        self.vault_path = Path(vault_path)
        self.runner = CliRunner(self.vault_path, ai_cli)
        self.content_language = normalize_language(content_language)

    def _cli_extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        mcp_config_path = (self.vault_path.parent / "mcp-config.json").resolve()
        if mcp_config_path.exists():
            env["MCP_CONFIG_PATH"] = str(mcp_config_path)
        return env

    def _build_prompt(self, image_path: Path) -> str:
        summary_language = prompt_language_name(self.content_language)
        return (
            "Open the local image at this absolute path and inspect the actual "
            "pixels:\n"
            f"{image_path}\n\n"
            "Return strict JSON with exactly two string fields:\n"
            '{\n  "description": "...",\n  "ocr_text": "..."\n}\n\n'
            "Rules:\n"
            f"- description: brief {summary_language} description of the whole image. "
            "Mention the main scene, objects, and any clearly visible text.\n"
            "- ocr_text: exact visible text from the image, preserving line "
            "breaks when possible.\n"
            "- If there is no readable text, return an empty string for ocr_text.\n"
            "- If the image is blurry or partially unreadable, describe that "
            "briefly and extract only text you are confident about.\n"
            "- Output JSON only. No markdown fences, no commentary.\n"
        )

    def analyze(self, relative_path: str) -> dict[str, str]:
        image_path = (self.vault_path / relative_path).resolve()
        image_path.relative_to(self.vault_path.resolve())
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        output = self.runner.run(
            self._build_prompt(image_path),
            timeout=IMAGE_ANALYSIS_TIMEOUT,
            extra_env=self._cli_extra_env(),
        )
        payload = extract_first_json_dict(
            output,
            error_context="image analysis output",
        )
        return {
            "description": _safe_text(payload.get("description")),
            "ocr_text": _safe_text(payload.get("ocr_text")),
        }
