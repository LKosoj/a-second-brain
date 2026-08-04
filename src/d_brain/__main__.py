"""Entry point for running d-brain as a module."""

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


async def main() -> None:
    """Main entry point."""
    from d_brain.bot.main import run_bot
    from d_brain.config import get_settings

    settings = get_settings()
    logger.info("d-brain starting...")
    logger.info("Vault path: %s", settings.vault_path)
    logger.info("Owner Telegram ID: %s", settings.owner_telegram_id)

    await run_bot(settings)


if __name__ == "__main__":
    asyncio.run(main())
