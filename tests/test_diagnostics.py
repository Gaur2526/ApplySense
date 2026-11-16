import pytest
from datetime import datetime, timedelta, timezone

import app.main as main
from app.config import AppConfig
from app.diagnostics import format_filter_diagnostics
from app.filters.evaluation import evaluate_filter_decisions


EXPLAIN_NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


@pytest.fixture
def diagnostic_jobs():
    return [
        {
            "source": "greenhouse", "source_id": "india-pass", "company": "Example",
            "title": "Software Engineer", "location": "Bengaluru, India",
            "employment_type": "Full-Time", "description": "Requirements: 1 year of experience",
            "canonical_url": "https://jobs.example/india-pass", "content_hash": "a" * 64,
            "posted_at": None, "experience_requirement": None,
        },
        {
            "source": "greenhouse", "source_id": "india-internal", "company": "Example",
            "title": "Internal Tools Engineer", "location": "Gurugram, India",
            "employment_type": "Full-Time", "description": "Requirements vary by team",
            "canonical_url": "https://jobs.example/india-internal", "content_hash": "b" * 64,
            "posted_at": None, "experience_requirement": None,
        },
        {
            "source": "greenhouse", "source_id": "india-senior", "company": "Example",
            "title": "Senior Software Engineer", "location": "Noida, India",
            "employment_type": "Contract", "description": "Requirements: 5+ years of experience",
            "canonical_url": "https://jobs.example/india-senior", "content_hash": "c" * 64,
            "posted_at": None, "experience_requirement": None,
        },
        {
            "source": "greenhouse", "source_id": "outside-keyword", "company": "Example",
            "title": "Backend Engineer", "location": "London, UK",
            "employment_type": "", "description": "Experience varies",
            "canonical_url": "https://jobs.example/outside-keyword", "content_hash": "d" * 64,
            "posted_at": None, "experience_requirement": None,
        },
    ]


def test_explain_reports_filter_counts_reasons_first_reject_and_all_india_jobs(
    diagnostic_jobs, tmp_path, monkeypatch, capsys
):
    config = AppConfig(
        database_url=f"sqlite:///{tmp_path / 'must-not-exist.db'}",
        filters={
            "keywords": ["software engineer"],
            "excluded_keywords": ["intern"],
            "seniority_titles": ["senior"],
            "preferred_cities": ["Bengaluru", "Gurgaon", "Noida"],
            "max_min_years": 2,
            "employment_types": ["full-time"],
        },
    )

    async def fake_discover(_config, *, stats=None, include_rejected=False, **_kwargs):
        assert include_rejected is True
        if stats is not None:
            stats.update(fetched=4, filtered=1, errors=0)
        return [dict(job) for job in diagnostic_jobs]

    def forbidden(*_args, **_kwargs):
        pytest.fail("explain dry-run must not open the database or send Telegram")

    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(main, "make_engine", forbidden)
    monkeypatch.setattr(main, "send_jobs", forbidden)
    monkeypatch.setattr(main, "send_summary", forbidden)

    assert main.run(config, dry_run=True, explain=True, notify=True) == 4
    output = capsys.readouterr().out
    assert "Dry-run filter diagnostic: 4 fetched jobs" in output
    assert "Unknown location policy: strict" in output
    assert "Text fields: keyword=title; excluded keyword=title;" in output
    assert "keyword: PASS=2 UNCERTAIN=0 REJECT=2" in output
    assert "excluded keyword: PASS=4 UNCERTAIN=0 REJECT=0" in output
    assert "seniority: PASS=3 UNCERTAIN=0 REJECT=1" in output
    assert "location: PASS=3 UNCERTAIN=0 REJECT=1" in output
    assert "experience: PASS=1 UNCERTAIN=2 REJECT=1" in output
    assert "employment: PASS=2 UNCERTAIN=1 REJECT=1" in output
    assert "keyword: 2" in output
    assert "seniority: 1" in output
    assert "2 — No configured include keyword matched." in output
    assert "India-located jobs (3):" in output
    assert "Internal Tools Engineer — Gurugram, India" in output
    assert "Senior Software Engineer — Noida, India" in output
    assert "seniority: REJECT — Title contains excluded seniority term: senior." in output
    assert "experience: REJECT — Minimum experience requirement is 5 years" in output
    assert "employment: REJECT — Contract roles are disabled" in output
    assert "first rejection: seniority" in output
    assert "  - Internal Tools Engineer\n  - Backend Engineer" in output
    assert not (tmp_path / "must-not-exist.db").exists()


def test_excluded_keyword_is_reported_as_first_rejection():
    config = AppConfig(filters={
        "keywords": ["software engineer"],
        "excluded_keywords": ["intern"],
    })
    individual, _pipeline, first = evaluate_filter_decisions(
        {"title": "Intern Software Engineer", "description": "", "company": "Example"}, config
    )
    assert individual["keyword"].status.value == "PASS"
    assert individual["excluded keyword"].status.value == "REJECT"
    assert 'Title "Intern Software Engineer" matched excluded keyword "intern".' == individual["excluded keyword"].reason
    assert first == "excluded keyword"


def test_keyword_diagnostic_reports_the_title_and_matching_keyword():
    config = AppConfig(filters={"keywords": ["software engineer"]})
    individual, _pipeline, _first = evaluate_filter_decisions(
        {"title": "Software Engineer 3", "description": "sales recruiter", "company": "Example"},
        config,
    )
    assert individual["keyword"].reason == 'Title "Software Engineer 3" matched include keyword "software engineer".'


def test_keyword_rejection_samples_are_limited_to_ten_titles():
    config = AppConfig(filters={"keywords": ["software engineer"]})
    jobs = [
        {"title": f"Unmatched Role {index}", "company": "Example", "location": "Unknown",
         "description": "", "canonical_url": f"https://example.test/{index}"}
        for index in range(12)
    ]
    report = format_filter_diagnostics(jobs, config)
    samples = report.split("Keyword-filter rejected job titles (up to 10):\n", 1)[1]
    assert sum(line.startswith("  - ") for line in samples.splitlines()) == 10
    assert "Unmatched Role 9" in samples
    assert "Unmatched Role 10" not in samples


def test_explain_lists_unrecognized_locations_and_age_only_near_misses():
    config = AppConfig(filters={"keywords": ["software engineer"], "unknown_location_policy": "strict"})

    def make_job(source, source_id, location, *, title="Software Engineer", age=None):
        return {
            "source": source, "source_id": source_id, "company": "Example Co",
            "title": title, "location": location, "employment_type": "full-time",
            "description": "Requirements: 1 year of experience",
            "canonical_url": f"https://jobs.example/{source_id}",
            "content_hash": "a" * 64, "experience_requirement": 1,
            "posted_at": EXPLAIN_NOW - timedelta(days=age) if age is not None else None,
            "age_source": "posted" if age is not None else None,
        }

    jobs = [
        make_job("greenhouse", "dublin-1", "Dublin"),
        make_job("greenhouse", "dublin-2", "Dublin"),
        make_job("ashby", "dublin-3", "Dublin"),
        make_job("greenhouse", "age-only", "Bengaluru, India", age=9),
        make_job("greenhouse", "multiple-rejects", "Dublin", title="Frontend Developer", age=9),
    ]

    report = format_filter_diagnostics(jobs, config, run_time=EXPLAIN_NOW)
    locations = report.split("Unrecognized locations (top 25):\n", 1)[1].split("\n\nNear misses", 1)[0]
    near_misses = report.split("Near misses (rejected only by age): 1\n", 1)[1].split("\n\nIndia-located", 1)[0]
    assert "4 — Dublin — ashby (1), greenhouse (3)" in locations
    assert "Software Engineer — Example Co — Bengaluru, India — 9 days — age_source=posted" in near_misses
    assert "multiple-rejects" not in near_misses


def test_explain_limits_unrecognized_location_list_to_top_25():
    config = AppConfig(filters={"keywords": ["software engineer"], "unknown_location_policy": "strict"})
    jobs = [
        {
            "source": "greenhouse", "source_id": f"location-{letter}",
            "company": "Example Co", "title": "Software Engineer",
            "location": f"Unrecognized Place {letter}", "employment_type": "full-time",
            "description": "Requirements: 1 year of experience",
            "canonical_url": f"https://jobs.example/location-{letter}",
            "content_hash": "b" * 64, "experience_requirement": 1,
        }
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ]
    report = format_filter_diagnostics(jobs, config, run_time=EXPLAIN_NOW)
    locations = report.split("Unrecognized locations (top 25):\n", 1)[1].split("\n\nNear misses", 1)[0]
    entries = [line for line in locations.splitlines() if line.strip()]
    assert len(entries) == 25
    assert any("Unrecognized Place A" in line for line in entries)
    assert not any("Unrecognized Place Z" in line for line in entries)
