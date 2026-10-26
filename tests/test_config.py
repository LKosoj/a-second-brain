from pathlib import Path

import pytest

from d_brain.config import Settings, parse_clock_time


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


def _write_env(tmp_path: Path, *lines: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(["TELEGRAM_BOT_TOKEN=test-token", "OWNER_TELEGRAM_ID=1", *lines]),
        encoding="utf-8",
    )
    return env_file


def test_settings_prefer_openai_prefixed_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "BASE_URL", "MODEL"):
        monkeypatch.delenv(name, raising=False)
    env_file = _write_env(
        tmp_path,
        "OPENAI_BASE_URL=https://gateway.example.com/v1",
        "OPENAI_MODEL=gpt-5-mini",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.openai_base_url == "https://gateway.example.com/v1"
    assert settings.openai_model == "gpt-5-mini"


def test_settings_new_names_win_over_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "BASE_URL", "MODEL"):
        monkeypatch.delenv(name, raising=False)
    env_file = _write_env(
        tmp_path,
        "BASE_URL=https://old.example.com/v1",
        "MODEL=old-model",
        "OPENAI_BASE_URL=https://new.example.com/v1",
        "OPENAI_MODEL=new-model",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.openai_base_url == "https://new.example.com/v1"
    assert settings.openai_model == "new-model"


def test_settings_work_without_deepgram_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    settings = Settings(_env_file=_write_env(tmp_path))  # type: ignore[call-arg]

    assert settings.deepgram_api_key == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("08:00", (8, 0)),
        ("23:59", (23, 59)),
        (" 06:30 ", (6, 30)),
        ("24:00", None),
        ("8:00", None),
        ("08:60", None),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_clock_time(raw: str, expected: tuple[int, int] | None) -> None:
    assert parse_clock_time(raw) == expected


def _tz_env(tmp_path: Path, timezone: str | None) -> Path:
    lines = ["TELEGRAM_BOT_TOKEN=test-token", "OWNER_TELEGRAM_ID=1"]
    if timezone is not None:
        lines.append(f"TIMEZONE={timezone}")
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines), encoding="utf-8")
    return env_file


def test_timezone_defaults_to_system(tmp_path: Path) -> None:
    settings = Settings(_env_file=_tz_env(tmp_path, None))  # type: ignore[call-arg]
    assert settings.timezone == ""
    settings = Settings(_env_file=_tz_env(tmp_path, "  "))  # type: ignore[call-arg]
    assert settings.timezone == ""


def test_timezone_accepts_iana_name(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=_tz_env(tmp_path, " Europe/Moscow ")
    )
    assert settings.timezone == "Europe/Moscow"


def test_timezone_rejects_unknown_name(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="IANA"):
        Settings(_env_file=_tz_env(tmp_path, "Mars/Base"))  # type: ignore[call-arg]


@pytest.mark.real_env_file
def test_get_settings_applies_owner_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from d_brain import clock
    from d_brain.config import get_settings

    _tz_env(tmp_path, "Europe/Moscow")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        get_settings()
        assert str(clock.owner_tz()) == "Europe/Moscow"
    finally:
        get_settings.cache_clear()
