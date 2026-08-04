"""CLI entrypoint for PLAUD sync."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from d_brain.config import get_settings
from d_brain.services.plaud import (
    PlaudAuthError,
    PlaudSyncAlreadyRunningError,
    PlaudSyncService,
    plaud_state_lock,
    write_json_atomic,
)
from d_brain.services.telegram_delivery import send_telegram_text_sync

logger = logging.getLogger(__name__)
PLAUD_AUTH_ALERT_KEY = "plaud_auth"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _state_defaults() -> dict[str, Any]:
    return {"recordings": {}, "dirty_weeks": [], "last_sync_at": None, "alerts": {}}


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return _state_defaults()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Invalid PLAUD state file while loading auth alert state")
        return _state_defaults()
    payload.setdefault("recordings", {})
    payload.setdefault("dirty_weeks", [])
    payload.setdefault("last_sync_at", None)
    payload.setdefault("alerts", {})
    return cast(dict[str, Any], payload)


def _save_state(state_path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(state_path, payload)


def _get_auth_alert(payload: dict[str, Any]) -> dict[str, Any]:
    alerts = cast(dict[str, Any], payload.setdefault("alerts", {}))
    alert = cast(dict[str, Any], alerts.setdefault(PLAUD_AUTH_ALERT_KEY, {}))
    alert.setdefault("active", False)
    return alert


def _notify_auth_failure(
    *,
    state_path: Path,
    error_message: str,
) -> bool:
    with plaud_state_lock(state_path):
        payload = _load_state(state_path)
        alert = _get_auth_alert(payload)
        if (
            alert.get("active") is True
            and alert.get("signature") == error_message
            and alert.get("last_notified_at")
        ):
            return False

        text = (
            "PLAUD sync: авторизация не удалась.\n"
            "Проверь PLAUD_BEARER_TOKEN и при необходимости обнови "
            "bearer из PLAUD Web.\n"
            f"Ошибка: {error_message}"
        )
        send_telegram_text_sync(text)
        alert.update(
            {
                "active": True,
                "signature": error_message,
                "last_error": error_message,
                "last_notified_at": _utc_now_iso(),
            }
        )
        _save_state(state_path, payload)
        return True


def _notify_auth_recovered(
    *,
    state_path: Path,
) -> bool:
    with plaud_state_lock(state_path):
        payload = _load_state(state_path)
        alert = _get_auth_alert(payload)
        if alert.get("active") is not True:
            return False

        send_telegram_text_sync(
            "PLAUD sync: авторизация восстановлена, синхронизация снова работает."
        )
        alert.update(
            {
                "active": False,
                "resolved_at": _utc_now_iso(),
            }
        )
        _save_state(state_path, payload)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync PLAUD recordings into the vault")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch the full archive instead of only the latest page",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Limit number of PLAUD pages to fetch",
    )
    parser.add_argument(
        "--no-retro-todo",
        action="store_true",
        help="Disable retrospective Todoist creation for recent backfill items",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.plaud_bearer_token:
        sys.stderr.write("PLAUD_BEARER_TOKEN is not configured\n")
        return 1
    state_path = settings.vault_path / ".sync" / "plaud-state.json"

    service = PlaudSyncService(
        settings.vault_path,
        bearer_token=settings.plaud_bearer_token,
        region=settings.plaud_region,
        ai_cli=settings.ai_cli,
        todoist_api_key=settings.todoist_api_key,
        owner_full_name=settings.owner_full_name,
        content_language=settings.content_language,
    )
    try:
        result = service.sync(
            backfill=args.backfill,
            allow_retro_todo=not args.no_retro_todo,
            max_pages=args.max_pages,
        )
    except PlaudSyncAlreadyRunningError as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "seen": 0,
                    "imported": 0,
                    "pending_summary": 0,
                    "unchanged": 0,
                    "tasks_created": 0,
                    "dirty_weeks": [],
                    "errors": [],
                    "skipped": True,
                    "reason": str(exc),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0
    except PlaudAuthError as exc:
        try:
            _notify_auth_failure(
                state_path=state_path,
                error_message=str(exc),
            )
        except Exception as notify_exc:
            logger.warning("Failed to send PLAUD auth alert: %s", notify_exc)
        sys.stderr.write(f"{exc}\n")
        return 1
    finally:
        service.close()

    try:
        _notify_auth_recovered(
            state_path=state_path,
        )
    except Exception as notify_exc:
        logger.warning("Failed to send PLAUD auth recovery alert: %s", notify_exc)

    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
