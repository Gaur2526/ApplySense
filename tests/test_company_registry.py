import asyncio

import pytest
from pydantic import ValidationError

from app.companies.registry import (
    CompanyEntry, load_registry, verified_greenhouse_entries, verified_workday_entries,
)
from app.config import load_config
from app.main import discover


PDF_COMPANY_NAMES = [
    "United Airlines", "GoDaddy", "Eptura", "Sopra Steria", "Dentsu", "AMEX", "PhonePe", "Mercari",
    "Ascendion", "Arcesium", "Qualcomm", "NVIDIA", "Paytm", "UKG", "NatWest Group", "IBM", "Flipkart",
    "Zeta", "Gojoko Technologies", "Axtria", "Samsung", "Wayfair", "Zepto", "Kotak", "MongoDB",
    "Zoxima Solutions", "MakeMyTrip", "eBay", "Cohesity", "American Express GBT", "PayPal", "Walmart",
    "Thoughtworks", "JPMorgan Chase", "SHL", "FreechargeBiz", "Thoughts2Binary", "Atlassian", "Visa",
    "Progress", "Tech Mahindra", "Tripjack", "Nykaa", "UiPath", "YesMadam", "Snapmint",
]


def test_registry_contains_exactly_the_pdf_companies_once():
    entries = load_registry()
    names = [entry.company_name for entry in entries]
    assert len(entries) == 47
    assert len(names) == len(set(names))
    assert names[:46] == PDF_COMPANY_NAMES
    assert names[46] == "Salesforce"


def test_pending_companies_are_preserved_with_only_mongodb_and_walmart_verified():
    entries = load_registry()
    assert len(entries) == 47
    pending = [entry for entry in entries if entry.company_name not in {"MongoDB", "Walmart"}]
    assert all(entry.status == "pending_verification" for entry in pending)
    assert all(
        entry.ats_provider is None
        for entry in pending if entry.company_name != "Salesforce"
    )
    assert all(
        entry.board_identifier is None
        for entry in pending if entry.company_name != "Salesforce"
    )
    mongodb = next(entry for entry in entries if entry.company_name == "MongoDB")
    assert (mongodb.status, mongodb.ats_provider, mongodb.board_identifier) == ("verified", "greenhouse", "mongodb")
    assert verified_greenhouse_entries(entries) == [mongodb]
    walmart = next(entry for entry in entries if entry.company_name == "Walmart")
    assert verified_workday_entries(entries) == [walmart]
    salesforce = next(entry for entry in entries if entry.company_name == "Salesforce")
    assert salesforce.status == "pending_verification"
    assert salesforce.workday is not None and salesforce.workday.tenant == "salesforce"


def test_walmart_workday_settings_are_configured_and_verified():
    walmart = next(entry for entry in load_registry() if entry.company_name == "Walmart")
    assert walmart.status == "verified"
    assert walmart.ats_provider == "workday"
    assert walmart.board_identifier is None
    assert walmart.workday is not None
    assert walmart.workday.host == "walmart.wd504.myworkdayjobs.com"
    assert walmart.workday.tenant == "walmart"
    assert walmart.workday.site == "WalmartExternal"
    assert walmart.workday.country_facet_id == "c4f78be1a8f14da0ab49ce1162348a5e"
    assert walmart.workday.max_pages == 15


def test_verified_workday_entries_require_explicit_configuration():
    values = {
        "company_name": "Example", "ats_provider": "workday", "status": "verified",
        "workday": {
            "host": "example.wd1.myworkdayjobs.com", "tenant": "example", "site": "External",
            "country_facet_id": "facet",
        },
    }
    entry = CompanyEntry.model_validate(values)
    assert verified_workday_entries([entry]) == [entry]
    with pytest.raises(ValidationError):
        CompanyEntry.model_validate({**values, "workday": None})


def test_existing_careers_urls_are_preserved():
    entries = {entry.company_name: entry for entry in load_registry()}
    expected = {
        "Mercari": "https://careers.mercari.com/en/jobs/",
        "Ascendion": "https://ascendion.com/who-we-are/careers/",
        "Axtria": "https://www.axtria.com/axtria-careers/",
        "Cohesity": "https://www.cohesity.com/careers/open-positions/",
    }
    assert {name: str(entries[name].careers_url) for name in expected} == expected


def test_unverified_board_identifier_is_rejected():
    with pytest.raises(ValidationError):
        CompanyEntry(company_name="Sample", ats_provider="greenhouse", board_identifier="unverified-token", status="pending_verification")


def test_pending_verification_is_not_an_ats_provider_identifier():
    with pytest.raises(ValidationError):
        CompanyEntry(company_name="Sample", ats_provider="pending_verification", status="pending_verification")


def test_verified_greenhouse_entries_require_and_expose_identifier():
    verified = CompanyEntry(company_name="Verified Co", ats_provider="greenhouse", board_identifier="confirmed-board", status="verified")
    pending = CompanyEntry(company_name="Pending Co", ats_provider="greenhouse", status="pending_verification")
    unknown_provider = CompanyEntry(company_name="Unknown Provider Co", ats_provider=None, status="verified")
    assert verified_greenhouse_entries([verified, pending, unknown_provider]) == [verified]


def test_verified_greenhouse_entry_cannot_have_missing_identifier():
    with pytest.raises(ValidationError):
        CompanyEntry(company_name="Invalid Co", ats_provider="greenhouse", status="verified")


def test_discovery_fetches_verified_boards_without_real_http(monkeypatch):
    from app.config import AppConfig
    config = AppConfig(
        database_url="sqlite:///:memory:",
        company_registry_path="data/company_registry.json",
        filters={"keywords": ["engineer"]},
    )
    fetched = []
    workday_fetched = []

    class ScoutStub:
        def __init__(self, **_kwargs):
            pass

        async def fetch_jobs(self, identifier, **_kwargs):
            fetched.append(identifier)
            return []

        async def close(self):
            return None

    class WorkdayMustNotFetch:
        def __init__(self, **_kwargs):
            pass
        async def fetch_jobs(self, _company_name, tenant, _config, **_kwargs):
            workday_fetched.append(tenant.tenant)
            return []
        async def close(self):
            pass

    monkeypatch.setattr("app.main.GreenhouseScout", ScoutStub)
    monkeypatch.setattr("app.main.WorkdayScout", WorkdayMustNotFetch)
    assert asyncio.run(discover(config, dry_run=True)) == []
    assert fetched == ["mongodb"]
    assert workday_fetched == ["walmart"]
