"""Application configuration using Pydantic Settings."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from d_brain.clock import resolve_timezone, set_owner_timezone

_CLOCK_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def parse_clock_time(raw: str) -> tuple[int, int] | None:
    """Parse a strict 24h ``HH:MM`` string, ``None`` when it is not one.

    Shared by doctor and the nightly runner: a typo in the schedule must not
    crash either of them, so callers decide what to do with ``None``.
    """
    match = _CLOCK_TIME_RE.fullmatch((raw or "").strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors would otherwise echo the rejected value, and the
        # calendar link (like every key here) is a secret.
        hide_input_in_errors=True,
    )

    telegram_bot_token: str = Field(description="Telegram Bot API token")
    deepgram_api_key: str = Field(
        default="",
        description=(
            "Deepgram API key for transcription; "
            "empty keeps voice as unrecognized audio"
        ),
    )
    todoist_api_key: str = Field(default="", description="Todoist API key for tasks")
    openai_api_key: str = Field(
        default="",
        description="OpenAI-compatible API key for the recall planner helper",
    )
    openai_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_BASE_URL", "BASE_URL"),
        description="OpenAI-compatible base URL for the recall planner helper",
    )
    openai_model: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_MODEL", "MODEL"),
        description=(
            "Chat model for classification and recall "
            "when CLASSIFICATION_MODEL is empty; OPENAI_MODEL, legacy MODEL"
        ),
    )
    classification_model: str = Field(
        default="",
        description=(
            "Decisions API model for classification and recall; "
            "empty uses OPENAI_MODEL (or legacy MODEL) or CLI"
        ),
    )
    recall_planner_disable_thinking: bool = Field(
        default=False,
        description=(
            "Whether to send extra_body thinking:{\"type\":\"disabled\"} "
            "on recall planner requests. 1 = send it (provider skips its "
            "thinking step before answering, if it supports the knob); "
            "0 = do not send extra_body at all, leaving thinking control "
            "to the provider default. Honoured only when the provider "
            "exposes the corresponding knob through the OpenAI-compatible "
            "API."
        ),
    )
    tavily_api_key: str = Field(
        default="",
        description="Tavily Extract API key for hard-to-read web pages",
    )
    jina_api_key: str = Field(
        default="",
        description="Jina Reader API key for browser-rendered content extraction",
    )
    zai_api_key: str = Field(
        default="",
        description="Z.AI Reader API key for content extraction fallback",
    )
    proxy_url: str = Field(
        default="",
        description="Optional proxy reader base URL for content extraction fallback",
    )
    plaud_bearer_token: str = Field(
        default="",
        description="PLAUD bearer token for recording import",
    )
    plaud_region: str = Field(
        default="api",
        description="PLAUD API region host prefix: api or api-euc1",
    )
    owner_full_name: str = Field(
        default="",
        description="Full name of the assistant owner for LLM prompts",
    )
    content_language: str = Field(
        default="ru",
        description="Language used for generated saved content and prompts",
    )
    ai_cli: Literal[
        "claude",
        "claude-tmux",
        "codex",
        "codex-tmux",
        "qwen",
        "gemini",
        "kimi",
        "grok",
        "opencode",
    ] = Field(
        default="claude",
        description="CLI backend used for agent processing",
    )
    vault_path: Path = Field(
        default=Path("./vault"),
        description="Path to Obsidian vault directory",
    )
    vault_backup_dir: Path = Field(
        default=Path("./.vault-backups"),
        description="Directory outside the vault for encrypted snapshots",
    )
    vault_backup_gpg_recipient: str = Field(
        default="",
        description="GPG public-key recipient for pre-write vault snapshots",
    )
    vault_backup_retention: int = Field(
        default=14,
        ge=1,
        description="Number of encrypted vault snapshots to retain",
    )
    owner_telegram_id: int = Field(
        description="Telegram user ID allowed to use the bot",
    )
    timezone: str = Field(
        default="",
        description=(
            "IANA zone name for the owner's day, such as Europe/Moscow; "
            "empty uses the system zone"
        ),
    )

    calendar_ics_url: str = Field(
        default="",
        description=(
            "Private ICS feed URL of the owner's calendar (read-only); "
            "empty disables the calendar blocks"
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = value.strip()
        resolve_timezone(value)
        return value

    @field_validator("calendar_ics_url")
    @classmethod
    def _validate_calendar_ics_url(cls, value: str) -> str:
        value = value.strip()
        # The link is a secret: the error must not repeat it.
        schemes = {"http", "https", "webcal", "webcals"}
        if value and urlsplit(value).scheme.lower() not in schemes:
            raise ValueError("CALENDAR_ICS_URL must be an http(s) or webcal link")
        return value

    @property
    def daily_path(self) -> Path:
        """Path to daily notes directory."""
        return self.vault_path / "daily"

    @property
    def attachments_path(self) -> Path:
        """Path to attachments directory."""
        return self.vault_path / "attachments"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get application settings instance."""
    settings = Settings()  # type: ignore[call-arg]
    set_owner_timezone(settings.timezone)
    return settings
