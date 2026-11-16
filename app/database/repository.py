from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import exists, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database.models import EvaluatedJob, Job, Notification, Run, utc_now
from app.utils.hashing import secondary_job_hash, url_hash


class NotificationClaimResult(str, Enum):
    CLAIMED = "claimed"
    BUSY = "busy"
    EXHAUSTED = "exhausted"
    ALREADY_EXHAUSTED = "already_exhausted"


class JobRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, canonical_url: str) -> Job | None:
        return self.session.scalar(select(Job).where(Job.canonical_url == canonical_url))

    def get_many_by_hashes(self, *, url_hashes: Iterable[str] = (), secondary_keys: Iterable[str] = (), chunk_size: int = 400) -> list[Job]:
        """Fetch matching rows in bounded WHERE IN batches."""
        found: dict[int, Job] = {}
        for field, values in ((Job.url_hash, set(url_hashes)), (Job.secondary_key, set(secondary_keys))):
            ordered = list(values)
            for offset in range(0, len(ordered), chunk_size):
                rows = self.session.scalars(select(Job).where(field.in_(ordered[offset:offset + chunk_size])))
                for row in rows:
                    found[row.id] = row
        return list(found.values())

    def get_many_by_source_ids(self, identities: Iterable[tuple[str, str]], *, chunk_size: int = 400) -> list[Job]:
        found: dict[int, Job] = {}
        ordered = list(set(identities))
        for offset in range(0, len(ordered), chunk_size):
            rows = self.session.scalars(select(Job).where(
                tuple_(Job.source, Job.source_id).in_(ordered[offset:offset + chunk_size])
            ))
            for row in rows:
                found[row.id] = row
        return list(found.values())

    def known_source_ids(self, source: str, source_ids: Iterable[str]) -> set[str]:
        ordered = list(set(source_ids))
        known: set[str] = set()
        for offset in range(0, len(ordered), 400):
            known.update(self.session.scalars(select(Job.source_id).where(
                Job.source == source, Job.source_id.in_(ordered[offset:offset + 400])
            )))
        return known

    def job_source_ids_for_source(self, source: str) -> set[str]:
        return set(self.session.scalars(select(Job.source_id).where(Job.source == source)))

    def evaluated_source_ids_for_source(self, source: str) -> set[str]:
        return set(self.session.scalars(
            select(EvaluatedJob.source_id).where(EvaluatedJob.source == source)
        ))

    def recently_evaluated_source_ids(
        self,
        source: str,
        source_ids: Iterable[str],
        *,
        reevaluate_after_days: int,
        settings_hash: str | None = None,
    ) -> set[str]:
        ordered = list(set(source_ids))
        cutoff = utc_now() - timedelta(days=reevaluate_after_days)
        recent: set[str] = set()
        for offset in range(0, len(ordered), 400):
            conditions = [
                EvaluatedJob.source == source,
                EvaluatedJob.source_id.in_(ordered[offset:offset + 400]),
                EvaluatedJob.evaluated_at >= cutoff,
            ]
            if settings_hash is not None:
                conditions.append(EvaluatedJob.settings_hash == settings_hash)
            recent.update(self.session.scalars(
                select(EvaluatedJob.source_id).where(*conditions)
            ))
        return recent

    def evaluation_settings_hashes(self, source: str) -> dict[str, str]:
        return {
            source_id: settings_hash
            for source_id, settings_hash in self.session.execute(
                select(EvaluatedJob.source_id, EvaluatedJob.settings_hash)
                .where(EvaluatedJob.source == source)
            )
        }

    def record_evaluations(self, evaluations: Iterable[dict]) -> None:
        for item in evaluations:
            row = self.session.scalar(select(EvaluatedJob).where(
                EvaluatedJob.source == item["source"],
                EvaluatedJob.source_id == item["source_id"],
            ))
            if row is None:
                row = EvaluatedJob(source=item["source"], source_id=item["source_id"])
                self.session.add(row)
            row.decision = item["decision"]
            row.reason = str(item.get("reason") or "")[:4000]
            row.settings_hash = str(item.get("settings_hash") or "")[:64]
            row.evaluated_at = item.get("evaluated_at") or utc_now()
        self.session.commit()

    def last_tenant_run(self, source: str, tenant_key: str) -> datetime | None:
        return self.session.scalar(
            select(Run.finished_at)
            .where(Run.source == source, Run.tenant_key == tenant_key, Run.finished_at.is_not(None))
            .order_by(Run.finished_at.desc())
            .limit(1)
        )

    def notification_statuses(self, job_ids: Iterable[int], *, chunk_size: int = 400) -> dict[int, Notification]:
        found: dict[int, Notification] = {}
        ordered = list(set(job_ids))
        for offset in range(0, len(ordered), chunk_size):
            rows = self.session.scalars(select(Notification).where(
                Notification.job_id.in_(ordered[offset:offset + chunk_size])
            ))
            for row in rows:
                found[row.job_id] = row
        return found

    def add_if_new(self, job: dict, *, baseline: bool = False) -> bool:
        return self.add_many([job], baseline=baseline) == 1

    def add_many(self, jobs: Iterable[dict], *, baseline: bool = False, chunk_size: int = 400) -> int:
        prepared = []
        for original in jobs:
            job = dict(original)
            job["url_hash"] = url_hash(job["canonical_url"])
            job["secondary_key"] = secondary_job_hash(job["company"], job["title"], job.get("location", ""))
            prepared.append(job)

        existing = self.get_many_by_hashes(
            url_hashes=(job["url_hash"] for job in prepared),
            secondary_keys=(job["secondary_key"] for job in prepared),
            chunk_size=chunk_size,
        )
        existing.extend(self.get_many_by_source_ids(
            ((job["source"], job["source_id"]) for job in prepared), chunk_size=chunk_size
        ))
        by_url_hash = {job.url_hash: job for job in existing}
        by_source_id = {(job.source, job.source_id): job for job in existing}
        by_secondary: dict[str, list[Job]] = {}
        for row in existing:
            by_secondary.setdefault(row.secondary_key, []).append(row)
        inserted = 0
        for job in prepared:
            found = by_url_hash.get(job["url_hash"]) or by_source_id.get((job["source"], job["source_id"]))
            if found is None:
                same_source_secondary = any(
                    row.source == job["source"] for row in by_secondary.get(job["secondary_key"], [])
                )
                if not same_source_secondary:
                    found = next((row for row in by_secondary.get(job["secondary_key"], [])
                                  if row.source != job["source"]), None)
            if found is not None:
                found.posted_at = job.get("posted_at")
                found.age_source = job.get("age_source")
                found.age_days = job.get("age_days")
                if found.content_hash != job["content_hash"]:
                    found.description = job.get("description", "")
                    found.content_hash = job["content_hash"]
                    found.needs_rescore = True
                if baseline:
                    found.is_baseline = True
                continue
            row = Job(**job, is_baseline=baseline)
            self.session.add(row)
            # Prevent duplicate items within the same input batch from being inserted.
            by_url_hash[row.url_hash] = row
            by_source_id[(row.source, row.source_id)] = row
            by_secondary.setdefault(row.secondary_key, []).append(row)
            inserted += 1
        self.session.commit()
        return inserted

    def claim_notification(
        self, job_id: int, *, lease_seconds: int = 900, max_attempts: int = 3
    ) -> NotificationClaimResult:
        """Atomically claim an initial, failed, or expired notification attempt."""
        inserted = self.session.execute(
            sqlite_insert(Notification)
            .values(job_id=job_id, state="pending", attempt_count=1, updated_at=utc_now())
            .on_conflict_do_nothing(index_elements=["job_id"])
        ).rowcount
        if inserted:
            self.session.commit()
            return NotificationClaimResult.CLAIMED
        stale_before = utc_now() - timedelta(seconds=lease_seconds)
        reclaimed_pending = self.session.execute(
            update(Notification)
            .where(Notification.job_id == job_id, Notification.state == "pending",
                   Notification.updated_at <= stale_before, Notification.attempt_count < max_attempts)
            .values(state="pending", attempt_count=Notification.attempt_count + 1,
                    updated_at=utc_now(), error=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        if reclaimed_pending:
            self.session.commit()
            return NotificationClaimResult.CLAIMED
        reclaimed = self.session.execute(
            update(Notification)
            .where(Notification.job_id == job_id, Notification.state == "failed",
                   Notification.attempt_count < max_attempts)
            .values(state="pending", attempt_count=Notification.attempt_count + 1, updated_at=utc_now(), error=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        if reclaimed:
            self.session.commit()
            return NotificationClaimResult.CLAIMED
        expired_exhausted = self.session.execute(
            update(Notification)
            .where(Notification.job_id == job_id, Notification.state == "pending",
                   Notification.updated_at <= stale_before, Notification.attempt_count >= max_attempts)
            .values(state="failed", updated_at=utc_now(), error="notification claim lease expired after maximum attempts",
                    permanent_failure_reported=True)
            .execution_options(synchronize_session=False)
        ).rowcount
        reported_now = self.session.execute(
            update(Notification)
            .where(Notification.job_id == job_id, Notification.state == "failed",
                   Notification.attempt_count >= max_attempts, Notification.permanent_failure_reported.is_(False))
            .values(permanent_failure_reported=True)
            .execution_options(synchronize_session=False)
        ).rowcount
        row = self.session.execute(select(Notification.state, Notification.attempt_count)
                                   .where(Notification.job_id == job_id)).one_or_none()
        self.session.commit()
        if expired_exhausted or reported_now:
            return NotificationClaimResult.EXHAUSTED
        if row and row.state == "failed" and row.attempt_count >= max_attempts:
            return NotificationClaimResult.ALREADY_EXHAUSTED
        return NotificationClaimResult.BUSY

    def mark_notification_sent(self, job_id: int) -> None:
        self.session.execute(
            update(Notification).where(Notification.job_id == job_id, Notification.state == "pending")
            .values(state="sent", updated_at=utc_now(), error=None)
        )
        job = self.session.get(Job, job_id)
        if job is not None:
            job.notified_at = utc_now()
        self.session.commit()

    def mark_notification_failed(self, job_id: int, error: str) -> None:
        self.session.execute(
            update(Notification).where(Notification.job_id == job_id, Notification.state == "pending")
            .values(state="failed", updated_at=utc_now(), error=error[:4000])
        )
        self.session.commit()

    def mark_notification_expired(self, job_id: int) -> None:
        row = self.session.scalar(
            select(Notification).where(Notification.job_id == job_id)
        )
        if row is None:
            self.session.add(Notification(
                job_id=job_id, state="expired", attempt_count=0, updated_at=utc_now(),
                error="Job exceeded maximum age before notification.",
            ))
        elif row.state != "sent":
            row.state = "expired"
            row.updated_at = utc_now()
            row.error = "Job exceeded maximum age before notification."
        self.session.commit()

    def create_run(self, *, source: str | None = None, tenant_key: str | None = None) -> Run:
        run = Run(started_at=utc_now(), source=source, tenant_key=tenant_key)
        self.session.add(run)
        self.session.commit()
        return run

    def finish_run(self, run: Run, *, fetched: int, filtered: int, new: int, notified: int, errors: int,
                   permanently_failed: int = 0, expired: int = 0) -> None:
        run.finished_at = utc_now()
        run.fetched = fetched
        run.filtered = filtered
        run.new = new
        run.notified = notified
        run.errors = errors
        run.permanently_failed = permanently_failed
        run.expired = expired
        self.session.commit()

    def unseen(self) -> list[Job]:
        expired_notification = exists(
            select(Notification.id).where(
                Notification.job_id == Job.id, Notification.state == "expired"
            )
        )
        return list(self.session.scalars(select(Job).where(
            Job.notified_at.is_(None), Job.is_baseline.is_(False), ~expired_notification
        )))

    def mark_notified(self, jobs: Iterable[Job]) -> None:
        now = utc_now()
        for job in jobs:
            job.notified_at = now
        self.session.commit()

    def count(self) -> int:
        return len(list(self.session.scalars(select(Job.id))))
