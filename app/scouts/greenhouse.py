"""Greenhouse public Job Board API client and normalizer."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.filters.experience_filter import parse_experience_requirement
from app.filters.age_filter import age_days_at
from app.schemas.job import Job as JobSchema
from app.scouts.base import Scout
from app.scouts.retry import (
    DEFAULT_BACKOFF_CAP_SECONDS, DEFAULT_BACKOFF_SECONDS,
    retry_after_header, retry_after_seconds, wait_before_retry,
)
from app.utils.hashing import content_hash
from app.utils.urls import normalize_url

LOG = logging.getLogger(__name__)
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
BLOCK_TAGS = {"address", "article", "br", "div", "h1", "h2", "h3", "h4", "li", "ol", "p", "section", "tr", "ul"}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class GreenhouseResponseError(RuntimeError):
    """The API returned JSON that does not match the documented response shape."""


class GreenhouseRateLimitedError(RuntimeError):
    def __init__(self, board: str, retry_after: str | None, retry_after_seconds_value: float | None):
        self.board = board
        self.host = "boards-api.greenhouse.io"
        self.retry_after = retry_after
        self.retry_after_seconds = retry_after_seconds_value
        super().__init__(f"Greenhouse board {board} was rate limited")


class _PlainTextParser(HTMLParser):
    """Extract readable text from HTML while discarding script/style contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _plain_text(value: str | None) -> str:
    """Strip markup and decode both ordinary and API-escaped HTML entities."""
    if not value:
        return ""
    decoded = value
    # Greenhouse documents content that can be entity-escaped more than once.
    for _ in range(8):
        next_value = unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parser = _PlainTextParser()
    parser.feed(decoded)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def _posted_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = parsedate_to_datetime(value)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None


def _validate_job_record(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "job record must be an object"
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "title is missing or is not a non-empty string"
    location = raw.get("location")
    if location is not None and not isinstance(location, dict):
        return None, "location must be an object or null"
    if isinstance(location, dict) and location.get("name") is not None and not isinstance(location["name"], str):
        return None, "location.name must be a string or null"
    content = raw.get("content")
    if content is not None and not isinstance(content, str):
        return None, "content must be a string or null"
    job_id = raw.get("id")
    if job_id is not None and (isinstance(job_id, bool) or not isinstance(job_id, (str, int))):
        return None, "id must be a string or integer"
    absolute_url = raw.get("absolute_url")
    if not isinstance(absolute_url, str) or not absolute_url.strip():
        return None, "absolute_url is missing or empty"
    try:
        parts = urlsplit(absolute_url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None, "absolute_url must be an absolute HTTP(S) URL"
        normalize_url(absolute_url)
    except (TypeError, ValueError):
        return None, "absolute_url is invalid"
    return raw, None


def normalize_greenhouse_job(
    raw: dict, board: str, *, run_time: datetime | None = None
) -> dict:
    """Validate normalized fields with JobSchema and return ORM-compatible values."""
    location_data = raw.get("location") or {}
    location = location_data.get("name") or ""
    canonical = normalize_url(raw["absolute_url"])
    description = _plain_text(raw.get("content"))
    title = raw.get("title") or ""
    source_id = raw.get("id")
    experience_requirement = parse_experience_requirement(f"{title} {description}")
    first_published = _posted_at(raw.get("first_published"))
    updated_at = _posted_at(raw.get("updated_at"))
    if first_published is not None:
        posted_at, age_source = first_published, "posted"
    elif updated_at is not None:
        posted_at, age_source = updated_at, "updated"
    else:
        posted_at, age_source = None, None
    employment_type = ""
    metadata = raw.get("metadata")
    if isinstance(metadata, list):
        employment_value = next(
            (
                item.get("value")
                for item in metadata
                if isinstance(item, dict)
                and item.get("name") == "Employment Type"
                and isinstance(item.get("value"), str)
            ),
            None,
        )
        employment_key = re.sub(r"[^a-z]+", " ", (employment_value or "").casefold()).strip()
        employment_type = {
            "full time": "full-time",
            "fulltime": "full-time",
            "contract": "contract",
            "contractor": "contract",
            "intern": "intern",
            "internship": "intern",
            "part time": "part-time",
        }.get(employment_key, "")
    normalized = {
        "source": "greenhouse",
        "source_id": str(source_id if source_id is not None else f"{board}:{canonical}"),
        "company": board,
        "title": title,
        "location": location,
        # The public Job Board API supplies departments, not employment type.
        # Leave this empty rather than misclassifying department names.
        "employment_type": employment_type,
        "description": description,
        "canonical_url": canonical,
        "content_hash": content_hash(title, location, description, employment_type),
        "experience_requirement": experience_requirement,
        "posted_at": posted_at,
        "age_source": age_source,
        "age_days": age_days_at({"posted_at": posted_at}, run_time) if posted_at is not None else None,
    }
    return JobSchema.model_validate(normalized).to_orm_dict()


_retry_after_seconds = retry_after_seconds
_wait_before_retry = wait_before_retry


class GreenhouseScout(Scout):
    def __init__(self, *, timeout: float = 15, retries: int = 3, concurrency: int = 4,
                 retry_backoff_seconds=DEFAULT_BACKOFF_SECONDS,
                 retry_backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
                 client: httpx.AsyncClient | None = None):
        self.timeout, self.retries = timeout, retries
        self.retry_backoff_seconds = tuple(retry_backoff_seconds)
        self.retry_backoff_cap_seconds = retry_backoff_cap_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self.client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch_jobs(
        self, board: str, *, run_time: datetime | None = None
    ) -> list[dict]:
        if not isinstance(board, str) or not board.strip():
            raise ValueError("Greenhouse board token must not be empty")
        url = API.format(board=board.strip())
        run_time = run_time or datetime.now(timezone.utc)
        async with self._semaphore:
            for attempt in range(self.retries + 1):
                try:
                    response = await self.client.get(url)
                except (httpx.TransportError, OSError) as exc:
                    if attempt >= self.retries:
                        LOG.error("Greenhouse request failed for board %s after %s attempts: %s", board, attempt + 1, exc)
                        raise
                    LOG.warning("Greenhouse network request failed for %s; retrying", board)
                    await _wait_before_retry(
                        attempt,
                        backoff_seconds=self.retry_backoff_seconds,
                        cap_seconds=self.retry_backoff_cap_seconds,
                    )
                    continue

                transient_status = response.status_code in RETRYABLE_STATUS_CODES
                if transient_status:
                    if attempt >= self.retries:
                        if response.status_code == 429:
                            retry_value = retry_after_header(response)
                            LOG.warning(
                                "Greenhouse host boards-api.greenhouse.io rate limited board %s; Retry-After=%s",
                                board, retry_value or "absent",
                            )
                            raise GreenhouseRateLimitedError(
                                board, retry_value, retry_after_seconds(response)
                            )
                        response.raise_for_status()
                    if response.status_code == 429:
                        LOG.info(
                            "Greenhouse host boards-api.greenhouse.io returned HTTP 429 for board %s; Retry-After=%s",
                            board, retry_after_header(response) or "absent",
                        )
                    else:
                        LOG.warning("Greenhouse returned HTTP %s for board %s; retrying", response.status_code, board)
                    await _wait_before_retry(
                        attempt, response,
                        backoff_seconds=self.retry_backoff_seconds,
                        cap_seconds=self.retry_backoff_cap_seconds,
                    )
                    continue

                # All permanent 4xx responses, including 400/401/403/404, fail
                # immediately. Other unexpected non-2xx statuses also fail.
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise GreenhouseResponseError(f"Greenhouse response for {board} was not valid JSON") from exc
                if not isinstance(payload, dict):
                    raise GreenhouseResponseError(f"Greenhouse response for {board} must be a JSON object")
                records = payload.get("jobs")
                if not isinstance(records, list):
                    raise GreenhouseResponseError(f"Greenhouse response for {board} must contain a jobs array")

                jobs: list[dict] = []
                for index, raw in enumerate(records):
                    valid, reason = _validate_job_record(raw)
                    if valid is None:
                        job_id = raw.get("id") if isinstance(raw, dict) else None
                        LOG.warning("Skipping malformed Greenhouse job board=%s index=%s id=%r: %s", board, index, job_id, reason)
                        continue
                    try:
                        jobs.append(normalize_greenhouse_job(valid, board, run_time=run_time))
                    except ValidationError as exc:
                        job_id = valid.get("id")
                        LOG.warning("Skipping invalid normalized Greenhouse job board=%s index=%s id=%r: %s", board, index, job_id, exc)
                return jobs
        return []
