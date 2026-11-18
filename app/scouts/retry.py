"""Shared HTTP retry timing for ATS scouts."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

DEFAULT_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
DEFAULT_BACKOFF_CAP_SECONDS = 120.0


def retry_after_header(response: httpx.Response | None) -> str | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    return value.strip() if value and value.strip() else None


def retry_after_seconds(
    response: httpx.Response | None,
    *,
    now: datetime | None = None,
) -> float | None:
    value = retry_after_header(response)
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            now_utc = now or datetime.now(timezone.utc)
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - now_utc.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


async def wait_before_retry(
    attempt: int,
    response: httpx.Response | None = None,
    *,
    backoff_seconds: tuple[float, ...] | list[float] = DEFAULT_BACKOFF_SECONDS,
    cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
    sleep=None,
) -> float:
    """Sleep for Retry-After or an exponentially increasing jittered delay.

    The returned delay is exposed for tests and diagnostics. Retry-After is
    honored as provided; the cap applies to generated exponential backoff.
    """
    delay = retry_after_seconds(response)
    if delay is None:
        schedule = tuple(float(value) for value in backoff_seconds) or DEFAULT_BACKOFF_SECONDS
        if attempt < len(schedule):
            ceiling = schedule[attempt]
        else:
            ceiling = schedule[-1] * (3 ** (attempt - len(schedule) + 1))
        delay = min(float(cap_seconds), ceiling * random.uniform(0.8, 1.2))
    await (sleep or asyncio.sleep)(delay)
    return delay
