from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import AppConfig
from app.companies.registry import load_registry, verified_greenhouse_entries
from app.database.connection import initialize_database, make_engine, make_session_factory
from app.database.repository import JobRepository, NotificationClaimResult
from app.filters.decision import FilterStatus
from app.filters.employment_filter import matches_employment
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.keyword_filter import matches_keywords
from app.filters.location_filter import matches_location, preferred_city_rank
from app.filters.seniority_filter import matches_seniority
from app.notifications.telegram import send_jobs, send_summary
from app.scouts.greenhouse import GreenhouseScout
from app.utils.logger import configure_logging

LOG = logging.getLogger(__name__)


def _notification_priority(job: dict, preferred_cities: list[str]) -> tuple[int, int, float]:
    location = str(job.get("location") or "")
    is_remote_india_compatible = "remote" in location.casefold() and matches_location(job).status is FilterStatus.PASS
    if is_remote_india_compatible:
        category, city_rank = 0, 0
    else:
        city_rank = preferred_city_rank(job, preferred_cities)
        if city_rank < len(preferred_cities):
            category = 1
        else:
            category, city_rank = 2, 0
    timestamp = job.get("posted_at") or job.get("discovered_at")
    try:
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            newest = timestamp.timestamp()
        else:
            newest = 0.0
    except (TypeError, ValueError, OverflowError):
        newest = 0.0
    return category, city_rank, -newest


def filter_jobs(jobs: list[dict], config: AppConfig) -> list[dict]:
    filters = config.filters
    selected: list[dict] = []
    preferred_cities = list(dict.fromkeys([*filters.preferred_cities, *filters.locations]))
    for job in jobs:
        if job.get("experience_requirement") is None:
            job["experience_requirement"] = parse_experience_requirement(
                f"{job.get('title', '')} {job.get('description', '')}"
            )
        decisions = [
            matches_keywords(job, filters.keywords, filters.excluded_keywords),
            matches_seniority(job, filters.seniority_titles),
            matches_experience(job, filters.max_min_years),
            matches_location(job),
            matches_employment(job, filters.employment_types),
        ]
        if any(decision.status is FilterStatus.REJECT for decision in decisions):
            continue
        job["uncertainty_reasons"] = [
            decision.reason for decision in decisions
            if decision.status is FilterStatus.UNCERTAIN
        ]
        if job.get("experience_requirement") is None:
            job["experience_requirement"] = None
        selected.append(job)
    return sorted(selected, key=lambda job: preferred_city_rank(job, preferred_cities))


async def discover(config: AppConfig, *, stats: dict | None = None) -> list[dict]:
    companies = verified_greenhouse_entries(load_registry(config.company_registry_path))
    if not companies:
        LOG.info("No verified Greenhouse companies with board identifiers are configured")
        return []
    scout = GreenhouseScout(timeout=config.scout.request_timeout_seconds, retries=config.scout.retries, concurrency=config.scout.concurrency)
    try:
        results = await asyncio.gather(*(scout.fetch_jobs(company.board_identifier) for company in companies), return_exceptions=True)
        jobs: list[dict] = []
        for company, result in zip(companies, results):
            if isinstance(result, Exception):
                LOG.error("Failed to fetch %s Greenhouse board %s: %s", company.company_name, company.board_identifier, result)
                if stats is not None:
                    stats["errors"] = stats.get("errors", 0) + 1
            else:
                for job in result:
                    job["company"] = company.company_name
                jobs.extend(result)
        # A role can appear under multiple boards; canonical URLs are the source
        # of identity for both in-memory dry runs and persisted discovery.
        if stats is not None:
            stats["fetched"] = len(jobs)
        unique: dict[str, dict] = {}
        filtered = filter_jobs(jobs, config)
        if stats is not None:
            stats["filtered"] = len(filtered)
        for job in filtered:
            unique.setdefault(job["canonical_url"], job)
        return list(unique.values())
    finally:
        await scout.close()


def baseline(config: AppConfig, *, dry_run: bool = False, limit: int | None = None) -> int:
    return run(config, dry_run=dry_run, baseline_mode=True, limit=limit)


def run(
    config: AppConfig,
    *,
    mode: str = "discovery",
    baseline_mode: bool = False,
    dry_run: bool = False,
    notify: bool = False,
    limit: int | None = None,
) -> int:
    if mode not in {"discovery", "qualified"}:
        raise ValueError("mode must be discovery or qualified")
    if limit is not None and limit < 0:
        raise ValueError("limit must not be negative")
    discovery_stats: dict[str, int] = {"errors": 0}
    jobs = asyncio.run(discover(config, stats=discovery_stats))
    if limit is not None:
        jobs = jobs[:limit]
    if dry_run:
        LOG.info("Dry-run: %d unique matching jobs fetched; database and Telegram are untouched", len(jobs))
        for job in jobs:
            LOG.info("Job: %s | %s | %s", job["title"], job["company"], job["canonical_url"])
        return len(jobs)

    engine = make_engine(config.database_url)
    initialize_database(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        repo = JobRepository(session)
        run_record = repo.create_run()
        inserted = repo.add_many(jobs, baseline=baseline_mode)
        candidates = [
            ({
                "source": stored.source,
                "source_id": stored.source_id,
                "company": stored.company,
                "title": stored.title,
                "location": stored.location,
                "employment_type": stored.employment_type,
                "description": stored.description,
                "canonical_url": stored.canonical_url,
                "content_hash": stored.content_hash,
                "experience_requirement": stored.experience_requirement,
                "posted_at": stored.posted_at,
                "discovered_at": stored.discovered_at,
                "uncertainty_reasons": stored.uncertainty_reasons or [],
            }, stored)
            for stored in repo.unseen()
        ]
        fresh = [job for job, stored in candidates if stored.notified_at is None]
        LOG.info("Found %d unsent matching jobs (%d new this run, mode=%s)", len(fresh), inserted, mode)
        if baseline_mode:
            LOG.info("Baseline stored %d new jobs; notifications skipped", inserted)
            repo.finish_run(run_record, fetched=discovery_stats.get("fetched", len(jobs)),
                            filtered=discovery_stats.get("filtered", len(jobs)), new=inserted,
                            notified=0, errors=discovery_stats.get("errors", 0))
            engine.dispose()
            return inserted
        notify_jobs = fresh if mode == "discovery" else [job for job in fresh if not job["uncertainty_reasons"]]
        preferred_cities = list(dict.fromkeys([*config.filters.preferred_cities, *config.filters.locations]))
        notify_jobs.sort(key=lambda job: _notification_priority(job, preferred_cities))
        if mode == "qualified":
            LOG.info("%d uncertain jobs remain stored for the later matcher", len(fresh) - len(notify_jobs))
        if not notify or not config.telegram.enabled:
            for job in fresh:
                LOG.info("Job: %s | %s | %s", job["title"], job["company"], job["canonical_url"])
            if notify and not config.telegram.enabled:
                LOG.warning("Telegram is disabled in configuration; no notifications sent")
            repo.finish_run(run_record, fetched=discovery_stats.get("fetched", len(jobs)),
                            filtered=discovery_stats.get("filtered", len(jobs)), new=inserted,
                            notified=0, errors=discovery_stats.get("errors", 0))
            engine.dispose()
            return len(notify_jobs)
        config.require_telegram()
        sent = 0
        errors = discovery_stats.get("errors", 0)
        permanently_failed = 0
        stored_for_job = {id(job): stored for job, stored in candidates}
        statuses = repo.notification_statuses(stored.id for job, stored in candidates)
        terminal_jobs = []
        for job in notify_jobs:
            stored = stored_for_job.get(id(job))
            notification = statuses.get(stored.id) if stored is not None else None
            if notification and notification.state == "failed" and notification.attempt_count >= config.notifications.max_attempts:
                terminal_jobs.append(job)
        for job in terminal_jobs:
            stored = stored_for_job[id(job)]
            claim = repo.claim_notification(
                stored.id,
                lease_seconds=config.notifications.claim_lease_seconds,
                max_attempts=config.notifications.max_attempts,
            )
            if claim is NotificationClaimResult.EXHAUSTED:
                permanently_failed += 1
                LOG.error("Permanently failed notification for job %s", stored.id)
        terminal_ids = {id(job) for job in terminal_jobs}
        notify_jobs = [job for job in notify_jobs if id(job) not in terminal_ids]
        cap = config.notifications.per_run_cap
        batch_jobs = notify_jobs[:cap]
        queued_count = max(0, len(notify_jobs) - len(batch_jobs))
        claimed: list[tuple[dict, int]] = []
        for job in batch_jobs:
            stored = stored_for_job.get(id(job))
            if stored is None:
                continue
            claim = repo.claim_notification(
                stored.id,
                lease_seconds=config.notifications.claim_lease_seconds,
                max_attempts=config.notifications.max_attempts,
            )
            if claim is NotificationClaimResult.EXHAUSTED:
                permanently_failed += 1
                LOG.error("Permanently failed notification for job %s", stored.id)
                continue
            if claim is not NotificationClaimResult.CLAIMED:
                continue
            claimed.append((job, stored.id))
        if claimed:
            results = asyncio.run(send_jobs(config.telegram.bot_token, config.telegram.chat_id,
                                            [job for job, _job_id in claimed]))
            for (_job, job_id), confirmed in zip(claimed, results):
                if confirmed:
                    sent += 1
                    repo.mark_notification_sent(job_id)
                else:
                    errors += 1
                    repo.mark_notification_failed(job_id, "Telegram did not confirm the message")
        if queued_count:
            summary = f"{queued_count} more new jobs queued for the next run"
            if not asyncio.run(send_summary(config.telegram.bot_token, config.telegram.chat_id, summary)):
                errors += 1
                LOG.warning("Telegram did not confirm the queued-jobs summary")
        repo.finish_run(run_record, fetched=discovery_stats.get("fetched", len(jobs)),
                        filtered=discovery_stats.get("filtered", len(jobs)), new=inserted,
                        notified=sent, errors=errors, permanently_failed=permanently_failed)
    engine.dispose()
    return sent
