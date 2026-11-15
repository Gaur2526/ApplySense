import asyncio

import pytest
from pydantic import ValidationError

from app.companies.registry import CompanyEntry, load_registry, verified_greenhouse_entries
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
    assert len(entries) == 46
    assert len(names) == len(set(names))
    assert names == PDF_COMPANY_NAMES


def test_pending_companies_are_preserved_and_only_mongodb_is_verified_greenhouse():
    entries = load_registry()
    assert len(entries) == 46
    pending = [entry for entry in entries if entry.company_name != "MongoDB"]
    assert all(entry.status == "pending_verification" for entry in pending)
    assert all(entry.ats_provider is None for entry in pending)
    assert all(entry.board_identifier is None for entry in pending)
    mongodb = next(entry for entry in entries if entry.company_name == "MongoDB")
    assert (mongodb.status, mongodb.ats_provider, mongodb.board_identifier) == ("verified", "greenhouse", "mongodb")
    assert verified_greenhouse_entries(entries) == [mongodb]


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


def test_discovery_fetches_only_the_verified_mongodb_board_without_real_http(monkeypatch):
    config = load_config("config/config.yaml", env_file=None)
    fetched = []

    class ScoutStub:
        def __init__(self, **_kwargs):
            pass

        async def fetch_jobs(self, identifier):
            fetched.append(identifier)
            return []

        async def close(self):
            return None

    monkeypatch.setattr("app.main.GreenhouseScout", ScoutStub)
    assert asyncio.run(discover(config)) == []
    assert fetched == ["mongodb"]
