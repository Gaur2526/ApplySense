import asyncio
import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.cli as cli
import app.main as main
import app.scouts.workday as workday
from app.companies.registry import CompanyEntry, WorkdayTenantConfig
from app.config import AppConfig
from app.diagnostics import format_filter_diagnostics
from app.database.connection import initialize_database
from app.database.models import EvaluatedJob, Run, utc_now
from app.database.models import Job, Notification
from app.database.repository import JobRepository
from app.filters.decision import FilterStatus
from app.filters.age_filter import matches_age
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.location_filter import matches_workday_location
from app.filters.settings import effective_filter_settings_hash
from app.scouts.workday import (
    WorkdayEvaluation,
    WorkdayResponseError,
    WorkdayRobotsBlockedError,
    WorkdayScout,
    _list_job,
    _posted_age_disagreement,
    normalize_workday_job,
    parse_workday_posted_age,
    title_level_rejection,
    workday_plain_text,
)


FIXTURES = Path(__file__).parent / "fixtures"
LIST_FIXTURE = json.loads((FIXTURES / "workday_walmart_list.json").read_text())
DETAIL_FIXTURE = json.loads((FIXTURES / "workday_walmart_details.json").read_text())


def tenant(**updates):
    values = {
        "host": "walmart.wd504.myworkdayjobs.com",
        "tenant": "walmart",
        "site": "WalmartExternal",
        "country_facet_id": "c4f78be1a8f14da0ab49ce1162348a5e",
        "assume_country": "IN",
        "search_texts": ["software engineer"],
        "page_size": 20,
        "max_pages": 10,
        "max_detail_requests_per_run": 30,
        "min_interval_seconds": 3600,
    }
    values.update(updates)
    return WorkdayTenantConfig.model_validate(values)


def config(**filter_updates):
    filters = {
        "keywords": ["software engineer"],
        "excluded_keywords": ["recruiter", "account development"],
        "seniority_titles": ["senior", "sr", "lead", "staff", "principal", "manager"],
        "unknown_location_policy": "strict",
        "max_min_years": 2,
        "employment_types": ["full-time"],
    }
    filters.update(filter_updates)
    return AppConfig.model_validate({"filters": filters})


def list_item(
    title="Software Engineer", req="R-100", location="IN KA BANGALORE",
    posted_on="Posted 2 Days Ago",
):
    return {
        "title": title,
        "externalPath": f"/job/IN-KA-BANGALORE/{title.replace(' ', '-')}_{req}",
        "locationsText": location,
        "postedOn": posted_on,
        "bulletFields": [req],
    }


def list_response(items, total=999):
    return {"total": total, "jobPostings": items, "userAuthenticated": False}


def detail_response(req="R-100", **updates):
    payload = copy.deepcopy(DETAIL_FIXTURE)
    payload["jobPostingInfo"]["jobReqId"] = req
    payload["jobPostingInfo"]["title"] = "Software Engineer"
    payload["jobPostingInfo"]["jobDescription"] = "Requirements: 1 year of experience."
    payload["jobPostingInfo"]["location"] = "IN KA BANGALORE Home Office PW II"
    payload["jobPostingInfo"]["externalUrl"] = f"https://jobs.example.test/{req}"
    payload["jobPostingInfo"].update(updates)
    return payload


def run_async(awaitable):
    return asyncio.run(awaitable)


def make_scout(handler, *, request_interval_seconds=0, retries=0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1)
    return WorkdayScout(
        client=client, request_interval_seconds=request_interval_seconds, retries=retries
    ), client


def test_post_request_uses_exact_list_endpoint_headers_and_space_search_text():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=LIST_FIXTURE)

    scout, client = make_scout(handler)
    async def scenario():
        try:
            total, jobs = await scout.fetch_first_page(tenant(), "software engineer")
            assert total == 200
            assert len(jobs) == 3
        finally:
            await client.aclose()
    run_async(scenario())
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/wday/cxs/walmart/WalmartExternal/jobs"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"] == "ApplySense/0.1 (personal job search)"
    body = json.loads(request.content)
    assert body == {
        "appliedFacets": {"locationCountry": ["c4f78be1a8f14da0ab49ce1162348a5e"]},
        "limit": 20, "offset": 0, "searchText": "software engineer",
    }
    assert "+" not in body["searchText"]


@pytest.mark.parametrize(
    ("responses", "page_size", "max_pages", "expected_requests"),
    [
        ([list_response([list_item()], total=400)], 2, 10, 1),  # short page beats total
        ([list_response([list_item("Software Engineer", "R-1")], total=400),
          list_response([], total=400)], 1, 10, 2),  # empty page
        ([list_response([list_item("Software Engineer", "R-1")]),
          list_response([list_item("Software Engineer", "R-2")]),
          list_response([list_item("Software Engineer", "R-3")])], 1, 2, 2),  # max pages
    ],
)
def test_pagination_stops_on_short_empty_or_max_pages(responses, page_size, max_pages, expected_requests):
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=responses[min(len(seen) - 1, len(responses) - 1)])
    scout, client = make_scout(handler)
    async def scenario():
        try:
            await scout.list_jobs("Walmart", tenant(page_size=page_size, max_pages=max_pages))
        finally:
            await client.aclose()
    run_async(scenario())
    assert len(seen) == expected_requests
    assert [body["offset"] for body in seen] == [i * page_size for i in range(expected_requests)]


def test_max_pages_truncation_logs_missing_jobs_warning_once(caplog):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        page = body["offset"] // 2
        records = [
            list_item(req=f"R-{page * 2 + index}")
            for index in range(2)
        ]
        return httpx.Response(200, json=list_response(records, total=11), request=request)

    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.list_jobs(
                "Walmart", tenant(page_size=2, max_pages=3, search_texts=["software engineer"])
            )
        finally:
            await client.aclose()

    jobs = run_async(scenario())
    warnings = [
        record.getMessage() for record in caplog.records
        if record.levelname == "WARNING" and "max_pages reached" in record.getMessage()
    ]
    assert len(jobs) == 6
    assert len(calls) == 3
    assert warnings == [
        "Workday tenant walmart: max_pages reached, 6 of total 11 jobs seen; some jobs may be missing"
    ]


def test_results_merge_across_search_texts_and_dedupe_by_source_id():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=list_response([list_item(req="R-1")]))
    scout, client = make_scout(handler)
    async def scenario():
        try:
            jobs = await scout.list_jobs("Walmart", tenant(search_texts=["software engineer", "engineer"]))
            return jobs
        finally:
            await client.aclose()
    jobs = run_async(scenario())
    assert len(calls) == 2
    assert [body["searchText"] for body in calls] == ["software engineer", "engineer"]
    assert [job["source_id"] for job in jobs] == ["walmart/R-1"]


def test_known_ids_do_not_stop_pagination_and_do_not_fetch_details():
    calls = []
    pages = [list_response([list_item(req="R-1")]), list_response([list_item(req="R-2")])]
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=pages.pop(0))
        pytest.fail("known list jobs must not trigger detail requests")
    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs(
                "Walmart", tenant(page_size=1, max_pages=2), config(),
                known_source_ids={"walmart/R-1", "walmart/R-2"},
            )
        finally:
            await client.aclose()
    assert run_async(scenario()) == []
    assert len(calls) == 2


def test_stage_one_title_filters_handle_comma_titles_and_title_levels():
    tenant_config = tenant()
    filters = config()
    assert workday._stage_one_rejection(
        {"title": "STAFF, SOFTWARE ENGINEER"}, filters, tenant_config
    )
    assert workday._stage_one_rejection(
        {"title": "Senior, Software Engineer - Java Fullstack"}, filters, tenant_config
    )
    assert workday._stage_one_rejection(
        {"title": "Associate, HR Shared Services"}, filters, tenant_config
    )
    assert workday._stage_one_rejection(
        {"title": "Account Development Representative"}, filters, tenant_config
    )
    for title in ("Software Engineer II", "Software Engineer III"):
        assert workday._stage_one_rejection({"title": title}, filters, tenant_config) is None
    for title, level in (
        ("Software Engineer IV", "IV"), ("Software Engineer V", "V"),
        ("Software Engineer VI", "VI"), ("Software Engineer 4", "4"),
        ("Software Engineer 5", "5"), ("Software Engineer 6", "6"),
    ):
        assert title_level_rejection(title) == level
    assert title_level_rejection("Software Engineer III") is None
    assert title_level_rejection("Software Engineer 3") is None
    assert title_level_rejection("Software Engineer 4", ["4"]) == "4"
    assert title_level_rejection("Software Engineer 4", ["V"]) is None


@pytest.mark.parametrize(
    "location",
    ["IN KA BANGALORE Home Office PW II", "IN TN CHENNAI Home Office Capita Land"],
)
def test_workday_indian_country_state_prefix_is_eligible(location):
    decision = matches_workday_location(
        {"source": "workday", "location": location}, "strict", "IN"
    )
    assert decision.status is FilterStatus.PASS


def test_india_state_prefix_passes_even_when_city_is_unrecognized_and_country_is_not_assumed():
    decision = matches_workday_location(
        {"source": "workday", "location": "IN ZZ UNKNOWNCITY Home Office"}, "strict", None
    )
    assert decision.status is FilterStatus.PASS


@pytest.mark.parametrize("location", ["2 Locations", "3 Locations"])
@pytest.mark.parametrize("policy", ["strict", "allow"])
def test_workday_assumed_country_makes_counted_locations_uncertain_for_both_policies(location, policy):
    assert matches_workday_location(
        {"source": "workday", "location": location}, policy, "IN"
    ).status is FilterStatus.UNCERTAIN


@pytest.mark.parametrize("policy", ["strict", "allow"])
def test_workday_assumed_country_makes_multiple_locations_uncertain_for_both_policies(policy):
    assert matches_workday_location(
        {"source": "workday", "location": "Multiple Locations"}, policy, "IN"
    ).status is FilterStatus.UNCERTAIN


@pytest.mark.parametrize(
    ("country_code", "expected"),
    [("IN", FilterStatus.PASS), ("US", FilterStatus.REJECT)],
)
def test_workday_details_country_code_resolves_multiple_location_label(country_code, expected):
    item = _list_job(list_item(req="R-COUNTRY", location="2 Locations"), "Walmart", tenant())
    payload = detail_response(
        "R-COUNTRY",
        location="2 Locations",
        jobRequisitionLocation={"country": {"alpha2Code": country_code}},
    )
    normalized = normalize_workday_job(item, payload, tenant())
    assert normalized["_workday_location_country"] == country_code
    assert matches_workday_location(normalized, "strict", "IN").status is expected


def test_explain_keeps_workday_assumed_country_for_location_diagnostics():
    cfg = config(unknown_location_policy="strict")
    item = {
        "source": "workday", "source_id": "walmart/R-MULTI", "company": "Walmart",
        "title": "Software Engineer", "location": "2 Locations",
        "employment_type": "full-time", "description": "Requirements: 1 year of experience",
        "canonical_url": "https://jobs.example.test/R-MULTI", "content_hash": "7" * 64,
        "experience_requirement": 1, "posted_at": None, "_workday_assume_country": "IN",
    }
    prepared = main.filter_jobs([item], cfg, preserve_workday_context=True)
    assert prepared[0]["_workday_assume_country"] == "IN"
    report = format_filter_diagnostics(
        prepared, cfg, run_time=datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    )
    assert "location: PASS=0 UNCERTAIN=1 REJECT=0" in report
    assert "Unrecognized locations (top 25):\n  None" in report


def test_workday_assumed_country_makes_generic_unrecognized_locations_uncertain():
    assert matches_workday_location({"location": "Bentonville"}, "strict", "IN").status is FilterStatus.UNCERTAIN
    assert matches_workday_location({"location": "Dublin"}, "strict", "IN").status is FilterStatus.UNCERTAIN


@pytest.mark.parametrize(
    ("label", "days"),
    [
        ("Posted Today", 0),
        ("Posted Yesterday", 1),
        ("Posted 2 Days Ago", 2),
        ("Posted 6 Days Ago", 6),
        ("Posted 13 Days Ago", 13),
        ("Posted 14 Days Ago", 14),
        ("Posted 30+ Days Ago", 30),
    ],
)
def test_workday_posted_on_labels_convert_to_age_days(label, days):
    assert parse_workday_posted_age(label) == days
    item = _list_job(list_item(posted_on=label), "Walmart", tenant())
    assert item["age_days"] == days
    expected = FilterStatus.PASS if days <= 7 else FilterStatus.REJECT
    assert matches_age(item, 7, "allow").status is expected


@pytest.mark.parametrize("policy", ["allow", "reject"])
def test_unknown_workday_posted_on_label_is_uncertain_under_both_policies(policy):
    item = _list_job(list_item(posted_on="Date unavailable"), "Walmart", tenant())
    assert item["_age_unknown_is_uncertain"] is True
    assert matches_age(item, 7, policy).status is FilterStatus.UNCERTAIN


@pytest.mark.parametrize(
    ("days", "expected"),
    [(7, FilterStatus.PASS), (8, FilterStatus.REJECT)],
)
def test_workday_posted_days_seven_passes_eight_rejects(days, expected):
    item = _list_job(
        list_item(posted_on=f"Posted {days} Days Ago"), "Walmart", tenant()
    )
    assert matches_age(item, 7, "allow").status is expected


def test_workday_thirty_plus_rejects_at_list_stage_without_detail_request():
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json=list_response([list_item(posted_on="Posted 30+ Days Ago")])
            )
        pytest.fail("an age-rejected Workday listing must not request details")
    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs("Walmart", tenant(), config())
        finally:
            await client.aclose()
    results = run_async(scenario())
    assert len(requests) == 1
    assert len(results) == 1
    assert results[0].decision == "REJECT"
    assert results[0].remember is True
    assert results[0].reached_details is False
    assert "30 days old" in results[0].reason


def test_workday_unrecognized_age_label_logs_once_per_run_per_label(caplog):
    calls = []
    records = [
        list_item(req="R-1", posted_on="Date not displayed"),
        list_item(req="R-2", posted_on="Date not displayed"),
    ]
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response(records))
        calls.append(request)
        return httpx.Response(200, json=detail_response(
            re.search(r"_R-(\d+)$", request.url.path).group(0)[1:],
            startDate=None,
        ))
    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs("Walmart", tenant(), config())
        finally:
            await client.aclose()
    run_async(scenario())
    assert len(calls) == 2
    assert caplog.text.count("Unrecognized Workday postedOn label: Date not displayed") == 1


def test_workday_start_date_overrides_disagreeing_posted_on_label_and_is_utc(caplog):
    fixed_run = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    item = _list_job(
        list_item(posted_on="Posted Today"), "Walmart", tenant()
    )
    payload = detail_response("R-100", startDate="2026-11-06")
    normalized = normalize_workday_job(item, payload, tenant(), run_time=fixed_run)
    assert normalized["posted_at"] == datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert normalized["age_days"] == 10
    assert matches_age(normalized, 7, "allow", run_time=fixed_run).status is FilterStatus.REJECT
    assert "disagrees with startDate" not in caplog.text


@pytest.mark.parametrize(
    ("label", "start_age", "expected"),
    [
        ("Posted 30+ Days Ago", 510, None),
        ("Posted 30+ Days Ago", 10, "real"),
        ("Posted 6 Days Ago", 6, None),
        ("Posted 6 Days Ago", 7, "minor"),
    ],
)
def test_workday_posted_on_label_comparison_handles_bounds_and_rounding(label, start_age, expected):
    assert _posted_age_disagreement(label, parse_workday_posted_age(label), start_age) == expected


@pytest.mark.parametrize(
    ("start_age", "expected_warning"),
    [(510, False), (10, True)],
)
def test_workday_30_plus_start_date_reconciles_lower_bound(start_age, expected_warning, caplog):
    fixed_run = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    postings = [list_item(req="R-30PLUS", posted_on="Posted 30+ Days Ago")]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response(postings), request=request)
        start_date = (fixed_run - timedelta(days=start_age)).date().isoformat()
        return httpx.Response(200, json=detail_response("R-30PLUS", startDate=start_date), request=request)

    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs(
                "Walmart", tenant(), config(max_age_days=60), run_time=fixed_run
            )
        finally:
            await client.aclose()

    results = run_async(scenario())
    assert len(results) == 1
    assert results[0].job["age_days"] == start_age
    assert ("postedOn age disagrees with startDate" in caplog.text) is expected_warning


def test_many_workday_age_disagreements_emit_one_warning_with_count(caplog):
    fixed_run = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    postings = [list_item(req=f"R-{index}", posted_on="Posted Today") for index in range(3)]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response(postings), request=request)
        req = int(re.search(r"_R-(\d+)$", request.url.path).group(1))
        start_date = (fixed_run - timedelta(days=10 + req)).date().isoformat()
        return httpx.Response(200, json=detail_response(f"R-{req}", startDate=start_date), request=request)

    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs("Walmart", tenant(), config(), run_time=fixed_run)
        finally:
            await client.aclose()

    run_async(scenario())
    warnings = [
        record.getMessage() for record in caplog.records
        if record.levelname == "WARNING" and "postedOn age disagrees with startDate" in record.getMessage()
    ]
    assert warnings == [
        "Workday postedOn age disagrees with startDate for 3 jobs in tenant walmart; using startDate"
    ]


def test_minor_workday_age_disagreement_is_debug_only(caplog):
    caplog.set_level("DEBUG")
    fixed_run = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    postings = [list_item(req="R-6", posted_on="Posted 6 Days Ago")]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response(postings), request=request)
        start_date = (fixed_run - timedelta(days=7)).date().isoformat()
        return httpx.Response(200, json=detail_response("R-6", startDate=start_date), request=request)

    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs("Walmart", tenant(), config(), run_time=fixed_run)
        finally:
            await client.aclose()

    run_async(scenario())
    messages = [record for record in caplog.records if "disagrees with startDate" in record.getMessage()]
    assert len(messages) == 1
    assert messages[0].levelname == "DEBUG"


def test_workday_missing_start_date_keeps_label_age():
    item = _list_job(list_item(posted_on="Posted 6 Days Ago"), "Walmart", tenant())
    normalized = normalize_workday_job(
        item, detail_response("R-100", startDate=None), tenant()
    )
    assert normalized["posted_at"] is None
    assert normalized["age_days"] == 6
    assert normalized["age_source"] == "posted"


def test_workday_without_assumed_country_keeps_strict_unknown_location_rejection():
    assert matches_workday_location({"location": "Dublin"}, "strict", None).status is FilterStatus.REJECT


def test_details_normalize_fields_url_start_date_and_ignore_other_payload_sections():
    list_record = LIST_FIXTURE["jobPostings"][0]
    item = _list_job(list_record, "Walmart", tenant())
    job = normalize_workday_job(item, DETAIL_FIXTURE, tenant())
    assert job["source"] == "workday"
    assert job["source_id"] == "walmart/R-1642453"
    assert job["title"] == "Senior Software Engineer"
    assert job["location"] == "IN KA BANGALORE Home Office PW II"
    assert job["employment_type"] == "full-time"
    assert job["posted_at"].date().isoformat() == "2025-06-24"
    assert job["canonical_url"] == DETAIL_FIXTURE["jobPostingInfo"]["externalUrl"]
    assert "<span" not in job["description"].casefold()
    assert "similarJobs" not in job
    assert "hiringOrganization" not in job
    assert "questionnaire" not in job
    assert "questionnaireId" not in job


def test_details_url_falls_back_to_configured_host_site_and_external_path():
    record = LIST_FIXTURE["jobPostings"][0]
    item = _list_job(record, "Walmart", tenant())
    payload = detail_response("R-1642453", externalUrl=None)
    job = normalize_workday_job(item, payload, tenant())
    assert job["canonical_url"] == (
        "https://walmart.wd504.myworkdayjobs.com/WalmartExternal"
        + record["externalPath"]
    )


def test_details_html_cleaning_preserves_requested_block_breaks_without_recursion():
    item = _list_job(list_item(req="R-100"), "Walmart", tenant())
    payload = detail_response(
        "R-100",
        jobDescription=(
            "<h2><span><span>Minimum Qualifications</span></span></h2>"
            "<p><span><span>First line</span></span><br>Second line</p>"
            "<li>Third line</li>"
        ),
    )
    job = normalize_workday_job(item, payload, tenant())
    assert "<" not in job["description"]
    assert "Minimum Qualifications" in job["description"]
    assert "Minimum Qualifications\n" in job["description"]
    assert "First line\nSecond line" in job["description"]
    assert re.search(r"Second line\n+Third line", job["description"])


def test_details_html_cleaning_handles_hundreds_of_nested_spans():
    nested = "<span>" * 500 + "Requirements: 1 year of experience" + "</span>" * 500
    cleaned = workday_plain_text(nested)
    assert cleaned == "Requirements: 1 year of experience"
    assert parse_experience_requirement(cleaned) == 1


def test_job_req_id_mismatch_is_logged_and_skipped():
    calls = []
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response([list_item(req="R-100")]))
        calls.append(request)
        return httpx.Response(200, json=detail_response("R-DIFFERENT"))
    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs("Walmart", tenant(), config())
        finally:
            await client.aclose()
    result = run_async(scenario())
    assert len(calls) == 1
    assert len(result) == 1
    assert result[0].decision == "REJECT"
    assert "jobReqId mismatch" in result[0].reason
    assert result[0].job is not None


def test_workday_request_has_language_header_and_details_route():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=detail_response())
    scout, client = make_scout(handler)
    async def scenario():
        try:
            await scout.fetch_details(tenant(), {
                "external_path": "/job/IN-TEST/Software-Engineer_R-100"
            })
        finally:
            await client.aclose()
    run_async(scenario())
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/wday/cxs/walmart/WalmartExternal/job/IN-TEST/Software-Engineer_R-100"
    assert requests[0].headers["Accept"] == "application/json"
    assert requests[0].headers["Accept-Language"] == "en-US"


def test_experience_parser_recognizes_curly_apostrophe_without_space():
    assert parse_experience_requirement("Minimum Qualifications: 3years’ experience in engineering") == 3


def test_option_alternatives_choose_lowest_even_when_full_fixture_has_boilerplate():
    description = (
        "Company overview: 5,900 retail units, 55 banners, and 26 countries.\n"
        "Minimum Qualifications: Option 1: Bachelor's degree and 3years’ experience.\n"
        "Option 2: 5 years’ experience.\n"
        "Preferred Qualifications: 12 years of experience."
    )
    assert parse_experience_requirement(description) == 3


def test_experience_alternatives_take_lowest_and_record_note():
    description = (
        "Minimum Qualifications: Option 1: Bachelor's degree and 3years’ experience. "
        "Option 2: 5 years’ experience."
    )
    assert parse_experience_requirement(description) == 3
    decision = matches_experience({"description": description}, max_min_years=2)
    assert decision.status is FilterStatus.REJECT
    assert "alternatives, lowest used" in decision.reason


def test_preferred_qualifications_do_not_affect_required_minimum():
    description = (
        "Minimum Qualifications: 2 years of required experience. "
        "Preferred Qualifications: 10 years of preferred experience."
    )
    assert parse_experience_requirement(description) == 2


def test_corporate_boilerplate_numbers_are_not_experience_requirements():
    description = (
        "Our company operates 5,900 retail units under 55 banners in 26 countries. "
        "We have supported customers for 25 years."
    )
    assert parse_experience_requirement(description) is None


def test_walmart_fixture_uses_alternatives_and_ignores_boilerplate():
    description = workday_plain_text(DETAIL_FIXTURE["jobPostingInfo"]["jobDescription"])
    assert "5,900 retail units" in description
    assert "55 banners" in description
    assert "26 countries" in description
    assert re.search(r"3years[’'] experience", description)
    assert "Option 1:" in description and "Option 2:" in description
    assert "Preferred Qualifications" in description
    item = _list_job(LIST_FIXTURE["jobPostings"][0], "Walmart", tenant())
    job = normalize_workday_job(item, DETAIL_FIXTURE, tenant())
    assert job["experience_requirement"] == 3
    decision = matches_experience(job, max_min_years=2)
    assert decision.status is FilterStatus.REJECT
    assert "alternatives, lowest used" in decision.reason


def test_workday_detail_cap_limits_requests_per_run():
    detail_requests = []
    postings = [list_item(req=f"R-{i}") for i in range(3)]
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response(postings))
        detail_requests.append(request)
        req = re.search(r"_R-(\d+)$", request.url.path).group(1)
        payload = detail_response(f"R-{req}", title="Software Engineer", location="IN KA BANGALORE")
        return httpx.Response(200, json=payload)
    scout, client = make_scout(handler)
    async def scenario():
        try:
            return await scout.fetch_jobs(
                "Walmart", tenant(max_detail_requests_per_run=2), config()
            )
        finally:
            await client.aclose()
    results = run_async(scenario())
    assert len(detail_requests) == 2
    assert len([item for item in results if item.reached_details]) == 2


def test_recent_rejected_evaluation_is_skipped_then_revisited_after_ttl(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'evaluated.db'}")
    initialize_database(db)
    req = "walmart/R-100"
    with Session(db) as session:
        repo = JobRepository(session)
        repo.record_evaluations([{
            "source": "workday", "source_id": req, "decision": "REJECT",
            "reason": "Title level excluded", "settings_hash": "d" * 64,
        }])
        assert repo.recently_evaluated_source_ids(
            "workday", {req}, reevaluate_after_days=14, settings_hash="d" * 64
        ) == {req}
        old = utc_now() - timedelta(days=15)
        session.scalar(select(EvaluatedJob).where(EvaluatedJob.source_id == req)).evaluated_at = old
        session.commit()
        assert repo.recently_evaluated_source_ids(
            "workday", {req}, reevaluate_after_days=14, settings_hash="d" * 64
        ) == set()
    db.dispose()

    details = []
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response([list_item(req="R-100")]))
        details.append(request)
        return httpx.Response(200, json=detail_response("R-100"))
    scout, client = make_scout(handler)
    async def scenario():
        try:
            skipped = await scout.fetch_jobs(
                "Walmart", tenant(), config(),
                recently_evaluated_source_ids={req},
            )
            assert skipped == []
            assert details == []
            revisited = await scout.fetch_jobs(
                "Walmart", tenant(), config(),
                recently_evaluated_source_ids=set(),
            )
            assert len(revisited) == 1
            assert details
        finally:
            await client.aclose()
    run_async(scenario())


@pytest.mark.parametrize(
    ("changes", "should_reuse"),
    [({}, True), ({"max_min_years": 1}, False), ({"keywords": ["data engineer"]}, False)],
)
def test_remembered_rejection_reuse_requires_matching_settings_hash(
    tmp_path, monkeypatch, changes, should_reuse
):
    old_config = config()
    current_config = config(**changes)
    source_id = "walmart/R-settings"
    database_path = tmp_path / f"settings-hash-{len(changes)}-{should_reuse}.db"
    engine = create_engine(f"sqlite:///{database_path}")
    initialize_database(engine)
    with Session(engine) as session:
        JobRepository(session).record_evaluations([{
            "source": "workday", "source_id": source_id, "decision": "REJECT",
            "reason": "Previously rejected", "settings_hash": effective_filter_settings_hash(old_config),
        }])
    engine.dispose()
    entry = CompanyEntry.model_validate({
        "company_name": "Walmart", "ats_provider": "workday", "status": "verified",
        "workday": tenant(min_interval_seconds=0).model_dump(),
    })
    observed = []

    class StubScout:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, _company, _tenant, _config, *, known_source_ids=None,
                             recently_evaluated_source_ids=None, baseline_mode=False):
            observed.append(set(recently_evaluated_source_ids or ()))
            return []
        async def close(self):
            pass

    monkeypatch.setattr(main, "WorkdayScout", StubScout)
    run_async(main._discover_workday(
        current_config.model_copy(update={"database_url": f"sqlite:///{database_path}"}),
        [entry], stats={}, include_rejected=False, dry_run=False, force=True,
    ))
    assert observed == [{source_id}] if should_reuse else observed == [set()]


def test_tenant_min_interval_uses_runs_and_force_overrides_it(tmp_path, monkeypatch):
    database_path = tmp_path / "tenant-interval.db"
    engine = create_engine(f"sqlite:///{database_path}")
    initialize_database(engine)
    with Session(engine) as session:
        repo = JobRepository(session)
        run = repo.create_run(source="workday", tenant_key="walmart")
        repo.finish_run(run, fetched=0, filtered=0, new=0, notified=0, errors=0)
    engine.dispose()

    entry = CompanyEntry.model_validate({
        "company_name": "Walmart", "ats_provider": "workday", "status": "verified",
        "workday": tenant(min_interval_seconds=3600).model_dump(),
    })
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps([entry.model_dump(mode="json")]))
    cfg = AppConfig(
        database_url=f"sqlite:///{database_path}",
        company_registry_path=str(registry_path),
        filters=config().filters,
    )
    calls = []
    class StubScout:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, *_args, **_kwargs):
            calls.append("fetch")
            return [WorkdayEvaluation(
                "walmart/R-DRY", "REJECT", "age rejected", None,
                reached_details=False, remember=True,
            )]
        async def close(self):
            pass
    monkeypatch.setattr(main, "WorkdayScout", StubScout)
    async def scenario():
        skipped = await main._discover_workday(
            cfg, [entry], stats={}, include_rejected=False, dry_run=True,
            force=False,
        )
        assert skipped == []
        assert calls == []
        await main._discover_workday(
            cfg, [entry], stats={}, include_rejected=False, dry_run=True,
            force=True,
        )
    run_async(scenario())
    assert calls == ["fetch"]
    with Session(create_engine(f"sqlite:///{database_path}")) as session:
        assert len(list(session.scalars(select(Run)))) == 1
        assert list(session.scalars(select(EvaluatedJob))) == []


def test_rejected_detail_is_remembered_without_persisting_description(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    entry = CompanyEntry.model_validate({
        "company_name": "Walmart", "ats_provider": "workday", "status": "verified",
        "workday": tenant().model_dump(),
    })
    registry_path.write_text(json.dumps([entry.model_dump(mode="json")]))
    database_path = tmp_path / "rejected-evaluation.db"
    cfg = AppConfig(
        database_url=f"sqlite:///{database_path}",
        company_registry_path=str(registry_path),
        filters=config().filters,
    )
    rejected_job = {
        "source": "workday", "source_id": "walmart/R-1642453", "company": "Walmart",
        "title": "Software Engineer", "location": "IN KA BANGALORE", "employment_type": "full-time",
        "description": "A long rejected description that must not be stored",
        "canonical_url": "https://jobs.example.test/rejected", "content_hash": "c" * 64,
        "experience_requirement": 5,
    }
    class StubScout:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, *_args, **_kwargs):
            return [WorkdayEvaluation(
                "walmart/R-1642453", "REJECT", "Minimum experience is too high", rejected_job
            )]
        async def close(self):
            pass
    monkeypatch.setattr(main, "WorkdayScout", StubScout)
    assert run_async(main._discover_workday(
        cfg, [entry], stats={}, include_rejected=False, dry_run=False,
        force=True,
    )) == []
    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as session:
        evaluation = session.scalar(select(EvaluatedJob))
        assert evaluation is not None
        assert evaluation.decision == "REJECT"
        assert not hasattr(evaluation, "description")
        assert session.scalar(select(Job)) is None
        assert "A long rejected description that must not be stored" not in str(evaluation.reason)
        assert session.scalar(select(Run).where(Run.source == "workday")) is not None
    engine.dispose()


def test_workday_jobs_use_existing_pipeline_persistence_and_notification_claims(tmp_path, monkeypatch):
    database_path = tmp_path / "workday-pipeline.db"
    jobs = [
        {
            "source": "workday", "source_id": f"walmart/R-20{i}", "company": "Walmart",
            "title": "Software Engineer", "location": "IN KA BANGALORE",
            "employment_type": "full-time", "description": "Requirements: 1 year experience",
            "canonical_url": f"https://jobs.example.test/R-20{i}", "content_hash": str(i) * 64,
            "experience_requirement": 1, "posted_at": None, "uncertainty_reasons": [],
        }
        for i in (0, 1)
    ]
    discovery_calls = 0
    async def fake_discover(_config, *, stats=None, include_rejected=False, dry_run=False, force=False):
        nonlocal discovery_calls
        discovery_calls += 1
        if stats is not None:
            stats.update(fetched=2 if discovery_calls == 1 else 0,
                         filtered=2 if discovery_calls == 1 else 0, errors=0)
        return [dict(item) for item in jobs] if discovery_calls == 1 else []
    sent = []
    summaries = []
    async def fake_send(_token, _chat_id, outgoing):
        sent.extend(outgoing)
        return [True for _ in outgoing]
    async def fake_summary(_token, _chat_id, message):
        summaries.append(message)
        return True
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", fake_send)
    monkeypatch.setattr(main, "send_summary", fake_summary)
    cfg = AppConfig(
        database_url=f"sqlite:///{database_path}",
        notifications={"per_run_cap": 1},
        telegram={"bot_token": "test-token", "chat_id": "test-chat", "enabled": True},
        filters=config().filters,
    )
    assert main.run(cfg, notify=True) == 1
    assert len(sent) == 1
    first_sent_id = sent[0]["source_id"]
    assert summaries == ["1 more new jobs queued for the next run"]
    assert main.run(cfg, notify=True) == 1
    assert {item["source_id"] for item in sent} == {"walmart/R-200", "walmart/R-201"}
    assert len(sent) == 2 and sent[1]["source_id"] != first_sent_id
    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as session:
        stored = list(session.scalars(select(Job).where(Job.source == "workday")))
        notifications = list(session.scalars(select(Notification)))
        assert len(stored) == 2
        assert len(notifications) == 2
        assert all(notification.state == "sent" for notification in notifications)
    engine.dispose()


def test_workday_jobs_obey_baseline_mode_without_telegram(tmp_path, monkeypatch):
    database_path = tmp_path / "workday-baseline.db"
    job = {
        "source": "workday", "source_id": "walmart/R-201", "company": "Walmart",
        "title": "Software Engineer", "location": "IN KA BANGALORE",
        "employment_type": "full-time", "description": "Requirements: 1 year experience",
        "canonical_url": "https://jobs.example.test/R-201", "content_hash": "e" * 64,
        "experience_requirement": 1, "posted_at": None, "uncertainty_reasons": [],
    }
    async def fake_discover(_config, **_kwargs):
        return [dict(job)]
    async def forbidden(*_args, **_kwargs):
        pytest.fail("baseline mode must not send Telegram")
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "send_jobs", forbidden)
    cfg = AppConfig(
        database_url=f"sqlite:///{database_path}",
        filters=config().filters,
    )
    assert main.run(cfg, baseline_mode=True, notify=True) == 1
    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as session:
        stored = session.scalar(select(Job).where(Job.source_id == "walmart/R-201"))
        assert stored is not None and stored.is_baseline
        assert session.scalar(select(Notification)) is None
    engine.dispose()


def test_baseline_skips_detail_fetch_and_remembers_jobs_for_next_normal_run(tmp_path, monkeypatch):
    database_path = tmp_path / "baseline-list-only.db"
    entry = CompanyEntry.model_validate({
        "company_name": "Walmart", "ats_provider": "workday", "status": "verified",
        "workday": tenant(min_interval_seconds=0).model_dump(),
    })
    registry_path = tmp_path / "baseline-registry.json"
    registry_path.write_text(json.dumps([entry.model_dump(mode="json")]))
    cfg = AppConfig(
        database_url=f"sqlite:///{database_path}",
        company_registry_path=str(registry_path),
        filters=config().filters,
        telegram={"bot_token": "test-token", "chat_id": "test-chat", "enabled": True},
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=list_response([list_item(req="R-BASELINE")]), request=request)
        pytest.fail("baseline and its following run must not fetch job details")

    def scout_factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1)
        return WorkdayScout(client=client, **kwargs)

    monkeypatch.setattr(main, "WorkdayScout", scout_factory)
    async def forbidden_send(*_args, **_kwargs):
        pytest.fail("a baselined job must not be notified on the following normal run")
    monkeypatch.setattr(main, "send_jobs", forbidden_send)

    assert main.run(cfg, baseline_mode=True, notify=True) == 0
    assert main.run(cfg, notify=True) == 0
    assert [request.method for request in requests] == ["POST", "POST"]
    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as session:
        evaluation = session.scalar(select(EvaluatedJob).where(
            EvaluatedJob.source_id == "walmart/R-BASELINE"
        ))
        assert evaluation is not None and evaluation.decision == "PASS"
        assert evaluation.settings_hash == effective_filter_settings_hash(cfg)
        assert session.scalar(select(Job)) is None
        assert session.scalar(select(Notification)) is None
    engine.dispose()


def test_workday_jobs_use_run_dry_run_and_explain_without_database_or_telegram(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "workday-dry-run.db"
    registry_path = tmp_path / "workday-registry.json"
    entry = CompanyEntry.model_validate({
        "company_name": "Walmart", "ats_provider": "workday", "status": "verified",
        "workday": tenant().model_dump(),
    })
    registry_path.write_text(json.dumps([entry.model_dump(mode="json")]))
    job = {
        "source": "workday", "source_id": "walmart/R-202", "company": "Walmart",
        "title": "Software Engineer", "location": "IN KA BANGALORE",
        "employment_type": "full-time", "description": "Requirements: 1 year experience",
        "canonical_url": "https://jobs.example.test/R-202", "content_hash": "f" * 64,
        "experience_requirement": 1, "posted_at": None, "uncertainty_reasons": [],
    }
    async def fake_discover(_config, *, stats=None, include_rejected=False, dry_run=False, force=False):
        assert include_rejected is True
        assert dry_run is True
        if stats is not None:
            stats.update(fetched=1, filtered=1, errors=0)
        return [dict(job)]
    def forbidden(*_args, **_kwargs):
        pytest.fail("Workday explain dry-run must not access persistence or Telegram")
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "make_engine", forbidden)
    monkeypatch.setattr(main, "send_jobs", forbidden)
    monkeypatch.setattr(main, "send_summary", forbidden)
    cfg = AppConfig(
        database_url=f"sqlite:///{database_path}",
        company_registry_path=str(registry_path),
        filters=config().filters,
    )
    assert main.run(cfg, dry_run=True, explain=True, notify=True) == 1
    output = capsys.readouterr().out
    assert "Dry-run filter diagnostic: 1 fetched jobs" in output
    assert "Unknown location policy: strict" in output
    assert not database_path.exists()


def test_maximum_details_deferred_jobs_are_not_marked_evaluated():
    requests = []
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=list_response([list_item(req=f"R-{i}") for i in range(3)]))
        requests.append(request)
        req = re.search(r"_R-(\d+)$", request.url.path).group(1)
        return httpx.Response(200, json=detail_response(f"R-{req}"))
    scout, client = make_scout(handler)
    async def scenario():
        try:
            result = await scout.fetch_jobs(
                "Walmart", tenant(max_detail_requests_per_run=1), config()
            )
            return result
        finally:
            await client.aclose()
    results = run_async(scenario())
    assert len(requests) == 1
    assert len(results) == 1


def test_html_block_page_is_reported_as_invalid_json():
    def handler(_request):
        return httpx.Response(200, text="<html>Request blocked</html>")
    scout, client = make_scout(handler)
    async def scenario():
        try:
            with pytest.raises(WorkdayResponseError):
                await scout.fetch_first_page(tenant())
        finally:
            await client.aclose()
    run_async(scenario())


def test_retries_429_and_honors_retry_after(monkeypatch):
    attempts = []
    waits = []
    async def fake_wait(attempt, response=None):
        waits.append((attempt, response.headers.get("Retry-After") if response else None))
    monkeypatch.setattr(workday, "_wait_before_retry", fake_wait)
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json=list_response([]))
    scout, client = make_scout(handler, retries=1)
    async def scenario():
        try:
            return await scout.fetch_first_page(tenant())
        finally:
            await client.aclose()
    assert run_async(scenario()) == (999, [])
    assert len(attempts) == 2
    assert waits == [(0, "3")]


def test_network_timeout_reuses_existing_retry_backoff(monkeypatch):
    attempts = []
    waits = []
    async def fake_wait(attempt, response=None):
        waits.append((attempt, response))
    monkeypatch.setattr(workday, "_wait_before_retry", fake_wait)
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("fixture timeout")
        return httpx.Response(200, json=list_response([]))
    scout, client = make_scout(handler, retries=1)
    async def scenario():
        try:
            return await scout.fetch_first_page(tenant())
        finally:
            await client.aclose()
    assert run_async(scenario()) == (999, [])
    assert len(attempts) == 2
    assert waits == [(0, None)]


def test_request_spacing_and_same_host_concurrency(monkeypatch):
    active = 0
    maximum = 0
    sleeps = []
    real_sleep = asyncio.sleep
    async def fake_sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(workday.asyncio, "sleep", fake_sleep)
    class AsyncFakeClient:
        async def request(self, method, url, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await real_sleep(0.005)
            active -= 1
            request = httpx.Request(method, url)
            return httpx.Response(200, json=list_response([]), request=request)
        async def aclose(self):
            pass
    scout = WorkdayScout(
        client=AsyncFakeClient(), request_interval_seconds=0
    )
    async def scenario():
        try:
            await asyncio.gather(
                scout.fetch_first_page(tenant()),
                scout.fetch_first_page(tenant()),
            )
        finally:
            await scout.close()
    run_async(scenario())
    assert maximum == 1

    spacer = WorkdayScout(
        client=AsyncFakeClient(), request_interval_seconds=1
    )
    async def spacing_scenario():
        try:
            await spacer.fetch_first_page(tenant())
            await spacer.fetch_first_page(tenant())
        finally:
            await spacer.close()
    run_async(spacing_scenario())
    assert any(delay > 0 for delay in sleeps)


@pytest.mark.parametrize(
    "path",
    ["/refreshFacet/country", "/refreshFacet/search", "/refreshFacet/a/b"],
)
def test_robots_request_guard_blocks_refresh_facet_path(path):
    def handler(_request):
        pytest.fail("robots-blocked paths must be rejected before issuing HTTP")
    scout, client = make_scout(handler)
    async def scenario():
        try:
            with pytest.raises(WorkdayRobotsBlockedError):
                await scout._request_json(
                    "GET", f"https://walmart.wd504.myworkdayjobs.com{path}"
                )
        finally:
            await client.aclose()
    run_async(scenario())
    WorkdayScout._guard_url("https://walmart.wd504.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs")


def test_cli_check_workday_prints_first_page_and_details_without_database_writes(tmp_path, monkeypatch, capsys):
    registry_path = tmp_path / "registry.json"
    entry = CompanyEntry.model_validate({
        "company_name": "Walmart", "ats_provider": "workday",
        "status": "pending_verification", "workday": tenant().model_dump(),
    })
    registry_path.write_text(json.dumps([entry.model_dump(mode="json")]))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("filters:\n  keywords: [software engineer]\n")
    database_path = tmp_path / "must-not-exist.db"
    monkeypatch.setattr(cli, "load_config", lambda _path: AppConfig(
        database_url=f"sqlite:///{database_path}",
        company_registry_path=str(registry_path),
        filters=config().filters,
    ))
    calls = []
    class StubScout:
        def __init__(self, **_kwargs):
            pass
        async def fetch_first_page(self, _tenant):
            calls.append("list")
            return LIST_FIXTURE["total"], LIST_FIXTURE["jobPostings"]
        async def fetch_details(self, _tenant, _item):
            calls.append("details")
            return DETAIL_FIXTURE
        async def close(self):
            pass
    monkeypatch.setattr(cli, "WorkdayScout", StubScout)
    assert cli.main(["--config", str(config_path), "check-workday", "walmart", "--details"]) == 0
    output = capsys.readouterr().out
    assert "Workday tenant walmart: total=200" in output
    assert "Senior Software Engineer" in output
    assert "req id: R-1642453" in output
    assert "minimum experience: 3" in output
    assert calls == ["list", "details"]
    assert not database_path.exists()


def test_one_failing_workday_tenant_does_not_stop_other_sources(tmp_path, monkeypatch, caplog):
    greenhouse = {
        "company_name": "Greenhouse Co", "ats_provider": "greenhouse",
        "board_identifier": "greenhouse-board", "status": "verified",
    }
    good_workday = CompanyEntry.model_validate({
        "company_name": "Good Workday", "ats_provider": "workday", "status": "verified",
        "workday": tenant(tenant="good", host="good.example.test").model_dump(),
    })
    bad_workday = CompanyEntry.model_validate({
        "company_name": "Bad Workday", "ats_provider": "workday", "status": "verified",
        "workday": tenant(tenant="bad", host="bad.example.test").model_dump(),
    })
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps([
        greenhouse, good_workday, bad_workday
    ], default=lambda item: item.model_dump(mode="json")))
    cfg = AppConfig(
        database_url=f"sqlite:///{tmp_path / 'discover.db'}",
        company_registry_path=str(registry_path),
        filters=config().filters,
    )

    greenhouse_job = {
        "source": "greenhouse", "source_id": "gh-1", "company": "Greenhouse Co",
        "title": "Software Engineer", "location": "Bengaluru, India", "employment_type": "full-time",
        "description": "1 year experience", "canonical_url": "https://example.test/gh",
        "content_hash": "a" * 64, "experience_requirement": 1, "posted_at": None,
    }
    class GreenhouseStub:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, _board):
            return [dict(greenhouse_job)]
        async def close(self):
            pass
    class WorkdayStub:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, company_name, tenant_cfg, _config, **_kwargs):
            if tenant_cfg.tenant == "bad":
                raise WorkdayResponseError("fixture block page")
            job = {
                "source": "workday", "source_id": "good/R-1", "company": company_name,
                "title": "Software Engineer", "location": "IN KA BANGALORE", "employment_type": "full-time",
                "description": "Requirements: 1 year of experience.", "canonical_url": "https://example.test/wd",
                "content_hash": "b" * 64, "experience_requirement": 1, "posted_at": None,
                "_workday_assume_country": "IN",
            }
            return [WorkdayEvaluation("good/R-1", "PASS", "passed", job)]
        async def close(self):
            pass
    monkeypatch.setattr(main, "GreenhouseScout", GreenhouseStub)
    monkeypatch.setattr(main, "WorkdayScout", WorkdayStub)
    stats = {}
    jobs = run_async(main.discover(
        cfg, stats=stats, dry_run=True, force=True
    ))
    assert {job["source"] for job in jobs} == {"greenhouse", "workday"}
    assert stats["errors"] == 1
    failures = [record for record in caplog.records if "Workday tenant bad failed" in record.getMessage()]
    assert len(failures) == 1
    assert not (tmp_path / "discover.db").exists()
