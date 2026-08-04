from pathlib import Path

from d_brain.config import Settings


def test_settings_load_owner_telegram_id(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=test-token",
                "DEEPGRAM_API_KEY=test-deepgram",
                "OWNER_TELEGRAM_ID=123456789",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.owner_telegram_id == 123456789


def test_settings_load_plaud_fields(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=test-token",
                "DEEPGRAM_API_KEY=test-deepgram",
                "OWNER_TELEGRAM_ID=123456789",
                "PLAUD_BEARER_TOKEN=plaud-token",
                "PLAUD_REGION=api-euc1",
                "OWNER_FULL_NAME=Иванов Иван",
                "CONTENT_LANGUAGE=en",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.plaud_bearer_token == "plaud-token"
    assert settings.plaud_region == "api-euc1"
    assert settings.owner_full_name == "Иванов Иван"
    assert settings.content_language == "en"


def test_settings_load_web_content_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=test-token",
                "DEEPGRAM_API_KEY=test-deepgram",
                "OWNER_TELEGRAM_ID=123456789",
                "TAVILY_API_KEY=tavily-token",
                "JINA_API_KEY=jina-token",
                "ZAI_API_KEY=zai-token",
                "PROXY_URL=https://proxy.example.com",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.tavily_api_key == "tavily-token"
    assert settings.jina_api_key == "jina-token"
    assert settings.zai_api_key == "zai-token"
    assert settings.proxy_url == "https://proxy.example.com"


def test_settings_load_recall_planner_fields(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=test-token",
                "DEEPGRAM_API_KEY=test-deepgram",
                "OWNER_TELEGRAM_ID=123456789",
                "OPENAI_API_KEY=test-openai",
                "BASE_URL=https://gateway.example.com/v1",
                "MODEL=gpt-5-mini",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.openai_api_key == "test-openai"
    assert settings.openai_base_url == "https://gateway.example.com/v1"
    assert settings.openai_model == "gpt-5-mini"


def test_settings_load_vault_backup_fields(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=test-token",
                "DEEPGRAM_API_KEY=test-deepgram",
                "OWNER_TELEGRAM_ID=123456789",
                "VAULT_BACKUP_DIR=/srv/backups/d-brain",
                "VAULT_BACKUP_GPG_RECIPIENT=backup@example.com",
                "VAULT_BACKUP_RETENTION=21",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.vault_backup_dir == Path("/srv/backups/d-brain")
    assert settings.vault_backup_gpg_recipient == "backup@example.com"
    assert settings.vault_backup_retention == 21
