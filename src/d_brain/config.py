"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(description="Telegram Bot API token")
    deepgram_api_key: str = Field(description="Deepgram API key for transcription")
    todoist_api_key: str = Field(default="", description="Todoist API key for tasks")
    openai_api_key: str = Field(
        default="",
        description="OpenAI-compatible API key for the recall planner helper",
    )
    openai_base_url: str = Field(
        default="",
        validation_alias="BASE_URL",
        description="OpenAI-compatible base URL for the recall planner helper",
    )
    openai_model: str = Field(
        default="",
        validation_alias="MODEL",
        description="Model name used by the recall planner helper",
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

    @property
    def daily_path(self) -> Path:
        """Path to daily notes directory."""
        return self.vault_path / "daily"

    @property
    def attachments_path(self) -> Path:
        """Path to attachments directory."""
        return self.vault_path / "attachments"

    @property
    def thoughts_path(self) -> Path:
        """Path to thoughts directory."""
        return self.vault_path / "thoughts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get application settings instance."""
    return Settings()  # type: ignore[call-arg]
