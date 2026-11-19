"""Polite Workday CXS scout; robots.txt checked by user on 2026-11-16.
The tenant disallows /refreshFacet/ only; /wday/ paths are allowed.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.companies.registry import WorkdayTenantConfig
from app.config import AppConfig
from app.filters.decision import FilterStatus
from app.filters.age_filter import age_days_at
from app.filters.evaluation import evaluate_filter_decisions
from app.filters.experience_filter import (
    DEFAULT_PREFERRED_HEADINGS, DEFAULT_REQUIRED_HEADINGS,
    parse_experience_requirement,
)
from app.scouts.greenhouse import RETRYABLE_STATUS_CODES
from app.scouts.retry import (
    DEFAULT_BACKOFF_CAP_SECONDS, DEFAULT_BACKOFF_SECONDS,
    retry_after_header, retry_after_seconds, wait_before_retry,
)
from app.schemas.job import Job as JobSchema
from app.utils.hashing import content_hash
from app.utils.urls import normalize_url

LOG = logging.getLogger(__name__)
_wait_before_retry = wait_before_retry
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "User-Agent": "ApplySense/0.1 (personal job search)",
}
TITLE_LEVEL_DEFAULTS = ("IV", "V", "VI", "4", "5", "6")
MULTIPLE_LOCATIONS = re.compile(r"^\d+\s+locations?$", re.I)
BLOCK_TAGS = {"br", "p", "li", "h2"}


class _WorkdayTextParser(HTMLParser):
    """Iteratively flatten nested Workday markup while keeping section breaks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored_depth:
            self.parts.append(data)


def workday_plain_text(value: str | None) -> str:
    if not value:
        return ""
    decoded = value
    for _ in range(8):
        next_value = html.unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parser = _WorkdayTextParser()
    parser.feed(decoded)
    parser.close()
    flattened = "".join(parser.parts)
    flattened = re.sub(r"[ \t\f\v]+", " ", flattened)
    flattened = re.sub(r" *\n *", "\n", flattened)
    flattened = re.sub(r"\n{2,}", "\n\n", flattened)
    return flattened.strip()


class WorkdayResponseError(RuntimeError):
    """Workday CXS returned non-JSON or an unsupported response shape."""


class WorkdayRobotsBlockedError(RuntimeError):
    """A request targets a path disallowed by the tenant robots policy."""


class WorkdayRateLimitedError(RuntimeError):
    def __init__(self, host: str, retry_after: str | None, retry_after_seconds_value: float | None,
                 *, circuit_open: bool = False):
        self.host = host
        self.retry_after = retry_after
        self.retry_after_seconds = retry_after_seconds_value
        self.circuit_open = circuit_open
        super().__init__(f"Workday host {host} was rate limited")


@dataclass(frozen=True)
class WorkdayEvaluation:
    source_id: str
    decision: str
    reason: str
    job: dict[str, Any] | None
    reached_details: bool = True
    remember: bool = False


def title_level_rejection(title: str, excluded_levels: list[str] | None = None) -> str | None:
    levels = list(TITLE_LEVEL_DEFAULTS) if excluded_levels is None else excluded_levels
    for level in levels:
        term = str(level).strip()
        if term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.I):
            return term
    return None


def _list_source_id(item: dict[str, Any], tenant: WorkdayTenantConfig) -> str | None:
    bullet_fields = item.get("bulletFields")
    if not isinstance(bullet_fields, list) or not bullet_fields:
        return None
    requisition_id = bullet_fields[0]
    if not isinstance(requisition_id, str) or not requisition_id.strip():
        return None
    return f"{tenant.tenant}/{requisition_id.strip()}"


def _list_job(item: dict[str, Any], company_name: str, tenant: WorkdayTenantConfig) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    external_path = item.get("externalPath")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(external_path, str) or not external_path.startswith("/"):
        return None
    source_id = _list_source_id(item, tenant)
    if source_id is None:
        return None
    posted_on = str(item.get("postedOn") or "")
    age_days = parse_workday_posted_age(posted_on)
    job = {
        "source": "workday",
        "source_id": source_id,
        "company": company_name,
        "title": title.strip(),
        "location": str(item.get("locationsText") or ""),
        "external_path": external_path,
        "posted_on": posted_on,
        "age_days": age_days,
        "age_source": "posted" if age_days is not None else None,
    }
    if age_days is None:
        job["_age_unknown_is_uncertain"] = True
    return job


def parse_workday_posted_age(label: str) -> int | None:
    text = " ".join(str(label or "").casefold().split())
    if text == "posted today":
        return 0
    if text == "posted yesterday":
        return 1
    match = re.fullmatch(r"posted\s+(\d+)\s+days?\s+ago", text)
    if match:
        return int(match.group(1))
    if text == "posted 30+ days ago":
        return 30
    return None


def _stage_one_rejection(
    job: dict[str, Any],
    config: AppConfig,
    tenant: WorkdayTenantConfig,
    run_time: datetime | None = None,
) -> tuple[str, str] | None:
    run_time = run_time or datetime.now(timezone.utc)
    title = job["title"]
    _individual, pipeline, _first = evaluate_filter_decisions(
        job, config, run_time=run_time
    )
    age = pipeline["age"]
    if age.status is FilterStatus.REJECT:
        return "age", age.reason
    keyword = pipeline["keyword"]
    if keyword.status is FilterStatus.REJECT:
        return "keyword", keyword.reason
    if keyword.status is FilterStatus.UNCERTAIN:
        return "keyword", keyword.reason
    seniority = pipeline["seniority"]
    if seniority.status is not FilterStatus.PASS:
        return "seniority", seniority.reason
    level = title_level_rejection(title, tenant.excluded_title_levels)
    if level is not None:
        return "seniority", f"Title contains excluded Workday level {level}."
    return None


def _posted_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_by(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except (TypeError, ValueError, OverflowError):
        return None


def _posted_age_disagreement(
    label: str, label_age: int | None, start_date_age: int | None
) -> str | None:
    """Classify date-label differences; 30+ is a lower-bound label."""
    if not isinstance(start_date_age, int) or not isinstance(label_age, int):
        return None
    normalized_label = " ".join(str(label or "").casefold().split())
    if normalized_label == "posted 30+ days ago":
        return "real" if start_date_age < 30 else None
    difference = abs(start_date_age - label_age)
    if difference == 0:
        return None
    return "minor" if difference <= 1 else "real"


def _time_type(value: Any) -> str:
    text = " ".join(str(value or "").casefold().split())
    if text in {"full time", "full-time", "fulltime"}:
        return "full-time"
    if text in {"part time", "part-time", "parttime"}:
        return "part-time"
    return text


def normalize_workday_job(
    list_job: dict[str, Any],
    detail_payload: dict[str, Any],
    tenant: WorkdayTenantConfig,
    *,
    run_time: datetime | None = None,
    required_experience_headings: list[str] | None = None,
    preferred_experience_headings: list[str] | None = None,
) -> dict[str, Any]:
    info = detail_payload.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise WorkdayResponseError("Workday details response must contain a jobPostingInfo object")
    expected_req_id = list_job["source_id"].split("/", 1)[1]
    req_id = info.get("jobReqId")
    if not isinstance(req_id, str) or req_id != expected_req_id:
        raise ValueError(f"jobReqId mismatch for {list_job['source_id']}")
    title = info.get("title") if isinstance(info.get("title"), str) else list_job["title"]
    description = workday_plain_text(info.get("jobDescription"))
    location_value = info.get("location")
    if isinstance(location_value, dict):
        location = str(location_value.get("name") or list_job["location"] or "")
    else:
        location = str(location_value or list_job["location"] or "")
    requisition_location = info.get("jobRequisitionLocation")
    country_data = (
        requisition_location.get("country")
        if isinstance(requisition_location, dict)
        else None
    )
    country_code = country_data.get("alpha2Code") if isinstance(country_data, dict) else None
    if not isinstance(country_code, str) or not re.fullmatch(r"[A-Za-z]{2}", country_code.strip()):
        country_code = None
    external_url = info.get("externalUrl")
    if not isinstance(external_url, str) or not external_url.strip():
        external_url = f"https://{tenant.host}/{tenant.site}{list_job['external_path']}"
    canonical_url = normalize_url(external_url)
    start_date = _posted_at(info.get("startDate"))
    apply_by = _apply_by(info.get("endDate"))
    label_age = list_job.get("age_days")
    detail_age = age_days_at({"posted_at": start_date}, run_time) if start_date is not None else None
    resolved_age = detail_age if detail_age is not None else label_age
    normalized = {
        "source": "workday",
        "source_id": list_job["source_id"],
        "company": list_job["company"],
        "title": title.strip(),
        "location": location,
        "employment_type": _time_type(info.get("timeType")),
        "description": description,
        "canonical_url": canonical_url,
        "content_hash": content_hash(title, location, description),
        "experience_requirement": parse_experience_requirement(
            f"{title} {description}",
            required_headings=required_experience_headings or DEFAULT_REQUIRED_HEADINGS,
            preferred_headings=preferred_experience_headings or DEFAULT_PREFERRED_HEADINGS,
        ),
        # Workday startDate is retained as informational posted_at, never a filter.
        "posted_at": start_date,
        "apply_by": apply_by,
        "age_source": "posted" if resolved_age is not None else None,
        "age_days": resolved_age,
    }
    job = JobSchema.model_validate(normalized).to_orm_dict()
    if country_code is not None:
        job["_workday_location_country"] = country_code.strip().upper()
    if start_date is None and list_job.get("_age_unknown_is_uncertain"):
        job["_age_unknown_is_uncertain"] = True
    return job


class WorkdayScout:
    def __init__(
        self,
        *,
        timeout: float = 15,
        retries: int = 3,
        request_interval_seconds: float = 2.5,
        retry_backoff_seconds: tuple[float, ...] | list[float] = DEFAULT_BACKOFF_SECONDS,
        retry_backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
        http2: bool = False,
        client: httpx.AsyncClient | None = None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.request_interval_seconds = request_interval_seconds
        self.retry_backoff_seconds = tuple(retry_backoff_seconds)
        self.retry_backoff_cap_seconds = retry_backoff_cap_seconds
        self.http2 = http2
        self.client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, http2=http2
        )
        self._owns_client = client is None
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_request_by_host: dict[str, float] = {}
        self._consecutive_rate_limited_requests: dict[str, int] = {}
        self._open_circuits: set[str] = set()
        self.rate_limited = False
        self.rate_limit_host: str | None = None
        self.rate_limit_retry_after: str | None = None
        self.rate_limit_retry_after_seconds: float | None = None
        self.pending_details_count = 0
        self.closed_count = 0
        self._unknown_age_labels_logged: set[str] = set()
        self._request_debug_logged: set[str] = set()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _guard_url(url: str) -> None:
        if urlsplit(url).path.startswith("/refreshFacet/"):
            raise WorkdayRobotsBlockedError("Workday request blocked by robots policy")

    def _base_url(self, tenant: WorkdayTenantConfig) -> str:
        return f"https://{tenant.host}/wday/cxs/{tenant.tenant}/{tenant.site}"

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        language: bool = False,
        tenant: WorkdayTenantConfig | None = None,
    ) -> dict[str, Any]:
        # robots.txt checked 2026-11-16: /refreshFacet/ is disallowed; /wday/ is not.
        # Keep this request-level guard even if another caller supplies a URL.
        self._guard_url(url)
        host = urlsplit(url).netloc
        if host in self._open_circuits:
            raise WorkdayRateLimitedError(
                host, self.rate_limit_retry_after, self.rate_limit_retry_after_seconds,
                circuit_open=True,
            )
        semaphore = self._host_semaphores.setdefault(host, asyncio.Semaphore(1))
        headers = dict(HEADERS)
        if tenant is not None:
            origin = f"https://{tenant.host}"
            headers["Origin"] = origin
            headers["Referer"] = f"{origin}/{tenant.site}"
        if method == "POST":
            headers["Content-Type"] = "application/json"

        async with semaphore:
            for attempt in range(self.retries + 1):
                last_request = self._last_request_by_host.get(host)
                if last_request is not None and self.request_interval_seconds > 0:
                    remaining = self.request_interval_seconds - (time.monotonic() - last_request)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                self._last_request_by_host[host] = time.monotonic()
                try:
                    response = await self.client.request(
                        method, url, headers=headers, json=json_body
                    )
                    debug_key = tenant.tenant if tenant is not None else host
                    if debug_key not in self._request_debug_logged:
                        request_headers = response.request.headers
                        header_names = sorted(name.lower() for name in request_headers.keys())
                        LOG.debug(
                            "Workday tenant %s request header names=%s negotiated HTTP version=%s",
                            debug_key, header_names, response.http_version,
                        )
                        self._request_debug_logged.add(debug_key)
                except (httpx.TransportError, OSError):
                    if attempt >= self.retries:
                        raise
                    await _wait_before_retry(
                        attempt, backoff_seconds=self.retry_backoff_seconds,
                        cap_seconds=self.retry_backoff_cap_seconds,
                    )
                    continue
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt >= self.retries:
                        if response.status_code == 429:
                            retry_value = retry_after_header(response)
                            self.rate_limited = True
                            self.rate_limit_host = host
                            self.rate_limit_retry_after = retry_value
                            self.rate_limit_retry_after_seconds = retry_after_seconds(response)
                            failures = self._consecutive_rate_limited_requests.get(host, 0) + 1
                            self._consecutive_rate_limited_requests[host] = failures
                            circuit_open = failures >= 2
                            if circuit_open:
                                self._open_circuits.add(host)
                            raise WorkdayRateLimitedError(
                                host, retry_value, self.rate_limit_retry_after_seconds,
                                circuit_open=circuit_open,
                            )
                        response.raise_for_status()
                    if response.status_code == 429:
                        LOG.info(
                            "Workday host %s returned HTTP 429; Retry-After=%s; retrying",
                            host, retry_after_header(response) or "absent",
                        )
                    await _wait_before_retry(
                        attempt, response,
                        backoff_seconds=self.retry_backoff_seconds,
                        cap_seconds=self.retry_backoff_cap_seconds,
                    )
                    continue
                response.raise_for_status()
                self._consecutive_rate_limited_requests[host] = 0
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise WorkdayResponseError("Workday response was not valid JSON") from exc
                if not isinstance(payload, dict):
                    raise WorkdayResponseError("Workday response must be a JSON object")
                return payload
        raise WorkdayResponseError("Workday request retry loop ended unexpectedly")

    async def fetch_list_page(
        self,
        tenant: WorkdayTenantConfig,
        search_text: str,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        url = f"{self._base_url(tenant)}/jobs"
        body = {
            "appliedFacets": {tenant.country_facet_key: [tenant.country_facet_id]},
            "limit": tenant.page_size,
            "offset": offset,
            "searchText": search_text,
        }
        payload = await self._request_json("POST", url, json_body=body, tenant=tenant)
        postings = payload.get("jobPostings")
        total = payload.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or not isinstance(postings, list):
            raise WorkdayResponseError("Workday list response must contain total and jobPostings")
        return total, postings

    async def fetch_details(
        self, tenant: WorkdayTenantConfig, list_job: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self._base_url(tenant)}{list_job['external_path']}"
        return await self._request_json("GET", url, language=True, tenant=tenant)

    async def fetch_first_page(
        self, tenant: WorkdayTenantConfig, search_text: str | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        return await self.fetch_list_page(
            tenant, search_text or tenant.search_texts[0], offset=0
        )

    async def list_jobs(self, company_name: str, tenant: WorkdayTenantConfig) -> list[dict[str, Any]]:
        deduplicated: dict[str, dict[str, Any]] = {}
        truncated_total: int | None = None
        rate_limited = False
        for search_text in tenant.search_texts:
            offset = 0
            for page_index in range(tenant.max_pages):
                try:
                    total, records = await self.fetch_list_page(tenant, search_text, offset)
                except WorkdayRateLimitedError:
                    rate_limited = True
                    break
                if not records:
                    break
                for item in records:
                    normalized = _list_job(item, company_name, tenant)
                    if normalized is None:
                        LOG.warning("Skipping malformed Workday list item for tenant %s", tenant.tenant)
                        continue
                    deduplicated.setdefault(normalized["source_id"], normalized)
                if len(records) < tenant.page_size:
                    break
                if page_index == tenant.max_pages - 1:
                    truncated_total = max(truncated_total or 0, total)
                offset += tenant.page_size
            if rate_limited:
                break
        if truncated_total is not None:
            LOG.warning(
                "Workday tenant %s: max_pages reached, %d of total %d jobs seen; some jobs may be missing",
                tenant.tenant, len(deduplicated), truncated_total,
            )
        return list(deduplicated.values())

    async def fetch_jobs(
        self,
        company_name: str,
        tenant: WorkdayTenantConfig,
        config: AppConfig,
        *,
        known_source_ids: set[str] | None = None,
        recently_evaluated_source_ids: set[str] | None = None,
        baseline_mode: bool = False,
        run_time: datetime | None = None,
    ) -> list[WorkdayEvaluation]:
        run_time = run_time or datetime.now(timezone.utc)
        self.rate_limited = False
        self.rate_limit_host = None
        self.rate_limit_retry_after = None
        self.rate_limit_retry_after_seconds = None
        self.pending_details_count = 0
        self.closed_count = 0
        known = known_source_ids or set()
        recently_evaluated = recently_evaluated_source_ids or set()
        list_jobs = await self.list_jobs(company_name, tenant)
        evaluations: list[WorkdayEvaluation] = []
        candidates = []
        for item in list_jobs:
            label = item.get("posted_on", "")
            if item.get("age_days") is None and label not in self._unknown_age_labels_logged:
                LOG.warning("Unrecognized Workday postedOn label: %s", label or "(empty)")
                self._unknown_age_labels_logged.add(label)
            source_id = item["source_id"]
            if source_id in known or source_id in recently_evaluated:
                continue
            stage_one_rejection = _stage_one_rejection(item, config, tenant, run_time)
            if stage_one_rejection is not None:
                rejection_filter, reason = stage_one_rejection
                evaluations.append(WorkdayEvaluation(
                    source_id,
                    FilterStatus.REJECT.value,
                    reason,
                    item,
                    reached_details=False,
                    remember=rejection_filter == "age",
                ))
            elif baseline_mode:
                age = evaluate_filter_decisions(
                    item, config, run_time=run_time
                )[1]["age"]
                decision = (
                    FilterStatus.UNCERTAIN.value
                    if age.status is FilterStatus.UNCERTAIN
                    else FilterStatus.PASS.value
                )
                evaluations.append(WorkdayEvaluation(
                    source_id,
                    decision,
                    "List-stage filters passed; baseline recorded without fetching details.",
                    item,
                    reached_details=False,
                    remember=True,
                ))
            else:
                candidates.append(item)
        details_count = 0
        real_age_disagreements = 0
        for candidate_index, item in enumerate(candidates):
            if details_count >= tenant.max_detail_requests_per_run:
                self.pending_details_count += len(candidates) - candidate_index
                break
            details_count += 1
            try:
                detail_payload = await self.fetch_details(tenant, item)
            except WorkdayRateLimitedError as exc:
                self.rate_limited = True
                self.rate_limit_host = exc.host
                self.rate_limit_retry_after = exc.retry_after
                self.rate_limit_retry_after_seconds = exc.retry_after_seconds
                self.pending_details_count += 1
                if exc.circuit_open:
                    self.pending_details_count += len(candidates) - candidate_index - 1
                    break
                # Keep trying in feed order. Successful details remain usable;
                # this requisition is not returned or remembered and is retried
                # from the list feed on the next run.
                continue
            try:
                job = normalize_workday_job(
                    item, detail_payload, tenant, run_time=run_time,
                    required_experience_headings=config.filters.experience_requirement_headings,
                    preferred_experience_headings=config.filters.experience_preferred_headings,
                )
            except (ValueError, ValidationError) as exc:
                reason = str(exc)
                LOG.warning("Skipping Workday job tenant=%s source_id=%s: %s", tenant.tenant, item["source_id"], reason)
                evaluations.append(WorkdayEvaluation(item["source_id"], "REJECT", reason, item))
                continue
            disagreement = _posted_age_disagreement(
                item.get("posted_on"), item.get("age_days"), job.get("age_days")
            ) if job.get("posted_at") is not None else None
            if disagreement == "minor":
                LOG.debug(
                    "Workday postedOn age disagrees with startDate by at most one day for %s; using startDate",
                    item["source_id"],
                )
            elif disagreement == "real":
                real_age_disagreements += 1
            run_utc = run_time if run_time.tzinfo else run_time.replace(tzinfo=timezone.utc)
            apply_by = job.get("apply_by")
            if apply_by is not None and apply_by < run_utc.astimezone(timezone.utc).date():
                self.closed_count += 1
                evaluations.append(WorkdayEvaluation(
                    item["source_id"], FilterStatus.REJECT.value,
                    f"closed: application deadline {apply_by.isoformat()} has passed.",
                    job,
                ))
                continue
            _individual, pipeline, _first_rejection = evaluate_filter_decisions(
                job, config, run_time=run_time
            )
            rejected = next(
                (decision for decision in pipeline.values() if decision.status is FilterStatus.REJECT),
                None,
            )
            if rejected is not None:
                decision = FilterStatus.REJECT.value
                reason = rejected.reason
                safe_job = job
            else:
                uncertainties = [
                    item_decision.reason for item_decision in pipeline.values()
                    if item_decision.status is FilterStatus.UNCERTAIN
                ]
                decision = (
                    FilterStatus.UNCERTAIN.value if uncertainties else FilterStatus.PASS.value
                )
                reason = "; ".join(uncertainties) if uncertainties else "All configured filters passed."
                job["uncertainty_reasons"] = uncertainties
                safe_job = job
            evaluations.append(WorkdayEvaluation(item["source_id"], decision, reason, safe_job))
        if real_age_disagreements:
            LOG.warning(
                "Workday postedOn age disagrees with startDate for %d jobs in tenant %s; using startDate",
                real_age_disagreements, tenant.tenant,
            )
        return evaluations
