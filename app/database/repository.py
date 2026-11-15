from collections.abc import Iterable
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database.models import Job, Notification, Run, utc_now
from app.utils.hashing import secondary_job_hash, url_hash


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
        by_url_hash = {job.url_hash: job for job in existing}
        by_secondary = {job.secondary_key: job for job in existing}
        inserted = 0
        for job in prepared:
            found = by_url_hash.get(job["url_hash"]) or by_secondary.get(job["secondary_key"])
            if found is not None:
                if found.content_hash != job["content_hash"]:
                    found.description = job.get("description", "")
                    found.content_hash = job["content_hash"]
                    found.needs_rescore = True
                continue
            row = Job(**job, is_baseline=baseline)
            self.session.add(row)
            # Prevent duplicate items within the same input batch from being inserted.
            by_url_hash[row.url_hash] = row
            by_secondary[row.secondary_key] = row
            inserted += 1
        self.session.commit()
        return inserted

    def claim_notification(self, job_id: int) -> bool:
        """Atomically claim an initial or previously failed notification attempt."""
        inserted = self.session.execute(
            sqlite_insert(Notification)
            .values(job_id=job_id, state="pending", attempt_count=1, updated_at=utc_now())
            .on_conflict_do_nothing(index_elements=["job_id"])
        ).rowcount
        if inserted:
            self.session.commit()
            return True
        reclaimed = self.session.execute(
            update(Notification)
            .where(Notification.job_id == job_id, Notification.state == "failed")
            .values(state="pending", attempt_count=Notification.attempt_count + 1, updated_at=utc_now(), error=None)
        ).rowcount
        self.session.commit()
        return reclaimed == 1

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

    def create_run(self) -> Run:
        run = Run(started_at=utc_now())
        self.session.add(run)
        self.session.commit()
        return run

    def finish_run(self, run: Run, *, fetched: int, filtered: int, new: int, notified: int, errors: int) -> None:
        run.finished_at = utc_now()
        run.fetched = fetched
        run.filtered = filtered
        run.new = new
        run.notified = notified
        run.errors = errors
        self.session.commit()

    def unseen(self) -> list[Job]:
        return list(self.session.scalars(select(Job).where(Job.notified_at.is_(None), Job.is_baseline.is_(False))))

    def mark_notified(self, jobs: Iterable[Job]) -> None:
        now = utc_now()
        for job in jobs:
            job.notified_at = now
        self.session.commit()

    def count(self) -> int:
        return len(list(self.session.scalars(select(Job.id))))
