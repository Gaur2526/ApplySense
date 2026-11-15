from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, event, select
from sqlalchemy.orm import Session

from app.database.connection import initialize_database
from app.database.models import Job, Notification, Run
from app.database.repository import JobRepository, NotificationClaimResult


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


def test_same_source_new_requisition_is_not_suppressed_by_a_cross_source_match():
    engine = create_engine("sqlite://")
    initialize_database(engine)
    greenhouse_one = {"source": "greenhouse", "source_id": "gh-1", "company": "Acme", "title": "Engineer",
                      "location": "Remote", "employment_type": "", "description": "",
                      "canonical_url": "https://one.example/job", "content_hash": "a" * 64}
    lever_duplicate = {**greenhouse_one, "source": "lever", "source_id": "lever-1",
                       "canonical_url": "https://lever.example/job"}
    greenhouse_two = {**greenhouse_one, "source_id": "gh-2", "canonical_url": "https://two.example/job"}
    with Session(engine) as session:
        repo = JobRepository(session)
        assert repo.add_many([greenhouse_one]) == 1
        assert repo.add_many([lever_duplicate]) == 0
        assert repo.add_many([greenhouse_two]) == 1
        assert repo.count() == 2
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
        assert repo1.claim_notification(job_id) is NotificationClaimResult.CLAIMED
        assert repo2.claim_notification(job_id) is NotificationClaimResult.BUSY
        repo1.mark_notification_failed(job_id, "temporary Telegram failure")
        assert repo2.claim_notification(job_id) is NotificationClaimResult.CLAIMED
        repo2.mark_notification_sent(job_id)
    with Session(engine) as session:
        notification = session.scalar(select(Notification).where(Notification.job_id == job_id))
        saved = session.get(Job, job_id)
        assert notification.state == "sent"
        assert notification.attempt_count == 2
        assert saved.notified_at is not None
        assert JobRepository(session).claim_notification(job_id) is NotificationClaimResult.BUSY
    engine.dispose()


def test_expired_pending_claim_can_be_reclaimed_but_fresh_pending_cannot(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'leases.db'}")
    initialize_database(engine)
    job = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
           "location": "Remote", "employment_type": "", "description": "",
           "canonical_url": "https://example.com/job", "content_hash": "a" * 64}
    with Session(engine) as session:
        repo = JobRepository(session)
        repo.add_many([job])
        job_id = repo.get(job["canonical_url"]).id
        assert repo.claim_notification(job_id, lease_seconds=900) is NotificationClaimResult.CLAIMED
        notification = session.scalar(select(Notification).where(Notification.job_id == job_id))
        notification.updated_at = datetime.now(timezone.utc) - timedelta(minutes=16)
        session.commit()
        assert repo.claim_notification(job_id, lease_seconds=900) is NotificationClaimResult.CLAIMED
        assert notification.attempt_count == 2
        assert repo.claim_notification(job_id, lease_seconds=900) is NotificationClaimResult.BUSY
        assert notification.attempt_count == 2
    engine.dispose()


def test_failed_notification_attempt_limit_exhausts_and_stays_failed(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'attempt-limit.db'}")
    initialize_database(engine)
    job = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
           "location": "Remote", "employment_type": "", "description": "",
           "canonical_url": "https://example.com/job", "content_hash": "a" * 64}
    with Session(engine) as session:
        repo = JobRepository(session)
        repo.add_many([job])
        job_id = repo.get(job["canonical_url"]).id
        assert repo.claim_notification(job_id, max_attempts=2) is NotificationClaimResult.CLAIMED
        repo.mark_notification_failed(job_id, "attempt one")
        assert repo.claim_notification(job_id, max_attempts=2) is NotificationClaimResult.CLAIMED
        repo.mark_notification_failed(job_id, "attempt two")
        assert repo.claim_notification(job_id, max_attempts=2) is NotificationClaimResult.EXHAUSTED
        assert repo.claim_notification(job_id, max_attempts=2) is NotificationClaimResult.ALREADY_EXHAUSTED
        notification = session.scalar(select(Notification).where(Notification.job_id == job_id))
        assert notification.state == "failed"
        assert notification.attempt_count == 2
    engine.dispose()


def test_two_simulated_concurrent_runs_have_exactly_one_claim_winner(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-claims.db'}", connect_args={"check_same_thread": False, "timeout": 10})
    initialize_database(engine)
    job = {"source": "greenhouse", "source_id": "1", "company": "Acme", "title": "Engineer",
           "location": "Remote", "employment_type": "", "description": "",
           "canonical_url": "https://example.com/job", "content_hash": "a" * 64}
    with Session(engine) as session:
        repo = JobRepository(session)
        repo.add_many([job])
        job_id = repo.get(job["canonical_url"]).id
    barrier = Barrier(2)

    def claim():
        with Session(engine) as session:
            barrier.wait()
            return JobRepository(session).claim_notification(job_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim(), range(2)))
    assert results.count(NotificationClaimResult.CLAIMED) == 1
    assert results.count(NotificationClaimResult.BUSY) == 1
    engine.dispose()


def test_notification_schema_has_unique_job_attempt_count_and_allowed_states():
    constraints = Notification.__table__.constraints
    assert any(isinstance(constraint, UniqueConstraint) and {column.name for column in constraint.columns} == {"job_id"}
               for constraint in constraints)
    assert "attempt_count" in Notification.__table__.columns
    state_check = next(constraint for constraint in constraints if isinstance(constraint, CheckConstraint)
                       and constraint.name == "ck_notifications_state")
    assert "pending" in str(state_check.sqltext) and "sent" in str(state_check.sqltext) and "failed" in str(state_check.sqltext)
    assert any(isinstance(constraint, UniqueConstraint) and {column.name for column in constraint.columns} == {"url_hash"}
               for constraint in Job.__table__.constraints)


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
        return [True] * len(jobs)

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


def test_same_source_distinct_requisitions_with_same_title_and_location_are_kept_and_notified(tmp_path, monkeypatch):
    import app.main as main
    from app.config import AppConfig, TelegramConfig

    jobs = [
        {"source": "greenhouse", "source_id": f"req-{index}", "company": "Acme", "title": "Engineer",
         "location": "Bengaluru, India", "employment_type": "Full-Time", "description": "Python",
         "canonical_url": f"https://example.com/job/{index}", "content_hash": f"{index:064x}",
         "experience_requirement": 0, "uncertainty_reasons": []}
        for index in (1, 2)
    ]
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'same-source.db'}",
                       telegram=TelegramConfig(bot_token="test-token", chat_id="test-chat", enabled=True))

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=2, filtered=2, errors=0)
        return [dict(job) for job in jobs]

    sent = []

    async def fake_send(_token, _chat, outgoing):
        sent.extend(outgoing)
        return [True] * len(outgoing)

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", fake_send)
    assert main.run(config, notify=True) == 2
    assert len(sent) == 2
    engine = create_engine(config.database_url)
    with Session(engine) as session:
        assert session.scalar(select(Job).where(Job.source_id == "req-1")) is not None
        assert session.scalar(select(Job).where(Job.source_id == "req-2")) is not None
        assert len(list(session.scalars(select(Notification)))) == 2
    engine.dispose()


def test_cross_source_secondary_duplicate_is_notified_once(tmp_path, monkeypatch):
    import app.main as main
    from app.config import AppConfig, TelegramConfig

    current = [{"source": "greenhouse", "source_id": "gh-1", "company": "Acme", "title": "Engineer",
                "location": "Bengaluru, India", "employment_type": "Full-Time", "description": "Python",
                "canonical_url": "https://example.com/gh-job", "content_hash": "a" * 64,
                "experience_requirement": 0, "uncertainty_reasons": []}]
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'cross-source.db'}",
                       telegram=TelegramConfig(bot_token="test-token", chat_id="test-chat", enabled=True))

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [dict(current[0])]

    sent = []

    async def fake_send(_token, _chat, outgoing):
        sent.extend(outgoing)
        return [True] * len(outgoing)

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", fake_send)
    assert main.run(config, notify=True) == 1
    current[:] = [{**current[0], "source": "lever", "source_id": "lever-1",
                   "canonical_url": "https://example.com/lever-job"}]
    assert main.run(config, notify=True) == 0
    assert len(sent) == 1
    engine = create_engine(config.database_url)
    with Session(engine) as session:
        assert len(list(session.scalars(select(Job)))) == 1
        assert len(list(session.scalars(select(Notification)))) == 1
    engine.dispose()


def test_baseline_jobs_are_suppressed_on_the_immediate_normal_run(tmp_path, monkeypatch):
    import app.main as main
    from app.config import AppConfig, TelegramConfig

    job = {"source": "greenhouse", "source_id": "baseline-1", "company": "Acme", "title": "Engineer",
           "location": "Bengaluru, India", "employment_type": "Full-Time", "description": "Python",
           "canonical_url": "https://example.com/baseline", "content_hash": "a" * 64,
           "experience_requirement": 0, "uncertainty_reasons": []}
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'baseline-run.db'}",
                       telegram=TelegramConfig(bot_token="test-token", chat_id="test-chat", enabled=True))

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [dict(job)]

    sent = []

    async def fake_send(_token, _chat, outgoing):
        sent.extend(outgoing)
        return [True] * len(outgoing)

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", fake_send)
    assert main.run(config, baseline_mode=True, notify=True) == 1
    assert main.run(config, notify=True) == 0
    assert sent == []
    engine = create_engine(config.database_url)
    with Session(engine) as session:
        stored = session.scalar(select(Job))
        assert stored.is_baseline is True
        assert list(session.scalars(select(Notification))) == []
    engine.dispose()


def test_exhausted_notification_attempt_is_counted_in_run_summary(tmp_path, monkeypatch):
    import app.main as main
    from app.config import AppConfig, TelegramConfig

    job = {"source": "greenhouse", "source_id": "exhausted-1", "company": "Acme", "title": "Engineer",
           "location": "Bengaluru, India", "employment_type": "Full-Time", "description": "Python",
           "canonical_url": "https://example.com/exhausted", "content_hash": "a" * 64,
           "experience_requirement": 0, "uncertainty_reasons": []}
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'exhausted.db'}",
                       telegram=TelegramConfig(bot_token="test-token", chat_id="test-chat", enabled=True))
    engine = create_engine(config.database_url)
    initialize_database(engine)
    with Session(engine) as session:
        repo = JobRepository(session)
        repo.add_many([job])
        job_id = repo.get(job["canonical_url"]).id
        session.add(Notification(job_id=job_id, state="failed", attempt_count=3))
        session.commit()

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [dict(job)]

    async def no_send(*_args):
        raise AssertionError("exhausted notification must not be sent")

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", no_send)
    assert main.run(config, notify=True) == 0
    assert main.run(config, notify=True) == 0
    with Session(engine) as session:
        runs = list(session.scalars(select(Run).order_by(Run.id)))
        notification = session.scalar(select(Notification))
        assert runs[-2].errors == 0 and runs[-2].permanently_failed == 1
        assert runs[-1].errors == 0 and runs[-1].permanently_failed == 0
        assert notification.state == "failed"
    engine.dispose()
