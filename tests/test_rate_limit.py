from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

import app.main as main
import app.scouts.workday as workday_module
from app.companies.registry import WorkdayTenantConfig, load_registry, verified_workday_entries
from app.config import AppConfig
from app.database.connection import initialize_database, make_engine, make_session_factory
from app.database.models import EvaluatedJob, Run, utc_now
from app.database.repository import JobRepository
from app.scouts.retry import retry_after_seconds, wait_before_retry
from app.scouts.greenhouse import GreenhouseRateLimitedError, GreenhouseScout
from app.scouts.workday import WorkdayScout


ROOT = Path(__file__).resolve().parents[1]
DETAIL_FIXTURE = json.loads((ROOT / "tests/fixtures/workday_walmart_details.json").read_text())


def tenant(**updates):
    values = {
        "host": "walmart.wd504.myworkdayjobs.com",
        "tenant": "walmart",
        "site": "WalmartExternal",
        "country_facet_id": "facet",
        "assume_country": "IN",
        "search_texts": ["software engineer"],
        "page_size": 20,
        "max_pages": 10,
        "max_detail_requests_per_run": 30,
    }
    values.update(updates)
    return WorkdayTenantConfig.model_validate(values)


def app_config(database_url="sqlite:///:memory:", **scout_updates):
    return AppConfig.model_validate({
        "database_url": database_url,
        "company_registry_path": str(ROOT / "data/company_registry.json"),
        "scout": {"request_interval_seconds": 0, "retries": 0, **scout_updates},
        "filters": {
            "keywords": ["software engineer"],
            "excluded_keywords": [],
            "seniority_titles": ["senior", "principal", "manager"],
            "unknown_location_policy": "strict",
            "max_min_years": 2,
            "employment_types": ["full-time"],
            "max_age_days": 7,
        },
    })


def postings(*ids):
    return [{
        "title": "Software Engineer",
        "externalPath": f"/job/engineering/Software-Engineer_{req_id}",
        "locationsText": "2 Locations",
        "postedOn": "Posted Today",
        "bulletFields": [req_id],
    } for req_id in ids]


def details(req_id):
    payload = json.loads(json.dumps(DETAIL_FIXTURE))
    info = payload["jobPostingInfo"]
    info.update({
        "jobReqId": req_id,
        "title": "Software Engineer",
        "jobDescription": "Minimum Qualifications: 1 year of software engineering experience.",
        "location": "2 Locations",
        "externalUrl": f"https://walmart.example/jobs/{req_id}",
        "startDate": datetime.now(timezone.utc).date().isoformat(),
    })
    info.pop("jobRequisitionLocation", None)
    return payload


def run_async(awaitable):
    return asyncio.run(awaitable)


def test_retry_after_seconds_and_http_date_are_supported():
    seconds = httpx.Response(429, headers={"Retry-After": "17"})
    assert retry_after_seconds(seconds) == 17

    fixed_now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    retry_at = fixed_now + timedelta(seconds=75)
    http_date = httpx.Response(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)})
    assert retry_after_seconds(http_date, now=fixed_now) == 75


def test_workday_default_pacing_is_2_5_seconds_across_list_and_details(monkeypatch):
    assert AppConfig().scout.request_interval_seconds == 2.5
    sleeps = []
    clock_values = iter([100.0, 100.1, 100.1])

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(workday_module, "time", SimpleNamespace(monotonic=lambda: next(clock_values)))
    monkeypatch.setattr(workday_module.asyncio, "sleep", fake_sleep)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 1, "jobPostings": postings("R-1")}, request=request)
        return httpx.Response(200, json=details("R-1"), request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scout = WorkdayScout(client=client, request_interval_seconds=2.5, retries=0)
            await scout.fetch_first_page(tenant())
            await scout.fetch_details(tenant(), {
                "external_path": "/job/engineering/Software-Engineer_R-1",
            })

    run_async(scenario())
    assert sleeps == pytest.approx([2.4])


def test_backoff_schedule_uses_injected_sleep_and_cap(monkeypatch):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.scouts.retry.random.uniform", lambda low, high: 1.0)

    async def scenario():
        for attempt in range(4):
            await wait_before_retry(attempt, backoff_seconds=[5, 15, 45], cap_seconds=120, sleep=fake_sleep)

    run_async(scenario())
    assert delays == [5, 15, 45, 120]


def test_greenhouse_reuses_configured_backoff_and_reports_rate_limit(monkeypatch, caplog):
    caplog.set_level("INFO")
    delays = []
    responses = 0

    async def fake_wait(attempt, response=None, **kwargs):
        delays.append((attempt, response.headers.get("Retry-After"), kwargs["backoff_seconds"]))

    monkeypatch.setattr("app.scouts.greenhouse._wait_before_retry", fake_wait)

    def handler(request):
        nonlocal responses
        responses += 1
        if responses == 1:
            return httpx.Response(429, headers={"Retry-After": "4"}, request=request)
        return httpx.Response(200, json={"jobs": []}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scout = GreenhouseScout(
                client=client, retries=1, retry_backoff_seconds=[2, 6],
                retry_backoff_cap_seconds=20,
            )
            assert await scout.fetch_jobs("mock_board") == []

    run_async(scenario())
    assert delays == [(0, "4", (2, 6))]

    async def limited():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "9"}, request=request)
        )) as client:
            scout = GreenhouseScout(client=client, retries=0)
            with pytest.raises(GreenhouseRateLimitedError) as error:
                await scout.fetch_jobs("mock_board")
            assert error.value.host == "boards-api.greenhouse.io"
            assert error.value.retry_after == "9"

    run_async(limited())
    assert "Retry-After=4" in caplog.text
    assert "Retry-After=9" in caplog.text


def test_workday_429_circuit_breaks_after_two_exhausted_requests(monkeypatch, caplog):
    caplog.set_level("INFO")
    requests = []

    async def no_sleep(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(workday_module, "_wait_before_retry", no_sleep)

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={
                "total": 3, "jobPostings": postings("R-1", "R-2", "R-3"),
            }, request=request)
        return httpx.Response(429, headers={"Retry-After": "60"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scout = WorkdayScout(client=client, retries=1, request_interval_seconds=0)
            result = await scout.fetch_jobs(
                "Walmart", tenant(), app_config(retries=1),
                run_time=datetime.now(timezone.utc),
            )
            return scout, result

    scout, evaluations = run_async(scenario())
    details_requested = [path for method, path in requests if method == "GET"]
    assert len(details_requested) == 4  # Two attempts for each of the first two jobs.
    assert scout.rate_limited is True
    assert scout.rate_limit_retry_after == "60"
    assert scout.pending_details_count == 3
    assert evaluations == []
    assert "Retry-After=60" in caplog.text


def test_rate_limited_list_keeps_partial_page_jobs_for_details(monkeypatch):
    requests = []
    list_calls = 0

    async def no_sleep(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(workday_module, "_wait_before_retry", no_sleep)

    def handler(request):
        nonlocal list_calls
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            list_calls += 1
            if list_calls == 1:
                return httpx.Response(200, json={"total": 2, "jobPostings": postings("R-1")}, request=request)
            return httpx.Response(429, request=request)
        return httpx.Response(200, json=details("R-1"), request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scout = WorkdayScout(client=client, retries=0, request_interval_seconds=0)
            result = await scout.fetch_jobs(
                "Walmart", tenant(page_size=1, max_pages=3), app_config(),
                run_time=datetime.now(timezone.utc),
            )
            return scout, result

    scout, evaluations = run_async(scenario())
    assert scout.rate_limited is True
    assert len([request for request in requests if request[0] == "GET"]) == 1
    assert len(evaluations) == 1
    assert evaluations[0].source_id == "walmart/R-1"
    assert evaluations[0].reached_details is True
    assert evaluations[0].job is not None


def test_unresolved_details_are_retried_next_run_in_feed_order(monkeypatch):
    detail_calls = []
    first_run = True

    async def no_sleep(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(workday_module, "_wait_before_retry", no_sleep)

    def handler(request):
        nonlocal first_run
        if request.method == "POST":
            return httpx.Response(200, json={
                "total": 3, "jobPostings": postings("R-1", "R-2", "R-3"),
            }, request=request)
        req_id = request.url.path.rsplit("_", 1)[-1]
        detail_calls.append(req_id)
        if first_run and req_id in {"R-1", "R-2"}:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json=details(req_id), request=request)

    async def scenario():
        nonlocal first_run
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first_scout = WorkdayScout(client=client, retries=0, request_interval_seconds=0)
            first = await first_scout.fetch_jobs("Walmart", tenant(), app_config())
            assert first_scout.pending_details_count == 3
            assert first == []
            first_run = False
            second_scout = WorkdayScout(client=client, retries=0, request_interval_seconds=0)
            second = await second_scout.fetch_jobs("Walmart", tenant(), app_config())
            return second

    second_run = run_async(scenario())
    assert detail_calls == ["R-1", "R-2", "R-1", "R-2", "R-3"]
    assert [item.source_id for item in second_run] == [
        "walmart/R-1", "walmart/R-2", "walmart/R-3",
    ]


def test_rate_limited_pending_job_is_not_remembered_and_retries_after_cooldown(tmp_path, monkeypatch):
    registry = load_registry(ROOT / "data/company_registry.json")
    walmart = next(item for item in verified_workday_entries(registry) if item.company_name == "Walmart")
    database_url = f"sqlite:///{tmp_path / 'pending-details.db'}"
    engine = make_engine(database_url)
    initialize_database(engine)
    engine.dispose()

    first_pass = True
    detail_calls = []
    real_async_client = httpx.AsyncClient

    def client_with_mock(*args, **kwargs):
        def handler(request):
            nonlocal first_pass
            if request.method == "POST":
                return httpx.Response(200, json={
                    "total": 3, "jobPostings": postings("R-1", "R-2", "R-3"),
                }, request=request)
            req_id = request.url.path.rsplit("_", 1)[-1]
            detail_calls.append(req_id)
            if first_pass and req_id == "R-1":
                return httpx.Response(429, headers={"Retry-After": "10"}, request=request)
            return httpx.Response(200, json=details(req_id), request=request)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    async def no_sleep(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock)
    monkeypatch.setattr(workday_module, "_wait_before_retry", no_sleep)
    cfg = app_config(database_url, retries=0, request_interval_seconds=0)

    async def discover_once(force):
        return await main._discover_workday(
            cfg, [walmart], stats={"sources": {}, "errors": 0},
            include_rejected=False, dry_run=False, force=force,
            run_time=datetime.now(timezone.utc),
        )

    first_jobs = run_async(discover_once(force=True))
    assert [job["source_id"] for job in first_jobs] == ["walmart/R-2", "walmart/R-3"]
    assert detail_calls == ["R-1", "R-2", "R-3"]

    engine = make_engine(database_url)
    with make_session_factory(engine)() as session:
        evaluated = set(session.scalars(select(EvaluatedJob.source_id)))
        assert evaluated == {"walmart/R-2", "walmart/R-3"}
        recent_run = session.scalar(select(Run).where(
            Run.source == "workday", Run.tenant_key == "walmart"
        ).order_by(Run.id.desc()))
        assert recent_run is not None
        # Simulate both the two hour cooldown and tenant interval having elapsed.
        recent_run.finished_at = utc_now() - timedelta(hours=2)
        recent_run.rate_limited_until = utc_now() - timedelta(seconds=1)
        session.commit()
    engine.dispose()

    first_pass = False
    second_jobs = run_async(discover_once(force=False))
    assert [job["source_id"] for job in second_jobs] == ["walmart/R-1"]
    assert detail_calls == ["R-1", "R-2", "R-3", "R-1"]


def test_workday_rate_limit_cooldown_skips_until_force_overrides(tmp_path, monkeypatch, caplog):
    registry = load_registry(ROOT / "data/company_registry.json")
    walmart = next(item for item in verified_workday_entries(registry) if item.company_name == "Walmart")
    database_url = f"sqlite:///{tmp_path / 'cooldown.db'}"
    engine = make_engine(database_url)
    initialize_database(engine)
    with make_session_factory(engine)() as session:
        repo = JobRepository(session)
        run = repo.create_run(source="workday", tenant_key="walmart")
        repo.finish_run(
            run, fetched=0, filtered=0, new=0, notified=0, errors=0,
            rate_limited_until=utc_now() + timedelta(hours=2),
            pending_details=4,
        )
    engine.dispose()

    requests = []
    real_async_client = httpx.AsyncClient

    def client_with_mock(*args, **kwargs):
        def handler(request):
            requests.append((request.method, request.url.host, request.url.path))
            return httpx.Response(200, json={"total": 0, "jobPostings": []}, request=request)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock)
    cfg = app_config(database_url, request_interval_seconds=0)
    stats = {"sources": {}, "errors": 0}

    async def scenario(force):
        return await main._discover_workday(
            cfg, [walmart], stats=stats, include_rejected=False, dry_run=False,
            force=force, run_time=datetime.now(timezone.utc),
        )

    assert run_async(scenario(False)) == []
    assert not any(host == walmart.workday.host for _method, host, _path in requests)
    assert stats["sources"]["workday/walmart"]["status"] == "rate_limited"
    assert stats["sources"]["workday/walmart"]["pending_details"] == 4

    caplog.set_level("WARNING")
    assert run_async(scenario(True)) == []
    assert any(host == walmart.workday.host for _method, host, _path in requests)
    assert "further requests risk additional rate limiting" in caplog.text
    engine = make_engine(database_url)
    with make_session_factory(engine)() as session:
        last = session.scalar(select(Run).where(Run.source == "workday").order_by(Run.id.desc()))
        assert last is not None and last.rate_limited_until is None
    engine.dispose()


def test_rate_limited_tenant_run_persists_configured_cooldown(tmp_path, monkeypatch):
    registry = load_registry(ROOT / "data/company_registry.json")
    walmart = next(item for item in verified_workday_entries(registry) if item.company_name == "Walmart")
    database_url = f"sqlite:///{tmp_path / 'rate-limited.db'}"
    engine = make_engine(database_url)
    initialize_database(engine)
    engine.dispose()

    real_async_client = httpx.AsyncClient

    def client_with_mock(*args, **kwargs):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={
                    "total": 2, "jobPostings": postings("R-1", "R-2"),
                }, request=request)
            return httpx.Response(429, headers={"Retry-After": "30"}, request=request)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    async def no_sleep(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock)
    monkeypatch.setattr(workday_module, "_wait_before_retry", no_sleep)
    cfg = app_config(
        database_url, retries=0, request_interval_seconds=0,
        rate_limit_cooldown_seconds=7200,
    )
    now = utc_now()

    async def scenario():
        return await main._discover_workday(
            cfg, [walmart], stats={"sources": {}, "errors": 0},
            include_rejected=False, dry_run=False, force=True, run_time=now,
        )

    assert run_async(scenario()) == []
    engine = make_engine(database_url)
    with make_session_factory(engine)() as session:
        saved = session.scalar(
            select(Run).where(Run.source == "workday", Run.tenant_key == "walmart")
            .order_by(Run.id.desc())
        )
        assert saved is not None
        assert saved.errors == 0
        assert saved.rate_limited_until is not None
        assert saved.pending_details == 2
        if saved.rate_limited_until.tzinfo is None:
            saved_until = saved.rate_limited_until.replace(tzinfo=timezone.utc)
        else:
            saved_until = saved.rate_limited_until
        assert saved_until >= now + timedelta(seconds=7190)
    engine.dispose()


def test_source_summary_formats_rate_limit_pending_and_failures(caplog):
    caplog.set_level("INFO")
    main._log_source_summary({"sources": {
        "workday/walmart": {
            "status": "rate_limited", "retry_after_seconds": 7200, "pending_details": 4,
        },
        "greenhouse/MongoDB": {"status": "ok", "pending_details": 0},
        "workday/other": {"status": "failed", "pending_details": 1},
    }})
    assert "Source workday/walmart: rate limited, retry after about 120 min; pending details=4" in caplog.text
    assert "Source greenhouse/MongoDB: ok; pending details=0" in caplog.text
    assert "Source workday/other: failed; pending details=1" in caplog.text
