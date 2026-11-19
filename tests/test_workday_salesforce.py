import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.companies.registry import CompanyEntry, WorkdayTenantConfig, load_registry
from app.config import AppConfig
from app.filters.decision import FilterStatus
from app.filters.experience_filter import matches_experience, parse_experience_requirement
from app.filters.location_filter import matches_location, preferred_city_rank
from app.filters.seniority_filter import matches_seniority
from app.notifications.formatting import format_job
from app.scouts.workday import (
    WorkdayScout, _list_job, normalize_workday_job,
)


FIXTURES = Path(__file__).parent / "fixtures"
SALESFORCE_LIST = json.loads((FIXTURES / "workday_salesforce_list.json").read_text())
SALESFORCE_DETAILS = json.loads((FIXTURES / "workday_salesforce_details.json").read_text())
RUN_TIME = datetime(2026, 11, 18, tzinfo=timezone.utc)


def registry_tenants():
    entries = load_registry("data/company_registry.json")
    salesforce = next(entry for entry in entries if entry.company_name == "Salesforce")
    walmart = next(entry for entry in entries if entry.company_name == "Walmart")
    return walmart.workday, salesforce.workday


def test_registry_adds_salesforce_without_changing_tenant_request_settings():
    entries = load_registry("data/company_registry.json")
    salesforce = next(entry for entry in entries if entry.company_name == "Salesforce")
    assert salesforce.status == "verified"
    assert salesforce.ats_provider == "workday"
    assert salesforce.workday.model_dump() == {
        "host": "salesforce.wd12.myworkdayjobs.com",
        "tenant": "salesforce",
        "site": "External_Career_Site",
        "country_facet_key": "CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country_from_Job_Posting_Location_Extended",
        "country_facet_id": "c4f78be1a8f14da0ab49ce1162348a5e",
        "assume_country": "IN", "search_texts": ["software"], "page_size": 20,
        "max_pages": 15, "max_detail_requests_per_run": 30,
        "min_interval_seconds": 3600, "reevaluate_after_days": 14,
        "excluded_title_levels": ["IV", "V", "VI", "4", "5", "6"],
    }


@pytest.mark.parametrize("tenant_index", [0, 1], ids=["walmart", "salesforce"])
def test_workday_request_body_uses_each_tenants_facet_key(tenant_index):
    tenants = registry_tenants()
    tenant = tenants[tenant_index]
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"total": 0, "jobPostings": []})

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        scout = WorkdayScout(client=client, retries=0, request_interval_seconds=0)
        try:
            await scout.fetch_first_page(tenant)
        finally:
            await scout.close()

    import asyncio
    asyncio.run(scenario())
    assert captured[0].method == "POST"
    assert captured[0].url.path.endswith("/wday/cxs/" + tenant.tenant + "/" + tenant.site + "/jobs")
    assert json.loads(captured[0].content)["appliedFacets"] == {
        tenant.country_facet_key: [tenant.country_facet_id]
    }


@pytest.mark.parametrize("tenant_index", [0, 1], ids=["walmart", "salesforce"])
def test_workday_headers_are_tenant_derived_for_list_and_details(tenant_index, caplog):
    import asyncio
    import logging

    tenants = registry_tenants()
    tenant = tenants[tenant_index]
    captured = []

    def handler(request):
        captured.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"total": 0, "jobPostings": []})
        return httpx.Response(200, json={"jobPostingInfo": {}})

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        scout = WorkdayScout(client=client, retries=0, request_interval_seconds=0)
        try:
            await scout.fetch_first_page(tenant)
            await scout.fetch_details(tenant, {"external_path": "/job/test"})
        finally:
            await scout.close()

    with caplog.at_level(logging.DEBUG, logger="app.scouts.workday"):
        asyncio.run(scenario())
    assert [request.method for request in captured] == ["POST", "GET"]
    expected_headers = {
        "origin": f"https://{tenant.host}",
        "referer": f"https://{tenant.host}/{tenant.site}",
        "accept-language": "en-US",
        "user-agent": "ApplySense/0.1 (personal job search)",
    }
    forbidden = {"cookie", "authorization", "sec-ch-ua", "sec-fetch-site", "sec-fetch-mode",
                 "sec-fetch-dest", "sec-fetch-user"}
    for request in captured:
        for name, value in expected_headers.items():
            assert request.headers[name] == value
        assert forbidden.isdisjoint({name.lower() for name in request.headers})
    assert caplog.text.count(f"Workday tenant {tenant.tenant} request header names=") == 1
    assert "negotiated HTTP version=HTTP/1.1" in caplog.text
    assert "en-US" not in caplog.text
    assert tenant.host not in caplog.text


def test_workday_http2_is_disabled_by_default():
    config = AppConfig()
    assert config.workday.http2 is False
    scout = WorkdayScout(http2=config.workday.http2)
    assert scout.http2 is False
    import asyncio
    asyncio.run(scout.close())


def test_salesforce_fixture_normalizes_real_shape_and_registry_company_name():
    _walmart, tenant = registry_tenants()
    posting = SALESFORCE_LIST["jobPostings"][0]
    item = _list_job(posting, "Salesforce", tenant)
    job = normalize_workday_job(item, SALESFORCE_DETAILS, tenant, run_time=RUN_TIME)
    assert job["company"] == "Salesforce"
    assert job["company"] != SALESFORCE_DETAILS["hiringOrganization"]["name"]
    assert job["title"] == "Data Engineering SMTS/LMTS"
    assert job["location"] == "India - Hyderabad"
    assert job["experience_requirement"] == 10
    assert job["apply_by"] == date(2026, 11, 23)
    assert job["age_days"] == 0
    assert "similarJobs" not in job
    assert matches_experience(job, max_min_years=2).status is FilterStatus.REJECT
    assert matches_location(job, "strict").status is FilterStatus.PASS


@pytest.mark.parametrize("location,city", [
    ("India - Hyderabad", "Hyderabad"), ("India - Bangalore", "Bangalore"),
])
def test_salesforce_india_city_is_eligible_and_ranked(location, city):
    job = {"location": location}
    assert matches_location(job, "strict").status is FilterStatus.PASS
    assert preferred_city_rank(job, [city, "Hyderabad", "Bangalore"]) == 0


@pytest.mark.parametrize("location", ["2 Locations", "4 Locations"])
def test_salesforce_multiple_locations_are_uncertain_under_assumed_country(location):
    # Salesforce labels are handled by the same Workday branch as Walmart.
    from app.filters.location_filter import matches_workday_location
    assert matches_workday_location(
        {"source": "workday", "location": location}, "strict", "IN"
    ).status is FilterStatus.UNCERTAIN


@pytest.mark.parametrize("title", ["Data Engineering SMTS", "Data Engineering LMTS", "Data Engineering PMTS"])
def test_salesforce_seniority_terms_are_rejected(title):
    terms = AppConfig().filters.seniority_titles
    assert matches_seniority({"title": title}, terms).status is FilterStatus.REJECT


@pytest.mark.parametrize("title", ["Software Engineering MTS", "Software Engineering AMTS"])
def test_mts_and_amts_are_not_salesforce_seniority_rejections(title):
    terms = AppConfig().filters.seniority_titles
    assert matches_seniority({"title": title}, terms).status is FilterStatus.PASS


@pytest.mark.parametrize("heading", [
    "Basic Qualifications", "Required Qualifications", "Minimum Qualifications",
    "What You Bring", "Requirements", "You're Our Person If",
])
def test_each_configured_required_heading_is_parsed(heading):
    text = f"{heading}: 10 to 14+ years of experience."
    assert parse_experience_requirement(text) == 10


@pytest.mark.parametrize("heading", ["Preferred Qualifications", "Nice to Have", "Bonus", "Even Better"])
def test_preferred_headings_are_ignored(heading):
    text = f"{heading}: 10 to 14+ years of experience."
    assert parse_experience_requirement(text) is None


def test_salesforce_html_entity_range_decodes_and_roles_boilerplate_is_ignored():
    assert parse_experience_requirement(
        "You&#39;re Our Person If... 10 to 14+ years of experience. "
        "Even Better If... applying for a maximum of 3 roles within 12 months."
    ) == 10
    assert parse_experience_requirement("Applying for a maximum of 3 roles within 12 months.") is None
    assert parse_experience_requirement("Please apply to 2 roles at once.") is None


@pytest.mark.parametrize("end_date,expected", [
    ("2026-11-23", "Apply by Nov 23 (4 days left)"),
    (None, None),
    ("2026-11-17", "Apply by Nov 17 (0 days left)"),
    ("2026-11-18", "Apply by Nov 18 (0 days left)"),
])
def test_telegram_apply_by_line(end_date, expected):
    _walmart, tenant = registry_tenants()
    payload = copy.deepcopy(SALESFORCE_DETAILS)
    if end_date is None:
        payload["jobPostingInfo"].pop("endDate", None)
    else:
        payload["jobPostingInfo"]["endDate"] = end_date
    payload["jobPostingInfo"]["timeLeftToApply"] = "99 years left to apply"
    posting = dict(SALESFORCE_LIST["jobPostings"][0])
    posting["title"] = "Software Engineer"
    job = normalize_workday_job(_list_job(posting, "Salesforce", tenant), payload, tenant, run_time=RUN_TIME)
    message = format_job(job, run_time=RUN_TIME)
    assert (expected in message) if expected else ("Apply by" not in message)


def test_salesforce_closed_job_is_counted_and_not_returned_for_pipeline():
    _walmart, tenant = registry_tenants()
    payload = copy.deepcopy(SALESFORCE_DETAILS)
    payload["jobPostingInfo"]["endDate"] = "2026-11-17"
    posting = dict(SALESFORCE_LIST["jobPostings"][0])
    posting["title"] = "Software Engineer"
    item = _list_job(posting, "Salesforce", tenant)
    detail_url = f"https://{tenant.host}/wday/cxs/{tenant.tenant}/{tenant.site}{item['external_path']}"

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 1, "jobPostings": [posting]})
        if str(request.url) == detail_url:
            return httpx.Response(200, json=payload)
        pytest.fail(f"Unexpected mocked request: {request.method} {request.url}")

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        scout = WorkdayScout(client=client, retries=0, request_interval_seconds=0)
        try:
            config = AppConfig.model_validate({"filters": {"keywords": ["software engineer"]}})
            result = await scout.fetch_jobs("Salesforce", tenant, config, run_time=RUN_TIME)
            return result, scout.closed_count
        finally:
            await scout.close()

    import asyncio
    result, closed = asyncio.run(scenario())
    assert closed == 1
    assert len(result) == 1
    assert result[0].decision == "REJECT"
    assert "closed" in result[0].reason


def test_refresh_facet_guard_applies_to_salesforce_host():
    from app.scouts.workday import WorkdayRobotsBlockedError

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("blocked URL reached HTTP")
        ))
        scout = WorkdayScout(client=client, retries=0)
        try:
            with pytest.raises(WorkdayRobotsBlockedError):
                await scout._request_json(
                    "GET", "https://salesforce.wd12.myworkdayjobs.com/refreshFacet/country"
                )
        finally:
            await scout.close()

    import asyncio
    asyncio.run(scenario())
