"""CLI entrypoint for rich Telegram report delivery."""

from __future__ import annotations

import sys

from d_brain.services.telegram_delivery import send_telegram_text_sync


def main() -> int:
    text = sys.stdin.read().strip()
    if not text:
        return 0
    send_telegram_text_sync(text, rich=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
