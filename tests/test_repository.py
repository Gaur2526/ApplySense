from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database.connection import initialize_database
from app.database.models import Job, Notification, Run
from app.database.repository import JobRepository


def test_repository_deduplicates_and_keeps_baseline_out_of_unseen():
    engine = create_engine("sqlite://")
    initialize_database(engine)
    job = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer", "location": "Remote", "employment_type": "Full-Time", "description": "", "canonical_url": "https://example.com/job", "content_hash": "abc"}
    with Session(engine) as session:
        repo = JobRepository(session)
        assert repo.add_many([job], baseline=True) == 1
        assert repo.add_many([job]) == 0
        assert repo.count() == 1
        assert repo.unseen() == []
        saved = repo.get("https://example.com/job")
        assert saved.url_hash and len(saved.url_hash) == 64
        assert saved.secondary_key and len(saved.secondary_key) == 64
        assert saved.needs_rescore is False
    engine.dispose()


def test_secondary_key_skips_cross_source_duplicate():
    engine = create_engine("sqlite://")
    initialize_database(engine)
    first = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
             "location": "Remote", "employment_type": "", "description": "Old", "canonical_url": "https://one.example/job",
             "content_hash": "a" * 64}
    second = {**first, "source": "lever", "source_id": "different", "company": " ACME ",
              "title": " engineer ", "canonical_url": "https://two.example/job"}
    with Session(engine) as session:
        repo = JobRepository(session)
        assert repo.add_many([first]) == 1
        assert repo.add_many([second]) == 0
        assert repo.count() == 1
    engine.dispose()


def test_content_change_updates_existing_row_and_marks_for_rescore_without_duplicate():
    engine = create_engine("sqlite://")
    initialize_database(engine)
    original = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
                "location": "Remote", "employment_type": "", "description": "Old description",
                "canonical_url": "https://example.com/job", "content_hash": "a" * 64}
    changed = {**original, "description": "Updated description", "content_hash": "b" * 64}
    with Session(engine) as session:
        repo = JobRepository(session)
        assert repo.add_many([original]) == 1
        assert repo.add_many([changed]) == 0
        saved = repo.get(original["canonical_url"])
        assert saved.description == "Updated description"
        assert saved.content_hash == "b" * 64
        assert saved.needs_rescore is True
        assert saved.notified_at is None
    engine.dispose()


def test_hash_lookups_are_chunked_for_bounded_where_in_queries():
    engine = create_engine("sqlite://")
    initialize_database(engine)
    jobs = [{"source": "greenhouse", "source_id": str(i), "company": f"Co {i}", "title": "Engineer",
             "location": f"City {i}", "employment_type": "", "description": "", 
             "canonical_url": f"https://example.com/{i}", "content_hash": "a" * 64} for i in range(5)]
    with Session(engine) as session:
        repo = JobRepository(session)
        assert repo.add_many(jobs) == 5
        statements = []
        event.listen(engine, "before_cursor_execute", lambda _conn, _cursor, statement, *_args: statements.append(statement))
        hashes = [job.url_hash for job in session.scalars(select(Job))]
        statements.clear()
        matches = repo.get_many_by_hashes(url_hashes=hashes, chunk_size=2)
        selects = [sql for sql in statements if "SELECT jobs" in sql]
        assert len(matches) == 5
        assert len(selects) == 3
    engine.dispose()


def test_notification_claim_is_atomic_stateful_and_failed_claim_can_retry(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'claims.db'}", connect_args={"check_same_thread": False})
    initialize_database(engine)
    job = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
           "location": "Remote", "employment_type": "", "description": "", 
           "canonical_url": "https://example.com/job", "content_hash": "a" * 64}
    with Session(engine) as setup:
        repo = JobRepository(setup)
        repo.add_many([job])
        job_id = repo.get(job["canonical_url"]).id
    with Session(engine) as first, Session(engine) as second:
        repo1, repo2 = JobRepository(first), JobRepository(second)
        assert repo1.claim_notification(job_id) is True
        assert repo2.claim_notification(job_id) is False
        repo1.mark_notification_failed(job_id, "temporary Telegram failure")
        assert repo2.claim_notification(job_id) is True
        repo2.mark_notification_sent(job_id)
    with Session(engine) as session:
        notification = session.scalar(select(Notification).where(Notification.job_id == job_id))
        saved = session.get(Job, job_id)
        assert notification.state == "sent"
        assert notification.attempt_count == 2
        assert saved.notified_at is not None
        assert JobRepository(session).claim_notification(job_id) is False
    engine.dispose()


def test_run_metrics_persist():
    engine = create_engine("sqlite://")
    initialize_database(engine)
    with Session(engine) as session:
        repo = JobRepository(session)
        run = repo.create_run()
        repo.finish_run(run, fetched=10, filtered=4, new=3, notified=2, errors=1)
        saved = session.get(Run, run.id)
        assert saved.finished_at is not None
        assert (saved.fetched, saved.filtered, saved.new, saved.notified, saved.errors) == (10, 4, 3, 2, 1)
    engine.dispose()


def test_second_discovery_run_cannot_notify_same_job(tmp_path, monkeypatch):
    import asyncio

    import app.main as main
    from app.config import AppConfig, TelegramConfig

    job = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
           "location": "Bengaluru, India", "employment_type": "Full-Time", "description": "Python",
           "canonical_url": "https://example.com/job", "content_hash": "a" * 64,
           "experience_requirement": 0, "uncertainty_reasons": []}
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'runs.db'}",
                       telegram=TelegramConfig(bot_token="test-token", chat_id="test-chat", enabled=True))

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [dict(job)]

    sent = []

    async def fake_send(_token, _chat, jobs):
        sent.extend(jobs)
        return len(jobs)

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", fake_send)
    assert main.run(config, notify=True) == 1
    assert main.run(config, notify=True) == 0
    assert len(sent) == 1

    engine = create_engine(config.database_url)
    with Session(engine) as session:
        assert session.scalar(select(Notification)).state == "sent"
        runs = list(session.scalars(select(Run).order_by(Run.id)))
        assert len(runs) == 2
        assert all(run.finished_at is not None for run in runs)
    engine.dispose()
