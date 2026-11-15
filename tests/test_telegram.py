import asyncio
import json
import re
from datetime import datetime

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.main as main
import app.notifications.telegram as telegram
from app.config import AppConfig, NotificationConfig, TelegramConfig
from app.database.connection import initialize_database
from app.database.models import Job, Notification
from app.notifications.formatting import format_job


TOKEN = "123456:TEST_SECRET_TOKEN"
CHAT = "test-chat"


def job(**overrides):
    values = {
        "source": "greenhouse", "source_id": "1", "company": "Example Co", "title": "Software Engineer",
        "location": "Remote - India", "employment_type": "Full-Time", "description": "Python role",
        "canonical_url": "https://jobs.example/role?x=1&y=2", "content_hash": "a" * 64,
        "experience_requirement": 0, "uncertainty_reasons": [],
    }
    values.update(overrides)
    return values


def install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(telegram.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    monkeypatch.setattr(telegram.asyncio, "sleep", _no_sleep)
    return transport


async def _no_sleep(_seconds):
    return None


def success(request):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=request)


def test_format_job_escapes_external_fields_and_uses_safe_anchor():
    message = format_job(job(
        title='A & <B> > "quoted"', company='Co & < > "', location='Remote <India> & "Anywhere"',
        canonical_url='https://jobs.example/a?x=1&y="two"<three>', uncertainty_reasons=["unknown"],
    ))
    assert "A &amp; &lt;B&gt; &gt; &quot;quoted&quot;" in message
    assert "Co &amp; &lt; &gt; &quot;" in message
    assert "Location: Remote &lt;India&gt; &amp; &quot;Anywhere&quot;" in message
    assert 'href="https://jobs.example/a?x=1&amp;y=&quot;two&quot;&lt;three&gt;"' in message
    assert "Check requirements: some role details are uncertain." in message


def test_format_job_truncates_without_splitting_html_tags_or_entities():
    message = format_job(job(title="Title & < > " + "x" * 5000,
                             company='Company " & ' + "c" * 5000,
                             location="Remote " + "l" * 5000,
                             canonical_url="https://jobs.example/" + "a&" * 1000), limit=4096)
    assert len(message) <= 4096
    assert message.count("<b>") == message.count("</b>")
    assert message.count("<a ") == message.count("</a>")
    assert not message.endswith(("&", "<", "&amp", "&quot", "&lt", "&gt"))
    assert not re.search(r"&(?:amp|lt|gt|quot|#x27)$", message)


def test_sender_uses_html_parse_mode_and_sends_escaped_payload(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return success(request)

    install_transport(monkeypatch, handler)
    results = asyncio.run(telegram.send_jobs(TOKEN, CHAT, [job(title='A & "B"')]))
    assert results == [True]
    payload = json.loads(requests[0].content)
    assert payload["parse_mode"] == "HTML"
    assert "A &amp; &quot;B&quot;" in payload["text"]


def test_429_honors_retry_after_then_confirms(monkeypatch):
    requests, delays = [], []

    async def record_sleep(seconds):
        delays.append(seconds)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 4}}, request=request)
        return success(request)

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(telegram.asyncio, "sleep", record_sleep)
    assert asyncio.run(telegram.send_jobs(TOKEN, CHAT, [job()])) == [True]
    assert len(requests) == 2
    assert delays == [4.0]


def test_http_400_fails_only_that_job_and_next_job_is_sent(monkeypatch):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload["text"])
        if len(calls) == 1:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request"}, request=request)
        return success(request)

    install_transport(monkeypatch, handler)
    results = asyncio.run(telegram.send_jobs(TOKEN, CHAT, [job(), job(source_id="2", title="Next")]))
    assert results == [False, True]
    assert len(calls) == 2


def test_cap_orders_priority_and_sends_queued_summary_then_remaining_job_next_run(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return success(request)

    install_transport(monkeypatch, handler)
    jobs = [
        job(source_id="remote", canonical_url="https://jobs.example/remote", title="Remote India", location="Remote - India", posted_at=datetime.fromisoformat("2020-01-01T00:00:00+00:00")),
        job(source_id="pune", canonical_url="https://jobs.example/pune", title="Pune role", location="Pune, India", posted_at=datetime.fromisoformat("2024-01-01T00:00:00+00:00")),
        job(source_id="bengaluru", canonical_url="https://jobs.example/bengaluru", title="Bengaluru role", location="Bengaluru, India", posted_at=datetime.fromisoformat("2025-01-01T00:00:00+00:00")),
        job(source_id="old", canonical_url="https://jobs.example/old", title="Older Mumbai", location="Mumbai, India", posted_at=datetime.fromisoformat("2023-01-01T00:00:00+00:00")),
        job(source_id="new", canonical_url="https://jobs.example/new", title="Newer Mumbai", location="Mumbai, India", posted_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00")),
    ]

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=len(jobs), filtered=len(jobs), errors=0)
        return [dict(item) for item in jobs]

    monkeypatch.setattr(main, "discover", fake_discover)
    config = AppConfig(
        database_url=f"sqlite:///{tmp_path / 'cap.db'}",
        filters={"preferred_cities": ["Pune", "Bengaluru"]},
        notifications=NotificationConfig(per_run_cap=4),
        telegram=TelegramConfig(bot_token=TOKEN, chat_id=CHAT, enabled=True),
    )
    assert main.run(config, notify=True) == 4
    sent_titles = [re.search(r"<b>(.*?) —", item["text"]).group(1) for item in requests[:4]]
    assert sent_titles == ["Remote India", "Pune role", "Bengaluru role", "Newer Mumbai"]
    assert requests[4]["text"] == "1 more new jobs queued for the next run"
    engine = create_engine(config.database_url)
    with Session(engine) as session:
        assert len(list(session.scalars(select(Notification)))) == 4
        older = session.scalar(select(Job).where(Job.source_id == "old"))
        assert older is not None and older.notified_at is None
    assert main.run(config, notify=True) == 1
    assert any("Older Mumbai" in item["text"] for item in requests[5:])
    engine.dispose()


def test_failed_send_is_retryable_and_marked_sent_only_after_success(tmp_path, monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"ok": False}, request=request)
        return success(request)

    install_transport(monkeypatch, handler)
    job_item = job()

    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [dict(job_item)]

    monkeypatch.setattr(main, "discover", fake_discover)
    config = AppConfig(
        database_url=f"sqlite:///{tmp_path / 'retry.db'}",
        telegram=TelegramConfig(bot_token=TOKEN, chat_id=CHAT, enabled=True),
    )
    assert main.run(config, notify=True) == 0
    engine = create_engine(config.database_url)
    with Session(engine) as session:
        record = session.scalar(select(Notification))
        assert record.state == "failed" and record.attempt_count == 1
    assert main.run(config, notify=True) == 1
    with Session(engine) as session:
        record = session.scalar(select(Notification))
        stored = session.scalar(select(Job))
        assert record.state == "sent" and record.attempt_count == 2
        assert stored.notified_at is not None
    engine.dispose()


def test_dry_run_never_sends_or_writes_notification_rows(tmp_path, monkeypatch):
    async def fake_discover(_config, *, stats=None):
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [job()]

    async def forbidden_send(*_args, **_kwargs):
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", forbidden_send)
    config = AppConfig(database_url=f"sqlite:///{tmp_path / 'dry-run.db'}",
                       telegram=TelegramConfig(bot_token=TOKEN, chat_id=CHAT, enabled=True))
    assert main.run(config, dry_run=True, notify=True) == 1
    assert not (tmp_path / "dry-run.db").exists()


def test_bot_token_never_appears_in_logs(caplog, monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"ok": False}, request=request)

    install_transport(monkeypatch, handler)
    asyncio.run(telegram.send_jobs(TOKEN, CHAT, [job()]))
    assert TOKEN not in caplog.text
    assert f"/bot{TOKEN}/" not in caplog.text
