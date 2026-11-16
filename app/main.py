from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import AppConfig
from app.companies.registry import (
    CompanyEntry, load_registry,
    verified_greenhouse_entries, verified_workday_entries,
)
from app.database.connection import (
    initialize_database, make_engine, make_session_factory, validate_database_version,
)
from app.database.models import utc_now
from app.database.repository import JobRepository, NotificationClaimResult
from app.filters.decision import FilterStatus
from app.filters.age_filter import age_days_at, matches_age
from app.filters.evaluation import evaluate_filter_decisions
from app.filters.settings import effective_filter_settings_hash
from app.filters.location_filter import matches_location, preferred_city_rank
from app.diagnostics import format_filter_diagnostics
from app.notifications.telegram import send_jobs, send_summary
from app.scouts.greenhouse import GreenhouseScout
from app.scouts.workday import WorkdayScout
from app.utils.logger import configure_logging

LOG = logging.getLogger(__name__)


def _notification_priority(
    job: dict, preferred_cities: list[str], unknown_location_policy: str = "strict"
) -> tuple[int, int, float]:
    location = str(job.get("location") or "")
    is_remote_india_compatible = (
        "remote" in location.casefold()
        and matches_location(job, unknown_location_policy).status is FilterStatus.PASS
    )
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
        _individual, pipeline, _first_rejection = evaluate_filter_decisions(
            job, config,
            location_assume_country=job.get("_workday_assume_country"),
        )
        decisions = list(pipeline.values())
        if any(decision.status is FilterStatus.REJECT for decision in decisions):
            continue
        job["uncertainty_reasons"] = [
            decision.reason for decision in decisions
            if decision.status is FilterStatus.UNCERTAIN
            and not (
                job.get("source") == "greenhouse"
                and decision.reason == "Employment type is not stated."
            )
        ]
        if job.get("experience_requirement") is None:
            job["experience_requirement"] = None
        job.pop("_workday_assume_country", None)
        selected.append(job)
    return sorted(selected, key=lambda job: preferred_city_rank(job, preferred_cities))


def _sqlite_database_exists(database_url: str) -> bool:
    if database_url in {"sqlite://", "sqlite:///:memory:"}:
        return False
    if not database_url.startswith("sqlite:///"):
        return True
    database_path = database_url.removeprefix("sqlite:///")
    if database_path in {":memory:", ""}:
        return False
    return Path(database_path).exists()


def _tenant_run_is_recent(last_finished: datetime | None, interval_seconds: int) -> bool:
    if last_finished is None:
        return False
    if last_finished.tzinfo is None:
        last_finished = last_finished.replace(tzinfo=timezone.utc)
    elapsed = (utc_now() - last_finished).total_seconds()
    return elapsed < interval_seconds


async def _discover_workday(
    config: AppConfig,
    entries: list[CompanyEntry],
    *,
    stats: dict | None,
    include_rejected: bool,
    dry_run: bool,
    force: bool,
    baseline_mode: bool = False,
) -> list[dict]:
    engine = None
    factory = None
    if not dry_run or _sqlite_database_exists(config.database_url):
        engine = make_engine(config.database_url)
        if not dry_run:
            initialize_database(engine)
        else:
            try:
                validate_database_version(engine)
            except Exception:
                engine.dispose()
                raise
        factory = make_session_factory(engine)
    scout = WorkdayScout(
        timeout=config.scout.request_timeout_seconds,
        retries=config.scout.retries,
        request_interval_seconds=1.0,
    )
    jobs: list[dict] = []
    try:
        for company in entries:
            tenant = company.workday
            if tenant is None:
                continue
            source_run = None
            known_ids: set[str] = set()
            recent_ids: set[str] = set()
            context = factory() if factory is not None else None
            repo = JobRepository(context) if context is not None else None
            try:
                if repo is not None:
                    if not force and _tenant_run_is_recent(
                        repo.last_tenant_run("workday", tenant.tenant),
                        tenant.min_interval_seconds,
                    ):
                        LOG.info("Skipping Workday tenant %s because its minimum interval has not elapsed", tenant.tenant)
                        continue
                    known_ids = repo.job_source_ids_for_source("workday")
                    evaluated_ids = repo.evaluated_source_ids_for_source("workday")
                    previous_settings_hashes = repo.evaluation_settings_hashes("workday")
                    active_settings_hash = effective_filter_settings_hash(config)
                    recent_ids = repo.recently_evaluated_source_ids(
                        "workday", evaluated_ids,
                        reevaluate_after_days=tenant.reevaluate_after_days,
                        settings_hash=active_settings_hash,
                    )
                    # Existing passing jobs remain deduped, but after the
                    # evaluation TTL they are fetched again for re-evaluation.
                    stale_evaluations = evaluated_ids - recent_ids
                    stale_evaluations.update(
                        source_id
                        for source_id, saved_hash in previous_settings_hashes.items()
                        if saved_hash != active_settings_hash
                    )
                    known_ids.difference_update(stale_evaluations)
                    if not dry_run:
                        source_run = repo.create_run(source="workday", tenant_key=tenant.tenant)

                try:
                    evaluations = await scout.fetch_jobs(
                        company.company_name,
                        tenant,
                        config,
                        known_source_ids=known_ids,
                        recently_evaluated_source_ids=recent_ids,
                        baseline_mode=baseline_mode,
                    )
                except Exception as exc:
                    LOG.error(
                        "Workday tenant %s failed for this run (%s)",
                        tenant.tenant, type(exc).__name__,
                    )
                    if stats is not None:
                        stats["errors"] = stats.get("errors", 0) + 1
                    if repo is not None and source_run is not None:
                        repo.finish_run(
                            source_run, fetched=0, filtered=0, new=0, notified=0, errors=1
                        )
                    continue

                remembered = [item for item in evaluations if item.reached_details or item.remember]
                if baseline_mode and config.filters.baseline_include_recent:
                    remembered = [
                        item for item in remembered
                        if not (
                            item.job is not None
                            and item.decision != FilterStatus.REJECT.value
                            and age_days_at(item.job) is not None
                            and age_days_at(item.job) <= config.filters.max_age_days
                        )
                    ]
                if repo is not None and not dry_run:
                    repo.record_evaluations([
                        {
                            "source": "workday",
                            "source_id": item.source_id,
                            "decision": item.decision,
                            "reason": item.reason,
                            "settings_hash": effective_filter_settings_hash(config),
                        }
                        for item in remembered
                    ])
                    if source_run is not None:
                        repo.finish_run(
                            source_run,
                            fetched=len(evaluations),
                            filtered=sum(item.decision == FilterStatus.REJECT.value for item in evaluations),
                            new=sum(item.decision != FilterStatus.REJECT.value for item in remembered),
                            notified=0,
                            errors=0,
                        )
                if stats is not None:
                    stats["fetched"] = stats.get("fetched", 0) + len(evaluations)
                for item in evaluations:
                    if (
                        item.job is not None
                        and (item.reached_details or include_rejected)
                        and (include_rejected or item.decision != FilterStatus.REJECT.value)
                    ):
                        jobs.append(item.job)
            finally:
                if context is not None:
                    context.close()
    finally:
        await scout.close()
        if engine is not None:
            engine.dispose()
    return jobs


async def discover(
    config: AppConfig,
    *,
    stats: dict | None = None,
    include_rejected: bool = False,
    dry_run: bool = False,
    force: bool = False,
    baseline_mode: bool = False,
) -> list[dict]:
    companies = verified_greenhouse_entries(load_registry(config.company_registry_path))
    if not companies:
        LOG.info("No verified Greenhouse companies with board identifiers are configured")
    scout = GreenhouseScout(timeout=config.scout.request_timeout_seconds, retries=config.scout.retries, concurrency=config.scout.concurrency)
    try:
        results = await asyncio.gather(
            *(scout.fetch_jobs(company.board_identifier) for company in companies),
            return_exceptions=True,
        )
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
        if stats is not None:
            stats["fetched"] = len(jobs)
        workday_entries = verified_workday_entries(load_registry(config.company_registry_path))
        if workday_entries:
            jobs.extend(await _discover_workday(
                config,
                workday_entries,
                stats=stats,
                include_rejected=include_rejected,
                dry_run=dry_run,
                force=force,
                baseline_mode=baseline_mode,
            ))
        # A role can appear under multiple boards; canonical URLs are the source
        # of identity for both in-memory dry runs and persisted discovery.
        unique: dict[str, dict] = {}
        filtered = filter_jobs(jobs, config)
        if stats is not None:
            stats["filtered"] = len(filtered)
        selected = jobs if include_rejected else filtered
        for job in selected:
            key = job.get("canonical_url") or f"{job.get('source')}:{job.get('source_id')}"
            unique.setdefault(key, job)
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
    explain: bool = False,
    force: bool = False,
) -> int:
    if mode not in {"discovery", "qualified"}:
        raise ValueError("mode must be discovery or qualified")
    if limit is not None and limit < 0:
        raise ValueError("limit must not be negative")
    if explain and not dry_run:
        raise ValueError("--explain requires --dry-run")
    workday_entries = verified_workday_entries(load_registry(config.company_registry_path))
    if not dry_run or (workday_entries and _sqlite_database_exists(config.database_url)):
        # Check or create the database before discovery makes any network calls.
        startup_engine = make_engine(config.database_url)
        try:
            if dry_run:
                validate_database_version(startup_engine)
            else:
                initialize_database(startup_engine)
        finally:
            startup_engine.dispose()
    discovery_stats: dict[str, int] = {"errors": 0}
    if workday_entries:
        discover_kwargs = {
            "stats": discovery_stats,
            "include_rejected": explain,
            "dry_run": dry_run,
            "force": force,
        }
        if baseline_mode:
            discover_kwargs["baseline_mode"] = True
        jobs = asyncio.run(discover(config, **discover_kwargs))
    elif explain:
        jobs = asyncio.run(discover(config, stats=discovery_stats, include_rejected=True))
    else:
        jobs = asyncio.run(discover(config, stats=discovery_stats))
    if limit is not None:
        jobs = jobs[:limit]
    if dry_run:
        if explain:
            print(format_filter_diagnostics(jobs, config))
            return len(jobs)
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
        baseline_jobs = jobs
        if baseline_mode and config.filters.baseline_include_recent:
            baseline_jobs = [
                job for job in jobs
                if not (
                    age_days_at(job) is not None
                    and age_days_at(job) <= config.filters.max_age_days
                )
            ]
        inserted = repo.add_many(baseline_jobs, baseline=baseline_mode)
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
                "age_source": stored.age_source,
                "age_days": stored.age_days,
                "discovered_at": stored.discovered_at,
                "uncertainty_reasons": stored.uncertainty_reasons or [],
            }, stored)
            for stored in repo.unseen()
        ]
        notification_statuses = repo.notification_statuses(stored.id for _job, stored in candidates)
        fresh = [
            job for job, stored in candidates
            if stored.notified_at is None
            and not (
                notification_statuses.get(stored.id)
                and notification_statuses[stored.id].state == "expired"
            )
        ]
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
        notify_jobs.sort(
            key=lambda job: _notification_priority(
                job, preferred_cities, config.filters.unknown_location_policy
            )
        )
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
        bot_token = config.telegram.bot_token.get_secret_value()
        chat_id = config.telegram.chat_id
        sent = 0
        errors = discovery_stats.get("errors", 0)
        permanently_failed = 0
        expired_count = 0
        stored_for_job = {id(job): stored for job, stored in candidates}
        statuses = notification_statuses
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
        age_eligible_jobs = []
        for job in notify_jobs:
            decision = matches_age(
                job,
                config.filters.max_age_days,
                config.filters.unknown_age_policy,
            )
            if decision.status is FilterStatus.REJECT:
                stored = stored_for_job.get(id(job))
                if stored is not None:
                    repo.mark_notification_expired(stored.id)
                    expired_count += 1
                continue
            age_eligible_jobs.append(job)
        notify_jobs = age_eligible_jobs
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
            results = asyncio.run(send_jobs(bot_token, chat_id,
                                            [job for job, _job_id in claimed]))
            for (_job, job_id), confirmed in zip(claimed, results):
                if confirmed:
                    sent += 1
                    repo.mark_notification_sent(job_id)
                else:
                    errors += 1
                    repo.mark_notification_failed(job_id, "Telegram did not confirm the message")
        summary_parts = []
        if queued_count:
            summary_parts.append(f"{queued_count} more new jobs queued for the next run")
        if expired_count:
            summary_parts.append(f"{expired_count} queued jobs expired")
        if summary_parts:
            summary = "\n".join(summary_parts)
            if not asyncio.run(send_summary(bot_token, chat_id, summary)):
                errors += 1
                LOG.warning("Telegram did not confirm the queued-jobs summary")
        repo.finish_run(run_record, fetched=discovery_stats.get("fetched", len(jobs)),
                        filtered=discovery_stats.get("filtered", len(jobs)), new=inserted,
                        notified=sent, errors=errors, permanently_failed=permanently_failed,
                        expired=expired_count)
    engine.dispose()
    return sent
