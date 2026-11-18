"""Telegram Bot API sender with safe, per-message failure handling."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.notifications.formatting import format_job

LOG = logging.getLogger(__name__)
MESSAGE_DELAY_SECONDS = 0.1
MAX_RATE_LIMIT_RETRIES = 3


async def _send_text(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    client: httpx.AsyncClient,
    retries: int = MAX_RATE_LIMIT_RETRIES,
) -> bool:
    # Never include this URL (which embeds the bot token) in errors or logs.
    endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(retries + 1):
        try:
            response = await client.post(endpoint, json=payload)
        except httpx.HTTPError:
            LOG.warning("Telegram request failed before receiving a response")
            return False
        try:
            result: dict[str, Any] = response.json()
        except ValueError:
            result = {}
        if response.status_code == 429:
            retry_after = result.get("parameters", {}).get("retry_after")
            if attempt < retries:
                try:
                    delay = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 1.0
                await asyncio.sleep(delay)
                continue
            LOG.warning("Telegram rate limit retry limit reached")
            return False
        if response.status_code == 400:
            LOG.warning("Telegram rejected one message (HTTP 400)")
            return False
        if not response.is_success or result.get("ok") is not True:
            LOG.warning("Telegram message was not accepted (HTTP %d)", response.status_code)
            return False
        return True
    return False


async def send_jobs(
    bot_token: str,
    chat_id: str,
    jobs: list[dict],
    *,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
    message_delay: float = MESSAGE_DELAY_SECONDS,
    run_time=None,
) -> list[bool]:
    """Send each job independently and return one confirmed result per job."""
    if dry_run:
        for job in jobs:
            LOG.info("Telegram dry-run message:\n%s", format_job(job, run_time=run_time))
        return [False for _ in jobs]

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=20.0)
    results: list[bool] = []
    try:
        for index, job in enumerate(jobs):
            if index:
                await asyncio.sleep(message_delay)
            results.append(await _send_text(
                bot_token, chat_id, format_job(job, run_time=run_time), client=active_client
            ))
    finally:
        if owns_client:
            await active_client.aclose()
    return results


async def send_summary(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
    delay_before: bool = True,
) -> bool:
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=20.0)
    try:
        if delay_before:
            await asyncio.sleep(MESSAGE_DELAY_SECONDS)
        return await _send_text(bot_token, chat_id, text, client=active_client)
    finally:
        if owns_client:
            await active_client.aclose()
