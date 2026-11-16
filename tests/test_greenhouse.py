import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.companies.registry import CompanyEntry, verified_greenhouse_entries
from app.database.connection import initialize_database
from app.database.repository import JobRepository
from app.schemas.job import Job as JobSchema
from app.filters.age_filter import matches_age
from app.filters.decision import FilterStatus
from app.filters.employment_filter import matches_employment
from app.notifications.formatting import format_job
from app.scouts.greenhouse import GreenhouseResponseError, GreenhouseScout, normalize_greenhouse_job


BOARD = "test_board_token"


def documented_job(**updates):
    job = {
        "id": 127817,
        "internal_job_id": 144381,
        "title": "Vault Designer",
        "updated_at": "2016-01-14T10:55:28-05:00",
        "requisition_id": "50",
        "location": {"name": "NYC"},
        "absolute_url": "https://boards.greenhouse.io/vaulttec/jobs/127817",
        "language": "en",
        "metadata": None,
        "content": "<p>Build &amp; ship</p>",
        "departments": [{"id": 13583, "name": "Department of Departments"}],
        "offices": [{"id": 8304, "name": "East Coast", "location": "United States"}],
    }
    job.update(updates)
    return job


def api_response(jobs=None):
    return {"jobs": [documented_job()] if jobs is None else jobs, "meta": {"total": 1}}


def schema_values(**updates):
    values = {
        "source": "greenhouse",
        "source_id": "job-1",
        "company": "Example Company",
        "title": "Software Engineer",
        "location": "Remote",
        "employment_type": "",
        "description": "Build software",
        "canonical_url": "https://example.com/jobs/1",
        "content_hash": "a" * 64,
        "posted_at": None,
    }
    values.update(updates)
    return values


def run_fetch(handler, *, retries=0, concurrency=4, board=BOARD):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scout = GreenhouseScout(client=client, retries=retries, concurrency=concurrency)
            return await scout.fetch_jobs(board)

    return asyncio.run(scenario())


def test_fetch_uses_documented_get_endpoint_and_content_parameter():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=api_response(), request=request)

    jobs = run_fetch(handler)
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"https://boards-api.greenhouse.io/v1/boards/{BOARD}/jobs?content=true"
    assert len(jobs) == 1


def test_documented_job_normalizes_to_orm_fields_without_department_as_employment():
    job = run_fetch(lambda request: httpx.Response(200, json=api_response(), request=request))[0]
    assert job["source"] == "greenhouse"
    assert job["source_id"] == "127817"
    assert job["title"] == "Vault Designer"
    assert job["location"] == "NYC"
    assert job["canonical_url"] == "https://boards.greenhouse.io/vaulttec/jobs/127817"
    assert job["description"] == "Build & ship"
    assert job["employment_type"] == ""
    assert job["posted_at"].isoformat() == "2016-01-14T15:55:28+00:00"
    assert job["age_source"] == "updated"


def test_normalized_greenhouse_fixture_passes_job_schema():
    normalized = normalize_greenhouse_job(documented_job(), BOARD)
    job = JobSchema.model_validate(normalized)
    assert job.source == "greenhouse"
    assert job.title == "Vault Designer"
    assert job.company == BOARD


def test_double_escaped_html_is_decoded_and_markup_is_removed():
    body = {"jobs": [documented_job(content="&amp;lt;p&amp;gt;Build &amp;amp; ship &amp;lt;strong&amp;gt;well&amp;lt;/strong&amp;gt;.&amp;lt;/p&amp;gt;")]}
    jobs = run_fetch(lambda request: httpx.Response(200, json=body, request=request))
    assert jobs[0]["description"] == "Build & ship well."


@pytest.mark.parametrize(
    "updates",
    [
        {"location": None, "content": None, "departments": None, "updated_at": None},
        {"location": None, "content": "", "departments": [], "updated_at": "not-a-date"},
        {"location": None, "content": None, "departments": None, "updated_at": None},
    ],
)
def test_optional_and_empty_fields_are_safe(updates):
    body = {"jobs": [documented_job(**updates)]}
    jobs = run_fetch(lambda request: httpx.Response(200, json=body, request=request))
    assert len(jobs) == 1
    assert jobs[0]["location"] == ""
    assert jobs[0]["description"] == ""
    assert jobs[0]["employment_type"] == ""
    assert jobs[0]["posted_at"] is None


def test_job_schema_defaults_optional_fields():
    job = JobSchema(
        source="greenhouse", source_id="job-1", company="Example", title="Engineer",
        canonical_url="https://example.com/jobs/1", content_hash="a" * 64,
    )
    assert job.location == ""
    assert job.employment_type == ""
    assert job.description == ""
    assert job.experience_requirement is None
    assert job.posted_at is None
    assert job.age_source is None
    assert job.age_days is None
    assert job.discovered_at.tzinfo is not None
    assert job.uncertainty_reasons == []


@pytest.mark.parametrize("url", ["", "ftp://example.com/job", "https:///job", "https://example.com:bad/job"])
def test_job_schema_rejects_invalid_urls(url):
    with pytest.raises(ValidationError):
        JobSchema(**schema_values(canonical_url=url))


def test_job_schema_rejects_invalid_sha256_hashes():
    with pytest.raises(ValidationError):
        JobSchema(**schema_values(content_hash="not-a-sha256"))


@pytest.mark.parametrize("field", ["source", "source_id", "company", "title", "canonical_url", "content_hash"])
def test_job_schema_rejects_missing_required_fields(field):
    values = schema_values()
    del values[field]
    with pytest.raises(ValidationError):
        JobSchema(**values)


@pytest.mark.parametrize(
    ("field", "maximum", "value"),
    [
        ("source", 40, "s"),
        ("source_id", 200, "i"),
        ("company", 300, "c"),
        ("title", 500, "t"),
        ("location", 500, "l"),
        ("employment_type", 100, "e"),
        ("canonical_url", 2000, "https://example.com/"),
    ],
)
def test_job_schema_enforces_orm_string_column_lengths(field, maximum, value):
    values = schema_values()
    values[field] = value * (maximum + 1)
    with pytest.raises(ValidationError):
        JobSchema(**values)


def test_to_orm_dict_contains_only_existing_orm_fields():
    job = JobSchema(**schema_values())
    values = job.to_orm_dict()
    assert values == job.model_dump(mode="python")
    assert set(values) == {
        "source", "source_id", "company", "title", "location", "employment_type",
        "description", "canonical_url", "content_hash", "experience_requirement",
        "posted_at", "age_source", "age_days", "discovered_at", "uncertainty_reasons",
    }


def test_schema_invalid_individual_job_is_skipped_with_valid_jobs_kept(caplog):
    payload = {"jobs": [documented_job(), documented_job(id=2, title="T" * 501)]}
    jobs = run_fetch(lambda request: httpx.Response(200, json=payload, request=request))
    assert len(jobs) == 1
    assert jobs[0]["source_id"] == "127817"
    assert "Skipping invalid normalized Greenhouse job" in caplog.text


def test_normalized_job_dictionary_persists_through_existing_repository():
    job = run_fetch(lambda request: httpx.Response(200, json=api_response(), request=request))[0]
    engine = create_engine("sqlite://")
    initialize_database(engine)
    with Session(engine) as session:
        repository = JobRepository(session)
        assert repository.add_many([job]) == 1
        saved = repository.get(job["canonical_url"])
        assert saved is not None
        assert saved.title == "Vault Designer"
        assert saved.company == BOARD
        assert saved.employment_type == ""
    engine.dispose()


@pytest.mark.parametrize("payload", [[], None, {"error": "invalid board"}, {"jobs": {}}, {"jobs": None}])
def test_malformed_response_structures_are_rejected(payload):
    with pytest.raises(GreenhouseResponseError):
        run_fetch(lambda request: httpx.Response(200, json=payload, request=request))


def test_malformed_json_is_rejected_without_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"{not json", headers={"Content-Type": "application/json"}, request=request)

    with pytest.raises(GreenhouseResponseError, match="not valid JSON"):
        run_fetch(handler, retries=2)
    assert calls == 1


def test_bad_records_and_invalid_urls_are_skipped_without_losing_valid_jobs(caplog):
    payload = {"jobs": [
        documented_job(),
        None,
        {"id": 2, "title": "Missing URL"},
        documented_job(id={"unexpected": "object"}),
        documented_job(id=5, absolute_url="not a URL"),
        documented_job(id=7, absolute_url="https://[invalid"),
        documented_job(id=6, location="not an object"),
    ]}
    jobs = run_fetch(lambda request: httpx.Response(200, json=payload, request=request))
    assert [job["source_id"] for job in jobs] == ["127817"]
    assert "Skipping malformed Greenhouse job" in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_4xx_errors_are_not_retried(status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    with pytest.raises(httpx.HTTPStatusError):
        run_fetch(handler, retries=3)
    assert calls == 1


@pytest.mark.parametrize("status", [501, 505])
def test_nontransient_5xx_errors_are_not_retried(status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    with pytest.raises(httpx.HTTPStatusError):
        run_fetch(handler, retries=3)
    assert calls == 1


def test_429_respects_retry_after(monkeypatch):
    calls, delays = 0, []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, request=request)
        return httpx.Response(200, json=api_response(), request=request)

    assert len(run_fetch(handler, retries=1)) == 1
    assert calls == 2
    assert delays == [3.0]


def test_5xx_retries_with_exponential_jitter(monkeypatch):
    calls, ceilings, delays = 0, [], []

    def fake_uniform(low, high):
        ceilings.append(high)
        return high / 2

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.scouts.greenhouse.random.uniform", fake_uniform)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=api_response(), request=request)

    assert len(run_fetch(handler, retries=2)) == 1
    assert calls == 3
    assert ceilings == [0.5, 1.0]
    assert delays == [0.25, 0.5]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_errors_and_timeouts_retry(monkeypatch, error):
    calls, delays = 0, []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error("transient failure", request=request)
        return httpx.Response(200, json=api_response(), request=request)

    assert len(run_fetch(handler, retries=1)) == 1
    assert calls == 2
    assert len(delays) == 1


def test_retry_limit_is_retries_plus_initial_attempt(monkeypatch):
    calls = 0

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    with pytest.raises(httpx.HTTPStatusError):
        run_fetch(handler, retries=2)
    assert calls == 3


def test_bounded_concurrency_caps_in_flight_requests():
    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.active = 0
            self.maximum = 0

        async def handle_async_request(self, request):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(0.01)
                return httpx.Response(200, json=api_response(), request=request)
            finally:
                self.active -= 1

    async def scenario():
        transport = CountingTransport()
        async with httpx.AsyncClient(transport=transport) as client:
            scout = GreenhouseScout(client=client, concurrency=2, retries=0)
            results = await asyncio.gather(*(scout.fetch_jobs(f"test_board_{i}") for i in range(7)))
        return transport.maximum, results

    maximum, results = asyncio.run(scenario())
    assert maximum == 2
    assert len(results) == 7


def test_configured_timeout_is_applied_to_http_client():
    async def scenario():
        scout = GreenhouseScout(timeout=0.17)
        try:
            return scout.client.timeout
        finally:
            await scout.close()

    timeout = asyncio.run(scenario())
    assert timeout.connect == 0.17
    assert timeout.read == 0.17


def test_scout_rejects_blank_token_before_request():
    def handler(_request):
        pytest.fail("blank board token must not cause an HTTP request")

    with pytest.raises(ValueError, match="must not be empty"):
        run_fetch(handler, board="   ")


def test_only_verified_greenhouse_entries_with_nonempty_tokens_are_selected():
    entries = [
        CompanyEntry(company_name="Verified", ats_provider="greenhouse", board_identifier="verified_test_token", status="verified"),
        CompanyEntry(company_name="Pending", ats_provider="greenhouse", board_identifier=None, status="pending_verification"),
        CompanyEntry(company_name="Other ATS", ats_provider="lever", board_identifier=None, status="verified"),
    ]
    selected = verified_greenhouse_entries(entries)
    assert [(entry.company_name, entry.board_identifier) for entry in selected] == [("Verified", "verified_test_token")]


def test_whitespace_only_board_identifier_is_rejected():
    with pytest.raises(ValueError, match="whitespace-only"):
        CompanyEntry(company_name="Blank Token", ats_provider="greenhouse", board_identifier="   ", status="verified")


def test_company_fetch_failure_does_not_discard_other_company_results(monkeypatch):
    import app.main as main

    entries = [
        CompanyEntry(company_name="Failed Company", ats_provider="greenhouse", board_identifier="failed_test_token", status="verified"),
        CompanyEntry(company_name="Working Company", ats_provider="greenhouse", board_identifier="working_test_token", status="verified"),
    ]
    monkeypatch.setattr(main, "load_registry", lambda _path: entries)

    class ScoutStub:
        def __init__(self, **_kwargs):
            pass

        async def fetch_jobs(self, board):
            if board == "failed_test_token":
                raise httpx.ConnectError("offline")
            return [{"canonical_url": "https://example.org/job", "title": "Engineer", "company": board,
                     "location": "Remote", "description": "", "employment_type": ""}]

        async def close(self):
            pass

    monkeypatch.setattr(main, "GreenhouseScout", ScoutStub)
    from app.config import AppConfig

    jobs = asyncio.run(main.discover(AppConfig()))
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Working Company"


def feed_shaped_job(first_published=None, updated_at=None, metadata=None):
    now = datetime.now(timezone.utc)
    return documented_job(
        id=8220001,
        internal_job_id=10001,
        requisition_id="R-10001",
        company_name="MongoDB",
        first_published=first_published,
        updated_at=updated_at,
        metadata=metadata,
    )


@pytest.mark.parametrize(
    ("age", "expected"),
    [(24, FilterStatus.REJECT), (6, FilterStatus.PASS), (7, FilterStatus.PASS), (8, FilterStatus.REJECT)],
)
def test_greenhouse_real_feed_first_published_age_boundaries(age, expected):
    now = datetime.now(timezone.utc)
    raw = feed_shaped_job(
        first_published=(now - timedelta(days=age)).isoformat(),
        updated_at=(now - timedelta(days=1)).isoformat(),
    )
    normalized = normalize_greenhouse_job(raw, BOARD, run_time=now)
    assert normalized["source_id"] == "8220001"
    assert normalized["age_source"] == "posted"
    assert matches_age(normalized, 7, "allow", run_time=now).status is expected


def test_greenhouse_missing_first_published_uses_updated_at_and_message_says_updated():
    now = datetime.now(timezone.utc)
    raw = feed_shaped_job(
        first_published=None,
        updated_at=(now - timedelta(days=2)).isoformat(),
    )
    normalized = normalize_greenhouse_job(raw, BOARD, run_time=now)
    normalized["age_days"] = 2
    message = format_job(normalized, run_time=now)
    assert normalized["posted_at"] is not None
    assert normalized["age_source"] == "updated"
    assert "Updated 2 days ago" in message


def test_greenhouse_unparseable_first_published_falls_back_to_updated_at():
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    normalized = normalize_greenhouse_job(
        feed_shaped_job(first_published="not-a-date", updated_at="2026-11-15T08:00:00+05:30"),
        BOARD, run_time=now,
    )
    assert normalized["age_source"] == "updated"
    assert normalized["posted_at"].isoformat() == "2026-11-15T02:30:00+00:00"


def test_greenhouse_first_published_offset_converts_to_utc():
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    normalized = normalize_greenhouse_job(
        feed_shaped_job(first_published="2026-11-15T08:00:00-04:00"),
        BOARD, run_time=now,
    )
    assert normalized["age_source"] == "posted"
    assert normalized["posted_at"].isoformat() == "2026-11-15T12:00:00+00:00"


def test_greenhouse_employment_type_is_read_only_from_exact_metadata_name(caplog):
    normalized = normalize_greenhouse_job(
        feed_shaped_job(metadata=[
            {"id": 1, "name": "Employment Type", "value": "Intern", "value_type": "single_select"},
            {"id": 2, "name": "Salary", "value": "$250,000", "value_type": "short_text"},
            {"id": 3, "name": "Currency Range", "value": "USD 200k-250k", "value_type": "short_text"},
        ]),
        BOARD,
    )
    assert normalized["employment_type"] == "intern"
    assert matches_employment(normalized, ["intern"]).status is FilterStatus.PASS
    assert "metadata" not in normalized
    assert "$250,000" not in repr(normalized)
    assert "USD 200k-250k" not in repr(normalized)
    assert "$250,000" not in caplog.text
    assert "USD 200k-250k" not in caplog.text


def test_greenhouse_does_not_guess_employment_from_other_metadata_names():
    normalized = normalize_greenhouse_job(
        feed_shaped_job(metadata=[
            {"id": 1, "name": "Employment", "value": "Full time", "value_type": "single_select"},
            {"id": 2, "name": "Job Type", "value": "Contract", "value_type": "single_select"},
        ]),
        BOARD,
    )
    assert normalized["employment_type"] == ""
    assert matches_employment(normalized, ["full-time"]).status is FilterStatus.UNCERTAIN


def test_greenhouse_future_timestamp_is_clamped_to_zero_without_schema_error():
    now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    normalized = normalize_greenhouse_job(
        feed_shaped_job(first_published="2026-11-18T08:00:00Z"),
        BOARD,
        run_time=now,
    )
    assert normalized["age_days"] == 0
    assert matches_age(normalized, 7, "allow", run_time=now).status is FilterStatus.PASS
