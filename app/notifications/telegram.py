import logging

from telegram import Bot

from app.notifications.formatting import format_job

LOG = logging.getLogger(__name__)


async def send_jobs(bot_token: str, chat_id: str, jobs: list[dict], *, dry_run: bool = False) -> int:
    """Send jobs; dry-run logs messages and never contacts Telegram."""
    if dry_run:
        for job in jobs:
            LOG.info("Telegram dry-run message:\n%s", format_job(job))
        return len(jobs)
    sent = 0
    async with Bot(token=bot_token) as bot:
        for job in jobs:
            await bot.send_message(chat_id=chat_id, text=format_job(job), disable_web_page_preview=True)
            sent += 1
    return sent
